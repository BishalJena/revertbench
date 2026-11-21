"""Extract source dependency closures without importing upstream tests or rewriting assertions."""

from __future__ import annotations

import ast
import builtins
import re
from pathlib import Path
from textwrap import dedent, indent

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


def _reference_nodes(tree: ast.Module) -> dict[str, ast.FunctionDef]:
    result = {name: node for name, node in _functions(tree).items() if name.startswith("test_")}
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            result.update(
                {
                    f"{node.name}::{method.name}": method
                    for method in node.body
                    if isinstance(method, ast.FunctionDef) and method.name.startswith("test_")
                }
            )
    return result


def changed_test_functions(before: str, after: str) -> tuple[str, ...]:
    """Return changed top-level functions and qualified unittest-method candidates.

    A qualified candidate is not admitted until extract_reference verifies its
    complete class/base context. The original qualified identity is retained.
    """
    old, new = _reference_nodes(_parse(before)), _reference_nodes(_parse(after))
    return tuple(
        name
        for name, node in new.items()
        if name not in old or ast.dump(node) != ast.dump(old[name])
    )


def _binding_nodes(tree: ast.Module, name: str) -> list[ast.AST]:
    """Find module-scope bindings, without confusing unrelated method locals."""
    result: list[ast.AST] = []

    class Bindings(ast.NodeVisitor):
        def visit_Name(self, node: ast.Name) -> None:
            if isinstance(node.ctx, (ast.Store, ast.Del)) and node.id == name:
                result.append(node)

        def visit_Import(self, node: ast.Import) -> None:
            result.extend(
                node for alias in node.names if (alias.asname or alias.name.split(".")[0]) == name
            )

        def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
            result.extend(node for alias in node.names if (alias.asname or alias.name) == name)

        def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
            if node.name == name:
                result.append(node)
            self.generic_visit(node)

        def visit_FunctionDef(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
            if node.name == name:
                result.append(node)
            for expression in [
                *node.decorator_list,
                *node.args.defaults,
                *(value for value in node.args.kw_defaults if value is not None),
            ]:
                self.visit(expression)
            if any(
                isinstance(child, ast.Global) and name in child.names for child in ast.walk(node)
            ):
                result.append(node)

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            if node.name == name:
                result.append(node)
            for expression in [
                *node.bases,
                *node.decorator_list,
                *(keyword.value for keyword in node.keywords),
            ]:
                self.visit(expression)
            if any(
                isinstance(child, ast.Global) and name in child.names for child in ast.walk(node)
            ):
                result.append(node)

        def visit_Lambda(self, node: ast.Lambda) -> None:
            for expression in [
                *node.args.defaults,
                *(value for value in node.args.kw_defaults if value is not None),
            ]:
                self.visit(expression)

    Bindings().visit(tree)
    return result


def _unittest_base(tree: ast.Module, base: ast.expr) -> None:
    alias: str
    module_form = (
        isinstance(base, ast.Attribute)
        and base.attr == "TestCase"
        and isinstance(base.value, ast.Name)
    )
    if isinstance(base, ast.Name):
        alias = base.id
    elif module_form:
        assert isinstance(base, ast.Attribute) and isinstance(base.value, ast.Name)
        alias = base.value.id
    else:
        raise ReferenceError("unsupported_unittest_base")
    bindings = _binding_nodes(tree, alias)
    if len(bindings) != 1 or bindings[0] not in tree.body:
        raise ReferenceError("unittest_base_alias_rebound")
    binding = bindings[0]
    direct = (
        not module_form
        and isinstance(binding, ast.ImportFrom)
        and binding.level == 0
        and binding.module == "unittest"
        and any(
            item.name == "TestCase" and (item.asname or item.name) == alias
            for item in binding.names
        )
    )
    module = (
        module_form
        and isinstance(binding, ast.Import)
        and any(
            item.name == "unittest" and (item.asname or item.name) == alias
            for item in binding.names
        )
    )
    if not direct and not module:
        raise ReferenceError("unsupported_unittest_base")
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.ctx, (ast.Store, ast.Del)):
            root: ast.expr = node.value
            while isinstance(root, ast.Attribute):
                root = root.value
            if isinstance(root, ast.Name) and root.id == alias:
                raise ReferenceError("unittest_base_attribute_rebound")


def _additive_doctest_loader(tree: ast.Module, bindings: list[ast.AST]) -> bool:
    if len(bindings) != 1 or not isinstance(bindings[0], ast.FunctionDef):
        return False
    node = bindings[0]
    if (
        node not in tree.body
        or node.decorator_list
        or node.returns
        or node.type_params
        or node.args.posonlyargs
        or node.args.vararg
        or node.args.kwarg
        or node.args.kwonlyargs
        or node.args.defaults
        or [arg.arg for arg in node.args.args] != ["loader", "tests", "ignore"]
        or any(arg.annotation for arg in node.args.args)
    ):
        return False
    body = node.body
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body = body[1:]
    if len(body) != 2 or not isinstance(body[0], ast.Expr) or not isinstance(body[1], ast.Return):
        return False
    call, returned = body[0].value, body[1].value
    if (
        not isinstance(returned, ast.Name)
        or returned.id != "tests"
        or not isinstance(call, ast.Call)
        or call.keywords
        or len(call.args) != 1
        or not isinstance(call.func, ast.Attribute)
        or call.func.attr != "addTests"
        or not isinstance(call.func.value, ast.Name)
        or call.func.value.id != "tests"
    ):
        return False
    suite = call.args[0]
    if (
        not isinstance(suite, ast.Call)
        or suite.keywords
        or len(suite.args) != 1
        or not isinstance(suite.func, ast.Name)
        or not isinstance(suite.args[0], ast.Constant)
        or not isinstance(suite.args[0].value, str)
        or re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*", suite.args[0].value) is None
    ):
        return False
    imports = _binding_nodes(tree, suite.func.id)
    if len(imports) != 1 or imports[0] not in tree.body:
        return False
    binding = imports[0]
    return (
        isinstance(binding, ast.ImportFrom)
        and binding.level == 0
        and binding.module == "doctest"
        and any(
            alias.name == "DocTestSuite" and (alias.asname or alias.name) == suite.func.id
            for alias in binding.names
        )
    )


def _unittest_method(tree: ast.Module, qualified: str) -> ast.FunctionDef:
    parts = qualified.split("::")
    if len(parts) != 2 or not parts[1].startswith("test_"):
        raise ReferenceError("unsupported_unittest_reference_identity")
    class_name, method_name = parts
    classes = [
        node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == class_name
    ]
    if len(classes) != 1 or len(_binding_nodes(tree, class_name)) != 1:
        raise ReferenceError("unsupported_unittest_class_binding")
    cls = classes[0]
    if len(cls.bases) != 1 or cls.decorator_list or cls.keywords or cls.type_params:
        raise ReferenceError("unsupported_unittest_class_context")
    _unittest_base(tree, cls.bases[0])
    methods: dict[str, ast.FunctionDef] = {}
    for member in cls.body:
        if (
            isinstance(member, ast.Expr)
            and isinstance(member.value, ast.Constant)
            and isinstance(member.value.value, str)
        ):
            continue
        if not isinstance(member, ast.FunctionDef) or not member.name.startswith("test_"):
            raise ReferenceError("unsupported_unittest_class_context")
        if (
            member.name in methods
            or member.decorator_list
            or member.type_params
            or member.returns
            or any(
                arg.annotation
                for arg in (*member.args.posonlyargs, *member.args.args, *member.args.kwonlyargs)
            )
        ):
            raise ReferenceError("unsupported_unittest_method_context")
        _parameters(member, test=False)
        if (
            len(member.args.args) != 1
            or member.args.args[0].arg != "self"
            or member.args.posonlyargs
        ):
            raise ReferenceError("unsupported_unittest_method_parameters")
        methods[member.name] = member
    method = methods.get(method_name)
    if method is None:
        raise ReferenceError("missing_unittest_method")
    if any(_binding_nodes(tree, name) for name in ("setUpModule", "tearDownModule")):
        raise ReferenceError("unsupported_unittest_module_context")
    loader = _binding_nodes(tree, "load_tests")
    if loader and not _additive_doctest_loader(tree, loader):
        raise ReferenceError("unsupported_unittest_module_context")
    parents = {
        child: parent for parent in ast.walk(method) for child in ast.iter_child_nodes(parent)
    }
    for node in ast.walk(method):
        if node is not method and isinstance(
            node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        ):
            raise ReferenceError("unsupported_unittest_helper_dependency")
        if isinstance(node, ast.Name) and node.id == "self":
            parent = parents.get(node)
            call = parents.get(parent) if parent is not None else None
            if (
                not isinstance(node.ctx, ast.Load)
                or not isinstance(parent, ast.Attribute)
                or parent.attr != "assertEqual"
                or not isinstance(call, ast.Call)
                or call.func is not parent
            ):
                raise ReferenceError("unsupported_unittest_self_access")
    global_helpers = {
        node.name for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    if _loads(method).intersection(global_helpers):
        raise ReferenceError("unsupported_unittest_helper_dependency")
    return method


def _lift_unittest_method(source: str, qualified: str) -> tuple[str, str]:
    tree = _parse(source)
    method = _unittest_method(tree, qualified)
    if _binding_nodes(tree, method.name):
        raise ReferenceError("unittest_lift_name_collision")
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    names.update(node.arg for node in ast.walk(tree) if isinstance(node, ast.arg))
    names.update(
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ExceptHandler) and node.name is not None
    )
    names.update(
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    )
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names.update(alias.asname or alias.name.split(".")[0] for alias in node.names)
    alias, suffix = "_revertbench_test_case", 0
    while alias in names:
        suffix += 1
        alias = f"_revertbench_test_case_{suffix}"
    lines = source.splitlines(keepends=True)
    body = dedent("".join(lines[method.body[0].lineno - 1 : method.body[-1].end_lineno]))
    wrapper = (
        f"from unittest import TestCase as {alias}\n\n"
        f"def {method.name}():\n    self = {alias}()\n" + indent(body, "    ")
    )
    lifted = _parse(wrapper).body[-1]
    assert isinstance(lifted, ast.FunctionDef)
    if ast.dump(ast.Module(body=method.body, type_ignores=[])) != ast.dump(
        ast.Module(body=lifted.body[1:], type_ignores=[])
    ):
        raise ReferenceError("unittest_body_identity_changed")
    return source + "\n\n" + wrapper, method.name


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
    """Collect outer dependencies without leaking a comprehension's local target."""
    local = {argument.arg for argument in (*function.args.posonlyargs, *function.args.args)}

    class Bindings(ast.NodeVisitor):
        def visit_Name(self, node: ast.Name) -> None:
            if isinstance(node.ctx, ast.Store):
                local.add(node.id)

        def visit_ListComp(self, node: ast.ListComp) -> None:
            # Its target belongs to Python's implicit comprehension scope.
            pass

    Bindings().visit(function)
    loaded: set[str] = set()

    class Loads(ast.NodeVisitor):
        def __init__(self) -> None:
            self.bound = local

        def visit_Name(self, node: ast.Name) -> None:
            if isinstance(node.ctx, ast.Load) and node.id not in self.bound:
                loaded.add(node.id)

        def visit_ListComp(self, node: ast.ListComp) -> None:
            if (
                len(node.generators) != 1
                or node.generators[0].is_async
                or node.generators[0].ifs
                or not isinstance(node.generators[0].target, ast.Name)
                or any(
                    isinstance(child, ast.ListComp) and child is not node
                    for child in ast.walk(node)
                )
            ):
                raise ReferenceError("unsupported_reference_comprehension")
            generator = node.generators[0]
            self.visit(generator.iter)
            assert isinstance(generator.target, ast.Name)
            outer = self.bound
            self.bound = outer | {generator.target.id}
            self.visit(node.elt)
            self.bound = outer

    Loads().visit(function)
    return loaded


def _segment(source: str, node: ast.stmt) -> str:
    start = node.lineno
    if isinstance(node, ast.FunctionDef) and node.decorator_list:
        start = min(start, *(decorator.lineno for decorator in node.decorator_list))
    return "\n".join(source.splitlines()[start - 1 : node.end_lineno]) + "\n"


def _extract_standalone_reference(
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


def extract_reference(
    source: str,
    function_name: str,
    *,
    max_lines: int = 80,
    bad_tree: Path | None = None,
) -> str:
    """Extract an unchanged function closure or a strictly self-contained unittest method.

    Qualified ``Class::method`` provenance is preserved by the caller. The lifted
    ordinary test adds only a fresh exact TestCase instance; the original method
    body remains AST-identical, with no assertion or parameter-case rewriting.
    """
    if "::" in function_name:
        source, function_name = _lift_unittest_method(source, function_name)
    return _extract_standalone_reference(
        source, function_name, max_lines=max_lines, bad_tree=bad_tree
    )


def reference_header(source: str) -> str:
    lines: list[str] = []
    for line in source.splitlines():
        if line.strip() and not line.lstrip().startswith("#"):
            break
        lines.append(line)
    return "\n".join(lines).strip()
