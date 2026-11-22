"""Static admission for a literal in-memory byte buffer passed to a public API."""

from pathlib import Path

import pytest

from revertbench.grade.policy import check_submission


@pytest.fixture
def message_tree(tmp_path: Path) -> Path:
    package = tmp_path / "messages"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "extract.py").write_text(
        "def extract(kind, buffer, names, tags, options):\n"
        "    return [(4, 'welcome', ['explanation'])]\n",
        encoding="utf-8",
    )
    return tmp_path


@pytest.mark.parametrize(
    ("import_line", "constructor"),
    [
        ("from io import BytesIO", "BytesIO"),
        ("from io import BytesIO as Buffer", "Buffer"),
        ("from io import BytesIO\nfrom io import BytesIO as Buffer", "Buffer"),
    ],
)
def test_literal_byte_buffer_can_be_passed_to_public_api(
    message_tree: Path, import_line: str, constructor: str
) -> None:
    source = (
        f"{import_line}\nfrom messages import extract\n\n"
        "def test_comment():\n"
        f"    buffer = {constructor}(b'# explanation\\nwelcome')\n"
        "    rows = list(extract.extract('python', buffer, {'_': None}, ['TAG'], {}))\n"
        "    row = rows[0]\n"
        "    assert row[0] in (4, 5)\n"
        "    assert row[1] == 'welcome'\n"
        "    assert row[2] == ['explanation']\n"
    )

    result = check_submission(source, message_tree)

    assert result.allowed, result


def test_exact_constructor_alias_keeps_literal_only_arity(message_tree: Path) -> None:
    source = (
        "from io import BytesIO as Buffer\ndef test_empty():\n"
        "    make_buffer = Buffer\n    buffer = make_buffer(b'')\n    assert buffer is not None\n"
    )

    assert check_submission(source, message_tree).allowed


@pytest.mark.parametrize(
    "expression",
    [
        "BytesIO()",
        "BytesIO(b'a', b'b')",
        "BytesIO(initial_bytes=b'a')",
        "BytesIO(*[b'a'])",
        "BytesIO(**{'initial_bytes': b'a'})",
        "BytesIO('a')",
        "BytesIO(1)",
        "BytesIO(None)",
        "BytesIO(data)",
        "BytesIO(b'a' + b'b')",
        "BytesIO(bytes([65]))",
        "BytesIO(b'a').read()",
        "BytesIO(b'a').write(b'b')",
        "BytesIO(b'a').getvalue()",
        "BytesIO(b'a').close()",
        "BytesIO(b'a').seek(0)",
    ],
)
def test_byte_buffer_does_not_grant_dynamic_inputs_or_methods(
    message_tree: Path, expression: str
) -> None:
    source = (
        "from io import BytesIO\ndef test_buffer():\n"
        f"    data = b'a'\n    buffer = {expression}\n    assert buffer is not None\n"
    )

    assert not check_submission(source, message_tree).allowed


@pytest.mark.parametrize(
    "source",
    [
        "import io\ndef test_buffer():\n    assert io.BytesIO(b'a')\n",
        "import io as memory\ndef test_buffer():\n    assert memory.BytesIO(b'a')\n",
        "from io import StringIO\ndef test_buffer():\n    assert StringIO('a')\n",
        "from io import BytesIO, StringIO\ndef test_buffer():\n    assert BytesIO(b'a')\n",
        "from io import open\ndef test_buffer():\n    assert True\n",
        "from io import *\ndef test_buffer():\n    assert True\n",
        "from io.BytesIO import read\ndef test_buffer():\n    assert True\n",
        "from io import BytesIO\nbuffer = BytesIO(b'a')\ndef test_buffer():\n    assert True\n",
        "from io import BytesIO\ndef helper(constructor):\n    return constructor(b'a')\n"
        "def test_buffer():\n    assert helper(BytesIO)\n",
        "from io import BytesIO\ndef test_buffer():\n"
        "    make_buffer = BytesIO\n    data = b'a'\n    assert make_buffer(data)\n",
        "from io import BytesIO\ndef test_buffer():\n"
        "    buffer = BytesIO(b'a')\n    alias = buffer\n    assert alias.getvalue() == b'a'\n",
    ],
)
def test_io_capability_remains_constructor_only(message_tree: Path, source: str) -> None:
    assert not check_submission(source, message_tree).allowed


@pytest.mark.parametrize("location", ["io.py", "io/__init__.py", "src/io.py", "src/io/__init__.py"])
def test_literal_buffer_rejects_project_stdlib_shadows(message_tree: Path, location: str) -> None:
    shadow = message_tree / location
    shadow.parent.mkdir(parents=True, exist_ok=True)
    shadow.write_text("class BytesIO:\n    pass\n", encoding="utf-8")
    source = "from io import BytesIO\ndef test_buffer():\n    assert BytesIO(b'a') is not None\n"

    result = check_submission(source, message_tree)

    assert not result.allowed
    assert result.reason == "unsupported_import"
