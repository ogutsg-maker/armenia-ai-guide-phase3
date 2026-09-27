"""Static guards for the direct AI -> Python -> Data Core -> DB architecture."""
from pathlib import Path
import ast

ROOT = Path(__file__).resolve().parents[1]


def _text(name):
    return (ROOT / name).read_text(encoding="utf-8")


def _imports(name):
    tree = ast.parse(_text(name), filename=name)
    result = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            result.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            result.add(node.module)
    return result


def test_ai_orchestration_does_not_import_context_layers():
    for name in ("admin_ai_api.py", "client_ai.py", "partner_ai.py", "ai_router.py", "partner_ai_assistant_api.py"):
        imports = _imports(name)
        assert "ai_context_layer" not in imports
        assert "ai_context_builder" not in imports


def test_ai_orchestration_has_no_sql_text():
    # Admin/partner assistant APIs still contain legacy non-AI web operations;
    # the AI orchestration modules themselves must not compose SQL.
    for name in ("client_ai.py", "partner_ai.py", "ai_router.py"):
        text = _text(name).lower()
        for keyword in ("select ", "insert ", "update ", "delete ", "create table"):
            assert keyword not in text, f"SQL text in AI orchestration layer {name}: {keyword}"


def test_data_core_is_the_database_gateway():
    names = {
        node.name
        for node in ast.walk(ast.parse(_text("data_core.py"), filename="data_core.py"))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    required = {
        "get_application", "get_partner", "get_company", "get_service",
        "search_services", "search_catalog", "operational_stats",
        "get_ai_entity", "check_application",
    }
    assert required <= names


def test_ai_cost_accounting_remains_present():
    text = _text("ai_cost_center.py")
    assert "record_usage" in text
    assert "ai_usage_ledger" in text


def test_client_uses_small_live_catalog_query():
    text = _text("client_ai.py")
    assert "search_catalog(query=" in text
    assert "limit=20" in text
    assert "limit=500" not in text


def test_partner_registration_does_not_assign_catalog_direction():
    text = _text("partner_ai.py")
    assert "upsert_direction" not in text
    assert "catalog_tree" not in text
    assert "catalog=_catalog_text" not in text


def test_removed_ai_layers_are_not_referenced():
    forbidden = ("ai_data_tools", "ai_action_plan", "ai_context_layer", "ai_context_builder")
    for name in ("admin_ai_api.py", "client_ai.py", "partner_ai.py", "ai_router.py", "partner_ai_assistant_api.py"):
        text = _text(name).lower()
        for module in forbidden:
            assert module not in text, f"Legacy AI module reference in {name}: {module}"


def test_partner_service_creation_is_in_data_core():
    text = _text("data_core.py")
    assert "def create_partner_service(" in text


def test_legacy_ai_modules_are_deleted():
    for name in (
        "ai_action_plan.py",
        "ai_context_builder.py",
        "ai_context_layer.py",
        "ai_data_tools.py",
        "ai_schema.py",
        "ai_first_partner_onboarding.py",
    ):
        assert not (ROOT / name).exists(), f"Obsolete AI layer still exists: {name}"


def test_ai_runtime_modules_use_data_core_as_db_gateway():
    files = (
        "ai_service.py",
        "ai_router.py",
        "client_ai.py",
        "partner_ai.py",
        "partner_ai_assistant_api.py",
        "partner_registration_ai.py",
        "potential_partner_ai.py",
    )
    forbidden_imports = {"database", "platform_db", "ai_schema"}
    for name in files:
        imports = _imports(name)
        assert not (imports & forbidden_imports), f"Direct DB/legacy import in {name}: {imports & forbidden_imports}"


def test_ai_service_does_not_execute_sql():
    text = _text("ai_service.py").lower()
    import re
    assert not re.search(r"(?im)^\\s*(SELECT|INSERT|UPDATE|DELETE|CREATE\\s+TABLE)\\b", text), "SQL statement in ai_service.py"
