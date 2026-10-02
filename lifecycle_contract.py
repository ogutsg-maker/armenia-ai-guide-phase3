"""Canonical lifecycle contract for Armenia AI Guide.

Business truth stays in PostgreSQL/Data Core. This module contains only
deterministic state-transition rules and small Data Core helpers; AI must not
call it directly to mutate data.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


BOOKING_STATES = (
    "pending_partner_confirmation",
    "pending_payment",
    "paid",
    "in_progress",
    "completed",
    "cancelled",
    "refunded",
)

ALLOWED_BOOKING_TRANSITIONS = {
    "pending_partner_confirmation": {"pending_payment", "cancelled"},
    "pending_payment": {"paid", "cancelled"},
    "paid": {"in_progress", "cancelled"},
    "in_progress": {"completed", "cancelled"},
    "completed": set(),
    "cancelled": {"refunded"},
    "refunded": set(),
}

PAYMENT_STATES = ("pending", "paid", "refund_pending", "partial_refund", "refunded")
QR_STATES = ("active", "checked_in", "expired", "cancelled", "arbitration")


class LifecycleViolation(ValueError):
    """Raised when a state transition violates the canonical lifecycle."""


def can_transition(current: str, target: str) -> bool:
    return str(target or "").lower() in ALLOWED_BOOKING_TRANSITIONS.get(
        str(current or "").lower(), set()
    )


def require_transition(current: str, target: str) -> str:
    current = str(current or "").lower()
    target = str(target or "").lower()
    if not can_transition(current, target):
        raise LifecycleViolation(f"invalid_booking_transition:{current}->{target}")
    return target


def validate_booking_price(
    *,
    exact: float | None,
    agreed_min: float | None,
    agreed_max: float | None,
) -> tuple[float, float | None, float | None]:
    if exact is not None:
        value = float(exact)
        if value <= 0:
            raise LifecycleViolation("invalid_exact_price")
        return value, value, value
    if agreed_min is None or agreed_max is None:
        raise LifecycleViolation("agreed_price_required")
    lo, hi = float(agreed_min), float(agreed_max)
    if lo <= 0 or hi < lo:
        raise LifecycleViolation("invalid_agreed_range")
    return round((lo + hi) / 2.0, 2), lo, hi


def commission_amount(
    base: float,
    commission_type: str,
    commission_value: float,
) -> tuple[float, float]:
    """Return (commission, partner_amount) from the agreed commission base."""
    base = round(float(base), 2)
    value = max(0.0, float(commission_value))
    mode = str(commission_type or "on_top").lower()
    if base < 0:
        raise LifecycleViolation("invalid_commission_base")
    if mode == "fixed":
        commission = round(value, 2)
        return commission, round(base - commission, 2)
    if mode == "inside":
        commission = round(base * value / 100.0, 2)
        return commission, round(base - commission, 2)
    if mode == "on_top":
        commission = round(base * value / 100.0, 2)
        return commission, round(base, 2)
    raise LifecycleViolation(f"unknown_commission_type:{mode}")


def payment_may_confirm(booking_status: str) -> bool:
    return str(booking_status or "").lower() == "pending_payment"


def qr_may_activate(booking_status: str, payment_status: str) -> bool:
    return (
        str(booking_status or "").lower() == "paid"
        and str(payment_status or "").lower() == "paid"
    )


def qr_may_checkin(qr_status: str, booking_status: str, arbitration_open: bool) -> bool:
    return (
        str(qr_status or "").lower() == "active"
        and str(booking_status or "").lower() == "paid"
        and not arbitration_open
    )


def contacts_may_disclose(payment_status: str, booking_status: str) -> bool:
    return (
        str(payment_status or "").lower() == "paid"
        and str(booking_status or "").lower() in {"paid", "in_progress", "completed"}
    )


def qr_expiry_hours() -> int:
    return 1


def normalize_event_kind(kind: str) -> str:
    value = str(kind or "").strip().lower()
    if not value:
        raise ValueError("notification_kind_required")
    return value


def service_activation_allowed(
    application_status: str,
    classification_status: str,
    admin_approved: bool,
) -> bool:
    return (
        str(application_status or "").lower() == "approved"
        and str(classification_status or "").lower() == "classified"
        and bool(admin_approved)
    )


def potential_partner_may_invite(status: str) -> bool:
    return str(status or "").lower() in {"new", "researched", "ready_for_review", "contacted", "interested"}


def potential_partner_may_link(status: str) -> bool:
    return str(status or "").lower() == "invited"
