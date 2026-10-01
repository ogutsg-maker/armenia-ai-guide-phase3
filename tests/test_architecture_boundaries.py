import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _tree(name):
    path = ROOT / name
    assert path.exists(), f"missing required source file: {name}"
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _imports(tree):
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.append(node.module)
    return found


def _function(tree, name):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"missing function: {name}")


def test_boundary_b_data_core_does_not_import_ai_layers():
    imports = _imports(_tree("data_core.py"))
    forbidden = ("ai_manager", "groq", "openai", "openrouter", "prompt_factory")
    violations = [item for item in imports if any(item == name or item.startswith(name + ".") for name in forbidden)]
    assert not violations, f"data_core.py imports forbidden AI-layer modules: {violations}"


def test_boundary_c_data_core_does_not_import_provider_modules():
    imports = _imports(_tree("data_core.py"))
    forbidden = ("groq", "openai", "openrouter")
    assert not [item for item in imports if any(item == name or item.startswith(name + ".") for name in forbidden)]


def test_boundary_d_ai_manager_contains_no_raw_sql():
    constants = [
        node.value.upper()
        for node in ast.walk(_tree("ai_manager.py"))
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]
    markers = ("SELECT ", "INSERT INTO", "UPDATE ", "DELETE FROM", "ALTER TABLE", "DROP CONSTRAINT")
    violations = [value[:160] for value in constants if any(marker in value for marker in markers)]
    assert not violations, f"ai_manager.py contains raw SQL strings: {violations}"


def test_boundary_e_platform_db_owns_transaction_api_and_no_ai_messages_ddl():
    tree = _tree("platform_db.py")
    names = {node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)}
    assert "transaction" in names

    constants = [
        node.value.upper()
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]
    markers = ("ALTER TABLE AI_MESSAGES", "DROP CONSTRAINT", "ADD CONSTRAINT")
    violations = [value[:160] for value in constants if any(marker in value for marker in markers)]
    assert not violations, "platform_db.py must not mutate ai_messages schema at runtime"


def test_boundary_f_tool_registry_enforces_declared_context():
    fn = _function(_tree("tool_registry.py"), "execute")
    source = ast.unparse(fn)
    assert "self.context_type not in spec.contexts" in source


def test_boundary_g_application_approval_is_transactional_and_has_no_direct_db_wrapper_calls():
    fn = _function(_tree("data_core.py"), "admin_approve_application")
    calls_transaction = [
        node for node in ast.walk(fn)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "transaction"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "platform_db"
    ]
    assert calls_transaction, "approval must run through platform_db.transaction()"

    direct_wrappers = [
        node for node in ast.walk(fn)
        if isinstance(node, ast.Call)
        and (
            (isinstance(node.func, ast.Name) and node.func.id in {"execute", "one", "rows"})
            or
            (isinstance(node.func, ast.Attribute)
             and isinstance(node.func.value, ast.Name)
             and node.func.value.id == "platform_db"
             and node.func.attr in {"execute", "one", "rows"})
        )
    ]
    assert not direct_wrappers, "approval transaction must use only the callback cursor for DB access"


def test_boundary_h_direction_verification_approval_is_transactional():
    fn = _function(_tree("data_core.py"), "admin_approve_direction_verification")
    assert any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "transaction"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "platform_db"
        for node in ast.walk(fn)
    )


def test_boundary_i_direction_verification_rejection_is_transactional():
    fn = _function(_tree("data_core.py"), "admin_reject_direction_verification")
    assert any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "transaction"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "platform_db"
        for node in ast.walk(fn)
    )
