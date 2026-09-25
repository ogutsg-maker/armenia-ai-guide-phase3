"""Static architecture guardrails.

These tests intentionally avoid importing the application because production
imports require environment/database dependencies. They catch accidental
reintroduction of SQL into the AI Context/AI Tools boundary.
"""
from pathlib import Path
import ast

ROOT = Path(__file__).resolve().parents[1]


def _tree(name):
    return ast.parse((ROOT / name).read_text(encoding="utf-8"), filename=name)


def test_ai_context_has_no_sql_calls():
    tree = _tree("ai_context_layer.py")
    forbidden = {"execute", "one", "rows", "cursor", "commit"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            assert node.func.attr not in forbidden, f"direct DB call in AI Context: {node.func.attr}"
    text = (ROOT / "ai_context_layer.py").read_text(encoding="utf-8").lower()
    assert "select " not in text
    assert "insert " not in text
    assert "update " not in text
    assert "delete " not in text


def test_ai_context_builder_has_no_sql_calls():
    tree = _tree("ai_context_builder.py")
    forbidden = {"execute", "one", "rows", "cursor", "commit"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            assert node.func.attr not in forbidden, f"direct DB call in AI Context Builder: {node.func.attr}"
    text = (ROOT / "ai_context_builder.py").read_text(encoding="utf-8").lower()
    for keyword in ("select ", "insert ", "update ", "delete "):
        assert keyword not in text


def test_ai_tools_has_no_sql_calls():
    tree = _tree("ai_data_tools.py")
    forbidden = {"execute", "one", "rows", "cursor", "commit"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            assert node.func.attr not in forbidden, f"direct DB call in AI Tools: {node.func.attr}"
    text = (ROOT / "ai_data_tools.py").read_text(encoding="utf-8").lower()
    assert "select " not in text
    assert "insert " not in text
    assert "update " not in text
    assert "delete " not in text


def test_data_core_exposes_required_gateway_operations():
    tree = _tree("data_core.py")
    names = {
        node.name for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    required = {
        "get_application", "get_partner", "get_company", "get_service",
        "search_services", "search_catalog", "operational_stats",
        "get_ai_entity", "check_application", "active_negotiation_id_for_user",
    }
    assert required <= names


def test_ai_orchestration_layers_have_no_sql_text():
    for name in ("client_ai.py", "ai_router.py", "marketplace_flow_api.py"):
        path = ROOT / name
        text = path.read_text(encoding="utf-8").lower()
        for keyword in ("select ", "insert ", "update ", "delete ", "create table"):
            assert keyword not in text, f"SQL text in AI orchestration layer {name}: {keyword}"
