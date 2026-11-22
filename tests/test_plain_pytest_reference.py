"""Source-only extraction of inert plain pytest class methods."""

import ast

import pytest

from revertbench.mine import reference
from revertbench.mine.reference import ReferenceError, changed_test_functions, extract_reference

METHOD = '''    def test_fields(self):
        """Keep fields from an identity transformation."""
        def preserve(owner, fields):
            yield from fields

        @api.record(auto_fields=True, transformer=preserve)
        class Record:
            amount: int

        assert ["amount"] == [field.name for field in api.fields(Record)]
'''
SOURCE = (
    "from __future__ import annotations\nimport records as api\nimport unused\n"
    'class TestRecords:\n    """Original class description."""\n'
    + METHOD
    + "    def test_unrelated(self):\n        assert unused.other()\n"
)
DERIVATION = "plain_pytest_method_dependency_closure_v1"


def test_plain_method_keeps_inert_class_whole_method_and_future_flag() -> None:
    extracted = extract_reference(SOURCE, "TestRecords::test_fields")
    tree = ast.parse(extracted)
    old_class = next(node for node in ast.parse(SOURCE).body if isinstance(node, ast.ClassDef))
    new_class = next(node for node in tree.body if isinstance(node, ast.ClassDef))
    old_method = next(node for node in old_class.body if isinstance(node, ast.FunctionDef))
    new_method = next(node for node in new_class.body if isinstance(node, ast.FunctionDef))

    assert new_class.name == old_class.name
    assert ast.dump(old_method) == ast.dump(new_method)
    assert METHOD in extracted
    assert extracted.startswith("from __future__ import annotations\n")
    assert '"""Original class description."""' in extracted
    assert "import records as api" in extracted
    assert "import unused" not in extracted and "test_unrelated" not in extracted
    assert "TestCase" not in extracted
    assert changed_test_functions("", SOURCE) == (
        "TestRecords::test_fields",
        "TestRecords::test_unrelated",
    )


def test_shared_selector_distinguishes_plain_pytest_and_preserves_legacy_tags() -> None:
    assert reference.reference_derivation(SOURCE, "TestRecords::test_fields") == DERIVATION
    assert (
        reference.reference_derivation("def test_value():\n    assert True\n", "test_value")
        == "standalone_function_dependency_closure"
    )
    unit = (
        "from unittest import TestCase\nclass Check(TestCase):\n"
        "    def test_value(self):\n        self.assertEqual(1, 1)\n"
    )
    assert (
        reference.reference_derivation(unit, "Check::test_value")
        == "unittest_method_dependency_closure"
    )


@pytest.mark.parametrize(
    "replacement",
    [
        "class Records:",
        "@decorate\nclass TestRecords:",
        "class TestRecords(object):",
        "class TestRecords(metaclass=Meta):",
        "class TestRecords:\n    state = 1",
        "class TestRecords:\n    pytestmark = []",
        "class TestRecords:\n    def setup_method(self): pass",
        "class TestRecords:\n    def teardown_class(self): pass",
        "class TestRecords:\n    def __init__(self): pass",
        "class TestRecords:\n    def helper(self): return 1",
    ],
)
def test_plain_method_does_not_drop_class_context(replacement: str) -> None:
    source = SOURCE.replace("class TestRecords:", replacement)
    with pytest.raises(ReferenceError):
        extract_reference(source, "TestRecords::test_fields")


@pytest.mark.parametrize(
    "before,after",
    [
        ("def test_fields(self):", "def test_fields(self, fixture):"),
        ("def test_fields(self):", "def test_fields(self=1):"),
        ("def test_fields(self):", "def test_fields(self) -> None:"),
        ("def test_fields(self):", "@decorate\n    def test_fields(self):"),
        ('assert ["amount"]', 'self.value = 1\n        assert ["amount"]'),
        ('assert ["amount"]', 'assert self.value == ["amount"]'),
        ("def test_unrelated(self):", "def test_unrelated(self, value=construct()):"),
    ],
)
def test_plain_method_requires_unused_self_and_inert_definitions(before: str, after: str) -> None:
    with pytest.raises(ReferenceError):
        extract_reference(SOURCE.replace(before, after), "TestRecords::test_fields")


@pytest.mark.parametrize(
    "prefix",
    [
        "import pytest\npytestmark = pytest.mark.skip\n",
        "def setup_module(): pass\n",
        "def setUpModule(): pass\n",
        "import pytest\n@pytest.fixture(autouse=True)\ndef context(): pass\n",
    ],
)
def test_plain_method_keeps_module_context_requirements(prefix: str) -> None:
    with pytest.raises(ReferenceError):
        extract_reference(
            prefix + SOURCE.replace("from __future__ import annotations\n", ""),
            "TestRecords::test_fields",
        )


def test_plain_method_tracks_local_class_and_outer_annotation_bindings() -> None:
    source = SOURCE.replace("amount: int", "amount: ValueType").replace(
        "import records as api", "import records as api\nfrom records import ValueType"
    )
    extracted = extract_reference(source, "TestRecords::test_fields")
    assert "from records import ValueType" in extracted
    assert "class Record:" in extracted and "Record" in extracted


def test_nested_class_field_does_not_hide_outer_module_dependency() -> None:
    source = SOURCE.replace(
        "import records as api", "import records as api\nfrom records import amount"
    )
    source = source.replace('assert ["amount"]', 'assert amount() == ["amount"]')
    extracted = extract_reference(source, "TestRecords::test_fields")
    assert "from records import amount" in extracted


def test_plain_method_length_bound_covers_class_wrapper() -> None:
    with pytest.raises(ReferenceError, match="line"):
        extract_reference(SOURCE, "TestRecords::test_fields", max_lines=4)


@pytest.mark.parametrize(
    "source",
    [
        SOURCE + "\nTestRecords = object\n",
        SOURCE + "\nclass TestRecords:\n    def test_other(self): pass\n",
        SOURCE.replace(
            '    """Original class description."""',
            '    """Original class description."""\n    "second string"',
        ),
    ],
)
def test_plain_class_namespace_and_optional_single_docstring_are_unambiguous(source: str) -> None:
    with pytest.raises(ReferenceError):
        extract_reference(source, "TestRecords::test_fields")
