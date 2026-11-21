import pytest

from revertbench.mine.reference import ReferenceError, changed_test_functions, extract_reference


def test_reference_preserves_asserts_and_only_the_needed_dependency_closure() -> None:
    source = (
        "import os\nimport pkg as target\nimport pytest\n"
        "EXPECTED = 'value'\nUNUSED = os.environ\n"
        "def expected():\n    return EXPECTED\n"
        "def unused():\n    return os.environ\n"
        "def test_regression():\n    assert target.run() == expected()\n"
    )
    extracted = extract_reference(source, "test_regression")
    assert "import pkg as target" in extracted
    assert "EXPECTED = 'value'" in extracted
    assert "def expected" in extracted
    assert "assert target.run() == expected()" in extracted
    assert "import os" not in extracted
    assert "import pytest" not in extracted
    assert "UNUSED" not in extracted
    assert "def unused" not in extracted


def test_literal_parametrization_keeps_pytest_import_and_original_cases() -> None:
    source = (
        "import pytest\nimport pkg\n"
        "@pytest.mark.parametrize('value', ['a','b'])\n"
        "def test_regression(value):\n    assert pkg.run(value) == value\n"
    )
    extracted = extract_reference(source, "test_regression")
    assert "import pytest" in extracted
    assert "@pytest.mark.parametrize('value', ['a','b'])" in extracted


@pytest.mark.parametrize(
    "source,name",
    [
        (
            "class TestAPI:\n    def test_regression(self):\n        assert True\n",
            "test_regression",
        ),
        ("def test_regression(tmp_path):\n    assert tmp_path\n", "test_regression"),
        (
            "import pytest\n@pytest.fixture\ndef fixture():\n    return 1\n"
            "def test_regression(fixture):\n    assert fixture == 1\n",
            "test_regression",
        ),
        ("VALUE = object()\ndef test_regression():\n    assert VALUE\n", "test_regression"),
        ("def test_other():\n    assert True\n", "test_regression"),
    ],
)
def test_reference_rejects_unsupported_definitions_without_semantic_rewrite(
    source: str, name: str
) -> None:
    with pytest.raises(ReferenceError):
        extract_reference(source, name)


def test_only_added_or_modified_top_level_test_functions_are_candidates() -> None:
    before = "def test_stable():\n    assert True\ndef test_change():\n    assert 1 == 1\n"
    after = "def test_stable():\n    assert True\ndef test_change():\n    assert 2 == 2\n"
    assert changed_test_functions(before, after) == ("test_change",)


def test_reference_length_limit_includes_dependency_closure() -> None:
    source = "import pkg\ndef test_long():\n" + "    assert pkg.run()\n" * 81
    with pytest.raises(ReferenceError, match="line"):
        extract_reference(source, "test_long")


@pytest.mark.parametrize(
    "prefix",
    [
        "import pytest\npytestmark = pytest.mark.xfail\n",
        "import pytest\n@pytest.fixture(autouse=True)\ndef context():\n    pass\n",
        "def setup_function():\n    pass\n",
    ],
)
def test_implicit_module_context_is_not_silently_dropped(prefix: str) -> None:
    with pytest.raises(ReferenceError, match="implicit"):
        extract_reference(prefix + "def test_regression():\n    assert True\n", "test_regression")


def test_shadowed_builtin_dependency_keeps_its_original_import() -> None:
    source = "from pkg import run as str\ndef test_regression():\n    assert str('a') == 'b'\n"
    assert "from pkg import run as str" in extract_reference(source, "test_regression")


def test_unsupported_encoding_cookie_is_rejected_without_codec_lookup() -> None:
    source = (
        "# coding: unicode_escape\n# opaque\\nimport os\ndef test_regression():\n    assert True\n"
    )
    with pytest.raises(ReferenceError, match="encoding"):
        extract_reference(source, "test_regression")
