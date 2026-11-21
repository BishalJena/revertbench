"""Deterministically transplant verified historical statements without executing source."""

from __future__ import annotations

import ast
import builtins
import copy
import difflib
import hashlib
import re
import symtable
from dataclasses import dataclass

from revertbench.build.patch import BuildError, parse_production_patch, production_path
from revertbench.schema import CaseArtifact, RepairDerivation

_COOKIE = re.compile(r"^[ \t\f]*#.*?coding[:=][ \t]*([-\w.]+)")


@dataclass(frozen=True)
class DerivedFunctionRepair:
    patch: str
    repaired_source: str
    proof: RepairDerivation


def _parse(source: str) -> ast.Module:
    if source.startswith("\ufeff") or not source.endswith("\n"):
        raise BuildError("unsupported_derived_source_format")
    for line in source.splitlines()[:2]:
        cookie = _COOKIE.match(line)
        if cookie and cookie.group(1).lower().replace("_", "-") not in {"utf8", "utf-8"}:
            raise BuildError("unsupported_derived_source_encoding")
    try:
        encoded = source.encode("utf-8")
        if len(encoded) > 4_194_304:
            raise BuildError("unsupported_derived_source_size")
        return ast.parse(encoded, type_comments=True)
    except (SyntaxError, UnicodeError, ValueError, RecursionError) as error:
        raise BuildError("unsupported_derived_source") from error


def _dump(node: ast.AST) -> str:
    return ast.dump(node, include_attributes=False)


def _hash(node: ast.AST) -> str:
    return hashlib.sha256(_dump(node).encode()).hexdigest()


def _body(function: ast.FunctionDef) -> list[ast.stmt]:
    nodes = function.body
    if (
        nodes
        and isinstance(nodes[0], ast.Expr)
        and isinstance(nodes[0].value, ast.Constant)
        and isinstance(nodes[0].value.value, str)
    ):
        nodes = nodes[1:]
    if not nodes:
        raise BuildError("empty_executable_function_body")
    return nodes


def _signature(function: ast.FunctionDef) -> str:
    cloned = copy.deepcopy(function)
    cloned.body = []
    return _dump(cloned)


def _supported_definition(source: str, function: ast.FunctionDef) -> None:
    args = function.args
    parameters = [*args.posonlyargs, *args.args, *args.kwonlyargs]
    parameters.extend(value for value in (args.vararg, args.kwarg) if value is not None)
    if (
        function.decorator_list
        or function.returns
        or function.type_params
        or any(arg.annotation for arg in parameters)
    ):
        raise BuildError("unsupported_derived_function_definition")
    for value in [*args.defaults, *(node for node in args.kw_defaults if node is not None)]:
        if any(isinstance(node, ast.Call) for node in ast.walk(value)):
            raise BuildError("unsupported_derived_function_default")
        try:
            ast.literal_eval(value)
        except (ValueError, TypeError, SyntaxError, RecursionError) as error:
            raise BuildError("unsupported_derived_function_default") from error
    _indent_only_prefix(source.splitlines(keepends=True), _body(function)[0])


class _Bindings(ast.NodeVisitor):
    """Record source-visible module bindings without entering function/class bodies."""

    def __init__(self) -> None:
        self.values: dict[str, list[str]] = {}

    def add(self, name: str, node: ast.AST) -> None:
        self.values.setdefault(name, []).append(_dump(node))

    def visit_FunctionDef(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self.add(node.name, node)
        for expr in [
            *node.decorator_list,
            *node.args.defaults,
            *(v for v in node.args.kw_defaults if v is not None),
        ]:
            self.visit(expr)

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.add(node.name, node)
        for expr in [*node.bases, *node.decorator_list, *(v.value for v in node.keywords)]:
            self.visit(expr)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self.add(alias.asname or alias.name.split(".")[0], ast.Import(names=[alias]))

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if any(alias.name == "*" for alias in node.names):
            self.values.setdefault("*", []).append(_dump(node))
        for alias in node.names:
            self.add(
                alias.asname or alias.name,
                ast.ImportFrom(module=node.module, names=[alias], level=node.level),
            )

    def visit_Assign(self, node: ast.Assign) -> None:
        for target in node.targets:
            for child in ast.walk(target):
                if isinstance(child, ast.Name):
                    self.add(child.id, node)
        self.visit(node.value)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if isinstance(node.target, ast.Name):
            self.add(node.target.id, node)
        if node.value:
            self.visit(node.value)

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, (ast.Store, ast.Del)):
            self.add(node.id, node)

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if node.name is not None:
            self.add(node.name, node)
        self.generic_visit(node)

    def visit_MatchAs(self, node: ast.MatchAs) -> None:
        if node.name is not None:
            self.add(node.name, node)
        self.generic_visit(node)

    def visit_MatchStar(self, node: ast.MatchStar) -> None:
        if node.name is not None:
            self.add(node.name, node)

    def visit_MatchMapping(self, node: ast.MatchMapping) -> None:
        if node.rest is not None:
            self.add(node.rest, node)
        self.generic_visit(node)


def _bindings(tree: ast.Module) -> dict[str, list[str]]:
    visitor = _Bindings()
    visitor.visit(tree)
    return visitor.values


def _function(tree: ast.Module, name: str) -> ast.FunctionDef:
    functions = [
        node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == name
    ]
    if len(functions) != 1 or len(_bindings(tree).get(name, [])) != 1:
        raise BuildError("ambiguous_derived_repair_target")
    return functions[0]


def function_identity(source: str, name: str) -> tuple[str, str]:
    function = _function(_parse(source), name)
    return hashlib.sha256(_signature(function).encode()).hexdigest(), _hash(
        ast.Module(body=_body(function), type_ignores=[])
    )


def _globals(source: str, function: ast.FunctionDef) -> set[str]:
    try:
        table = symtable.symtable(source, "<historical-source>", "exec")
        selected = [
            child
            for child in table.get_children()
            if child.get_type() == "function"
            and child.get_name() == function.name
            and child.get_lineno() == function.lineno
        ]
    except (SyntaxError, ValueError, RecursionError) as error:
        raise BuildError("unsupported_function_scope") from error
    if len(selected) != 1:
        raise BuildError("unsupported_function_scope")

    def collect(scope: symtable.SymbolTable) -> set[str]:
        names = {
            symbol.get_name()
            for symbol in scope.get_symbols()
            if symbol.is_global() and symbol.is_referenced()
        }
        for child in scope.get_children():
            names.update(collect(child))
        return names

    return collect(selected[0])


def _dependencies(
    before: ast.Module,
    after: ast.Module,
    bad: ast.Module,
    before_fn: ast.FunctionDef,
    after_fn: ast.FunctionDef,
    source: str,
) -> None:
    before_imports = {
        _dump(node)
        for node in ast.walk(before_fn)
        if isinstance(node, (ast.Import, ast.ImportFrom))
    }
    if any(
        _dump(node) not in before_imports
        for node in ast.walk(after_fn)
        if isinstance(node, (ast.Import, ast.ImportFrom))
    ):
        raise BuildError("new_derived_function_dependency")
    historical_bindings, bad_bindings = _bindings(before), _bindings(bad)
    for name in _globals(source, after_fn) - {after_fn.name}:
        old, current = historical_bindings.get(name, []), bad_bindings.get(name, [])
        if not old and not current and name in vars(builtins):
            continue
        if len(old) != 1 or old != current:
            raise BuildError("unavailable_or_changed_derived_dependency")


def _start(node: ast.stmt) -> int:
    if (
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        and node.decorator_list
    ):
        return min(node.lineno, *(decorator.lineno for decorator in node.decorator_list))
    return node.lineno


def _indent_only_prefix(lines: list[str], node: ast.stmt) -> None:
    prefix = lines[_start(node) - 1].encode("utf-8")[: node.col_offset]
    if prefix.strip(b" \t"):
        raise BuildError("derived_statement_shares_definition_or_code_line")


def _span(nodes: list[ast.stmt], start: int, end: int) -> tuple[int, int]:
    if start == end:
        position = _start(nodes[start]) - 1 if start < len(nodes) else nodes[-1].end_lineno
        assert position is not None
        return position, position
    first, last = _start(nodes[start]) - 1, nodes[end - 1].end_lineno
    assert last is not None
    previous_end = nodes[start - 1].end_lineno if start else None
    if (previous_end is not None and previous_end > first) or (
        end < len(nodes) and _start(nodes[end]) <= last
    ):
        raise BuildError("overlapping_derived_statement_lines")
    return first, last


def derive_function_body_patch(
    before: str, after: str, bad: str, *, path: str, function_name: str, historical_patch: str
) -> DerivedFunctionRepair:
    """Rebind only historical changed statements after full-body equivalence checks."""
    production_path(path)
    if parse_production_patch(historical_patch, max_changed_lines=200).paths != (path,):
        raise BuildError("historical_derivation_path_mismatch")
    modules = [_parse(source) for source in (before, after, bad)]
    old, new, current = [_function(module, function_name) for module in modules]
    for source, function in zip((before, after, bad), (old, new, current), strict=True):
        _supported_definition(source, function)
    if not _signature(old) == _signature(new) == _signature(current):
        raise BuildError("derived_repair_signature_changed")
    old_body, new_body, current_body = map(_body, (old, new, current))
    if [_dump(node) for node in old_body] != [_dump(node) for node in current_body]:
        raise BuildError("derived_repair_algorithm_drift")
    before_mask, after_mask = copy.deepcopy(modules[0]), copy.deepcopy(modules[1])
    _function(before_mask, function_name).body = []
    _function(after_mask, function_name).body = []
    if _dump(before_mask) != _dump(after_mask):
        raise BuildError("historical_changes_outside_repair_target")
    _dependencies(modules[0], modules[1], modules[2], old, new, after)
    prefix = 0
    while prefix < min(len(old_body), len(new_body)) and _dump(old_body[prefix]) == _dump(
        new_body[prefix]
    ):
        prefix += 1
    suffix = 0
    while suffix < min(len(old_body) - prefix, len(new_body) - prefix) and _dump(
        old_body[-1 - suffix]
    ) == _dump(new_body[-1 - suffix]):
        suffix += 1
    old_end, new_end = len(old_body) - suffix, len(new_body) - suffix
    if prefix == old_end == new_end:
        raise BuildError("no_executable_repair_change")
    first, last = _span(current_body, prefix, old_end)
    new_first, new_last = _span(new_body, prefix, new_end)
    bad_lines, after_lines = bad.splitlines(keepends=True), after.splitlines(keepends=True)
    if prefix < len(current_body):
        _indent_only_prefix(bad_lines, current_body[prefix])
    if prefix < len(new_body):
        _indent_only_prefix(after_lines, new_body[prefix])
    replacement = after_lines[new_first:new_last]
    reference_line = bad_lines[_start(current_body[min(prefix, len(current_body) - 1)]) - 1]
    target_indent = re.match(r"[ \t]*", reference_line)
    if replacement:
        historical_indent = re.match(r"[ \t]*", replacement[0])
        assert target_indent is not None and historical_indent is not None
        old_indent, new_indent = historical_indent.group(), target_indent.group()
        replacement = [
            new_indent + line[len(old_indent) :] if line.startswith(old_indent) else line
            for line in replacement
        ]
    repaired = "".join([*bad_lines[:first], *replacement, *bad_lines[last:]])
    repaired_function = _function(_parse(repaired), function_name)
    if _signature(repaired_function) != _signature(new) or [
        _dump(node) for node in _body(repaired_function)
    ] != [_dump(node) for node in new_body]:
        raise BuildError("derived_repair_body_mismatch")
    patch = f"diff --git a/{path} b/{path}\n" + "".join(
        difflib.unified_diff(
            bad_lines, repaired.splitlines(keepends=True), fromfile=f"a/{path}", tofile=f"b/{path}"
        )
    )
    parse_production_patch(patch, max_changed_lines=200)
    proof = RepairDerivation(
        path=path,
        function=function_name,
        signature_sha256=hashlib.sha256(_signature(old).encode()).hexdigest(),
        before_body_sha256=_hash(ast.Module(body=old_body, type_ignores=[])),
        after_body_sha256=_hash(ast.Module(body=new_body, type_ignores=[])),
        derived_patch_sha256=hashlib.sha256(patch.encode()).hexdigest(),
        historical_diff=CaseArtifact(
            path="historical-production.diff",
            sha256=hashlib.sha256(historical_patch.encode()).hexdigest(),
        ),
    )
    return DerivedFunctionRepair(patch, repaired, proof)
