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

def test_storefront_has_no_test_payment_or_early_qr_message():
    src = read("web_apps/storefront.html")
    assert "Тестовая оплата Idram" not in src
    assert "QR для check-in выдаётся сразу" not in src
    assert "только после подтверждённой оплаты" in src

def test_direct_booking_contract_waits_for_partner_confirmation():
    src = read("marketplace_flow_api.py")
    block = src[src.index("async def direct_booking"):src.index("# --- Phase 3: cancellations", src.index("async def direct_booking"))]
    assert "status='pending_partner_confirmation'" in block
    assert "checkin" in block

def test_legacy_partner_agree_route_is_removed():
    src = read("marketplace_flow_api.py")
    assert "/api/market/partner/negotiation/{negotiation_id}/agree" not in src

def test_admin_tariff_mutations_are_not_public_http_routes():
    src = read("admin_tariff_api.py")
    assert 'add_post("/api/admin/tariffs/direction/{id}"' not in src
    assert 'add_post("/api/admin/tariffs/category/{id}"' not in src
    assert 'add_post("/api/admin/tariffs/service/{id}"' not in src
    assert 'add_get("/api/admin/tariffs"' in src

def test_admin_ai_mutations_are_not_public_http_routes():
    src = read("admin_ai_api.py")
    assert "add_post('/api/admin/ai/economics/expense'" not in src
    assert "add_post('/api/admin/ai/catalog-proposals/{id}/{action}'" not in src
    assert "add_post('/api/admin/potential-partners/structure'" not in src
    assert "add_post('/api/admin/potential-partners/research'" not in src
    assert "add_post('/api/admin/potential-partners/{id}/status'" not in src

def test_admin_ui_does_not_offer_direct_catalog_or_potential_mutations():
    src = read("web_apps/admin.html")
    assert "proposalAction(" not in src
    assert "/api/admin/potential-partners/research" not in src
    assert "/api/admin/potential-partners/structure" not in src

def test_partner_cabinet_has_no_direct_business_object_service_mutation_routes():
    src = read("master_cabinet_api.py")
    for route in (
        'add_post("/api/master/{id}/businesses"',
        'add_delete("/api/master/{id}/businesses/{business_id}"',
        'add_post("/api/master/{id}/businesses/{business_id}"',
        'add_post("/api/master/{id}/objects"',
        'add_delete("/api/master/{id}/objects/{object_id}"',
        'add_post("/api/master/{id}/objects/{object_id}"',
        'add_post("/api/master/{id}/services"',
        'add_post("/api/master/{id}/services/{service_id}"',
        'add_delete("/api/master/{id}/services/{service_id}"',
        'add_post("/api/master/{id}/settings"',
    ):
        assert route not in src

def test_partner_negotiation_and_application_mutations_do_not_bypass_ai():
    src = read("master_cabinet_api.py")
    assert 'add_post("/api/master/{id}/negotiations/{negotiation_id}/agree"' not in src
    assert 'add_put("/api/master/{id}/applications/{application_id}"' not in src
    assert 'add_post("/api/master/{id}/applications/{application_id}/submit"' not in src
    assert 'add_delete("/api/master/{id}/applications/{application_id}"' not in src

def test_partner_cabinet_ui_does_not_offer_closed_mutation_actions():
    src = read("web_apps/master_cabinet.html")
    assert 'onclick="addService()"' not in src
    assert "onclick=\"agreeNegotiation(" not in src
    assert "saveGeneralSetting(&quot;contact_sharing_enabled&quot;" not in src
    assert "saveGeneralSetting(&quot;premium_contact_sharing_enabled&quot;" not in src

def test_client_negotiation_exposes_booking_payment_state_without_mutation():
    src = read("marketplace_flow_api.py")
    block = src[src.index("async def negotiation_get"):src.index("def _state",src.index("async def negotiation_get"))]
    assert "'booking': booking" in block
    assert "'payment_url': payment_url" in block

def test_client_ui_shows_payment_only_after_partner_confirmation():
    src = read("web_apps/client.html")
    assert "r.booking.status" in src
    assert "pending_payment" in src
    assert "Վճարել Idram-ով" in src

def test_client_ui_represents_full_booking_lifecycle():
    src = read("web_apps/client.html")
    for state in ("paid","in_progress","completed","cancelled","refunded"):
        assert state in src
    assert "provider-ի հաստատումը" in src

def test_cancellation_does_not_mark_booking_refunded_without_provider_settlement():
    src = read("data_core.py")
    block = src[src.index("def cancel_booking"):src.index("def record_payment_provider_fee")]
    assert 'final_status="cancelled"' in block
    assert "refund_pending" in block
    assert 'final_status="refunded"' not in block

def test_refund_settlement_requires_real_provider_reference_and_cancelled_booking():
    src = read("data_core.py")
    block = src[src.index("def reconcile_refund"):src.index("def marketplace_existing_payment")]
    assert 'booking_status != "cancelled"' in block
    assert "if not provider_refund_id:" in block
    assert "provider_refund_id" in block
    assert '"already_refunded":True' in block

def test_no_public_refund_settlement_endpoint():
    src = read("marketplace_flow_api.py")
    assert "/refund/settle" not in src
    assert "/refund/confirm" not in src
