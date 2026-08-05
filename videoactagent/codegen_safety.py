"""Static AST gate for Python emitted by the isolated Blender codegen model."""

from __future__ import annotations

import ast
import hashlib
from typing import Any


ALLOWED_IMPORTS = {"bpy", "math", "mathutils"}
BLOCKED_NAMES = {
    "open", "eval", "exec", "compile", "__import__", "getattr", "setattr",
    "delattr", "globals", "locals", "vars", "input",
}
BLOCKED_BPY_PREFIXES = {
    "bpy.ops.wm", "bpy.ops.script", "bpy.ops.render",
    "bpy.data.libraries.load", "bpy.context.preferences",
}
MAX_CODE_BYTES = 40 * 1024
if hasattr(ast, "Constant"):
    _CONSTANT_TYPES = (ast.Constant,)
else:
    _CONSTANT_TYPES = (ast.Num, ast.Str, ast.Bytes, ast.NameConstant)


class CodegenSafetyError(ValueError):
    """Raised when generated Python is outside the intentionally small allowlist."""


def _dotted(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _dotted(node.value)
        return f"{prefix}.{node.attr}" if prefix else None
    return None


def _error(message: str, node: ast.AST | None = None) -> CodegenSafetyError:
    if node is None:
        return CodegenSafetyError(message)
    return CodegenSafetyError(f"{message} at line {getattr(node, 'lineno', '?')}")


def validate_generated_code(code: str) -> dict[str, Any]:
    """Require one ``build_scene(context)`` function and safe Blender imports."""

    if not isinstance(code, str) or not code.strip():
        raise CodegenSafetyError("generated code is empty")
    byte_count = len(code.encode("utf-8"))
    if byte_count > MAX_CODE_BYTES:
        raise CodegenSafetyError("generated code exceeds 40 KiB")
    try:
        tree = ast.parse(code, mode="exec")
    except SyntaxError as exc:
        raise CodegenSafetyError(f"generated code is not valid Python: {exc}") from exc

    entrypoints = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "build_scene"]
    if len(entrypoints) != 1:
        raise CodegenSafetyError("code must define exactly one build_scene")
    function = entrypoints[0]
    if isinstance(function, ast.AsyncFunctionDef):
        raise CodegenSafetyError("build_scene must be a synchronous function")
    if function.decorator_list:
        raise CodegenSafetyError("build_scene decorators are not allowed")
    args = function.args
    if getattr(args, "posonlyargs", []) or len(args.args) != 1 or args.vararg or args.kwarg or args.kwonlyargs or args.defaults or args.kw_defaults:
        raise CodegenSafetyError("build_scene must accept exactly one context argument")
    if args.args[0].arg != "context":
        raise CodegenSafetyError("build_scene argument must be named context")

    imports: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname is not None or alias.name not in ALLOWED_IMPORTS:
                    raise _error(f"import {alias.name} is not allowed", node)
                if alias.name not in imports:
                    imports.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level or node.module not in ALLOWED_IMPORTS:
                raise _error(f"import from {node.module!r} is not allowed", node)
            if any(alias.asname is not None for alias in node.names):
                raise _error("import aliases are not allowed", node)
            if node.module not in imports:
                imports.append(node.module)
        elif isinstance(node, ast.Name) and node.id in BLOCKED_NAMES:
            raise _error(f"blocked name {node.id}", node)
        elif isinstance(node, ast.Call):
            dotted = _dotted(node.func)
            if dotted and any(dotted == prefix or dotted.startswith(prefix + ".") for prefix in BLOCKED_BPY_PREFIXES):
                raise _error(f"blocked {dotted}", node)

    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            if node.value.id == "context":
                raise _error("context is a dict; use context['scene'] or context['input']", node)
            if node.value.id in {"math", "mathutils"} and node.value.id not in imports:
                raise _error(f"module {node.value.id} is used without an explicit import", node)

    allowed_top_level = (ast.Import, ast.ImportFrom, ast.FunctionDef, ast.Assign, ast.AnnAssign, ast.Expr)
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom, ast.FunctionDef)):
            continue
        if isinstance(node, ast.Expr) and isinstance(node.value, _CONSTANT_TYPES) and isinstance(getattr(node.value, "value", None), str):
            continue
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            value = node.value
            if not isinstance(value, _CONSTANT_TYPES + (ast.List, ast.Tuple, ast.Dict, ast.Set)):
                raise _error("top-level assignments must be constants", node)
            continue
        raise _error("top-level executable statements are not allowed", node)

    return {
        "schema_version": "blender-codegen-safety-1.0",
        "status": "accepted",
        "entrypoint": "build_scene",
        "imports": imports,
        "bytes": byte_count,
        "sha256": hashlib.sha256(code.encode("utf-8")).hexdigest(),
    }
