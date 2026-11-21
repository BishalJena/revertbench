"""Conservative, source-only admission policy for behavioral regression tests.

Admission is not sandboxing or a proof that an assertion detects the intended bug.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path
from typing import Never

POLICY_VERSION = "1"
MAX_SOURCE_BYTES = 65_536
MAX_AST_NODES = 3_000
MAX_AST_DEPTH = 64
_MAX_MODULE_BYTES = 1_048_576
_MAX_MODULES = 64


@dataclass(frozen=True)
class PolicyResult:
    """Admission decision; reason and details explain a rejection without running code."""

    allowed: bool
    reason: str | None
    details: tuple[str, ...] = ()


class _Violation(Exception):
    def __init__(self, reason: str, detail: str) -> None:
        self.reason = reason
        self.detail = detail
        super().__init__(detail)


def _reject(reason: str, detail: str) -> Never:
    raise _Violation(reason, detail)


@dataclass(frozen=True)
class _Symbol:
    kind: str
    module: str = ""
    node: ast.AST | None = None
    type_name: str | None = None


_UNKNOWN = _Symbol("value")
_BUILTIN_RETURNS = {
    "abs": None,
    "all": "bool",
    "any": "bool",
    "bool": "bool",
    "dict": "dict",
    "enumerate": None,
    "float": "float",
    "int": "int",
    "isinstance": "bool",
    "len": "int",
    "list": "list",
    "max": None,
    "min": None,
    "range": "range",
    "reversed": None,
    "round": None,
    "set": "set",
    "sorted": "list",
    "str": "str",
    "sum": None,
    "tuple": "tuple",
    "zip": None,
}
_EXCEPTIONS = frozenset(
    {
        "ArithmeticError",
        "AssertionError",
        "Exception",
        "IndexError",
        "KeyError",
        "LookupError",
        "OverflowError",
        "RuntimeError",
        "TypeError",
        "ValueError",
        "ZeroDivisionError",
    }
)
_METADATA_NAMES = frozenset(
    {
        "version",
        "version_info",
        "get_version",
        "version_tuple",
        "platform",
        "environ",
        "environment",
        "co_code",
        "co_consts",
        "f_back",
        "f_globals",
        "f_locals",
        "gi_frame",
        "cr_frame",
        "tb_frame",
        "func_globals",
        "func_code",
        "im_func",
    }
)
_DENIED_MODULES = frozenset(
    {
        "builtins",
        "ctypes",
        "importlib",
        "inspect",
        "io",
        "marshal",
        "os",
        "pathlib",
        "pickle",
        "platform",
        "resource",
        "runpy",
        "socket",
        "subprocess",
        "sys",
        "threading",
        "time",
        "urllib",
        "multiprocessing",
        "pytest_cov",
        "coverage",
        "revertbench",
    }
)
_MATH_FUNCTIONS = frozenset(
    {
        "acos",
        "acosh",
        "asin",
        "asinh",
        "atan",
        "atan2",
        "atanh",
        "ceil",
        "comb",
        "copysign",
        "cos",
        "cosh",
        "degrees",
        "dist",
        "erf",
        "erfc",
        "exp",
        "expm1",
        "fabs",
        "factorial",
        "floor",
        "fmod",
        "frexp",
        "fsum",
        "gamma",
        "gcd",
        "hypot",
        "isclose",
        "isfinite",
        "isinf",
        "isnan",
        "isqrt",
        "lcm",
        "ldexp",
        "lgamma",
        "log",
        "log10",
        "log1p",
        "log2",
        "modf",
        "nextafter",
        "perm",
        "pow",
        "prod",
        "radians",
        "remainder",
        "sin",
        "sinh",
        "sqrt",
        "tan",
        "tanh",
        "trunc",
        "ulp",
    }
)
_VALUE_METHODS: dict[str, dict[str, str | None]] = {
    "str": {
        "capitalize": "str",
        "casefold": "str",
        "center": "str",
        "count": "int",
        "endswith": "bool",
        "find": "int",
        "index": "int",
        "isalnum": "bool",
        "isalpha": "bool",
        "isascii": "bool",
        "isdecimal": "bool",
        "isdigit": "bool",
        "islower": "bool",
        "isnumeric": "bool",
        "isspace": "bool",
        "istitle": "bool",
        "isupper": "bool",
        "join": "str",
        "lower": "str",
        "lstrip": "str",
        "partition": "tuple",
        "removeprefix": "str",
        "removesuffix": "str",
        "replace": "str",
        "rfind": "int",
        "rindex": "int",
        "rpartition": "tuple",
        "rsplit": "list",
        "rstrip": "str",
        "split": "list",
        "splitlines": "list",
        "startswith": "bool",
        "strip": "str",
        "swapcase": "str",
        "title": "str",
        "upper": "str",
        "zfill": "str",
    },
    "list": {
        "append": None,
        "clear": None,
        "copy": "list",
        "count": "int",
        "extend": None,
        "index": "int",
        "insert": None,
        "pop": None,
        "remove": None,
        "reverse": None,
        "sort": None,
    },
    "tuple": {"count": "int", "index": "int"},
    "dict": {
        "clear": None,
        "copy": "dict",
        "get": None,
        "items": None,
        "keys": None,
        "pop": None,
        "setdefault": None,
        "update": None,
        "values": None,
    },
    "set": {
        "add": None,
        "clear": None,
        "copy": "set",
        "difference": "set",
        "discard": None,
        "intersection": "set",
        "isdisjoint": "bool",
        "issubset": "bool",
        "issuperset": "bool",
        "pop": None,
        "remove": None,
        "symmetric_difference": "set",
        "union": "set",
        "update": None,
    },
}


def _check_name(name: str) -> None:
    if name.startswith("__") or name.startswith("pytest_") or name.lower() in _METADATA_NAMES:
        _reject("introspection", f"Reserved or metadata name is outside policy: {name}")


def _check_member(name: str) -> None:
    _check_name(name)
    if name.startswith("_"):
        _reject("introspection", "Private attribute or imported name is outside policy")


def _value_symbol(node: ast.AST | None) -> _Symbol:
    if isinstance(node, ast.Constant):
        return _Symbol("value", node=node, type_name=type(node.value).__name__)
    for node_type, name in (
        (ast.List, "list"),
        (ast.Tuple, "tuple"),
        (ast.Set, "set"),
        (ast.Dict, "dict"),
    ):
        if isinstance(node, node_type):
            return _Symbol("value", type_name=name)
    return _UNKNOWN


def _with_origin(symbol: _Symbol, module: str) -> _Symbol:
    if symbol.kind == "value":
        return _Symbol("value", module=module, node=symbol.node, type_name=symbol.type_name)
    return symbol


def _check_metadata_key(container: _Symbol, key: _Symbol) -> None:
    if container.module and isinstance(key.node, ast.Constant) and isinstance(key.node.value, str):
        _check_name(key.node.value)


def _literal(node: ast.AST, reason: str, detail: str) -> object:
    # literal_eval accepts set(), whose spelling could bind to a project function.
    if any(isinstance(child, ast.Call) for child in ast.walk(node)):
        _reject(reason, detail)
    try:
        return ast.literal_eval(node)
    except (ValueError, TypeError, SyntaxError, RecursionError):
        _reject(reason, detail)


def _text_add(left: _Symbol, right: _Symbol) -> _Symbol:
    if (
        isinstance(left.node, ast.Constant)
        and isinstance(left.node.value, str)
        and isinstance(right.node, ast.Constant)
        and isinstance(right.node.value, str)
        and len(left.node.value) + len(right.node.value) <= 256
    ):
        return _value_symbol(ast.Constant(value=left.node.value + right.node.value))
    return _UNKNOWN


@dataclass
class _Module:
    tree: ast.Module
    package: bool
    definitions: dict[str, ast.AST] = field(default_factory=dict)
    imports: dict[str, tuple[str, str | None]] = field(default_factory=dict)


class _SourceIndex:
    """Resolve a bounded set of source definitions, never Python runtime objects."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.modules: dict[str, _Module] = {}
        self.resolving: set[tuple[str, str]] = set()

    def module(self, name: str) -> _Symbol:
        self._load(name)
        return _Symbol("module", module=name)

    def _path(self, name: str) -> tuple[Path, bool]:
        parts = name.split(".")
        for part in parts:
            _check_member(part)
            if part in {"tests", "conftest"} or part.startswith("test_"):
                _reject("unsupported_import", "Test support modules are not approved project APIs")
        bases: tuple[Path, ...]
        if len(parts) > 1:
            parent, package = self._path(".".join(parts[:-1]))
            if not package:
                _reject(
                    "unresolved_symbol", "A non-package module cannot contain a source submodule"
                )
            bases = (parent.parent,)
        else:
            bases = (self.root / "src", self.root)
        for base in bases:
            stem = base / parts[-1]
            for candidate, package in (
                (stem / "__init__.py", True),
                (stem.with_suffix(".py"), False),
            ):
                try:
                    resolved = candidate.resolve()
                    if resolved.is_relative_to(self.root) and resolved.is_file():
                        return resolved, package
                except (OSError, RuntimeError):
                    continue
        _reject("unresolved_symbol", f"No in-tree Python source for module: {name}")

    def _load(self, name: str) -> _Module:
        if name in self.modules:
            return self.modules[name]
        if len(self.modules) >= _MAX_MODULES:
            _reject("unresolved_symbol", "Source resolution exceeded its module budget")
        path, is_package = self._path(name)
        try:
            with path.open("rb") as stream:
                data = stream.read(_MAX_MODULE_BYTES + 1)
            if len(data) > _MAX_MODULE_BYTES:
                _reject("unresolved_symbol", "Project module exceeds source-resolution limit")
            tree = ast.parse(data, filename=str(path))
        except OSError:
            _reject("project_unavailable", "Project module cannot be read")
        except (SyntaxError, ValueError, RecursionError):
            _reject("unresolved_symbol", "Project module could not be parsed safely")
        result = _Module(tree=tree, package=is_package)
        self.modules[name] = result
        for node in tree.body:
            self._index_statement(name, result, node)
        return result

    def _index_statement(self, name: str, result: _Module, node: ast.stmt) -> None:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            result.definitions[node.name] = node
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    result.definitions[target.id] = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            result.definitions[node.target.id] = node.value or node.annotation
        elif isinstance(node, ast.Import):
            for alias in node.names:
                binding = alias.asname or alias.name.split(".")[0]
                result.imports[binding] = (alias.name if alias.asname else binding, None)
        elif isinstance(node, ast.ImportFrom):
            target_module = self._relative_module(name, result.package, node)
            for alias in node.names:
                if alias.name != "*":
                    result.imports[alias.asname or alias.name] = (target_module, alias.name)

    @staticmethod
    def _relative_module(name: str, package: bool, node: ast.ImportFrom) -> str:
        if not node.level:
            return node.module or ""
        parts = name.split(".") if package else name.split(".")[:-1]
        if node.level > len(parts):
            _reject("unresolved_symbol", "Relative project import escapes its package")
        prefix = parts[: len(parts) - node.level + 1]
        return ".".join([*prefix, *([node.module] if node.module else [])])

    def symbol(self, module: str, name: str) -> _Symbol:
        _check_member(name)
        key = (module, name)
        if key in self.resolving:
            _reject("unresolved_symbol", "Cyclic project reexport cannot be resolved")
        self.resolving.add(key)
        try:
            return self._resolve_symbol(module, name)
        finally:
            self.resolving.remove(key)

    def _resolve_symbol(self, module: str, name: str) -> _Symbol:
        info = self._load(module)
        node = info.definitions.get(name)
        if isinstance(node, ast.FunctionDef):
            return _Symbol("function", module=module, node=node)
        if isinstance(node, ast.ClassDef):
            return _Symbol("class", module=module, node=node)
        if isinstance(node, ast.Name):
            return self.symbol(module, node.id)
        if node is not None:
            return _with_origin(_value_symbol(node), module)
        if name in info.imports:
            origin, imported = info.imports[name]
            if info.package and origin == module and imported == name:
                return self.module(f"{module}.{name}")
            return self.module(origin) if imported is None else self.symbol(origin, imported)
        if info.package:
            return self.module(f"{module}.{name}")
        _reject("unresolved_symbol", f"Name is not defined on the bad tree: {module}.{name}")

    def member(self, symbol: _Symbol, name: str) -> _Symbol:
        _check_member(name)
        if symbol.kind == "module":
            return self.symbol(symbol.module, name)
        if symbol.kind in {"class", "instance"} and isinstance(symbol.node, ast.ClassDef):
            return _with_origin(self._class_member(symbol, name), symbol.module)
        _reject("unresolved_symbol", f"Attribute cannot be resolved from source: {name}")

    def _class_member(self, symbol: _Symbol, name: str) -> _Symbol:
        assert isinstance(symbol.node, ast.ClassDef)
        for statement in symbol.node.body:
            if isinstance(statement, ast.FunctionDef) and statement.name == name:
                result = _Symbol("function", module=symbol.module, node=statement)
                if any(
                    isinstance(d, ast.Name) and d.id == "property" for d in statement.decorator_list
                ):
                    return self.return_value(result)
                return result
            if (
                isinstance(statement, ast.AnnAssign)
                and isinstance(statement.target, ast.Name)
                and statement.target.id == name
            ):
                return self._annotation(symbol.module, statement.annotation)
            if isinstance(statement, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == name for t in statement.targets
            ):
                return _value_symbol(statement.value)
            if isinstance(statement, ast.FunctionDef) and statement.name == "__init__":
                value = self._instance_field(symbol.module, statement, name)
                if value is not None:
                    return value
        _reject("unresolved_symbol", f"Class attribute is not defined on the bad tree: {name}")

    def _instance_field(self, module: str, init: ast.FunctionDef, name: str) -> _Symbol | None:
        arguments = {arg.arg: arg.annotation for arg in init.args.args}
        for node in ast.walk(init):
            targets = node.targets if isinstance(node, ast.Assign) else []
            if isinstance(node, ast.AnnAssign):
                targets = [node.target]
            for target in targets:
                if (
                    isinstance(target, ast.Attribute)
                    and isinstance(target.value, ast.Name)
                    and target.value.id == "self"
                    and target.attr == name
                ):
                    if isinstance(node, ast.AnnAssign):
                        return self._annotation(module, node.annotation)
                    if isinstance(node, ast.Assign) and isinstance(node.value, ast.Name):
                        return self._annotation(module, arguments.get(node.value.id))
                    return _value_symbol(node.value if isinstance(node, ast.Assign) else None)
        return None

    def return_value(self, symbol: _Symbol) -> _Symbol:
        if isinstance(symbol.node, ast.FunctionDef):
            return _with_origin(self._annotation(symbol.module, symbol.node.returns), symbol.module)
        return _UNKNOWN

    def _annotation(self, module: str, node: ast.AST | None) -> _Symbol:
        if isinstance(node, ast.Subscript):
            return self._annotation(module, node.value)
        if isinstance(node, ast.Name):
            if node.id in {"str", "int", "float", "bool", "list", "dict", "tuple", "set"}:
                return _Symbol("value", type_name=node.id)
            try:
                symbol = self.symbol(module, node.id)
            except _Violation:
                return _UNKNOWN
            if symbol.kind == "class":
                return _Symbol("instance", module=symbol.module, node=symbol.node)
        return _UNKNOWN


class _Submission:
    def __init__(self, index: _SourceIndex) -> None:
        self.index = index
        self.globals: dict[str, _Symbol] = {}
        self.observed = False

    def check(self, tree: ast.Module) -> None:
        functions: list[ast.FunctionDef] = []
        for node in tree.body:
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                self._import(node)
            elif isinstance(node, ast.FunctionDef):
                _check_name(node.name)
                self._declare(node.name, _Symbol("helper", node=node))
                functions.append(node)
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                self._constant(node)
            elif not self._docstring(node):
                _reject(
                    "top_level_effect", "Module scope permits only imports, constants and functions"
                )
        tests = [node for node in functions if node.name.startswith("test_")]
        if not tests:
            _reject("empty_submission", "Submission must define at least one test function")
        for node in functions:
            self._function(node)

    def _declare(self, name: str, symbol: _Symbol) -> None:
        _check_name(name)
        if name in self.globals:
            _reject("duplicate_binding", f"Duplicate module binding: {name}")
        self.globals[name] = symbol

    def _module(self, name: str) -> _Symbol:
        if name == "pytest":
            return _Symbol("pytest")
        if name == "math":
            return _Symbol("math")
        if name.split(".")[0] in _DENIED_MODULES:
            _reject("unsupported_import", f"Module is outside the supported import subset: {name}")
        return self.index.module(name)

    def _import(self, node: ast.Import | ast.ImportFrom) -> None:
        if isinstance(node, ast.Import):
            for alias in node.names:
                module = self._module(alias.name)
                if not alias.asname and "." in alias.name:
                    module = self._module(alias.name.split(".")[0])
                self._declare(alias.asname or alias.name.split(".")[0], module)
            return
        if node.level or not node.module:
            _reject("unsupported_import", "Relative imports are outside the submission subset")
        module = self._module(node.module)
        for alias in node.names:
            if alias.name == "*":
                _reject("unsupported_import", "Wildcard imports are outside the submission subset")
            self._declare(alias.asname or alias.name, self._member(module, alias.name))

    def _constant(self, node: ast.Assign | ast.AnnAssign) -> None:
        if isinstance(node, ast.AnnAssign):
            _reject("unsupported_syntax", "Module annotations are outside the supported subset")
        _literal(
            node.value, "top_level_effect", "Module constants must be call-free literal values"
        )
        for target in node.targets:
            if not isinstance(target, ast.Name):
                _reject("unsupported_syntax", "Module constants require simple name targets")
            self._declare(target.id, _value_symbol(node.value))

    def _function(self, node: ast.FunctionDef) -> None:
        parameters = self._parameters(node)
        parametrized = self._decorators(node)
        if node.name.startswith("test_") and set(parameters) != parametrized:
            _reject(
                "unsupported_fixture", "Test parameters must be declared by literal parametrize"
            )
        env = {**self.globals, **dict.fromkeys(parameters, _UNKNOWN)}
        self.observed = False
        self._statements(node.body, env)
        if node.name.startswith("test_") and not self.observed:
            _reject("empty_test", "A test must assert, expect an exception or call a project API")

    @staticmethod
    def _parameters(node: ast.FunctionDef) -> list[str]:
        args = node.args
        if args.vararg or args.kwarg or args.kwonlyargs or node.returns:
            _reject(
                "unsupported_syntax",
                "Variadic/keyword-only parameters and annotations are unsupported",
            )
        parameters = [*args.posonlyargs, *args.args]
        if any(arg.annotation for arg in parameters) or args.defaults:
            _reject(
                "unsupported_syntax", "Function annotations and default arguments are unsupported"
            )
        names = [arg.arg for arg in parameters]
        for name in names:
            _check_name(name)
        return names

    def _decorators(self, node: ast.FunctionDef) -> set[str]:
        names: set[str] = set()
        for decorator in node.decorator_list:
            if not node.name.startswith("test_") or not isinstance(decorator, ast.Call):
                _reject("unsupported_decorator", "Only literal test parametrization is supported")
            symbol = self._expression(decorator.func, self.globals)
            if symbol.kind != "parametrize" or decorator.keywords or len(decorator.args) != 2:
                _reject(
                    "unsupported_decorator", "Only two-argument literal parametrize is supported"
                )
            declared, values = (
                _literal(
                    argument,
                    "unsupported_decorator",
                    "Parametrization must use call-free literal names and values",
                )
                for argument in decorator.args
            )
            if not isinstance(declared, str) or not isinstance(values, (list, tuple)) or not values:
                _reject(
                    "unsupported_decorator", "Parametrization needs named, non-empty literal cases"
                )
            incoming = [part.strip() for part in declared.split(",")]
            if (
                any(not name.isidentifier() for name in incoming)
                or len(set(incoming)) != len(incoming)
                or names.intersection(incoming)
            ):
                _reject(
                    "unsupported_decorator", "Parametrization names must be distinct identifiers"
                )
            for name in incoming:
                _check_name(name)
            names.update(incoming)
        return names

    def _statements(self, nodes: list[ast.stmt], env: dict[str, _Symbol]) -> None:
        for node in nodes:
            if isinstance(node, ast.Assert):
                self._expression(node.test, env)
                if node.msg:
                    self._expression(node.msg, env)
                self.observed = True
            elif isinstance(node, ast.Expr):
                self._expression(node.value, env)
            elif isinstance(node, ast.Assign):
                symbol = self._expression(node.value, env)
                for target in node.targets:
                    self._bind(target, symbol, env)
            elif isinstance(node, ast.AugAssign):
                self._expression(node.target, env)
                self._expression(node.value, env)
                self._bind(node.target, _UNKNOWN, env)
            elif isinstance(node, ast.Return):
                if node.value:
                    self._expression(node.value, env)
            elif isinstance(node, ast.If):
                self._expression(node.test, env)
                self._branches(node.body, node.orelse, env)
            elif isinstance(node, (ast.For, ast.While)):
                self._loop(node, env)
            elif isinstance(node, ast.With):
                self._with(node, env)
            elif not isinstance(node, (ast.Pass, ast.Break, ast.Continue)):
                _reject("unsupported_syntax", f"Unsupported statement: {type(node).__name__}")

    def _branches(
        self, left: list[ast.stmt], right: list[ast.stmt], env: dict[str, _Symbol]
    ) -> None:
        left_env, right_env = env.copy(), env.copy()
        self._statements(left, left_env)
        self._statements(right, right_env)
        for key in left_env.keys() | right_env.keys():
            if key in left_env and left_env.get(key) == right_env.get(key):
                env[key] = left_env[key]
            else:
                env[key] = _UNKNOWN

    def _loop(self, node: ast.For | ast.While, env: dict[str, _Symbol]) -> None:
        if isinstance(node, ast.For):
            self._expression(node.iter, env)
            self._bind(node.target, _UNKNOWN, env)
        else:
            self._expression(node.test, env)
        self._branches(node.body, node.orelse, env)

    @staticmethod
    def _bind(node: ast.expr, symbol: _Symbol, env: dict[str, _Symbol]) -> None:
        if isinstance(node, ast.Name):
            _check_name(node.id)
            env[node.id] = symbol
        elif isinstance(node, (ast.Tuple, ast.List)):
            for target in node.elts:
                _Submission._bind(target, _UNKNOWN, env)
        else:
            _reject(
                "unsupported_syntax", "Assignments may bind local names, not attributes or items"
            )

    def _with(self, node: ast.With, env: dict[str, _Symbol]) -> None:
        for item in node.items:
            if item.optional_vars or not isinstance(item.context_expr, ast.Call):
                _reject(
                    "unsupported_context",
                    "Only pytest.raises without captured runtime objects is supported",
                )
            call = item.context_expr
            if self._expression(call.func, env).kind != "raises" or len(call.args) != 1:
                _reject(
                    "unsupported_context",
                    "Only single-exception pytest.raises contexts are supported",
                )
            exception = self._expression(call.args[0], env)
            if exception.kind != "exception":
                _reject("unsupported_context", "pytest.raises requires an approved exception type")
            for keyword in call.keywords:
                if keyword.arg != "match" or not isinstance(keyword.value, ast.Constant):
                    _reject(
                        "unsupported_context", "pytest.raises supports only a literal match pattern"
                    )
                if not isinstance(keyword.value.value, str):
                    _reject("unsupported_context", "Exception match must be a literal string")
            self.observed = True
        self._statements(node.body, env)

    def _expression(self, node: ast.expr, env: dict[str, _Symbol]) -> _Symbol:
        if isinstance(node, ast.Name):
            _check_name(node.id)
            if node.id in env:
                return env[node.id]
            if node.id in _BUILTIN_RETURNS:
                return _Symbol("builtin", type_name=_BUILTIN_RETURNS[node.id])
            if node.id in _EXCEPTIONS:
                return _Symbol("exception")
            _reject("unresolved_symbol", f"Name is not an approved binding: {node.id}")
        if isinstance(node, ast.Attribute):
            return self._member(self._expression(node.value, env), node.attr)
        if isinstance(node, ast.Call):
            return self._call(node, env)
        if isinstance(node, ast.Constant):
            return _value_symbol(node)
        if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
            for item in node.elts:
                self._expression(item, env)
            return _value_symbol(node)
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values, strict=True):
                if key is None:
                    _reject("unsupported_syntax", "Dictionary unpacking is outside the subset")
                self._expression(key, env)
                self._expression(value, env)
            return _value_symbol(node)
        if isinstance(node, ast.Subscript):
            container = self._expression(node.value, env)
            key_symbol = self._expression(node.slice, env)
            _check_metadata_key(container, key_symbol)
            return _with_origin(_UNKNOWN, container.module)
        if isinstance(node, ast.Slice):
            for part in (node.lower, node.upper, node.step):
                if part:
                    self._expression(part, env)
            return _UNKNOWN
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            return _text_add(self._expression(node.left, env), self._expression(node.right, env))
        if isinstance(node, (ast.BoolOp, ast.BinOp, ast.UnaryOp, ast.Compare, ast.IfExp)):
            for child in ast.iter_child_nodes(node):
                if isinstance(child, ast.expr):
                    self._expression(child, env)
            return _UNKNOWN
        _reject("unsupported_syntax", f"Unsupported expression: {type(node).__name__}")

    def _member(self, symbol: _Symbol, name: str) -> _Symbol:
        _check_member(name)
        if symbol.kind == "pytest" and name == "raises":
            return _Symbol("raises")
        if symbol.kind == "pytest" and name == "mark":
            return _Symbol("pytest_mark")
        if symbol.kind == "pytest_mark" and name == "parametrize":
            return _Symbol("parametrize")
        if symbol.kind == "math":
            if name in _MATH_FUNCTIONS:
                return _Symbol("builtin")
            if name in {"e", "inf", "nan", "pi", "tau"}:
                return _Symbol("value", type_name="float")
        if symbol.kind == "value":
            methods = _VALUE_METHODS.get(symbol.type_name or "", {})
            if name in methods:
                kind = (
                    "mapping_lookup"
                    if symbol.type_name == "dict" and name in {"get", "pop", "setdefault"}
                    else "builtin"
                )
                return _Symbol(kind, module=symbol.module, type_name=methods[name])
        return self.index.member(symbol, name)

    def _call(self, node: ast.Call, env: dict[str, _Symbol]) -> _Symbol:
        symbol = self._expression(node.func, env)
        arguments = [self._expression(argument, env) for argument in node.args]
        for keyword in node.keywords:
            if keyword.arg is None:
                _reject("unsupported_syntax", "Keyword unpacking is outside the subset")
            _check_name(keyword.arg)
            arguments.append(self._expression(keyword.value, env))
        if symbol.kind == "function":
            self.observed = True
            return self.index.return_value(symbol)
        if symbol.kind == "class":
            self.observed = True
            return _Symbol("instance", module=symbol.module, node=symbol.node)
        if symbol.kind == "mapping_lookup" and node.args:
            _check_metadata_key(symbol, self._expression(node.args[0], env))
        if symbol.kind in {"builtin", "exception", "mapping_lookup"}:
            origin = symbol.module or next((arg.module for arg in arguments if arg.module), "")
            return _Symbol("value", module=origin, type_name=symbol.type_name)
        if symbol.kind == "helper":
            return _UNKNOWN
        _reject("unsupported_call", "Call target is not an approved statically resolved function")

    @staticmethod
    def _docstring(node: ast.stmt) -> bool:
        return (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        )


def _bounded_parse(source: str) -> ast.Module:
    try:
        length = len(source.encode("utf-8"))
    except UnicodeError:
        _reject("invalid_source", "Source must be valid UTF-8 text")
    if length > MAX_SOURCE_BYTES:
        _reject("source_too_large", f"Submission exceeds {MAX_SOURCE_BYTES} UTF-8 bytes")
    try:
        tree = ast.parse(source, filename="test_submission.py")
    except (SyntaxError, ValueError, RecursionError):
        _reject("invalid_source", "Submission cannot be parsed as supported Python")
    pending: list[tuple[ast.AST, int]] = [(tree, 0)]
    count = 0
    while pending:
        node, depth = pending.pop()
        count += 1
        if count > MAX_AST_NODES or depth > MAX_AST_DEPTH:
            _reject("source_too_complex", "Submission exceeds AST node or depth limits")
        pending.extend((child, depth + 1) for child in ast.iter_child_nodes(node))
    return tree


def check_submission(source: str, bad_tree: Path) -> PolicyResult:
    """Admit only the supported test subset without executing submission or source.

    ``project_unavailable`` identifies missing infrastructure rather than a bad test.
    All other reasons indicate policy rejection, not a behavioral grading outcome.
    """
    try:
        if not bad_tree.is_dir():
            return PolicyResult(
                False, "project_unavailable", ("Bad-tree directory is unavailable",)
            )
        tree = _bounded_parse(source)
        _Submission(_SourceIndex(bad_tree)).check(tree)
    except _Violation as error:
        return PolicyResult(False, error.reason, (error.detail,))
    except OSError:
        return PolicyResult(False, "project_unavailable", ("Bad-tree directory cannot be read",))
    return PolicyResult(True, None)
