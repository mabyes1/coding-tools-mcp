from __future__ import annotations

import ast
from pathlib import Path
from typing import Any


ALLOWED_SERVER_IMPORTERS = {"server.py", "__main__.py"}
SERVER_FACADE_WARNING_LINES = 800
MODULE_SIZE_WARNING_LINES = 1500
MODULE_IMPORT_WARNING_COUNT = 45


def _imports_server(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(alias.name in {"server", "coding_tools_mcp.server"} for alias in node.names):
                return True
        elif isinstance(node, ast.ImportFrom):
            if node.module in {"server", "coding_tools_mcp.server"}:
                return True
            if node.module == "coding_tools_mcp" and any(alias.name == "server" for alias in node.names):
                return True
            if node.level > 0 and node.module == "server":
                return True
    return False


def _import_count(tree: ast.AST) -> int:
    return sum(isinstance(node, (ast.Import, ast.ImportFrom)) for node in ast.walk(tree))


def run_architecture_checks(package_parent: Path, server: Any) -> list[str]:
    """Fail on dependency/registry regressions and return non-fatal growth warnings."""

    package_root = package_parent / "coding_tools_mcp"
    if not package_root.is_dir():
        raise RuntimeError(f"private package root is missing: {package_root}")

    warnings: list[str] = []
    reverse_imports: list[str] = []
    for path in sorted(package_root.rglob("*.py")):
        relative = path.relative_to(package_root).as_posix()
        source = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(source, filename=str(path))
        except SyntaxError as exc:
            raise RuntimeError(f"architecture scan could not parse {relative}: {exc}") from exc

        if path.name not in ALLOWED_SERVER_IMPORTERS and _imports_server(tree):
            reverse_imports.append(relative)

        line_count = source.count("\n") + (0 if source.endswith("\n") else 1)
        if path.name == "server.py":
            if line_count > SERVER_FACADE_WARNING_LINES:
                warnings.append(
                    f"server.py facade grew to {line_count} lines (warning threshold {SERVER_FACADE_WARNING_LINES})"
                )
        elif line_count > MODULE_SIZE_WARNING_LINES:
            warnings.append(
                f"{relative} grew to {line_count} lines (warning threshold {MODULE_SIZE_WARNING_LINES})"
            )

        import_count = _import_count(tree)
        if path.name != "server.py" and import_count > MODULE_IMPORT_WARNING_COUNT:
            warnings.append(
                f"{relative} has {import_count} import statements (warning threshold {MODULE_IMPORT_WARNING_COUNT})"
            )

    if reverse_imports:
        raise RuntimeError(
            "low-level production modules must not import the server.py compatibility facade: "
            + ", ".join(reverse_imports)
        )

    registry_names = set(server.TOOL_REGISTRY)
    schema_names = set(server.input_schemas())
    public_names = set(server.PUBLIC_TOOL_NAMES)
    if registry_names != schema_names:
        raise RuntimeError(
            "tool registry/input-schema key drift: "
            f"missing_schemas={sorted(registry_names - schema_names)}, "
            f"orphan_schemas={sorted(schema_names - registry_names)}"
        )
    if not public_names.issubset(registry_names):
        raise RuntimeError(
            "public tool catalog contains unregistered names: "
            + ", ".join(sorted(public_names - registry_names))
        )
    if len(server.PUBLIC_TOOL_NAMES) != len(public_names):
        raise RuntimeError("public tool catalog contains duplicate names")
    for name in server.PUBLIC_TOOL_NAMES:
        definition = server.tool_definition(name)
        if definition.get("name") != name or definition.get("inputSchema") != server.input_schemas()[name]:
            raise RuntimeError(f"tool definition/catalog consistency drifted for {name}")

    return warnings
