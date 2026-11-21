"""The recognized root test suite is source support, never a project API."""

from pathlib import Path

import pytest

from revertbench.grade.policy import check_submission


@pytest.fixture
def source_tree(tmp_path: Path) -> Path:
    (tmp_path / "toy.py").write_text("def normalize(value):\n    return value.strip()\n")
    (tmp_path / "test.py").write_text("__all__ = ['observe']\ndef observe():\n    return 1\n")
    (tmp_path / "bridge.py").write_text("from test import observe as exported\n")
    (tmp_path / "public.py").write_text("from bridge import exported as observe\n")
    (tmp_path / "stars.py").write_text("from test import *\n")
    return tmp_path


@pytest.mark.parametrize(
    "source",
    [
        "from test import observe\ndef test_value():\n    assert observe() == 1\n",
        "from test import observe as answer\ndef test_value():\n    assert answer() == 1\n",
        "import test\ndef test_value():\n    assert test.observe() == 1\n",
        "import test as helper\ndef test_value():\n    assert helper.observe() == 1\n",
        "from bridge import exported\ndef test_value():\n    assert exported() == 1\n",
        "from public import observe\ndef test_value():\n    assert observe() == 1\n",
        "from stars import observe\ndef test_value():\n    assert observe() == 1\n",
        "import public as helper\ndef test_value():\n    assert helper.observe() == 1\n",
        "from toy import normalize\nfrom test import observe\ndef test_value():\n"
        "    normalize(' X ')\n    assert observe() == 1\n",
    ],
)
def test_rejects_resolved_root_test_support_directly_or_through_reexports(
    source_tree: Path, source: str
) -> None:
    result = check_submission(source, source_tree)

    assert not result.allowed
    assert result.reason == "unsupported_import"
    assert any("test.py" in detail for detail in result.details)


def test_src_test_module_precedes_root_test_support(source_tree: Path) -> None:
    source = source_tree / "src"
    source.mkdir()
    (source / "test.py").write_text("def production_api():\n    return 1\n")

    result = check_submission(
        "from test import production_api\ndef test_value():\n    assert production_api() == 1\n",
        source_tree,
    )

    assert result.allowed, result


def test_nested_test_module_remains_a_production_module(source_tree: Path) -> None:
    package = source_tree / "pkg"
    package.mkdir()
    (package / "__init__.py").write_text("from .test import production_api\n")
    (package / "test.py").write_text("def production_api():\n    return 1\n")

    result = check_submission(
        "from pkg import production_api\ndef test_value():\n    assert production_api() == 1\n",
        source_tree,
    )

    assert result.allowed, result


def test_rejects_root_test_source_without_executing_it(source_tree: Path) -> None:
    marker = source_tree / "executed"
    (source_tree / "test.py").write_text(
        f"open({str(marker)!r}, 'w').write('executed')\ndef observe():\n    return 1\n"
    )

    result = check_submission(
        "from test import observe\ndef test_value():\n    assert observe() == 1\n", source_tree
    )

    assert not result.allowed
    assert result.reason == "unsupported_import"
    assert not marker.exists()
