"""Extract source dependency closures without importing upstream tests or rewriting assertions."""

from __future__ import annotations

import ast
import builtins
import re
from pathlib import Path

from revertbench.grade.policy import check_submission

_COOKIE = re.compile(r"^[ \t\f]*#.*?coding[:=][ \t]*([-\w.]+)")


class ReferenceError(ValueError):
    """An upstream reference cannot be preserved within the supported format."""


def _parse(source: str) -> ast.Module:
    try:
        encoded = source.encode("utf-8")
    except UnicodeError as error:
        raise ReferenceError("unsupported_source_encoding") from error
    if source.startswith("\ufeff") or len(encoded) > 1_048_576:
        raise ReferenceError("unsupported_source_encoding_or_size")
    for line in source.splitlines()[:2]:
        cookie = _COOKIE.match(line)
        if cookie and cookie.group(1).lower().replace("_", "-") not in {"utf-8", "utf8"}:
            raise ReferenceError("unsupported_source_encoding")
    try:
        return ast.parse(encoded)
    except (SyntaxError, ValueError, RecursionError, UnicodeError) as error:
        raise ReferenceError("unparseable_reference_module") from error


def _functions(tree: ast.Module) -> dict[str, ast.FunctionDef]:
    return {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}


def _implicit_context(tree: ast.Module) -> None:
    reserved = {
        "pytestmark",
        "pytest_plugins",
        "setup_module",
        "teardown_module",
        "setup_function",
        "teardown_function",
    }
    for node in tree.body:
        bound: set[str] = set()
        if isinstance(node, ast.Assign):
            bound = {target.id for target in node.targets if isinstance(target, ast.Name)}
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            bound = {node.target.id}
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            bound = {alias.asname or alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.FunctionDef):
            bound = {node.name}
        if reserved.intersection(bound) or any(name.startswith("pytest_") for name in bound):
            raise ReferenceError("unsupported_implicit_module_context")
    for child in ast.walk(tree):
        if isinstance(child, ast.Call) and any(
            keyword.arg == "autouse"
            and not (isinstance(keyword.value, ast.Constant) and keyword.value.value is False)
            for keyword in child.keywords
        ):
            raise ReferenceError("unsupported_implicit_autouse_context")


def changed_test_functions(before: str, after: str) -> tuple[str, ...]:
    old, new = _functions(_parse(before)), _functions(_parse(after))
    return tuple(
        name
        for name, node in new.items()
        if name.startswith("test_") and (name not in old or ast.dump(node) != ast.dump(old[name]))
    )


def _literal(node: ast.AST) -> object:
    if any(isinstance(child, ast.Call) for child in ast.walk(node)):
        raise ReferenceError("nonliteral_module_dependency")
    try:
        return ast.literal_eval(node)
    except (ValueError, TypeError, SyntaxError, RecursionError) as error:
        raise ReferenceError("nonliteral_module_dependency") from error


def _parameters(function: ast.FunctionDef, *, test: bool) -> None:
    arguments = function.args
    names = {argument.arg for argument in (*arguments.posonlyargs, *arguments.args)}
    if arguments.vararg or arguments.kwarg or arguments.kwonlyargs or arguments.defaults:
        raise ReferenceError("unsupported_reference_arguments")
    declared: set[str] = set()
    for decorator in function.decorator_list:
        if (
            not test
            or not isinstance(decorator, ast.Call)
            or not isinstance(decorator.func, ast.Attribute)
            or decorator.func.attr != "parametrize"
            or len(decorator.args) != 2
            or decorator.keywords
        ):
            raise ReferenceError("unsupported_reference_decorator_or_fixture")
        value = _literal(decorator.args[0])
        _literal(decorator.args[1])
        if not isinstance(value, str):
            raise ReferenceError("unsupported_reference_parameters")
        declared.update(part.strip() for part in value.split(","))
    if test and names != declared:
        raise ReferenceError("unsupported_reference_fixture")


def _loads(function: ast.FunctionDef) -> set[str]:
    local = {argument.arg for argument in (*function.args.posonlyargs, *function.args.args)}
    local.update(
        node.id
        for node in ast.walk(function)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store)
    )
    return {
        node.id
        for node in ast.walk(function)
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
    } - local


def _segment(source: str, node: ast.stmt) -> str:
    start = node.lineno
    if isinstance(node, ast.FunctionDef) and node.decorator_list:
        start = min(start, *(decorator.lineno for decorator in node.decorator_list))
    return "\n".join(source.splitlines()[start - 1 : node.end_lineno]) + "\n"


def extract_reference(
    source: str,
    function_name: str,
    *,
    max_lines: int = 80,
    bad_tree: Path | None = None,
) -> str:
    """Preserve one top-level test and its source-visible imports/literals/helpers.

    Unsupported fixtures, classes, global initialization and unresolved module
    dependencies are rejected instead of being rewritten into a different test.
    """
    tree = _parse(source)
    _implicit_context(tree)
    functions = _functions(tree)
    if function_name not in functions or not function_name.startswith("test_"):
        raise ReferenceError("missing_top_level_test")
    bindings: dict[str, ast.stmt] = {}
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                if alias.name == "*":
                    raise ReferenceError("wildcard_test_import")
                bindings[alias.asname or alias.name.split(".")[0]] = node
        elif isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            bindings[node.name] = node
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    bindings[target.id] = node
    selected: dict[int, ast.stmt] = {}
    needed = {function_name}
    used_names: set[str] = set()
    while needed:
        name = needed.pop()
        if name in used_names or (name not in bindings and name in vars(builtins)):
            continue
        used_names.add(name)
        dependency = bindings.get(name)
        if dependency is None:
            raise ReferenceError(f"unresolved_module_dependency:{name}")
        selected[dependency.lineno] = dependency
        if isinstance(dependency, ast.FunctionDef):
            _parameters(dependency, test=dependency is functions[function_name])
            needed.update(_loads(dependency) - used_names)
        elif isinstance(dependency, ast.Assign):
            _literal(dependency.value)
        elif isinstance(dependency, ast.ClassDef):
            raise ReferenceError("unsupported_reference_class_dependency")
    segments: list[str] = []
    for _, node in sorted(selected.items()):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            aliases = [
                alias
                for alias in node.names
                if (alias.asname or alias.name.split(".")[0]) in used_names
            ]
            clone = (
                ast.Import(names=aliases)
                if isinstance(node, ast.Import)
                else ast.ImportFrom(module=node.module, names=aliases, level=node.level)
            )
            segments.append(ast.unparse(clone) + "\n")
        else:
            segments.append(_segment(source, node))
    extracted = "\n".join(segments)
    if len(extracted.splitlines()) > max_lines:
        raise ReferenceError("reference_exceeds_line_limit")
    if bad_tree is not None:
        policy = check_submission(extracted, bad_tree)
        if not policy.allowed:
            raise ReferenceError(f"unsupported_reference_policy:{policy.reason}")
    return extracted


def reference_header(source: str) -> str:
    lines: list[str] = []
    for line in source.splitlines():
        if line.strip() and not line.lstrip().startswith("#"):
            break
        lines.append(line)
    return "\n".join(lines).strip()
