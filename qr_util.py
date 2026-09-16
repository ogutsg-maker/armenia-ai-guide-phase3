"""Standalone QR helpers for the new booking check-in flow.

No dependency on the legacy ``billing``/``database`` modules. Produces either a
raw PNG (bytes) or a ``data:image/png;base64,...`` URI that a Telegram WebApp
can embed directly in an ``<img>`` tag.
"""
from __future__ import annotations

import base64
import io

import qrcode


def qr_png_bytes(data: str) -> bytes:
    qr = qrcode.QRCode(
        version=None,
        error_correction=qrcode.constants.ERROR_CORRECT_M,
        box_size=8,
        border=3,
    )
    qr.add_data(data)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def qr_data_uri(data: str) -> str:
    b64 = base64.b64encode(qr_png_bytes(data)).decode("ascii")
    return "data:image/png;base64," + b64
