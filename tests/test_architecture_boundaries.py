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


def _string_constants(tree):
    return [
        node.value.upper()
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]


def test_boundary_b_data_core_does_not_import_ai_layers():
    imports = _imports(_tree("data_core.py"))
    forbidden = ("ai_manager", "groq", "openai", "openrouter", "prompt_factory")
    violations = [item for item in imports if any(item == name or item.startswith(name + ".") for name in forbidden)]
    assert not violations, f"data_core.py imports forbidden AI-layer modules: {violations}"


def test_boundary_c_data_core_does_not_import_prompt_or_provider_modules():
    imports = _imports(_tree("data_core.py"))
    forbidden = ("prompt_factory", "groq", "openai", "openrouter")
    assert not [item for item in imports if any(item == name or item.startswith(name + ".") for name in forbidden)]


def test_boundary_d_ai_manager_contains_no_raw_sql():
    constants = _string_constants(_tree("ai_manager.py"))
    sql_markers = ("SELECT ", "INSERT INTO", "UPDATE ", "DELETE FROM", "ALTER TABLE", "DROP CONSTRAINT")
    violations = [value[:160] for value in constants if any(marker in value for marker in sql_markers)]
    assert not violations, f"ai_manager.py contains raw SQL strings: {violations}"


def test_boundary_e_platform_db_owns_transaction_api():
    tree = _tree("platform_db.py")
    names = {node.name for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)}
    assert "transaction" in names, "platform_db.py must expose the canonical transaction(callback) API"


def test_boundary_f_tool_specs_declare_contexts():
    tree = _tree("tool_registry.py")
    classes = [node for node in ast.walk(tree) if isinstance(node, ast.ClassDef) and node.name == "ToolSpec"]
    assert classes, "ToolSpec must remain the canonical tool declaration"
    fields = {
        target.id
        for node in ast.walk(classes[0])
        if isinstance(node, ast.AnnAssign)
        and isinstance(target := node.target, ast.Name)
    }
    assert "contexts" in fields, "ToolSpec must declare explicit context ownership"
