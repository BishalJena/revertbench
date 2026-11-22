"""Static admission of the bounded attrs field-transformer observation."""

from pathlib import Path
from textwrap import indent

import pytest

from revertbench.grade.policy import check_submission

IMPORTS = "from __future__ import annotations\nimport attr\n"
BODY = """\
def preserve(cls, fields):
    yield from fields

@attr.s(auto_attribs=True, field_transformer=preserve)
class Record:
    value: int

assert ["value"] == [field.name for field in attr.fields(Record)]
"""


@pytest.fixture
def attrs_tree(tmp_path: Path) -> Path:
    package = tmp_path / "src" / "attr"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("from ._make import attrs as s, fields\n")
    (package / "_make.py").write_text(
        "def attrs(**kwargs): pass\n"
        "def fields(cls): pass\n"
        "class Attribute:\n    __slots__ = ('name', 'type')\n"
        "Attribute = add_equality_methods(Attribute)\n"
    )
    return tmp_path


def source(body: str = BODY, imports: str = IMPORTS, *, method: bool = True) -> str:
    if method:
        return (
            imports + "\nclass TestTransform:\n    def test_fields(self):\n" + indent(body, " " * 8)
        )
    return imports + "\ndef test_fields():\n" + indent(body, " " * 4)


@pytest.mark.parametrize("method", [True, False])
def test_admits_original_field_transformer_shape(attrs_tree: Path, method: bool) -> None:
    result = check_submission(source(method=method), attrs_tree)

    assert result.allowed, result


def test_supports_source_resolved_attrs_import_aliases(attrs_tree: Path) -> None:
    submitted = source(
        BODY.replace("attr.s", "decorator").replace("attr.fields", "read_fields"),
        "from __future__ import annotations\n"
        "from attr import s as decorator, fields as read_fields\n",
    )

    assert check_submission(submitted, attrs_tree).allowed


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("yield from fields", "yield from cls"),
        ("yield from fields", "yield fields"),
        ("yield from fields", "return fields"),
        ("def preserve(cls, fields):", "def preserve(cls, fields, extra=None):"),
        ("def preserve(cls, fields):", "def preserve(cls, fields: int):"),
        ("def preserve(cls, fields):", "def preserve(cls, fields):\n    pass"),
        ("auto_attribs=True", "auto_attribs=False"),
        ("auto_attribs=True", "auto_attribs=True, slots=True"),
        ("field_transformer=preserve", "field_transformer=None"),
        ("class Record:", "class Record(object):"),
        ("class Record:", "class Record(metaclass=int):"),
        ("value: int", "value: int = 2"),
        ("value: int", "value: str"),
        ("value: int", "value: int\n    other: int"),
        ("value: int", "value: int\n    def helper(self): pass"),
        ("attr.fields(Record)", "attr.fields(cls=Record)"),
        ("attr.fields(Record)", "attr.fields(Record, Record)"),
        ("attr.fields(Record)", "attr.fields(Record())"),
        ("attr.fields(Record)", "[Record]"),
        ("field.name", "field.type"),
        ("field.name", "field.name.upper()"),
        ("field.name", "str(field.name)"),
    ],
)
def test_rejects_shapes_outside_the_bounded_profile(attrs_tree: Path, old: str, new: str) -> None:
    assert not check_submission(source(BODY.replace(old, new)), attrs_tree).allowed


@pytest.mark.parametrize(
    "extra",
    ["attr.s(Record)\n", "attr.fields(Record)\n", "preserve(None, [])\n", "self\n"],
)
def test_local_transformer_and_class_remain_opaque(attrs_tree: Path, extra: str) -> None:
    assert not check_submission(source(BODY + extra), attrs_tree).allowed


def test_rejects_field_transformer_without_original_future_flag(attrs_tree: Path) -> None:
    assert not check_submission(source(imports="import attr\n"), attrs_tree).allowed


def test_rejects_shadowed_decorator(attrs_tree: Path) -> None:
    binding = attrs_tree / "src" / "attr" / "__init__.py"
    binding.write_text("from ._make import fields\ndef s(**kwargs): pass\n")

    assert not check_submission(source(), attrs_tree).allowed


def test_rejects_missing_public_field_member(attrs_tree: Path) -> None:
    implementation = attrs_tree / "src" / "attr" / "_make.py"
    implementation.write_text(implementation.read_text().replace("'name', ", ""))

    assert not check_submission(source(), attrs_tree).allowed


def test_rejects_method_wrapper_with_lifecycle_hook(attrs_tree: Path) -> None:
    submitted = source().replace(
        "    def test_fields", "    def setup_method(self): pass\n    def test_fields"
    )

    assert not check_submission(submitted, attrs_tree).allowed


def test_public_source_lookups_do_not_enable_private_import(attrs_tree: Path) -> None:
    submitted = source(
        imports=IMPORTS + "from attr import s\nfrom attr._make import attrs\n",
    )

    assert not check_submission(submitted, attrs_tree).allowed


def test_preserves_real_attrs_alias_assignment_shape(attrs_tree: Path) -> None:
    binding = attrs_tree / "src" / "attr" / "__init__.py"
    binding.write_text("from ._make import attrs, fields\ns = attributes = attrs\n")

    assert check_submission(source(), attrs_tree).allowed


def test_rejects_rebound_intermediate_decorator_alias(attrs_tree: Path) -> None:
    binding = attrs_tree / "src" / "attr" / "__init__.py"
    binding.write_text(
        "from ._make import attrs, fields\n"
        "def replacement(**kwargs): pass\n"
        "attrs = replacement\n"
        "s = attrs\n"
    )

    assert not check_submission(source(), attrs_tree).allowed
