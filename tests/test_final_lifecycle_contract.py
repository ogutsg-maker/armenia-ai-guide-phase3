from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read(name: str) -> str:
    return (ROOT / name).read_text(encoding="utf-8")


def test_partner_registration_is_minimal():
    html = read("partner.html")
    assert "business_name" in html
    assert 'id="phone"' in html
    assert "services" not in html.split("<form", 1)[1].split("</form>", 1)[0]
    assert "working_hours" not in html
    assert "document" not in html.split("<form", 1)[1].split("</form>", 1)[0]


def test_service_tool_has_no_dispatch_base_contract():
    src = read("tool_registry.py")
    assert '"base_location"' not in src
    assert "dispatch base" not in src.lower()
    assert "base_location_required" not in src


def test_service_creation_does_not_make_optional_settings_required():
    src = read("tool_registry.py")
    block_start = src.index('if name == "add_services":')
    block = src[block_start:src.index('if name == "update_service":', block_start)]
    assert 'missing.append("address")' not in block
    assert 'missing.append("phone")' not in block
    assert 'missing.append("service_mode")' not in block
    assert 'missing.append("document")' not in block


def test_service_application_does_not_require_document_or_dispatch_base():
    src = read("data_core.py")
    start = src.index("def create_partner_services_proposal")
    block = src[start:src.index("def ", start + 10)]
    assert "base_location_required" not in block
    assert "document_required" not in block
    assert "service_phone_required" not in block
    assert '"base_location": base_location' not in block


def test_prompt_has_no_dispatch_base_instruction():
    src = read("prompt_factory.py")
    assert "dispatch/base location" not in src
    assert "dispatch base" not in src.lower()


def test_client_has_no_test_payment_path():
    html = read("web_apps/client.html")
    assert "/pay-test" not in html
    assert "payTest" not in html
    assert "Test Idram" not in html


def test_partner_cabinet_has_no_hardcoded_master_zero_route():
    html = read("web_apps/master_cabinet.html")
    assert "/api/master/0" not in html
    assert "/api/master/current" in html

def test_qr_creation_requires_paid_booking_and_is_idempotent():
    src = read("data_core.py")
    block = src[src.index("def create_booking_checkin"):src.index("def get_partner_booking_display",src.index("def create_booking_checkin"))]
    assert "status IN ('active','checked_in')" in block
    assert "status='paid'" in block

def test_qr_creation_uses_database_uniqueness_for_concurrent_callbacks():
    src = read("data_core.py")
    block = src[src.index("def create_booking_checkin"):src.index("def get_partner_booking_display",src.index("def create_booking_checkin"))]
    assert "ON CONFLICT (booking_id) WHERE status IN ('active','checked_in') DO NOTHING" in block
def test_refund_settlement_closes_qr():
    src = read("data_core.py")
    block = src[src.index("def reconcile_refund"):src.index("def marketplace_existing_payment",src.index("def reconcile_refund"))]
    assert "status IN ('active','checked_in','expired')" in block

def test_idram_is_live_only_and_has_no_fake_settlement():
    src = read("idram.py")
    assert "TEST-IDRAM-" not in src
    block = src[src.index("def create_invoice"):src.index("def build_payment_url")]
    assert 'status="paid"' not in block
    assert "Idram live credentials are not configured" in block
