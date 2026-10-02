"""Live Idram payment provider abstraction for Armenia AI Guide.

Payment creation is live-only: without real merchant credentials the provider refuses to create an invoice. Settlement happens only after a real Idram callback is verified, so the application never fabricates a successful payment.

Real Idram result-callback checksum:
    md5(EDP_REC_ACCOUNT:EDP_AMOUNT:SECRET_KEY:EDP_BILL_NO:
        EDP_PAYER_ACCOUNT:EDP_TRANS_ID:EDP_TRANS_DATE)
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field, asdict
from urllib.parse import urlencode

try:  # config is always present in this project
    from config import (
        IDRAM_MERCHANT_ID,
        IDRAM_SECRET_KEY,
        IDRAM_SUCCESS_URL,
        IDRAM_FAIL_URL,
    )
except Exception:  # pragma: no cover - defensive
    import os
    IDRAM_MERCHANT_ID = os.getenv("IDRAM_MERCHANT_ID", "test")
    IDRAM_SECRET_KEY = os.getenv("IDRAM_SECRET_KEY", "test")
    IDRAM_SUCCESS_URL = os.getenv("IDRAM_SUCCESS_URL", "")
    IDRAM_FAIL_URL = os.getenv("IDRAM_FAIL_URL", "")

logger = logging.getLogger(__name__)

# Official Idram payment endpoint (used only in live mode).
IDRAM_PAYMENT_ENDPOINT = "https://banking.idram.am/Payment/GetPayment"

# Provider tags stored on payment rows so we can always tell test vs live money.
PROVIDER_LIVE = "IDRAM"


@dataclass
class PaymentIntent:
    """Result of :meth:`IdramProvider.create_invoice`.

    Callers persist ``transaction_id`` as ``provider_payment_id`` and ``provider``
    as the payment provider tag. ``payment_url`` is empty in test mode (nothing
    to redirect to) and a real Idram URL in live mode.
    """
    transaction_id: str
    bill_no: str
    amount: float
    currency: str
    status: str            # 'pending' until Idram confirms settlement
    provider: str          # PROVIDER_LIVE
    mode: str              # 'live'
    payment_url: str = ""
    description: str = ""
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class CallbackResult:
    """Normalised outcome of an Idram result callback / return."""
    ok: bool
    status: str            # 'paid' | 'failed' | 'precheck'
    bill_no: str = ""
    transaction_id: str = ""
    amount: float | None = None
    reason: str = ""


class IdramProvider:
    """Thin, reusable wrapper around the live Idram provider."""

    def __init__(
        self,
        merchant_id: str | None = None,
        secret_key: str | None = None,
        success_url: str | None = None,
        fail_url: str | None = None,
    ):
        self.merchant_id = (merchant_id if merchant_id is not None else IDRAM_MERCHANT_ID) or ""
        self.secret_key = (secret_key if secret_key is not None else IDRAM_SECRET_KEY) or ""
        self.success_url = (success_url if success_url is not None else IDRAM_SUCCESS_URL) or ""
        self.fail_url = (fail_url if fail_url is not None else IDRAM_FAIL_URL) or ""

    # ------------------------------------------------------------------
    # Mode detection
    # ------------------------------------------------------------------
    @property
    def is_live(self) -> bool:
        """True only when real (non-placeholder) credentials are configured.

        The project ships with ``IDRAM_MERCHANT_ID=test`` /
        ``IDRAM_SECRET_KEY=test`` placeholders, which keep us in the fictitious
        test mode until a real merchant account is plugged in at the end.
        """
        placeholders = {"", "test"}
        return (
            self.merchant_id.strip().lower() not in placeholders
            and self.secret_key.strip().lower() not in placeholders
        )

    @property
    def mode(self) -> str:
        return "live" if self.is_live else "test"

    @property
    def provider_tag(self) -> str:
        return PROVIDER_LIVE

    # ------------------------------------------------------------------
    # Invoice creation
    # ------------------------------------------------------------------
    def create_invoice(
        self,
        *,
        amount: float,
        currency: str = "AMD",
        description: str = "",
        order_id: str | int | None = None,
        metadata: dict | None = None,
    ) -> PaymentIntent:
        """Create a payment intent.

        * **Live-only mode:** no fictitious transaction is ever created.
        * **Live mode:** returns a ``status='pending'`` intent whose
          ``payment_url`` redirects the client to Idram; settlement is later
          confirmed via :meth:`verify_callback`.
        """
        amount = round(float(amount or 0), 2)
        bill_no = str(order_id) if order_id is not None else secrets.token_hex(6)
        metadata = metadata or {}

        if not self.is_live:
            raise RuntimeError("Idram live credentials are not configured")

        # Live mode: no transaction id yet (Idram assigns EDP_TRANS_ID on the
        # result callback). We hand the client a redirect URL and wait.
        return PaymentIntent(
            transaction_id="",
            bill_no=bill_no,
            amount=amount,
            currency=currency,
            status="pending",
            provider=PROVIDER_LIVE,
            mode="live",
            payment_url=self.build_payment_url(amount=amount, bill_no=bill_no,
                                               description=description),
            description=description,
            metadata=metadata,
        )

    # ------------------------------------------------------------------
    # Live-mode redirect URL
    # ------------------------------------------------------------------
    def build_payment_url(self, *, amount: float, bill_no: str, description: str = "") -> str:
        """Build the Idram GetPayment redirect URL (live mode only)."""
        params = {
            "EDP_LANGUAGE": "EN",
            "EDP_REC_ACCOUNT": self.merchant_id,
            "EDP_DESCRIPTION": description or f"Order {bill_no}",
            "EDP_AMOUNT": f"{float(amount):.1f}",
            "EDP_BILL_NO": str(bill_no),
        }
        return f"{IDRAM_PAYMENT_ENDPOINT}?{urlencode(params)}"

    # ------------------------------------------------------------------
    # Callback / return verification
    # ------------------------------------------------------------------
    def verify_callback(self, payload: dict) -> CallbackResult:
        """Verify an Idram result callback.

        * **Live-only mode:** rejects callbacks when credentials are missing.
        * **Live mode:** honours Idram's two-step protocol — answers the
          pre-check (``EDP_PRECHECK=YES``) and validates the ``EDP_CHECKSUM``
          MD5 signature on the settlement callback before accepting the payment.
        """
        payload = payload or {}

        if not self.is_live:
            return CallbackResult(ok=False, status="failed", bill_no=str(payload.get("EDP_BILL_NO") or ""), reason="idram_credentials_not_configured")

        # Idram pre-check ping: reply is handled by the caller (must echo "OK").
        if str(payload.get("EDP_PRECHECK", "")).upper() == "YES":
            return CallbackResult(ok=True, status="precheck",
                                  bill_no=str(payload.get("EDP_BILL_NO") or ""))

        expected = self._checksum(payload)
        received = str(payload.get("EDP_CHECKSUM", "")).lower()
        if not received or received != expected:
            logger.warning("Idram checksum mismatch for bill %s", payload.get("EDP_BILL_NO"))
            return CallbackResult(ok=False, status="failed",
                                  bill_no=str(payload.get("EDP_BILL_NO") or ""),
                                  reason="checksum_mismatch")
        return CallbackResult(
            ok=True,
            status="paid",
            bill_no=str(payload.get("EDP_BILL_NO") or ""),
            transaction_id=str(payload.get("EDP_TRANS_ID") or ""),
            amount=_as_float(payload.get("EDP_AMOUNT")),
        )

    def _checksum(self, p: dict) -> str:
        raw = ":".join([
            str(p.get("EDP_REC_ACCOUNT", self.merchant_id)),
            str(p.get("EDP_AMOUNT", "")),
            self.secret_key,
            str(p.get("EDP_BILL_NO", "")),
            str(p.get("EDP_PAYER_ACCOUNT", "")),
            str(p.get("EDP_TRANS_ID", "")),
            str(p.get("EDP_TRANS_DATE", "")),
        ])
        return hashlib.md5(raw.encode("utf-8")).hexdigest().lower()


def _as_float(value) -> float | None:
    try:
        return round(float(value), 2)
    except (TypeError, ValueError):
        return None


# Convenience singleton factory ---------------------------------------------
def build_idram() -> IdramProvider:
    return IdramProvider()
