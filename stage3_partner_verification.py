"""Stage 3: partner verification documents + admin approval workflow.
Uses the existing PostgreSQL database and Supabase Storage without replacing database.py.
"""
import base64
import hashlib
import hmac
import os
import time
import json
import uuid
from datetime import datetime, date, timezone
from decimal import Decimal

import aiohttp
import psycopg
from aiohttp import web

from telegram_webapp_auth import TelegramWebAppAuthError, validate_telegram_webapp_init_data

try:
    from notify import notify as _notify
except Exception:  # pragma: no cover - notify optional at import time
    _notify = None

MAX_FILE_SIZE = 10 * 1024 * 1024
ALLOWED_TYPES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "application/pdf": ".pdf",
    "image/webp": ".webp",
}


def _database_url():
    value = os.getenv("DATABASE_URL", "").strip()
    if not value:
        raise RuntimeError("DATABASE_URL is not configured")
    return value


def _storage_config():
    url = os.getenv("SUPABASE_URL", "").strip().rstrip("/")
    # Server-side document uploads MUST use the service-role key.
    # The anon/publishable key cannot be relied upon for a private Storage bucket.
    key = (os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()
           or os.getenv("SUPABASE_KEY", "").strip())
    bucket = (os.getenv("SUPABASE_STORAGE_BUCKET", "partner-verification-documents") or "partner-verification-documents").strip()
    if not url:
        raise RuntimeError("SUPABASE_URL is not configured")
    if not key:
        raise RuntimeError("SUPABASE_SERVICE_ROLE_KEY or SUPABASE_KEY is not configured")
    return url, key, bucket


async def _storage_request(method, url, *, key, **kwargs):
    headers = dict(kwargs.pop("headers", {}) or {})
    headers.update({"Authorization": f"Bearer {key}", "apikey": key})
    timeout = kwargs.pop("timeout", aiohttp.ClientTimeout(total=60))
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.request(method, url, headers=headers, **kwargs) as response:
            body = await response.text()
            return response.status, body, response.headers


async def _ensure_storage_bucket():
    base, key, bucket = _storage_config()
    # First check that the bucket exists.
    status, body, _ = await _storage_request(
        "GET", f"{base}/storage/v1/bucket/{bucket}", key=key,
        timeout=aiohttp.ClientTimeout(total=20),
    )
    if status in (200, 201):
        return
    if status not in (404, 400):
        raise RuntimeError(f"Supabase Storage bucket check failed ({status}): {body[:500]}")

    # Create it as a private bucket. A 409 means another request already created it.
    status, body, _ = await _storage_request(
        "POST", f"{base}/storage/v1/bucket", key=key,
        json={"id": bucket, "name": bucket, "public": False},
        headers={"Content-Type": "application/json"},
        timeout=aiohttp.ClientTimeout(total=20),
    )
    if status not in (200, 201, 409):
        raise RuntimeError(f"Supabase Storage bucket create failed ({status}): {body[:500]}")


async def _storage_upload(path, content, mime):
    base, key, bucket = _storage_config()
    await _ensure_storage_bucket()
    url = f"{base}/storage/v1/object/{bucket}/{path}"
    headers = {"Content-Type": mime, "x-upsert": "false"}
    status, body, _ = await _storage_request(
        "POST", url, key=key, headers=headers, data=content,
        timeout=aiohttp.ClientTimeout(total=60),
    )
    if status not in (200, 201):
        # Retry once after a bucket-not-found response in case the bucket was removed between check/upload.
        if status in (400, 404) and ("bucket" in body.lower() or "not found" in body.lower()):
            await _ensure_storage_bucket()
            status, body, _ = await _storage_request(
                "POST", url, key=key, headers=headers, data=content,
                timeout=aiohttp.ClientTimeout(total=60),
            )
        if status not in (200, 201):
            raise RuntimeError(f"Supabase Storage upload failed ({status}): {body[:1000]}")


async def _storage_signed_url(path, expires=900):
    base, key, bucket = _storage_config()
    await _ensure_storage_bucket()
    url = f"{base}/storage/v1/object/sign/{bucket}/{path}"
    status, body, _ = await _storage_request(
        "POST", url, key=key,
        headers={"Content-Type": "application/json"},
        json={"expiresIn": expires},
        timeout=aiohttp.ClientTimeout(total=30),
    )
    if status not in (200, 201):
        raise RuntimeError(f"Supabase Storage signed URL failed ({status}): {body[:1000]}")
    try:
        payload = __import__("json").loads(body)
    except Exception:
        payload = {}
    signed = payload.get("signedURL") or payload.get("signedUrl")
    if not signed:
        raise RuntimeError(f"Supabase did not return a signed URL: {body[:500]}")
    return signed if signed.startswith("http") else base + signed

def _json_safe(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return value


def _db_fetchone(sql, params=()):
    with psycopg.connect(_database_url(), prepare_threshold=None) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
            cols = [d.name for d in cur.description] if cur.description else []
            return _json_safe(dict(zip(cols, row))) if row else None


def _db_fetchall(sql, params=()):
    with psycopg.connect(_database_url(), prepare_threshold=None) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
            cols = [d.name for d in cur.description] if cur.description else []
            return [_json_safe(dict(zip(cols, row))) for row in rows]


def _db_execute(sql, params=(), returning=False):
    with psycopg.connect(_database_url(), prepare_threshold=None) as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            result = None
            if returning:
                row = cur.fetchone()
                cols = [d.name for d in cur.description] if cur.description else []
                result = _json_safe(dict(zip(cols, row))) if row else None
        conn.commit()
        return result


def ensure_stage3_schema():
    """Safe PostgreSQL migration; can run on every startup."""
    sql = """
    ALTER TABLE partners ADD COLUMN IF NOT EXISTS rejection_reason TEXT;

    CREATE TABLE IF NOT EXISTS partner_verification_documents (
        id BIGSERIAL PRIMARY KEY,
        partner_id BIGINT NOT NULL REFERENCES partners(id) ON DELETE CASCADE,
        document_type TEXT NOT NULL DEFAULT 'business_document',
        original_filename TEXT NOT NULL,
        storage_path TEXT UNIQUE,
        file_data BYTEA,
        mime_type TEXT NOT NULL,
        file_size BIGINT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        rejection_reason TEXT,
        reviewed_by BIGINT,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        reviewed_at TIMESTAMPTZ
    );

    ALTER TABLE partner_verification_documents ADD COLUMN IF NOT EXISTS storage_path TEXT;
    ALTER TABLE partner_verification_documents ALTER COLUMN storage_path DROP NOT NULL;
    ALTER TABLE partner_verification_documents ADD COLUMN IF NOT EXISTS partner_direction_id BIGINT;
    ALTER TABLE partner_verification_documents ADD COLUMN IF NOT EXISTS file_data BYTEA;
    ALTER TABLE partner_verification_documents ADD COLUMN IF NOT EXISTS original_filename TEXT;
    ALTER TABLE partner_verification_documents ADD COLUMN IF NOT EXISTS mime_type TEXT;
    ALTER TABLE partner_verification_documents ADD COLUMN IF NOT EXISTS file_size BIGINT DEFAULT 0;
    ALTER TABLE partner_verification_documents ADD COLUMN IF NOT EXISTS status TEXT DEFAULT 'pending';
    ALTER TABLE partner_verification_documents ADD COLUMN IF NOT EXISTS rejection_reason TEXT;
    ALTER TABLE partner_verification_documents ADD COLUMN IF NOT EXISTS reviewed_by BIGINT;
    ALTER TABLE partner_verification_documents ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT NOW();
    ALTER TABLE partner_verification_documents ADD COLUMN IF NOT EXISTS reviewed_at TIMESTAMPTZ;

    CREATE INDEX IF NOT EXISTS idx_partner_verification_documents_partner
        ON partner_verification_documents(partner_id, created_at DESC);
    CREATE INDEX IF NOT EXISTS idx_partner_verification_documents_status
        ON partner_verification_documents(status, created_at DESC);

    CREATE TABLE IF NOT EXISTS admin_audit_log (
        id BIGSERIAL PRIMARY KEY,
        admin_telegram_id BIGINT NOT NULL,
        action TEXT NOT NULL,
        entity_type TEXT NOT NULL,
        entity_id BIGINT,
        details_json JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );
    """
    with psycopg.connect(_database_url(), prepare_threshold=None) as conn:
        with conn.cursor() as cur:
            cur.execute(sql)
        conn.commit()


def _doc_token_secret(bot_token=None):
    return (bot_token or os.getenv("TELEGRAM_BOT_TOKEN", "").strip() or os.getenv("BOT_TOKEN", "").strip()).encode()


def _mint_doc_token(pid, doc_id, admin_id, bot_token=None, ttl=900):
    """Short-lived HMAC token (signed with the bot token) authorising ONE admin
    to fetch ONE document. Embedded in the /download URL so a plain browser tab
    opened via tg.openLink()/window.open (which cannot send the
    X-Telegram-Init-Data header) can still be authorised."""
    exp = int(time.time()) + int(ttl)
    msg = f"{int(pid)}.{int(doc_id)}.{int(admin_id)}.{exp}"
    sig = hmac.new(_doc_token_secret(bot_token), msg.encode(), hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(f"{msg}.{sig}".encode()).decode().rstrip("=")


def _verify_doc_token(token, pid, doc_id, bot_token=None):
    """Return the admin_id if the token is valid for (pid, doc_id) and unexpired,
    otherwise None."""
    try:
        pad = "=" * (-len(token) % 4)
        decoded = base64.urlsafe_b64decode(token + pad).decode()
        p_s, d_s, a_s, e_s, sig = decoded.split(".")
        msg = f"{p_s}.{d_s}.{a_s}.{e_s}"
        expected = hmac.new(_doc_token_secret(bot_token), msg.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, sig):
            return None
        if int(p_s) != int(pid) or int(d_s) != int(doc_id):
            return None
        if int(e_s) < int(time.time()):
            return None
        return int(a_s)
    except Exception:
        return None


def _admin_telegram_id(request, bot_token=None, admin_id=None):
    # Prefer the header (used by fetch()), but also accept the init-data as a
    # query param `tgwad`. Direct navigation / window.open cannot attach custom
    # headers, so document links carry the (signed, time-limited) init-data in
    # the query string instead. Validation below is identical either way.
    raw = request.headers.get("X-Telegram-Init-Data", "").strip() or request.query.get("tgwad", "").strip()
    if not raw:
        raise web.HTTPUnauthorized(text='{"ok":false,"error":"telegram_init_data_required"}', content_type="application/json")
    try:
        user = validate_telegram_webapp_init_data(raw, (bot_token or os.getenv("TELEGRAM_BOT_TOKEN", "").strip() or os.getenv("BOT_TOKEN", "").strip()))
    except TelegramWebAppAuthError as exc:
        raise web.HTTPUnauthorized(text='{"ok":false,"error":"%s"}' % str(exc).replace('"', "'"), content_type="application/json")
    uid = int(user["id"])
    admin_id = int(admin_id or os.getenv("ADMIN_TELEGRAM_ID", "0") or os.getenv("ADMIN_ID", "0") or 0)
    if not admin_id or uid != admin_id:
        raise web.HTTPForbidden(text='{"ok":false,"error":"admin_access_required"}', content_type="application/json")
    return uid


async def _storage_signed_url(path, expires=900):
    base, key, bucket = _storage_config()
    url = f"{base}/storage/v1/object/sign/{bucket}/{path}"
    headers = {"Authorization": f"Bearer {key}", "apikey": key, "Content-Type": "application/json"}
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(url, headers=headers, json={"expiresIn": expires}) as response:
            body = await response.json(content_type=None)
            if response.status not in (200, 201):
                raise RuntimeError(f"Supabase Storage signed URL failed ({response.status}): {body}")
            signed = body.get("signedURL") or body.get("signedUrl")
            if not signed:
                raise RuntimeError("Supabase did not return a signed URL")
            return signed if signed.startswith("http") else base + signed



async def api_admin_auth(request):
    admin_id = _admin_telegram_id(
        request,
        request.app.get("stage3_bot_token"),
        request.app.get("stage3_admin_id"),
    )
    return web.json_response({"ok": True, "admin_telegram_id": admin_id})
def _partner_for_user(uid):
    return _db_fetchone("SELECT * FROM partners WHERE user_id = %s LIMIT 1", (uid,))


def _audit(admin_id, action, entity_id, details=None):
    import json
    _db_execute(
        "INSERT INTO admin_audit_log(admin_telegram_id,action,entity_type,entity_id,details_json) VALUES(%s,%s,%s,%s,%s::jsonb)",
        (admin_id, action, "partner", entity_id, json.dumps(details or {}, ensure_ascii=False)),
    )


async def api_partner_documents(request):
    # The partner cabinet uses the same Telegram WebApp authentication and
    # firm scoping as the rest of the current cabinet API.  This endpoint
    # used to trust the numeric URL id and returned partner-wide documents,
    # which made the firm room show 0 documents even when an approved
    # document already belonged to the selected firm.
    from master_cabinet_api import _auth_partner, _business_id, _require_partner

    uid = _auth_partner(request)
    pid = _require_partner(uid)
    bid = _business_id(request, pid)

    docs = _db_fetchall(
        """SELECT id, partner_id, business_id, partner_direction_id,
                  document_type, original_filename, mime_type, file_size,
                  status, rejection_reason, created_at, reviewed_at
           FROM partner_verification_documents
           WHERE partner_id=%s AND business_id=%s
           ORDER BY id DESC""",
        (pid, bid),
    )
    return web.json_response({
        "ok": True,
        "partner": {
            "id": pid,
            "status": "approved",
            "verification_status": "approved",
        },
        "documents": docs,
    })


async def api_partner_document_upload(request):
    uid = int(request.match_info["id"])
    partner = _partner_for_user(uid)
    if not partner:
        return web.json_response({"ok": False, "error": "partner_registration_required"}, status=404)
    if str(partner.get("status") or "") in ("blocked", "suspended"):
        return web.json_response({"ok": False, "error": "partner_not_allowed"}, status=403)

    reader = await request.multipart()
    document_type = "business_document"
    direction_id = None
    file_part = None
    async for part in reader:
        if part.name == "document_type":
            document_type = (await part.text()).strip()[:80] or document_type
        elif part.name in ("direction_id", "partner_direction_id"):
            raw_direction = (await part.text()).strip()
            direction_id = int(raw_direction) if raw_direction.isdigit() else None
        elif part.name == "file":
            file_part = part
            break
    if file_part is None:
        return web.json_response({"ok": False, "error": "file_required"}, status=400)

    mime = (file_part.headers.get("Content-Type") or "application/octet-stream").lower()
    if mime not in ALLOWED_TYPES:
        return web.json_response({"ok": False, "error": "unsupported_file_type", "allowed": sorted(ALLOWED_TYPES)}, status=400)

    original = os.path.basename(file_part.filename or "document")[:180]
    ext = ALLOWED_TYPES[mime]
    data = bytearray()
    while True:
        chunk = await file_part.read_chunk(1024 * 1024)
        if not chunk:
            break
        data.extend(chunk)
        if len(data) > MAX_FILE_SIZE:
            return web.json_response({"ok": False, "error": "file_too_large", "max_bytes": MAX_FILE_SIZE}, status=413)

    if not data:
        return web.json_response({"ok": False, "error": "empty_file"}, status=400)

    # Resolve a direction for the registration flow when the frontend does not send one.
    try:
        if direction_id is None:
            row = _db_fetchone(
                "SELECT id FROM partner_directions WHERE partner_id=%s AND status IN ('draft','pending','rejected') ORDER BY id DESC LIMIT 1",
                (partner["id"],),
            )
            if row:
                direction_id = int(row["id"])
        if direction_id is not None:
            row = _db_fetchone(
                "SELECT id,status FROM partner_directions WHERE id=%s AND partner_id=%s",
                (direction_id, partner["id"]),
            )
            if not row:
                return web.json_response({"ok": False, "error": "partner_direction_not_found"}, status=404)
            if row.get("status") in ("approved", "frozen"):
                return web.json_response({"ok": False, "error": "direction_already_approved"}, status=400)
    except Exception:
        direction_id = None

    path = f"partners/{partner['id']}/{uuid.uuid4().hex}{ext}"
    storage_ok = False
    storage_error = None
    try:
        await _storage_upload(path, bytes(data), mime)
        storage_ok = True
    except Exception as exc:
        storage_error = str(exc)
        print(f"[partner-verification] Storage upload failed, using PostgreSQL fallback: partner={partner['id']} error={exc!r}", flush=True)

    try:
        if storage_ok:
            doc = _db_execute(
                "INSERT INTO partner_verification_documents(partner_id,partner_direction_id,document_type,original_filename,storage_path,file_data,mime_type,file_size,status) VALUES(%s,%s,%s,%s,%s,NULL,%s,%s,'pending') RETURNING id, document_type, original_filename, mime_type, file_size, status, created_at",
                (partner["id"], direction_id, document_type, original, path, mime, len(data)),
                returning=True,
            )
        else:
            doc = _db_execute(
                "INSERT INTO partner_verification_documents(partner_id,partner_direction_id,document_type,original_filename,storage_path,file_data,mime_type,file_size,status) VALUES(%s,%s,%s,%s,NULL,%s,%s,%s,'pending') RETURNING id, document_type, original_filename, mime_type, file_size, status, created_at",
                (partner["id"], direction_id, document_type, original, bytes(data), mime, len(data)),
                returning=True,
            )
        _db_execute("UPDATE partners SET verification_status=CASE WHEN status='approved' THEN verification_status ELSE 'pending' END, rejection_reason=NULL, status=CASE WHEN status IN ('draft','rejected') THEN 'pending' ELSE status END WHERE id=%s", (partner["id"],))
    except Exception as exc:
        # Keep the client response safe but log the real Storage/DB error in Render logs.
        print(f"[partner-verification] document upload failed: partner={partner['id']} error={exc!r}", flush=True)
        return web.json_response({"ok": False, "error": "document_upload_failed", "details": str(exc)[:1000]}, status=500)

    # The AI registration page uploads through this legacy verification endpoint.
    # Attach that document to the same universal application created by AI.
    # This prevents the admin from seeing one half under Partners and another
    # half under Applications.
    try:
        linked = _db_execute(
            """UPDATE partner_applications a
               SET document_id=%s,
                   status='pending_admin',
                   updated_at=NOW()
               WHERE a.id=(
                   SELECT id FROM partner_applications
                   WHERE partner_id=%s
                     AND status='document_pending'
                     AND document_id IS NULL
                   ORDER BY created_at DESC,id DESC
                   LIMIT 1
               )
               RETURNING id,business_id""",
            (doc["id"], partner["id"]),
            returning=True,
        )
        if linked:
            _db_execute(
                "UPDATE partner_verification_documents SET business_id=%s WHERE id=%s",
                (linked.get("business_id"), doc["id"]),
            )
    except Exception as exc:
        print(f"[partner-verification] application/document link failed: partner={partner['id']} document={doc.get('id')} error={exc!r}", flush=True)

    return web.json_response({"ok": True, "document": doc, "verification_status": "pending"})


async def api_admin_partner_applications(request):
    admin_id = _admin_telegram_id(request, request.app.get("stage3_bot_token"), request.app.get("stage3_admin_id"))
    rows = _db_fetchall(
        """
        SELECT p.id, p.user_id, p.business_name, p.business_description, p.status,
               p.verification_status, p.rejection_reason, p.created_at,
               (SELECT po.city FROM partner_objects po JOIN partner_businesses pb ON pb.id=po.business_id WHERE po.partner_id=p.id ORDER BY pb.is_default DESC,po.id LIMIT 1) AS city,
               COALESCE((SELECT COUNT(*) FROM partner_verification_documents d WHERE d.partner_id=p.id),0) AS document_count,
               (SELECT MAX(d.created_at) FROM partner_verification_documents d WHERE d.partner_id=p.id) AS last_document_at
        FROM partners p
        WHERE p.status IN ('approved','suspended','blocked')
        ORDER BY CASE WHEN p.status='approved' THEN 0 ELSE 1 END, p.created_at DESC
        """
    )
    return web.json_response({"ok": True, "admin_id": admin_id, "partners": rows})


async def api_admin_partner_detail(request):
    admin_id = _admin_telegram_id(request, request.app.get("stage3_bot_token"), request.app.get("stage3_admin_id"))
    pid = int(request.match_info["id"])
    partner = _db_fetchone("SELECT * FROM partners WHERE id=%s", (pid,))
    if not partner:
        return web.json_response({"ok": False, "error": "partner_not_found"}, status=404)
    docs = _db_fetchall("SELECT id, partner_direction_id, document_type, original_filename, mime_type, file_size, status, rejection_reason, storage_path, created_at, reviewed_at FROM partner_verification_documents WHERE partner_id=%s ORDER BY created_at DESC", (pid,))
    businesses = _db_fetchall("SELECT id, name, description, phone, status, is_default FROM partner_businesses WHERE partner_id=%s AND status<>'archived' ORDER BY is_default DESC,id", (pid,))
    for business in businesses:
        objects = _db_fetchall("SELECT id, object_name, address, city, marz, data_json, working_hours FROM partner_objects WHERE partner_id=%s AND business_id=%s ORDER BY id", (pid, business["id"]))
        # Legacy approved registrations may predate object-level working-hours storage.
        # Recover the original hours from the approved application so admin sees
        # the complete schedule just like the partner cabinet.
        for obj in objects:
            data_json = obj.get("data_json") if isinstance(obj, dict) else None
            if isinstance(data_json, str):
                try:
                    data_json = json.loads(data_json or "{}")
                except Exception:
                    data_json = {}
            if not isinstance(data_json, dict):
                data_json = {}
            canonical_hours = obj.get("working_hours") if isinstance(obj, dict) else None
            if isinstance(canonical_hours, str):
                try:
                    canonical_hours = json.loads(canonical_hours or "{}")
                except Exception:
                    canonical_hours = {}
            if isinstance(canonical_hours, dict) and canonical_hours:
                # The object-level schedule is canonical. Never rebuild it from
                # the registration transcript after the partner has edited it.
                data_json["working_hours"] = canonical_hours
                obj["data_json"] = data_json
                continue

            existing_hours = data_json.get("working_hours")
            complete_week = (
                isinstance(existing_hours, dict)
                and all(
                    isinstance(existing_hours.get(day), dict)
                    and existing_hours[day].get("from")
                    and existing_hours[day].get("to")
                    for day in ("mon", "tue", "wed", "thu", "fri", "sat")
                )
                and isinstance(existing_hours.get("sun"), dict)
                and (
                    existing_hours["sun"].get("closed")
                    or (
                        existing_hours["sun"].get("from")
                        and existing_hours["sun"].get("to")
                    )
                )
            )
            if complete_week:
                obj["data_json"] = data_json
                continue
            app_row = _db_fetchone(
                """SELECT description, payload_json
                   FROM partner_applications
                   WHERE partner_id=%s AND business_id=%s AND status='approved'
                   ORDER BY created_at DESC,id DESC LIMIT 1""",
                (pid, business["id"]),
            )
            if not app_row:
                # Some legacy applications were created before business_id was
                # attached. Fall back to the partner's latest approved application.
                app_row = _db_fetchone(
                    """SELECT description, payload_json
                       FROM partner_applications
                       WHERE partner_id=%s AND status='approved'
                       ORDER BY created_at DESC,id DESC LIMIT 1""",
                    (pid,),
                )
            if not app_row:
                obj["data_json"] = data_json
                continue
            payload = app_row.get("payload_json") if isinstance(app_row, dict) else {}
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload or "{}")
                except Exception:
                    payload = {}
            if not isinstance(payload, dict):
                payload = {}
            import re
            payload_hours = payload.get("working_hours")
            payload_text_parts = [
                str(app_row.get("description") or ""),
                str(partner.get("business_description") or ""),
                str(partner.get("description") or ""),
                str(payload.get("raw_text") or ""),
                str(payload.get("free_text") or ""),
                str(payload.get("text") or ""),
                str(payload_hours or "") if isinstance(payload_hours, str) else "",
            ]
            # Legacy payloads may keep the original partner message under a
            # different field. Include every nested string value when recovering
            # facts, without changing or persisting the payload itself.
            def _payload_strings(value):
                if isinstance(value, str):
                    return [value]
                if isinstance(value, dict):
                    out = []
                    for item in value.values():
                        out.extend(_payload_strings(item))
                    return out
                if isinstance(value, list):
                    out = []
                    for item in value:
                        out.extend(_payload_strings(item))
                    return out
                return []
            payload_text_parts.extend(_payload_strings(payload))
            text_value = " ".join(x for x in payload_text_parts if x).strip()
            hours = {}
            mm = re.search(
                r"(?:երկուշաբթի(?:ից|ից մինչև)?\s*(?:շաբաթ|շաբաթվա)|"
                r"понедельник(?:а)?\s*(?:по|до)\s*(?:суббота|субботы)|"
                r"monday\s*(?:to|through|- )\s*saturday).{0,80}?"
                r"(\d{1,2}:\d{2}).{0,40}?"
                r"(?:մինչև|до|to|-)\s*(\d{1,2}:\d{2})",
                text_value, re.IGNORECASE | re.DOTALL,
            )
            if mm:
                for day in ("mon", "tue", "wed", "thu", "fri", "sat"):
                    hours[day] = {"from": mm.group(1), "to": mm.group(2)}
            # Registration text can contain extra words such as "ժամը" or
            # punctuation between the weekday range and the time. If the
            # strict pattern above misses it, recover the same Mon-Sat range
            # from the presence of "շաբաթ" plus two clock times.
            if not hours and re.search(r"(?:երկուշաբթի|понедельник|monday).*?(?:շաբաթ|суббот|saturday)", text_value, re.IGNORECASE | re.DOTALL):
                clocks = re.findall(r"\b(\d{1,2}:\d{2})\b", text_value)
                if len(clocks) >= 2:
                    start, end = clocks[-2], clocks[-1]
                    for day in ("mon", "tue", "wed", "thu", "fri", "sat"):
                        hours[day] = {"from": start, "to": end}
            if re.search(
                r"(?:կիրակի|воскресенье|sunday).{0,120}"
                r"(?:հանգստյան(?:\s+օր)?|հանգստի|փակ|չի\s+աշխատ|չենք\s+աշխատ|"
                r"выходн|не\s+работ|закрыт|closed|off)",
                text_value, re.IGNORECASE | re.DOTALL,
            ) or re.search(
                r"(?:հանգստյան(?:\s+օր)?|հանգստի|փակ|չի\s+աշխատ|չենք\s+աշխատ|"
                r"выходн|не\s+работ|закрыт|closed|off).{0,120}"
                r"(?:կիրակի|воскресенье|sunday)",
                text_value, re.IGNORECASE | re.DOTALL,
            ):
                hours["sun"] = {"closed": True}
            if isinstance(payload_hours, dict) and payload_hours:
                # Payload values are authoritative only when they actually
                # contain a usable day definition. Do not let empty legacy
                # values such as sun={} erase a recovered closed Sunday.
                for day, value in payload_hours.items():
                    if not isinstance(value, dict):
                        continue
                    if value.get("closed") or (value.get("from") and value.get("to")):
                        hours[day] = value
            if hours:
                data_json["working_hours"] = hours
                # Persist recovered legacy hours so the admin view and every
                # later request use one canonical object-level schedule.
                _db_execute(
                    """UPDATE partner_objects
                       SET data_json=COALESCE(data_json,'{}'::jsonb) || %s::jsonb
                       WHERE id=%s AND partner_id=%s AND business_id=%s""",
                    (
                        json.dumps({"working_hours": hours}, ensure_ascii=False),
                        obj["id"], pid, business["id"],
                    ),
                )
            obj["data_json"] = data_json
        business["objects"] = objects
    return web.json_response({"ok": True, "admin_id": admin_id, "partner": partner, "documents": docs, "businesses": businesses})


async def api_admin_partner_document_url(request):
    admin_id = _admin_telegram_id(request, request.app.get("stage3_bot_token"), request.app.get("stage3_admin_id"))
    pid = int(request.match_info["id"])
    doc_id = int(request.match_info["doc_id"])
    row = _db_fetchone("SELECT id FROM partner_verification_documents WHERE id=%s AND partner_id=%s", (doc_id, pid))
    if not row:
        return web.json_response({"ok": False, "error": "document_not_found"}, status=404)
    # The document is opened in a plain browser tab (tg.openLink / window.open),
    # which cannot attach the admin X-Telegram-Init-Data header. A platform-wide
    # middleware guards every /api/admin/ path and demands that header — EXCEPT
    # paths ending in /open-file (and /viewer, /proxy). So we hand back a
    # self-authorising proxy URL that ends in /open-file and carries a
    # short-lived signed `access` token, which the proxy handler verifies on its
    # own. The proxy serves both Storage-backed and DB-backed (BYTEA) documents.
    try:
        from runtime_platform_bootstrap import _make_document_access_token
        token = _make_document_access_token(pid, doc_id, 900)
    except Exception as exc:
        return web.json_response({"ok": False, "error": "document_url_unavailable", "details": str(exc)[:300]}, status=500)
    url = f"/api/admin/partner-applications/{pid}/documents/{doc_id}/open-file?access={token}"
    return web.json_response({"ok": True, "admin_id": admin_id, "url": url, "expires_in": 900, "source": "proxy"})


async def api_admin_partner_document_download(request):
    pid = int(request.match_info["id"])
    doc_id = int(request.match_info["doc_id"])
    # Accept a signed one-off token (?t=...) so the document can be opened in a
    # plain browser tab; otherwise fall back to standard header-based admin auth.
    token = request.query.get("t", "").strip()
    admin_id = _verify_doc_token(token, pid, doc_id, request.app.get("stage3_bot_token")) if token else None
    if admin_id is None:
        admin_id = _admin_telegram_id(request, request.app.get("stage3_bot_token"), request.app.get("stage3_admin_id"))
    row = _db_fetchone("SELECT original_filename, mime_type, file_data FROM partner_verification_documents WHERE id=%s AND partner_id=%s", (doc_id, pid))
    if not row or row.get("file_data") is None:
        return web.json_response({"ok": False, "error": "document_file_not_available"}, status=404)
    return web.Response(body=bytes(row["file_data"]), content_type=row.get("mime_type") or "application/octet-stream", headers={"Content-Disposition": f'inline; filename="{str(row.get("original_filename") or "document").replace(chr(34), "")}"'})


async def _notify_partner_decision(request, partner, decision, reason=""):
    """Best-effort push an approval/rejection notification to the partner."""
    if _notify is None:
        return
    user_id = partner.get("user_id") if isinstance(partner, dict) else None
    if not user_id:
        return
    if decision == "approve":
        title = "Заявка одобрена"
        body = (
            "Поздравляем! Ваш профиль партнёра прошёл проверку и опубликован. "
            "Теперь вы можете принимать заказы."
        )
        kind = "success"
    else:
        title = "Заявка отклонена"
        body = "К сожалению, ваша заявка отклонена."
        if reason:
            body += f" Причина: {reason}"
        kind = "error"
    try:
        await _notify(
            request.app, user_id, title=title, body=body,
            kind=kind, audience="partner",
            data={"partner_id": partner.get("id"), "decision": decision},
        )
    except Exception:
        pass


async def _set_partner_decision(request, decision):
    admin_id = _admin_telegram_id(request, request.app.get("stage3_bot_token"), request.app.get("stage3_admin_id"))
    pid = int(request.match_info["id"])
    data = await request.json() if request.can_read_body else {}
    reason = str(data.get("reason") or "").strip()[:1000]
    partner = _db_fetchone("SELECT id, user_id, status, verification_status FROM partners WHERE id=%s", (pid,))
    if not partner:
        return web.json_response({"ok": False, "error": "partner_not_found"}, status=404)

    if decision == "approve":
        approved_direction = _db_fetchone(
            "SELECT id FROM partner_directions WHERE partner_id=%s AND status='approved' ORDER BY updated_at DESC LIMIT 1",
            (pid,),
        )
        if not approved_direction:
            return web.json_response({
                "ok": False,
                "error": "partner_direction_approval_required",
                "message": "Сначала одобрите направление партнёра и его документ.",
            }, status=400)
        approved_doc = _db_fetchone(
            "SELECT id FROM partner_verification_documents WHERE partner_id=%s AND status='approved' ORDER BY reviewed_at DESC NULLS LAST, created_at DESC LIMIT 1",
            (pid,),
        )
        if not approved_doc:
            return web.json_response({
                "ok": False,
                "error": "verification_document_required",
                "message": "Сначала проверьте и одобрите документ направления партнёра.",
            }, status=400)
        _db_execute("UPDATE partners SET status='approved', verification_status='approved', rejection_reason=NULL WHERE id=%s", (pid,))
        _db_execute("UPDATE partner_verification_documents SET status='approved', rejection_reason=NULL, reviewed_by=%s, reviewed_at=NOW() WHERE partner_id=%s AND status='pending'", (admin_id, pid))
        _audit(admin_id, "partner_approved", pid)
        await _notify_partner_decision(request, partner, "approve")
        return web.json_response({"ok": True, "partner_id": pid, "status": "approved", "verification_status": "approved"})

    _db_execute("UPDATE partners SET status='rejected', verification_status='rejected', rejection_reason=%s WHERE id=%s", (reason or "Հայտը մերժվել է ադմինիստրատորի կողմից։", pid))
    _db_execute("UPDATE partner_verification_documents SET status='rejected', rejection_reason=%s, reviewed_by=%s, reviewed_at=NOW() WHERE partner_id=%s AND status='pending'", (reason or "Հայտը մերժվել է ադմինիստրատորի կողմից։", admin_id, pid))
    _audit(admin_id, "partner_rejected", pid, {"reason": reason})
    await _notify_partner_decision(request, partner, "reject", reason)
    return web.json_response({"ok": True, "partner_id": pid, "status": "rejected", "verification_status": "rejected", "reason": reason})


async def api_admin_service_direction_requests(request):
    _admin_telegram_id(request, request.app.get("stage3_bot_token"), request.app.get("stage3_admin_id"))
    rows = _db_fetchall("""
        SELECT r.*, p.business_name, p.user_id,
               m.name_am AS master_name_am, m.name_ru AS master_name_ru, m.name_en AS master_name_en,
               pd.status AS direction_status,
               d.status AS document_status, d.original_filename
        FROM service_direction_requests r
        JOIN partners p ON p.id=r.partner_id
        JOIN master_categories m ON m.id=r.requested_master_category_id
        LEFT JOIN partner_directions pd ON pd.id=r.partner_direction_id
        LEFT JOIN partner_verification_documents d ON d.id=r.document_id
        WHERE r.status <> 'approved'
        ORDER BY r.created_at DESC
    """)
    return web.json_response({"ok": True, "requests": rows})

async def api_admin_service_direction_request_action(request):
    admin_id = _admin_telegram_id(request, request.app.get("stage3_bot_token"), request.app.get("stage3_admin_id"))
    rid = int(request.match_info["id"])
    data = await request.json() if request.can_read_body else {}
    action = str(data.get("action") or "").strip().lower()
    req = _db_fetchone("SELECT * FROM service_direction_requests WHERE id=%s", (rid,))
    if not req:
        return web.json_response({"ok":False,"error":"direction_request_not_found"},status=404)
    if req["status"] == "approved":
        return web.json_response({"ok":False,"error":"direction_request_already_completed"},status=400)

    if action == "edit":
        mid = int(data.get("master_category_id") or req["requested_master_category_id"])
        master = _db_fetchone("SELECT id,name_am,name_ru,name_en FROM master_categories WHERE id=%s AND is_active=TRUE",(mid,))
        if not master:
            return web.json_response({"ok":False,"error":"master_direction_not_found"},status=400)
        sub = str(data.get("proposed_subcategory_name") if data.get("proposed_subcategory_name") is not None else (req.get("proposed_subcategory_name") or "")).strip()[:200]
        service = str(data.get("service_name") if data.get("service_name") is not None else (req.get("requested_service_name") or "")).strip()[:200]
        desc = str(data.get("description") if data.get("description") is not None else (req.get("description") or ""))[:3000]
        reason = str(data.get("reason") if data.get("reason") is not None else (req.get("reason") or ""))[:2000]
        note = str(data.get("admin_note") or "")[:2000]
        price = data.get("price", req.get("price"))
        try:
            price = float(price) if price not in (None,"") else None
        except (TypeError,ValueError):
            return web.json_response({"ok":False,"error":"invalid_price"},status=400)
        if not sub or not service:
            return web.json_response({"ok":False,"error":"subcategory_and_service_required"},status=400)
        _db_execute("""UPDATE service_direction_requests
                       SET requested_master_category_id=%s,requested_master_name=%s,
                           proposed_subcategory_name=%s,requested_service_name=%s,
                           description=%s,price=%s,reason=%s,admin_note=%s,
                           status='pending_admin',updated_at=NOW()
                       WHERE id=%s""",
                    (mid,master.get("name_am") or master.get("name_ru"),sub,service,desc,price,reason,note,rid))
        return web.json_response({"ok":True,"request":_db_fetchone("SELECT * FROM service_direction_requests WHERE id=%s",(rid,))})

    if action == "send_to_partner":
        if req["status"] not in ("pending_admin","rejected"):
            return web.json_response({"ok":False,"error":"invalid_request_status"},status=400)
        mid = int(req["requested_master_category_id"])
        sub = str(req.get("proposed_subcategory_name") or "").strip()
        service = str(req.get("requested_service_name") or "").strip()
        if not sub or not service:
            return web.json_response({"ok":False,"error":"request_not_ready"},status=400)
        master = _db_fetchone("SELECT id,name_am,name_ru,name_en FROM master_categories WHERE id=%s AND is_active=TRUE",(mid,))
        if not master:
            return web.json_response({"ok":False,"error":"master_direction_not_found"},status=400)
        existing_pd = _db_fetchone("SELECT * FROM partner_directions WHERE partner_id=%s AND master_category_id=%s",(req["partner_id"],mid))
        if existing_pd and existing_pd["status"]=="approved":
            return web.json_response({"ok":False,"error":"direction_already_approved"},status=400)
        if existing_pd:
            pd=existing_pd
            _db_execute("UPDATE partner_directions SET status='pending',rejection_reason=NULL,updated_at=NOW() WHERE id=%s",(pd["id"],))
        else:
            pd=_db_execute("""INSERT INTO partner_directions(partner_id,master_category_id,status)
                              VALUES(%s,%s,'pending') RETURNING *""",(req["partner_id"],mid),True)

        import re
        slug=re.sub(r"[^a-z0-9\u0531-\u0587]+","-",sub.lower()).strip("-") or ("partner-request-"+str(rid))
        category=_db_fetchone("""SELECT id,master_category_id,name_am,name_ru,name_en,slug
                                FROM categories
                                WHERE master_category_id=%s AND
                                      (lower(trim(name_am))=lower(trim(%s)) OR lower(trim(name_ru))=lower(trim(%s)) OR lower(trim(name_en))=lower(trim(%s)))
                                LIMIT 1""",(mid,sub,sub,sub))
        if not category:
            category=_db_execute("""INSERT INTO categories(master_category_id,name_am,name_ru,name_en,slug,is_active,commission_type,commission_value)
                                    VALUES(%s,%s,%s,%s,%s,TRUE,'inside',0)
                                    RETURNING id,master_category_id,name_am,name_ru,name_en,slug""",
                                 (mid,sub,sub,sub,slug),True)
        _db_execute("""INSERT INTO partner_direction_categories(partner_direction_id,category_id)
                       VALUES(%s,%s) ON CONFLICT DO NOTHING""",(pd["id"],category["id"]))
        service_row=_db_fetchone("""SELECT id FROM services WHERE partner_id=%s AND category_id=%s
                                    AND lower(trim(name))=lower(trim(%s)) AND status<>'deleted'
                                    ORDER BY id DESC LIMIT 1""",(req["partner_id"],category["id"],service))
        if service_row:
            _db_execute("""UPDATE services SET price=%s,description=%s,status='pending',updated_at=NOW()
                           WHERE id=%s""",(req.get("price"),req.get("description") or "",service_row["id"]))
        else:
            _db_execute("""INSERT INTO services(partner_id,category_id,subcategory_id,name,description,price,status,data_json)
                           VALUES(%s,%s,%s,%s,%s,%s,'pending',%s::jsonb)""",
                        (req["partner_id"],category["id"],category["id"],service,req.get("description") or "",req.get("price"),
                         '{"source":"service_direction_request"}'))
        _db_execute("""UPDATE service_direction_requests
                       SET status='document_pending',partner_direction_id=%s,admin_note=%s,
                           reviewed_by=%s,reviewed_at=NOW(),updated_at=NOW()
                       WHERE id=%s""",(pd["id"],req.get("admin_note") or "",admin_id,rid))
        try:
            bot=request.app.get("partner_direction_bot")
            user=_db_fetchone("SELECT user_id FROM partners WHERE id=%s",(req["partner_id"],))
            if bot and user:
                await bot.send_message(int(user["user_id"]),
                    "🧭 Նոր ուղղության հայտի կառուցվածքը հաստատված է ադմինիստրատորի կողմից։\n\n"
                    "📄 Հաջորդ քայլը՝ բացեք գործընկերոջ կաբինետը և ուղարկեք հաստատող փաստաթուղթը։")
        except Exception:
            pass
        return web.json_response({"ok":True,"status":"document_pending","partner_direction_id":pd["id"],"request_id":rid})

    if action in ("reject","reject_document"):
        reason = str(data.get("reason") or "Մերժվել է ադմինիստրատորի կողմից")[:1000]
        _db_execute("""UPDATE service_direction_requests SET status='rejected',admin_note=%s,
                       reviewed_by=%s,reviewed_at=NOW(),updated_at=NOW() WHERE id=%s""",(reason,admin_id,rid))
        if req.get("partner_direction_id"):
            _db_execute("UPDATE partner_directions SET status='rejected',rejection_reason=%s,updated_at=NOW() WHERE id=%s",(reason,req["partner_direction_id"]))
        return web.json_response({"ok":True,"status":"rejected"})

    if action == "approve_document":
        if req["status"] != "document_under_review" or not req.get("partner_direction_id") or not req.get("document_id"):
            return web.json_response({"ok":False,"error":"document_not_ready"},status=400)
        doc = _db_fetchone("SELECT * FROM partner_verification_documents WHERE id=%s AND partner_direction_id=%s AND status='pending'",(req["document_id"],req["partner_direction_id"]))
        if not doc:
            return web.json_response({"ok":False,"error":"pending_document_not_found"},status=404)
        _db_execute("UPDATE partner_verification_documents SET status='approved',reviewed_by=%s,reviewed_at=NOW(),rejection_reason=NULL WHERE id=%s",(admin_id,doc["id"]))
        _db_execute("UPDATE partner_directions SET status='approved',rejection_reason=NULL,updated_at=NOW() WHERE id=%s",(req["partner_direction_id"],))
        _db_execute("""UPDATE services SET status='approved',updated_at=NOW()
                       WHERE partner_id=%s AND category_id IN
                         (SELECT category_id FROM partner_direction_categories WHERE partner_direction_id=%s)
                         AND status='pending'""",(req["partner_id"],req["partner_direction_id"]))
        _db_execute("UPDATE service_direction_requests SET status='approved',admin_note=NULL,reviewed_by=%s,reviewed_at=NOW(),updated_at=NOW() WHERE id=%s",(admin_id,rid))
        try:
            bot=request.app.get("partner_direction_bot")
            user=_db_fetchone("SELECT user_id FROM partners WHERE id=%s",(req["partner_id"],))
            if bot and user:
                await bot.send_message(int(user["user_id"]),
                    "✅ Նոր ուղղությունը հաստատված է։\n\nԱյժմ կարող եք ավելացնել ծառայություններ այս ուղղության ներքո։")
        except Exception:
            pass
        return web.json_response({"ok":True,"status":"approved"})

    return web.json_response({"ok":False,"error":"unknown_action"},status=400)


async def api_admin_partner_approve(request):
    return await _set_partner_decision(request, "approve")


async def api_admin_partner_reject(request):
    return await _set_partner_decision(request, "reject")


async def api_admin_partner_suspend(request):
    admin_id = _admin_telegram_id(request, request.app.get("stage3_bot_token"), request.app.get("stage3_admin_id"))
    pid = int(request.match_info["id"])
    _db_execute("UPDATE partners SET status='suspended' WHERE id=%s", (pid,))
    _audit(admin_id, "partner_suspended", pid)
    return web.json_response({"ok": True, "partner_id": pid, "status": "suspended"})


async def api_admin_partner_block(request):
    admin_id = _admin_telegram_id(request, request.app.get("stage3_bot_token"), request.app.get("stage3_admin_id"))
    pid = int(request.match_info["id"])
    _db_execute("UPDATE partners SET status='blocked' WHERE id=%s", (pid,))
    _audit(admin_id, "partner_blocked", pid)
    return web.json_response({"ok": True, "partner_id": pid, "status": "blocked"})


def register_stage3_routes(app, bot_token=None, admin_id=None):
    ensure_stage3_schema()
    app["stage3_bot_token"] = bot_token
    app["stage3_admin_id"] = admin_id
    app.router.add_get("/api/master/{id}/documents", api_partner_documents)
    app.router.add_post("/api/master/{id}/documents/upload", api_partner_document_upload)
    app.router.add_get("/api/admin/auth", api_admin_auth)
    app.router.add_get("/api/admin/partner-applications", api_admin_partner_applications)
    app.router.add_get("/api/admin/service-direction-requests", api_admin_service_direction_requests)
    app.router.add_post("/api/admin/service-direction-requests/{id}/action", api_admin_service_direction_request_action)
    app.router.add_get("/api/admin/partner-applications/{id}", api_admin_partner_detail)
    app.router.add_get("/api/admin/partner-applications/{id}/documents/{doc_id}/url", api_admin_partner_document_url)
    app.router.add_get("/api/admin/partner-applications/{id}/documents/{doc_id}/download", api_admin_partner_document_download)
    app.router.add_post("/api/admin/partner-applications/{id}/approve", api_admin_partner_approve)
    app.router.add_post("/api/admin/partner-applications/{id}/reject", api_admin_partner_reject)
    app.router.add_post("/api/admin/partner-applications/{id}/suspend", api_admin_partner_suspend)
    app.router.add_post("/api/admin/partner-applications/{id}/block", api_admin_partner_block)

