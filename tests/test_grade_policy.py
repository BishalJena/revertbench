"""Adversarial tests for the supported submission subset; payloads are never executed."""

from pathlib import Path

import pytest

from revertbench.grade.policy import MAX_SOURCE_BYTES, POLICY_VERSION, check_submission


@pytest.fixture
def bad_tree(tmp_path: Path) -> Path:
    package = tmp_path / "toy"
    package.mkdir()
    (package / "__init__.py").write_text(
        "from .api import Counter, normalize, validate\nVERSION = '1.0'\n",
        encoding="utf-8",
    )
    (package / "api.py").write_text(
        "def normalize(value: str) -> str:\n"
        "    return value.strip()\n\n"
        "def validate(value: int) -> int:\n"
        "    if value < 0:\n"
        "        raise ValueError('negative')\n"
        "    return value\n\n"
        "class Counter:\n"
        "    def __init__(self, value: int) -> None:\n"
        "        self.value = value\n"
        "    def add(self, amount: int) -> int:\n"
        "        self.value += amount\n"
        "        return self.value\n",
        encoding="utf-8",
    )
    return tmp_path


@pytest.mark.parametrize(
    "source",
    [
        "from toy import normalize\ndef test_behavior():\n    assert normalize(' X ') == 'x'\n",
        "from toy import normalize as clean\ndef test_behavior():\n"
        "    assert clean(' X ') == 'x'\n",
        "import toy\ndef test_behavior():\n    assert toy.normalize(' X ') == 'x'\n",
        "import toy.api as api\ndef test_behavior():\n    assert api.normalize(' X ') == 'x'\n",
        "from toy import Counter\ndef test_behavior():\n"
        "    counter = Counter(1)\n    assert counter.add(2) == 3\n    assert counter.value == 3\n",
        "from toy import normalize\ndef expected(value):\n    return value + '!'\n"
        "def test_behavior():\n    values = [' X ', ' Y ']\n"
        "    for value in values:\n        assert expected(normalize(value)) != ''\n",
        "import pytest\nfrom toy import validate\ndef test_behavior():\n"
        "    with pytest.raises(ValueError):\n        validate(-1)\n",
        "import pytest as pt\nfrom toy import normalize\n"
        "@pt.mark.parametrize('value, expected', [(' X ', 'x'), (' Y ', 'y')])\n"
        "def test_behavior(value, expected):\n    assert normalize(value) == expected\n",
        "from pytest import raises\nfrom toy import validate\ndef test_behavior():\n"
        "    with raises(ValueError, match='negative'):\n        validate(-1)\n",
        "from toy import normalize\ndef test_behavior():\n"
        "    assert normalize(' X ').lower() == 'x'\n",
        "from toy import normalize\nimport math\ndef test_behavior():\n"
        "    assert len(normalize(' X ')) == math.floor(1.5)\n",
    ],
)
def test_accepts_supported_behavioral_tests(bad_tree: Path, source: str) -> None:
    result = check_submission(source, bad_tree)

    assert result.allowed, result
    assert result.reason is None
    assert POLICY_VERSION == "4"


@pytest.mark.parametrize(
    "source",
    [
        "from toy import added_in_fix\ndef test_cheat():\n    assert added_in_fix()\n",
        "import toy\ndef test_cheat():\n    toy.normalize(' X ')\n    assert toy.added_in_fix\n",
        "from toy import Counter\ndef test_cheat():\n"
        "    counter = Counter(1)\n    counter.add(1)\n    assert counter.new_method()\n",
        "import toy\ndef test_cheat():\n    toy.normalize(' X ')\n"
        "    assert hasattr(toy, 'added_in_fix')\n",
        "import toy\ndef test_cheat():\n    toy.normalize(' X ')\n"
        "    probe = getattr\n    assert probe(toy, 'added_in_fix', None)\n",
        "import toy\ndef test_cheat():\n    toy.normalize(' X ')\n"
        "    assert toy.VERSION == '2.0'\n",
        "from toy import normalize\ndef test_cheat():\n"
        "    normalize(' X ')\n    assert normalize.__code__.co_consts\n",
        "import toy\ndef test_cheat():\n    assert toy.__file__\n",
        "def test_cheat():\n    assert ().__class__.__base__.__subclasses__()\n",
        "from pathlib import Path\ndef test_cheat():\n    assert Path('toy/api.py').read_text()\n",
        "import inspect as probe\nfrom toy import normalize\ndef test_cheat():\n"
        "    normalize(' X ')\n    assert probe.getsource(normalize)\n",
        "import os\ndef test_cheat():\n    assert os.environ['VERSION']\n",
        "import sys\ndef test_cheat():\n    assert sys.version_info >= (3, 12)\n",
        "import importlib\ndef test_cheat():\n    assert importlib.import_module('toy')\n",
        "def test_cheat():\n    assert __import__('toy')\n",
        "def test_cheat():\n    assert eval('1 + 1') == 2\n",
        "def test_cheat():\n    execute = exec\n    execute('assert True')\n",
        "def test_cheat():\n    assert globals()\n",
        "def test_cheat():\n    assert vars()\n",
        "def test_cheat():\n    open('report.json', 'w').write('{}')\n",
        "import socket\ndef test_cheat():\n    socket.create_connection(('example.com', 443))\n",
        "import subprocess\ndef test_cheat():\n    subprocess.run(['true'])\n",
        "import pytest\ndef pytest_sessionfinish(session, exitstatus):\n    pass\n"
        "def test_cheat():\n    assert True\n",
        "import pytest\n@pytest.fixture\ndef injected():\n    return True\n"
        "def test_cheat(injected):\n    assert injected\n",
        "import pytest\n@pytest.mark.skip\ndef test_cheat():\n    assert False\n",
        "import pytest\n@pytest.mark.xfail\ndef test_cheat():\n    assert False\n",
        "import pytest\ndef test_cheat():\n    pytest.skip('bad snapshot')\n",
        "def test_cheat(tmp_path):\n    assert tmp_path\n",
        "from toy import normalize\nnormalize(' X ')\ndef test_cheat():\n    assert True\n",
        "from toy import normalize\nVALUE = normalize(' X ')\n"
        "def test_cheat():\n    assert VALUE\n",
        "if True:\n    def test_cheat():\n        assert True\n",
        "from toy import *\ndef test_cheat():\n    assert True\n",
        "def test_cheat():\n    import toy\n    assert toy.normalize(' X ')\n",
        "def test_cheat():\n    value = unknown_factory()\n    assert value\n",
        "from toy import normalize\ndef test_cheat():\n    assert normalize.missing\n",
        "from toy import normalize\ndef test_cheat():\n    assert normalize('a').format()\n",
        "import pytest\n@pytest.mark.parametrize('value', [object()])\n"
        "def test_cheat(value):\n    assert value\n",
        "import pytest\ndef test_cheat():\n"
        "    with pytest.raises(ValueError) as info:\n        assert info.traceback\n",
        "def test_cheat():\n    pass\n",
        "def test_cheat():\n    assert True\ndef test_cheat():\n    assert False\n",
        "class TestCheat:\n    def test_cheat(self):\n        assert True\n",
    ],
)
def test_rejects_unsupported_or_adversarial_tests(bad_tree: Path, source: str) -> None:
    result = check_submission(source, bad_tree)

    assert not result.allowed
    assert result.reason
    assert result.details


@pytest.mark.parametrize("source", ["", "def broken(:\n", "def test_nul():\n    assert '\x00'\n"])
def test_rejects_empty_or_malformed_sources(bad_tree: Path, source: str) -> None:
    assert not check_submission(source, bad_tree).allowed


def test_rejects_oversize_source_before_parsing(bad_tree: Path) -> None:
    result = check_submission("#" * (MAX_SOURCE_BYTES + 1), bad_tree)

    assert not result.allowed
    assert result.reason == "source_too_large"


def test_rejects_excessive_ast_complexity(bad_tree: Path) -> None:
    source = "def test_large():\n" + "    assert True\n" * 2000

    assert len(source.encode()) < MAX_SOURCE_BYTES
    result = check_submission(source, bad_tree)
    assert not result.allowed
    assert result.reason == "source_too_complex"


def test_does_not_execute_project_module_during_symbol_resolution(bad_tree: Path) -> None:
    marker = bad_tree / "executed"
    module = bad_tree / "toy" / "unsafe.py"
    module.write_text(
        f"open({str(marker)!r}, 'w').write('executed')\n"
        "def normalize(value: str) -> str:\n    return value\n",
        encoding="utf-8",
    )

    result = check_submission(
        "from toy.unsafe import normalize\ndef test_behavior():\n"
        "    assert normalize('x') == 'x'\n",
        bad_tree,
    )

    assert result.allowed
    assert not marker.exists()


def test_resolves_src_layout_and_transitive_reexports(tmp_path: Path) -> None:
    package = tmp_path / "src" / "toy"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("from .facade import normalize\n", encoding="utf-8")
    (package / "facade.py").write_text("from .api import normalize\n", encoding="utf-8")
    (package / "api.py").write_text(
        "def normalize(value: str) -> str:\n    return value\n", encoding="utf-8"
    )

    assert check_submission(
        "from toy import normalize\ndef test_behavior():\n    assert normalize('x') == 'x'\n",
        tmp_path,
    ).allowed


def test_cyclic_reexports_fail_closed(bad_tree: Path) -> None:
    (bad_tree / "toy" / "a.py").write_text("from .b import absent\n", encoding="utf-8")
    (bad_tree / "toy" / "b.py").write_text("from .a import absent\n", encoding="utf-8")

    result = check_submission(
        "from toy.a import absent\ndef test_cheat():\n    assert absent()\n", bad_tree
    )

    assert not result.allowed
    assert result.reason == "unresolved_symbol"


def test_source_symlinks_cannot_escape_bad_tree(bad_tree: Path, tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-external.py"
    outside.write_text("def hidden():\n    return True\n", encoding="utf-8")
    (bad_tree / "leaked.py").symlink_to(outside)

    result = check_submission(
        "from leaked import hidden\ndef test_cheat():\n    assert hidden()\n", bad_tree
    )

    assert not result.allowed


def test_missing_project_is_infrastructure_evidence(tmp_path: Path) -> None:
    result = check_submission("def test_behavior():\n    assert True\n", tmp_path / "missing")

    assert not result.allowed
    assert result.reason == "project_unavailable"


def test_rejects_invalid_unicode_without_raising(bad_tree: Path) -> None:
    result = check_submission("def test_invalid():\n    assert '\ud800'\n", bad_tree)

    assert not result.allowed
    assert result.reason == "invalid_source"


def test_deeply_nested_syntax_fails_closed(bad_tree: Path) -> None:
    source = "def test_nested():\n    assert " + "(" * 500 + "True" + ")" * 500

    assert not check_submission(source, bad_tree).allowed


def test_resolves_explicit_package_submodule_reexport(bad_tree: Path) -> None:
    (bad_tree / "toy" / "__init__.py").write_text("from . import api\n", encoding="utf-8")

    result = check_submission(
        "from toy import api\ndef test_behavior():\n    assert api.normalize(' X ') == 'x'\n",
        bad_tree,
    )

    assert result.allowed, result


def test_branch_rebinding_does_not_keep_an_obsolete_module_type(bad_tree: Path) -> None:
    result = check_submission(
        "import toy\ndef test_behavior():\n"
        "    if True:\n        toy = 1\n    else:\n        toy = []\n"
        "    assert toy.normalize(' X ') == 'x'\n",
        bad_tree,
    )

    assert not result.allowed
    assert result.reason == "unresolved_symbol"


def test_duplicate_parameter_names_are_rejected(bad_tree: Path) -> None:
    result = check_submission(
        "import pytest\n@pytest.mark.parametrize('value,value', [(1, 1)])\n"
        "def test_behavior(value):\n    assert value == 1\n",
        bad_tree,
    )

    assert not result.allowed
    assert result.reason == "unsupported_decorator"


@pytest.mark.parametrize("module", ["conftest", "tests.helpers", "toy.test_hooks"])
def test_project_test_support_imports_are_not_an_api_escape(bad_tree: Path, module: str) -> None:
    module_path = bad_tree.joinpath(*module.split(".")).with_suffix(".py")
    module_path.parent.mkdir(parents=True, exist_ok=True)
    module_path.write_text("def force_pass():\n    return True\n", encoding="utf-8")

    result = check_submission(
        f"from {module} import force_pass\ndef test_cheat():\n    assert force_pass()\n", bad_tree
    )

    assert not result.allowed
    assert result.reason == "unsupported_import"


def test_src_layout_has_the_same_precedence_as_the_runner(tmp_path: Path) -> None:
    (tmp_path / "toy.py").write_text("def shadow_only():\n    return True\n", encoding="utf-8")
    src = tmp_path / "src"
    src.mkdir()
    (src / "toy.py").write_text(
        "def normalize(value: str) -> str:\n    return value\n", encoding="utf-8"
    )

    accepted = check_submission(
        "from toy import normalize\ndef test_behavior():\n    assert normalize('x') == 'x'\n",
        tmp_path,
    )
    rejected = check_submission(
        "from toy import shadow_only\ndef test_cheat():\n    assert shadow_only()\n", tmp_path
    )

    assert accepted.allowed, accepted
    assert not rejected.allowed


def test_package_resolution_does_not_mix_root_and_src_packages(tmp_path: Path) -> None:
    for base in (tmp_path, tmp_path / "src"):
        package = base / "toy"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "toy" / "root_only.py").write_text(
        "def hidden():\n    return True\n", encoding="utf-8"
    )

    result = check_submission(
        "from toy.root_only import hidden\ndef test_cheat():\n    assert hidden()\n", tmp_path
    )

    assert not result.allowed


def test_loop_rebinding_invalidates_an_imported_callable(bad_tree: Path) -> None:
    result = check_submission(
        "from toy import normalize\ndef test_behavior():\n"
        "    for value in [1]:\n        normalize = value\n"
        "    assert normalize(' X ') == 'x'\n",
        bad_tree,
    )

    assert not result.allowed
    assert result.reason == "unsupported_call"


@pytest.mark.parametrize(
    "access",
    [
        "output()['version']",
        "output()['__version__']",
        "output().get('version')",
        "output().get('__version__')",
        "output().setdefault('VERSION', 'old')",
    ],
)
def test_project_output_metadata_keys_do_not_borrow_legitimate_coverage(
    bad_tree: Path, access: str
) -> None:
    (bad_tree / "toy" / "data.py").write_text(
        "def output() -> dict:\n    return {'version': 'old', 'value': 1}\n", encoding="utf-8"
    )
    result = check_submission(
        "from toy import normalize\nfrom toy.data import output\n"
        f"def test_cheat():\n    normalize(' X ')\n    assert {access} == 'new'\n",
        bad_tree,
    )

    assert not result.allowed
    assert result.reason == "introspection"


def test_literal_metadata_key_and_mapping_method_aliases_are_tracked(bad_tree: Path) -> None:
    (bad_tree / "toy" / "data.py").write_text(
        "def output() -> dict:\n    return {'version': 'old'}\n", encoding="utf-8"
    )
    result = check_submission(
        "from toy.data import output\ndef test_cheat():\n"
        "    details = output()\n    lookup = details.get\n"
        "    field = 'version'\n    assert lookup(field) == 'new'\n",
        bad_tree,
    )

    assert not result.allowed
    assert result.reason == "introspection"


def test_literal_data_keys_do_not_become_forbidden_metadata_everywhere(bad_tree: Path) -> None:
    result = check_submission(
        "def test_literal_data():\n    data = {'version': 1}\n"
        "    assert data['version'] == 1\n    assert data.get('version') == 1\n",
        bad_tree,
    )

    assert result.allowed, result


@pytest.mark.parametrize("constant", ["set()", "[set()]", "{'nested': set()}"])
def test_literal_constants_never_hide_calls_to_shadowed_builtins(
    bad_tree: Path, constant: str
) -> None:
    marker = bad_tree / "collection-effect"
    (bad_tree / "toy" / "effects.py").write_text(
        f"def touch():\n    open({str(marker)!r}, 'w').write('called')\n    return None\n",
        encoding="utf-8",
    )
    result = check_submission(
        f"from toy.effects import touch as set\nEMPTY = {constant}\n"
        "def test_behavior():\n    assert True\n",
        bad_tree,
    )

    assert not result.allowed
    assert result.reason == "top_level_effect"
    assert not marker.exists()


def test_parametrization_literals_never_hide_calls_to_shadowed_builtins(bad_tree: Path) -> None:
    marker = bad_tree / "parametrize-effect"
    (bad_tree / "toy" / "effects.py").write_text(
        f"def touch():\n    open({str(marker)!r}, 'w').write('called')\n    return None\n",
        encoding="utf-8",
    )
    result = check_submission(
        "import pytest\nfrom toy.effects import touch as set\n"
        "@pytest.mark.parametrize('value', [set()])\n"
        "def test_behavior(value):\n    assert value is None\n",
        bad_tree,
    )

    assert not result.allowed
    assert result.reason == "unsupported_decorator"
    assert not marker.exists()


@pytest.mark.parametrize(
    "access",
    [
        "dict(INFO)['version']",
        "dict(INFO).get('version')",
        "INFO['ver' + 'sion']",
        "INFO.get('ver' + 'sion')",
        "dict(INFO)['__' + 'version__']",
    ],
)
def test_simple_wrappers_and_literal_key_concatenation_keep_metadata_checks(
    bad_tree: Path, access: str
) -> None:
    (bad_tree / "toy" / "data.py").write_text(
        "INFO = {'version': 'old', '__version__': 'old'}\n", encoding="utf-8"
    )
    result = check_submission(
        "from toy import normalize\nfrom toy.data import INFO\n"
        f"def test_cheat():\n    normalize(' X ')\n    assert {access} == 'new'\n",
        bad_tree,
    )

    assert not result.allowed
    assert result.reason == "introspection"


@pytest.mark.parametrize("encoding", ["unicode_escape", "utf7", "latin1", "iso-8859-1"])
@pytest.mark.parametrize("prefix", ["", "#!/usr/bin/env python\n"])
def test_rejects_non_utf8_source_cookies_before_parsing(
    bad_tree: Path, encoding: str, prefix: str
) -> None:
    source = (
        f"{prefix}# coding: {encoding}\n"
        "from toy import normalize\ndef test_behavior():\n"
        "    assert normalize('x') == 'x'\n"
    )
    result = check_submission(source, bad_tree)
    assert not result.allowed
    assert result.reason == "unsupported_encoding"


@pytest.mark.parametrize("encoding", ["utf-8", "utf8", "utf_8", "UTF-8", "UTF8"])
def test_accepts_canonical_utf8_source_cookie_aliases(bad_tree: Path, encoding: str) -> None:
    source = (
        f"# coding: {encoding}\n"
        "from toy import normalize\ndef test_behavior():\n"
        "    assert normalize('x') == 'x'\n"
    )
    assert check_submission(source, bad_tree).allowed


def test_escaped_newline_comment_cannot_hide_runtime_imports(bad_tree: Path) -> None:
    import ast

    source = (
        "# coding: unicode_escape\n# "
        r"\u000aimport os\u000aos.getcwd()"
        "\nfrom toy import normalize\ndef test_behavior():\n"
        "    assert normalize('x') == 'x'\n"
    )
    # Demonstrate the mismatch on this fixed fixture without executing either AST
    # or decoding an untrusted submission with a user-selected codec.
    original = ast.parse(source)
    diagnostic = ast.parse(source.replace(r"\u000a", "\n"))
    assert not any(isinstance(node, ast.Import) for node in original.body)
    assert any(
        isinstance(node, ast.Import) and any(alias.name == "os" for alias in node.names)
        for node in diagnostic.body
    )
    result = check_submission(source, bad_tree)
    assert not result.allowed
    assert result.reason == "unsupported_encoding"


def test_utf8_bom_is_outside_supported_source_format(bad_tree: Path) -> None:
    source = (
        "\ufefffrom toy import normalize\ndef test_behavior():\n    assert normalize('x') == 'x'\n"
    )
    result = check_submission(source, bad_tree)
    assert not result.allowed
    assert result.reason == "unsupported_encoding"


@pytest.mark.parametrize("prefix", ["", "#!/usr/bin/env python\n"])
def test_unknown_encoding_cookie_never_reaches_registered_codec_search(
    bad_tree: Path, prefix: str
) -> None:
    import codecs

    searched: list[str] = []

    def record_search(name: str) -> codecs.CodecInfo | None:
        searched.append(name)
        return None

    source = (
        f"{prefix}# coding: revertbench_untrusted_source_codec\n"
        "from toy import normalize\ndef test_behavior():\n"
        "    assert normalize('x') == 'x'\n"
    )
    codecs.register(record_search)
    try:
        result = check_submission(source, bad_tree)
    finally:
        codecs.unregister(record_search)
    assert not result.allowed
    assert result.reason == "unsupported_encoding"
    assert searched == []


@pytest.mark.parametrize(
    "source",
    [
        "import unittest\nfrom toy import normalize\ndef test_behavior():\n"
        "    unittest.TestCase().assertEqual(normalize('x'), 'x')\n",
        "from unittest import TestCase as _case\nfrom toy import normalize\n"
        "def test_behavior():\n    self = _case()\n"
        "    self.assertEqual(normalize(list(iter(['x']))[0]), 'x')\n",
        "import unittest as ut\nfrom toy import normalize\ndef test_behavior():\n"
        "    constructor = ut.TestCase\n    case = constructor()\n"
        "    equal = case.assertEqual\n    iterate = iter\n"
        "    equal(normalize(list(iterate(['x']))[0]), 'x')\n",
        "from unittest import TestCase\ndef test_observation():\n"
        "    TestCase().assertEqual(1, 1)\n",
    ],
)
def test_accepts_only_supported_unittest_observation_forms(bad_tree: Path, source: str) -> None:
    assert check_submission(source, bad_tree).allowed


@pytest.mark.parametrize(
    "expression",
    [
        "unittest.TestCase('test_behavior')",
        "unittest.TestCase(methodName='runTest')",
        "unittest.TestCase.assertEqual(1, 1)",
        "unittest.TestCase().assertEqual()",
        "unittest.TestCase().assertEqual(1)",
        "unittest.TestCase().assertEqual(1, 1, 'message')",
        "unittest.TestCase().assertEqual(first=1, second=1)",
        "unittest.TestCase().assertEqual(1, 1, msg='message')",
        "unittest.TestCase().assertTrue(True)",
        "unittest.TestCase().assertRaises(ValueError)",
        "unittest.TestCase().run()",
        "unittest.TestCase().debug()",
        "unittest.TestCase().fail()",
        "unittest.TestCase().skipTest('skip')",
        "unittest.TestCase().addCleanup(normalize, 'x')",
        "unittest.TestCase().subTest()",
        "unittest.TestLoader()",
        "unittest.mock",
        "iter()",
        "iter(normalize, 'sentinel')",
        "iter(object=['x'])",
    ],
)
def test_unittest_and_iter_arities_and_other_apis_are_rejected(
    bad_tree: Path, expression: str
) -> None:
    source = (
        "import unittest\nfrom toy import normalize\ndef test_behavior():\n"
        f"    normalize('x')\n    {expression}\n"
    )
    assert not check_submission(source, bad_tree).allowed


@pytest.mark.parametrize(
    "body",
    [
        "equal = unittest.TestCase().assertEqual\n    equal(1, 1, 'message')",
        "iterate = iter\n    iterate(normalize, 'sentinel')",
        "case = unittest.TestCase()\n    case = 1\n    case.assertEqual(1, 1)",
        "case = unittest.TestCase()\n    if True:\n        case = 1\n    case.assertEqual(1, 1)",
        "case = unittest.TestCase()\n    case.assertEqual = normalize",
        "unittest = 1\n    unittest.TestCase()",
    ],
)
def test_unittest_and_iter_aliases_do_not_bypass_restrictions(bad_tree: Path, body: str) -> None:
    source = (
        "import unittest\nfrom toy import normalize\ndef test_behavior():\n"
        f"    normalize('x')\n    {body}\n"
    )
    assert not check_submission(source, bad_tree).allowed


@pytest.mark.parametrize(
    "path", ["unittest.py", "unittest/__init__.py", "src/unittest.py", "src/unittest/__init__.py"]
)
def test_unittest_source_shadowing_is_rejected_without_execution(bad_tree: Path, path: str) -> None:
    target = bad_tree / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("raise RuntimeError('project shadow must never execute')\n")
    source = "import unittest\ndef test_behavior():\n    unittest.TestCase().assertEqual(1, 1)\n"
    result = check_submission(source, bad_tree)
    assert not result.allowed


def test_iter_preserves_project_metadata_origin(bad_tree: Path) -> None:
    (bad_tree / "toy" / "data.py").write_text("INFO = {'version': 'old'}\n")
    source = (
        "from toy.data import INFO\nfrom toy import normalize\ndef test_behavior():\n"
        "    normalize('x')\n    assert list(iter(INFO))['version'] == 'new'\n"
    )
    result = check_submission(source, bad_tree)
    assert not result.allowed
    assert result.reason == "introspection"


def _export_package(bad_tree: Path, consumer: str, providers: dict[str, str]) -> None:
    package = bad_tree / "exports"
    package.mkdir()
    (package / "__init__.py").write_text(consumer)
    for name, source in providers.items():
        (package / f"{name}.py").write_text(source)


@pytest.mark.parametrize("declaration", ["['normalize']", "('normalize',)"])
def test_literal_project_star_reexports_resolve_only_declared_names(
    bad_tree: Path, declaration: str
) -> None:
    _export_package(
        bad_tree,
        "from .api import *\n",
        {
            "api": f"__all__ = {declaration}\ndef normalize(value):\n    return value\n"
            "def hidden(value):\n    return value\n",
        },
    )
    assert check_submission(
        "import exports\ndef test_behavior():\n    assert exports.normalize('x') == 'x'\n", bad_tree
    ).allowed
    assert not check_submission(
        "import exports\ndef test_behavior():\n    assert exports.hidden('x') == 'x'\n", bad_tree
    ).allowed
    assert not check_submission(
        "from exports import *\ndef test_behavior():\n    assert True\n", bad_tree
    ).allowed


@pytest.mark.parametrize(
    "declaration",
    [
        "",
        "__all__ = names\n",
        "__all__ = list(['normalize'])\n",
        "__all__ = ['normalize'] + []\n",
        "__all__ = ['normalize', 'normalize']\n",
        "__all__ = ['_private', 'normalize']\n",
        "__all__ = ['bad-name', 'normalize']\n",
        "__all__ = [1, 'normalize']\n",
        "__all__ = {'normalize'}\n",
        "__all__ = ['normalize']\n__all__.append('hidden')\n",
        "__all__ = ['normalize']\n__all__ += ['hidden']\n",
        "__all__ = ['normalize']\n__all__[0] = 'hidden'\n",
        "__all__ = ['normalize']\nalias = __all__\n",
        "__all__ = ['normalize']\n__all__ = ['hidden']\n",
        "__all__ = ['normalize']\ndel __all__[0]\n",
    ],
)
def test_project_exports_fail_closed_for_dynamic_or_mutated_declarations(
    bad_tree: Path, declaration: str
) -> None:
    _export_package(
        bad_tree,
        "from .api import *\n",
        {
            "api": declaration + "def normalize(value):\n    return value\n",
        },
    )
    assert not check_submission(
        "import exports\ndef test_behavior():\n    assert exports.normalize('x') == 'x'\n", bad_tree
    ).allowed


@pytest.mark.parametrize(
    "explicit", ["def normalize(value):\n    return value\n", "from .other import normalize\n"]
)
def test_project_star_reexport_collisions_are_rejected(bad_tree: Path, explicit: str) -> None:
    definition = "__all__ = ['normalize']\ndef normalize(value):\n    return value\n"
    _export_package(
        bad_tree, "from .api import *\n" + explicit, {"api": definition, "other": definition}
    )
    assert not check_submission(
        "import exports\ndef test_behavior():\n    assert exports.normalize('x') == 'x'\n", bad_tree
    ).allowed


def test_ambiguous_project_star_providers_are_rejected(bad_tree: Path) -> None:
    definition = "__all__ = ['normalize']\ndef normalize(value):\n    return value\n"
    _export_package(
        bad_tree,
        "from .api import *\nfrom .other import *\n",
        {"api": definition, "other": definition},
    )
    assert not check_submission(
        "import exports\ndef test_behavior():\n    assert exports.normalize('x') == 'x'\n", bad_tree
    ).allowed


def test_project_star_reexport_cycles_are_rejected(bad_tree: Path) -> None:
    _export_package(
        bad_tree,
        "from .api import *\n",
        {
            "api": "__all__ = ['normalize']\nfrom .other import *\n",
            "other": "__all__ = ['normalize']\nfrom .api import *\n",
        },
    )
    assert not check_submission(
        "import exports\ndef test_behavior():\n    assert exports.normalize('x') == 'x'\n", bad_tree
    ).allowed


def test_project_export_name_count_is_bounded(bad_tree: Path) -> None:
    names = ["normalize", *(f"name{i}" for i in range(1024))]
    _export_package(
        bad_tree,
        "from .api import *\n",
        {
            "api": f"__all__ = {names!r}\ndef normalize(value):\n    return value\n",
        },
    )
    assert not check_submission(
        "import exports\ndef test_behavior():\n    assert exports.normalize('x') == 'x'\n", bad_tree
    ).allowed


def test_project_unknown_codec_cookie_never_invokes_host_search(bad_tree: Path) -> None:
    import codecs

    searches: list[str] = []

    def record(name: str) -> codecs.CodecInfo | None:
        searches.append(name)
        return None

    (bad_tree / "toy" / "encoded.py").write_text(
        "# coding: policy_untrusted_project_codec\ndef normalize(value):\n    return value\n"
    )
    codecs.register(record)
    try:
        result = check_submission(
            "from toy.encoded import normalize\ndef test_behavior():\n"
            "    assert normalize('x') == 'x'\n",
            bad_tree,
        )
    finally:
        codecs.unregister(record)
    assert not result.allowed
    assert searches == []


@pytest.mark.parametrize(
    "expression", ["iter(['x'])", "unittest.TestCase()", "unittest.TestCase().assertEqual"]
)
def test_iterator_and_testcase_construction_are_not_test_observations(
    bad_tree: Path, expression: str
) -> None:
    result = check_submission(f"import unittest\ndef test_empty():\n    {expression}\n", bad_tree)
    assert not result.allowed
    assert result.reason == "empty_test"


def test_project_star_provider_without_known_exports_cannot_be_ignored(bad_tree: Path) -> None:
    _export_package(
        bad_tree,
        "from .api import *\nfrom .dynamic import *\n",
        {
            "api": "__all__ = ['normalize']\ndef normalize(value):\n    return value\n",
            "dynamic": "def unrelated():\n    return 1\n",
        },
    )
    assert not check_submission(
        "import exports\ndef test_behavior():\n    assert exports.normalize('x') == 'x'\n", bad_tree
    ).allowed


def test_project_star_provider_module_budget_is_bounded(bad_tree: Path) -> None:
    providers = {
        f"level{i}": "__all__ = ['normalize']\n"
        + (
            f"from .level{i + 1} import *\n"
            if i < 64
            else "def normalize(value):\n    return value\n"
        )
        for i in range(65)
    }
    _export_package(bad_tree, "from .level0 import *\n", providers)
    result = check_submission(
        "import exports\ndef test_behavior():\n    assert exports.normalize('x') == 'x'\n", bad_tree
    )
    assert not result.allowed
    assert result.reason == "unresolved_symbol"


def test_conditional_project_star_provider_is_rejected(bad_tree: Path) -> None:
    _export_package(
        bad_tree,
        "if True:\n    from .api import *\n",
        {
            "api": "__all__ = ['normalize']\ndef normalize(value):\n    return value\n",
        },
    )
    assert not check_submission(
        "import exports\ndef test_behavior():\n    assert exports.normalize('x') == 'x'\n", bad_tree
    ).allowed


@pytest.mark.parametrize("encoding", ["utf-8", "utf8", "utf_8", "UTF-8"])
def test_project_source_accepts_only_recognized_utf8_cookies(bad_tree: Path, encoding: str) -> None:
    (bad_tree / "toy" / "encoded.py").write_text(
        f"# coding: {encoding}\ndef normalize(value):\n    return value\n"
    )
    assert check_submission(
        "from toy.encoded import normalize\ndef test_behavior():\n"
        "    assert normalize('x') == 'x'\n",
        bad_tree,
    ).allowed


@pytest.mark.parametrize(
    "body",
    [
        "assert [list(c) for c in normalize('x')] == []",
        "assert [value for value in [1, 2]].count(1) == 1",
        "assert [normalize for normalize in normalize('x')] == ['x']",
        "actual = [normalize for normalize in []]\n    assert normalize('x') == 'x'",
        "actual = [list for list in []]\n    assert list([1]) == [1]",
        "actual = [toy for toy in []]\n    assert toy.normalize('x') == 'x'",
    ],
)
def test_one_list_comprehension_has_its_own_unknown_target_scope(bad_tree: Path, body: str) -> None:
    source = f"import toy\nfrom toy import normalize\ndef test_behavior():\n    {body}\n"
    result = check_submission(source, bad_tree)
    assert result.allowed, result


def test_ichunked_original_full_method_observations_are_admitted(bad_tree: Path) -> None:
    (bad_tree / "toy" / "chunks.py").write_text("def ichunked(iterable, size):\n    return []\n")
    source = (
        "from unittest import TestCase\nfrom toy.chunks import ichunked\n"
        "def test_zero_nonempty():\n"
        "    self = TestCase()\n"
        "    iterable = [1, 2, 3]\n"
        "    actual = [list(c) for c in ichunked(iterable, 0)]\n"
        "    self.assertEqual(actual, [])\n"
        "    source = iter([1, 2, 3])\n"
        "    self.assertEqual([list(c) for c in ichunked(source, 0)], [])\n"
        "    self.assertEqual(list(source), [1, 2, 3])\n"
    )
    result = check_submission(source, bad_tree)
    assert result.allowed, result


@pytest.mark.parametrize(
    "expression,reason",
    [
        ("[added_in_fix() for value in []]", "unresolved_symbol"),
        ("[value for value in toy.added_in_fix()]", "unresolved_symbol"),
        ("[value() for value in []]", "unsupported_call"),
        ("[list([]) for list in []]", "unsupported_call"),
        ("[toy.normalize('x') for toy in []]", "unresolved_symbol"),
        ("[value.lower() for value in normalize('x')]", "unresolved_symbol"),
        ("[value.__class__ for value in []]", "introspection"),
        ("[value for __version__ in []]", "introspection"),
    ],
)
def test_empty_comprehensions_still_check_every_expression_and_binding(
    bad_tree: Path, expression: str, reason: str
) -> None:
    source = (
        "import toy\nfrom toy import normalize\ndef test_cheat():\n"
        f"    normalize('x')\n    assert {expression} == []\n"
    )
    result = check_submission(source, bad_tree)
    assert not result.allowed and result.reason == reason, result


def test_comprehension_target_does_not_leak_to_later_statements(bad_tree: Path) -> None:
    result = check_submission(
        "def test_cheat():\n    actual = [value for value in []]\n    assert value == 1\n",
        bad_tree,
    )
    assert not result.allowed and result.reason == "unresolved_symbol", result


@pytest.mark.parametrize(
    "access",
    [
        "[row['version'] for row in output()]",
        "[row for row in output()]['version']",
        "[output() for row in []]['version']",
        "[list(row) for row in output()]['ver' + 'sion']",
        "dict([row for row in output()]).get('version')",
    ],
)
def test_comprehensions_retain_project_origin_for_elements_and_materialized_lists(
    bad_tree: Path, access: str
) -> None:
    (bad_tree / "toy" / "data.py").write_text("def output() -> list:\n    return []\n")
    result = check_submission(
        "from toy import normalize\nfrom toy.data import output\ndef test_cheat():\n"
        f"    normalize('x')\n    assert {access} == 'new'\n",
        bad_tree,
    )
    assert not result.allowed and result.reason == "introspection", result


@pytest.mark.parametrize(
    "expression",
    [
        "[x for x in [1] if x]",
        "[x for x in [] for y in []]",
        "[x for x, y in []]",
        "[x for [x] in []]",
        "[x async for x in []]",
        "[[y for y in []] for x in []]",
        "[x for x in [y for y in []]]",
        "[len([y for y in []]) for x in []]",
        "[(value := x) for x in []]",
        "(x for x in [])",
        "{x for x in []}",
        "{x: x for x in []}",
        "[lambda: x for x in []]",
    ],
)
def test_list_comprehension_profile_keeps_other_forms_unsupported(
    bad_tree: Path, expression: str
) -> None:
    result = check_submission(f"def test_cheat():\n    assert {expression} == []\n", bad_tree)
    assert not result.allowed and result.reason == "unsupported_syntax", result


@pytest.mark.parametrize(
    "source,reason",
    [
        (
            "VALUES = [x for x in []]\ndef test_behavior():\n    assert VALUES == []\n",
            "top_level_effect",
        ),
        (
            "import pytest\n@pytest.mark.parametrize('value', [x for x in []])\n"
            "def test_behavior(value):\n    assert value\n",
            "unsupported_decorator",
        ),
    ],
)
def test_comprehensions_do_not_expand_literal_only_collection_contexts(
    bad_tree: Path, source: str, reason: str
) -> None:
    result = check_submission(source, bad_tree)
    assert not result.allowed and result.reason == reason, result


def test_project_export_lists_stay_call_free_literals_not_comprehensions(bad_tree: Path) -> None:
    _export_package(
        bad_tree,
        "from .api import *\n",
        {"api": "__all__ = [name for name in ['normalize']]\ndef normalize(x):\n    return x\n"},
    )
    result = check_submission(
        "from exports import normalize\ndef test_behavior():\n    assert normalize('x') == 'x'\n",
        bad_tree,
    )
    assert not result.allowed and result.reason == "unresolved_symbol", result


@pytest.mark.parametrize(
    "body",
    [
        "with case.subTest(value=1):\n        case.assertEqual(normalize('x'), 'x')",
        "with case.subTest(left=1, right=2):\n        assert normalize('x') == 'x'",
    ],
)
def test_direct_subtest_context_preserves_existing_observations(bad_tree: Path, body: str) -> None:
    result = check_submission(
        "from unittest import TestCase\nfrom toy import normalize\n"
        f"def test_behavior():\n    case = TestCase()\n    {body}\n",
        bad_tree,
    )
    assert result.allowed, result


@pytest.mark.parametrize(
    "body",
    [
        "with case.subTest():\n        assert True",
        "with case.subTest('message', value=1):\n        assert True",
        "with case.subTest(msg='message'): \n        assert True",
        "with case.subTest(a=1, b=2, c=3):\n        assert True",
        "with case.subTest(**{'value': 1}):\n        assert True",
        "with case.subTest(value=1) as captured:\n        assert True",
        "context = case.subTest\n    with context(value=1):\n        assert True",
        "case.subTest(value=1)\n    assert True",
        "with case.subTest(value=1), case.subTest(other=2):\n        assert True",
        "with case.subTest(value=1):\n        pass",
        "with case.subTest(value=toy.added_in_fix()):\n        assert True",
        "with case.subTest(value=toy.VERSION):\n        assert True",
        "with case.subTest(__version__=1):\n        assert True",
    ],
)
def test_subtest_does_not_admit_arbitrary_context_or_observation_capabilities(
    bad_tree: Path, body: str
) -> None:
    source = (
        "import toy\nfrom unittest import TestCase\ndef test_cheat():\n"
        f"    case = TestCase()\n    {body}\n"
    )
    assert not check_submission(source, bad_tree).allowed


@pytest.mark.parametrize(
    "declaration,expression",
    [
        ("args = (0, 3, 1)", "range(*args)"),
        ("args = [0, 3]", "range(*args)"),
        ("args = ('x',)", "normalize(*args)"),
        ("args = (1,)", "toy.Counter(*args)"),
        ("cases = [(3,), (0, 3), (0, 3, 1)]\n    for args in cases", "range(*args)"),
        ("cases = [((3,), 1), ((0, 3, 1), 2)]\n    for args, expected in cases", "range(*args)"),
    ],
)
def test_source_built_bounded_packs_support_single_named_star_calls(
    bad_tree: Path, declaration: str, expression: str
) -> None:
    body = (
        declaration + f":\n        assert list({expression})"
        if "\n    for" in declaration
        else declaration + f"\n    assert {expression} is not None"
    )
    source = f"import toy\nfrom toy import normalize\ndef test_behavior():\n    {body}\n"
    result = check_submission(source, bad_tree)
    assert result.allowed, result


@pytest.mark.parametrize(
    "body",
    [
        "args = ()\n    assert range(*args)",
        "args = (0, 1, 2, 3)\n    assert range(*args)",
        "args = iter([0, 3])\n    assert range(*args)",
        "args = normalize('x')\n    assert range(*args)",
        "args = (0, 3)\n    args = normalize('x')\n    assert range(*args)",
        "args = (0, 3)\n    assert range(*args, 1)",
        "args = (0, 3)\n    assert range(*args, *args)",
        "args = (0, 3)\n    assert range(*args, stop=4)",
        "assert range(*(0, 3))",
        "args = (0, 3)\n    assert iter(*args)",
        "args = (0, 3)\n    assert TestCase(*args)",
        "args = (0, 3)\n    TestCase().assertEqual(*args)",
        "args = ('version',)\n    assert {}.get(*args)",
        "args = ('x',)\n    assert helper(*args)",
        "cases = [(3,), (0, 1, 2, 3)]\n    for args in cases:\n        assert range(*args)",
        "cases = [(3,), normalize('x')]\n    for args in cases:\n        assert range(*args)",
        "args = [0, 3]\n    alias = args\n    alias.append(4)\n    assert range(*args)",
        "args = [0, 3]\n    normalize(args)\n    assert range(*args)",
        "args = [0, 3]\n    helper()\n    assert range(*args)",
        "for callback in [normalize]:\n        assert callback('x')",
        "for value in ['x']:\n        assert value.lower()",
        "args = (toy.added_in_fix(),)\n    assert range(*args)",
        "cases = [(3,), (toy.VERSION,)]\n    for args in cases:\n        assert range(*args)",
    ],
)
def test_star_calls_need_complete_current_pack_proof_and_safe_targets(
    bad_tree: Path, body: str
) -> None:
    source = (
        "import toy\nfrom toy import normalize\nfrom unittest import TestCase\n"
        "def helper():\n    return 1\n"
        f"def test_cheat():\n    {body}\n"
    )
    assert not check_submission(source, bad_tree).allowed


def test_pack_shape_proofs_are_bounded_even_with_repeated_alias_doubling(bad_tree: Path) -> None:
    body = "    packed = (1,)\n" + "    packed = (packed, packed)\n" * 15
    result = check_submission(
        "def test_cheat():\n" + body + "    assert range(*packed)\n", bad_tree
    )
    assert not result.allowed


def test_original_numeric_range_six_case_body_is_admitted_without_rewrite(bad_tree: Path) -> None:
    with (bad_tree / "toy" / "__init__.py").open("a") as output:
        output.write("\ndef numeric_range(*args):\n    return range(*args)\n")
    source = (
        "from unittest import TestCase\nimport toy as mi\ndef test_eq_above_maxsize():\n"
        "    self = TestCase()\n    stop = 10**30\n"
        "    cases = [\n"
        "        ((0, stop, 1), (0, stop, 1), True),\n"
        "        ((0, stop, 2), (0, stop - 1, 2), True),\n"
        "        ((stop, 0, -2), (stop, 1, -2), True),\n"
        "        ((0, stop, 1), (0, stop + 1, 1), False),\n"
        "        ((0, stop, 1), (1, stop + 1, 1), False),\n"
        "        ((0, stop, 1), (0, stop * 2, 2), False),\n"
        "    ]\n"
        "    for args1, args2, expected in cases:\n"
        "        with self.subTest(args1=args1, args2=args2):\n"
        "            range1 = mi.numeric_range(*args1)\n"
        "            range2 = mi.numeric_range(*args2)\n"
        "            self.assertEqual(range(*args1) == range(*args2), expected)\n"
        "            self.assertEqual(range1 == range2, expected)\n"
    )
    result = check_submission(source, bad_tree)
    assert result.allowed, result


@pytest.mark.parametrize(
    "binding,call",
    [
        ("import itertools as it", "it.permutations"),
        ("from itertools import permutations as permute", "permute"),
    ],
)
def test_only_one_argument_permutations_is_admitted(
    bad_tree: Path, binding: str, call: str
) -> None:
    result = check_submission(
        f"{binding}\ndef test_behavior():\n    assert set({call}([1, 2])) == {{(1, 2), (2, 1)}}\n",
        bad_tree,
    )
    assert result.allowed, result


@pytest.mark.parametrize(
    "call",
    ["permutations()", "permutations([1], 1)", "permutations(iterable=[1])", "permutations(*args)"],
)
def test_permutations_keeps_exact_call_contract(bad_tree: Path, call: str) -> None:
    assert not check_submission(
        "from itertools import permutations\ndef test_cheat():\n"
        f"    args = ([1],)\n    assert {call}\n",
        bad_tree,
    ).allowed


@pytest.mark.parametrize("path", ["itertools.py", "src/itertools/__init__.py"])
def test_itertools_project_shadow_is_never_imported(bad_tree: Path, path: str) -> None:
    target = bad_tree / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("raise RuntimeError('must not run')\n")
    assert not check_submission(
        "from itertools import permutations\ndef test_cheat():\n"
        "    assert list(permutations([1]))\n",
        bad_tree,
    ).allowed


def test_permutations_materialization_retains_project_metadata_origin(bad_tree: Path) -> None:
    (bad_tree / "toy" / "data.py").write_text("INFO = [1, 2]\n")
    result = check_submission(
        "from itertools import permutations\nfrom toy.data import INFO\ndef test_cheat():\n"
        "    assert list(permutations(INFO))['version'] == 'new'\n",
        bad_tree,
    )
    assert not result.allowed and result.reason == "introspection", result


@pytest.mark.parametrize(
    "body",
    [
        "args = [0, 3]\n    mutate = args.append\n    mutate(4)\n    assert range(*args)",
        "cases = [(0, 3)]\n    alias = cases\n    alias.clear()\n"
        "    for args in cases:\n        assert range(*args)",
        "for range in [(0, 3)]:\n        assert range(*range)",
        "args = (((((1,),),),),)\n    assert range(*args)",
        "args = ([" + ",".join("1" for _ in range(127)) + "],)\n    assert range(*args)",
        "cases = ["
        + ",".join("(3,)" for _ in range(17))
        + "]\n    for args in cases:\n        assert range(*args)",
        "for value in []:\n        with TestCase().subTest(value=toy.added_in_fix()):\n"
        "            assert True",
        "with toy.Counter(1).subTest(value=1):\n        assert True",
    ],
)
def test_aliases_shape_budgets_and_empty_contexts_cannot_weaken_admission(
    bad_tree: Path, body: str
) -> None:
    source = f"import toy\nfrom unittest import TestCase\ndef test_cheat():\n    {body}\n"
    assert not check_submission(source, bad_tree).allowed


def test_fresh_pack_can_restore_proof_after_an_alias_mutation(bad_tree: Path) -> None:
    source = (
        "def test_behavior():\n    args = [0, 3]\n    alias = args\n    alias.clear()\n"
        "    args = (0, 3)\n    selected = range\n    assert list(selected(*args)) == [0, 1, 2]\n"
    )
    result = check_submission(source, bad_tree)
    assert result.allowed, result


@pytest.mark.parametrize("name", ["cycle", "product", "islice", "tee"])
def test_itertools_does_not_expose_other_apis(bad_tree: Path, name: str) -> None:
    assert not check_submission(
        f"from itertools import {name}\ndef test_cheat():\n    assert {name}([1])\n", bad_tree
    ).allowed


@pytest.mark.parametrize(
    "body",
    [
        "args = [0, 3]\n    cases = [args, (helper(),)]\n"
        "    for packed in cases:\n        assert range(*packed)",
        "args = [0, 3]\n    alias = args\n    alias += [1, 2]\n    assert range(*args)",
        "cases = [(0, 3)]\n    for args in cases:\n        assert range(*args)\n"
        "        cases.append((0, 1, 2, 3))",
    ],
)
def test_pack_proof_cannot_survive_effects_within_a_literal_or_between_loop_iterations(
    bad_tree: Path, body: str
) -> None:
    source = f"def helper():\n    return 1\ndef test_cheat():\n    {body}\n"
    assert not check_submission(source, bad_tree).allowed


def test_passing_a_local_mutating_callback_discards_pack_proof(bad_tree: Path) -> None:
    source = (
        "from toy import normalize\ndef test_cheat():\n    args = [0, 3]\n"
        "    callback = args.append\n    sorted(normalize('x'), key=callback)\n"
        "    assert range(*args)\n"
    )
    assert not check_submission(source, bad_tree).allowed


def test_itertools_submodule_spelling_cannot_bypass_stdlib_shadow_guard(bad_tree: Path) -> None:
    package = bad_tree / "itertools"
    package.mkdir()
    (package / "__init__.py").write_text("")
    (package / "fake.py").write_text("def run():\n    return 1\n")
    source = "import itertools.fake as fake\ndef test_cheat():\n    assert fake.run() == 1\n"
    assert not check_submission(source, bad_tree).allowed


def test_comprehension_effects_cannot_leave_outer_pack_proofs_alive(bad_tree: Path) -> None:
    source = (
        "def helper(values):\n    return 1\n"
        "def test_cheat():\n    args = [0, 3]\n"
        "    observed = [helper(args) for value in [1]]\n"
        "    assert range(*args)\n"
    )
    assert not check_submission(source, bad_tree).allowed


def test_module_pack_certificate_cannot_reset_between_test_functions(bad_tree: Path) -> None:
    source = (
        "ARGS = [0, 3]\ndef test_mutate():\n    ARGS.extend([1, 2])\n    assert True\n"
        "def test_star():\n    assert range(*ARGS)\n"
    )
    assert not check_submission(source, bad_tree).allowed


@pytest.mark.parametrize("literal", ["(0, 3)", "[0, 3]"])
@pytest.mark.parametrize("binding", ["ARGS", "local"])
def test_module_literals_and_local_aliases_never_gain_pack_certificates(
    bad_tree: Path, literal: str, binding: str
) -> None:
    source = f"ARGS = {literal}\ndef test_star():\n    local = ARGS\n    assert range(*{binding})\n"
    assert not check_submission(source, bad_tree).allowed
