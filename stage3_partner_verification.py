"""Stage 3: partner verification documents + admin approval workflow.
Uses the existing PostgreSQL database and Supabase Storage without replacing database.py.
"""
import os
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


def _admin_telegram_id(request, bot_token=None, admin_id=None):
    raw = request.headers.get("X-Telegram-Init-Data", "").strip()
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
    uid = int(request.match_info["id"])
    partner = _partner_for_user(uid)
    if not partner:
        return web.json_response({"ok": False, "error": "partner_registration_required"}, status=404)
    docs = _db_fetchall(
        "SELECT id, partner_direction_id, document_type, original_filename, mime_type, file_size, status, rejection_reason, created_at, reviewed_at FROM partner_verification_documents WHERE partner_id=%s ORDER BY created_at DESC",
        (partner["id"],),
    )
    return web.json_response({
        "ok": True,
        "partner": {
            "id": partner["id"],
            "status": partner.get("status"),
            "verification_status": partner.get("verification_status"),
            "rejection_reason": partner.get("rejection_reason") or "",
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

    return web.json_response({"ok": True, "document": doc, "verification_status": "pending"})


async def api_admin_partner_applications(request):
    admin_id = _admin_telegram_id(request, request.app.get("stage3_bot_token"), request.app.get("stage3_admin_id"))
    rows = _db_fetchall(
        """
        SELECT p.id, p.user_id, p.business_name, p.business_description, p.status,
               p.verification_status, p.rejection_reason, p.created_at,
               COALESCE((SELECT COUNT(*) FROM partner_verification_documents d WHERE d.partner_id=p.id),0) AS document_count,
               (SELECT MAX(d.created_at) FROM partner_verification_documents d WHERE d.partner_id=p.id) AS last_document_at
        FROM partners p
        ORDER BY CASE WHEN p.status IN ('pending','under_review') THEN 0 ELSE 1 END, p.created_at DESC
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
    return web.json_response({"ok": True, "admin_id": admin_id, "partner": partner, "documents": docs})


async def api_admin_partner_document_url(request):
    admin_id = _admin_telegram_id(request, request.app.get("stage3_bot_token"), request.app.get("stage3_admin_id"))
    pid = int(request.match_info["id"])
    doc_id = int(request.match_info["doc_id"])
    row = _db_fetchone("SELECT id, partner_id, storage_path, file_data FROM partner_verification_documents WHERE id=%s AND partner_id=%s", (doc_id, pid))
    if not row:
        return web.json_response({"ok": False, "error": "document_not_found"}, status=404)
    if row.get("storage_path"):
        try:
            url = await _storage_signed_url(row["storage_path"], 900)
            return web.json_response({"ok": True, "admin_id": admin_id, "url": url, "expires_in": 900, "source": "storage"})
        except Exception as exc:
            print(f"[partner-verification] signed URL failed, database fallback available: doc={doc_id} error={exc!r}", flush=True)
    return web.json_response({"ok": True, "admin_id": admin_id, "url": f"/api/admin/partner-applications/{pid}/documents/{doc_id}/download", "source": "database"})


async def api_admin_partner_document_download(request):
    admin_id = _admin_telegram_id(request, request.app.get("stage3_bot_token"), request.app.get("stage3_admin_id"))
    pid = int(request.match_info["id"])
    doc_id = int(request.match_info["doc_id"])
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
        pending_doc = _db_fetchone(
            "SELECT id FROM partner_verification_documents WHERE partner_id=%s AND status='pending' ORDER BY created_at DESC LIMIT 1",
            (pid,),
        )
        if not pending_doc:
            return web.json_response({
                "ok": False,
                "error": "verification_document_required",
                "message": "Партнёра нельзя одобрить без загруженного документа на проверке.",
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
    app.router.add_get("/api/admin/partner-applications/{id}", api_admin_partner_detail)
    app.router.add_get("/api/admin/partner-applications/{id}/documents/{doc_id}/url", api_admin_partner_document_url)
    app.router.add_get("/api/admin/partner-applications/{id}/documents/{doc_id}/download", api_admin_partner_document_download)
    app.router.add_post("/api/admin/partner-applications/{id}/approve", api_admin_partner_approve)
    app.router.add_post("/api/admin/partner-applications/{id}/reject", api_admin_partner_reject)
    app.router.add_post("/api/admin/partner-applications/{id}/suspend", api_admin_partner_suspend)
    app.router.add_post("/api/admin/partner-applications/{id}/block", api_admin_partner_block)

