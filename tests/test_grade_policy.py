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
    assert POLICY_VERSION == "1"


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
