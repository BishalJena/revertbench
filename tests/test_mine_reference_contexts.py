"""Source-only checks for stateless methods in otherwise inert unittest classes."""

import ast
from textwrap import indent

import pytest

from revertbench.mine.reference import ReferenceError, extract_reference


def _source(extra: str = "", body: str = "self.assertEqual(1, 1)\n") -> str:
    return (
        "from unittest import TestCase\nimport target\n"
        "class TestAPI(TestCase):\n"
        + indent(extra, "    ")
        + "    def test_api(self):\n"
        + indent(body, "        ")
    )


def _preserved_body(source: str) -> str:
    extracted = extract_reference(source, "TestAPI::test_api")
    cls = next(node for node in ast.parse(source).body if isinstance(node, ast.ClassDef))
    original = next(
        node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name == "test_api"
    )
    lifted = next(node for node in ast.parse(extracted).body if isinstance(node, ast.FunctionDef))
    assert [ast.dump(node) for node in original.body] == [
        ast.dump(node) for node in lifted.body[1:]
    ]
    return extracted


def test_unused_helper_with_literal_default_is_inert_and_not_extracted() -> None:
    source = _source(
        "def helper(self, iterable, r=None):\n"
        "    self.value = iterable\n"
        "    return arbitrary_unexecuted_body(r)\n",
        "self.assertEqual(target.run(), 1)\n",
    )
    extracted = _preserved_body(source)
    assert "def helper" not in extracted
    assert "arbitrary_unexecuted_body" not in extracted
    assert "import target" in extracted


def test_unused_helpers_do_not_change_legacy_reference_bytes() -> None:
    ordinary = _source(body="self.assertEqual(target.run(), 1)\n")
    with_helper = _source(
        "def helper(self, value=1):\n    return value\n",
        "self.assertEqual(target.run(), 1)\n",
    )
    assert extract_reference(with_helper, "TestAPI::test_api") == extract_reference(
        ordinary, "TestAPI::test_api"
    )


@pytest.mark.parametrize(
    "body",
    [
        "for value in (None, 2, 'U', True, False, b'x', 0, -1, (), (1,), 2.5, '', 'a'):\n"
        "    with self.subTest(iterable=value):\n"
        "        self.assertEqual(target.run(value), value)\n",
        "cases = ((0, 1, True), (1, 2, False), (2, 3, True),\n"
        "         (3, 4, False), (4, 5, True), (5, 6, False))\n"
        "for args1, args2, expected in cases:\n"
        "    with self.subTest(args1=args1, args2=args2):\n"
        "        self.assertEqual(target.run(args1, args2), expected)\n",
    ],
)
def test_direct_subtest_preserves_entire_loop_and_all_input_observations(body: str) -> None:
    extracted = _preserved_body(_source(body=body))
    assert "with self.subTest(" in extracted


@pytest.mark.parametrize(
    "member",
    [
        "VALUE = 1\n",
        "def setUp(self):\n    pass\n",
        "def tearDown(self):\n    pass\n",
        "def setup_method(self, method):\n    pass\n",
        "def __getattr__(self, name):\n    return 1\n",
        "def assertEqual(self, first, second):\n    pass\n",
        "def subTest(self, value=None):\n    pass\n",
        "def _formatMessage(self, msg, standardMsg):\n    return ''\n",
        "def _getAssertEqualityFunc(self, first, second):\n    pass\n",
        "def _outcome(self):\n    return None\n",
        "def _callTestMethod(self, method):\n    pass\n",
        "def addTypeEqualityFunc(self, first, second):\n    pass\n",
        "@decorate\ndef helper(self):\n    pass\n",
        "@property\ndef helper(self):\n    return 1\n",
        "def helper(self, value=initialize()):\n    return value\n",
        "def helper(self, value=set()):\n    return value\n",
        "def helper(self, value=shared_state):\n    return value\n",
        "def helper(self, value: initialize()):\n    pass\n",
        "def helper(self) -> initialize():\n    pass\n",
        "async def helper(self):\n    pass\n",
    ],
)
def test_unused_member_still_cannot_change_class_or_framework_context(member: str) -> None:
    with pytest.raises(ReferenceError):
        extract_reference(_source(member), "TestAPI::test_api")


@pytest.mark.parametrize(
    "body",
    [
        "self.assertEqual(self.helper(), 1)\n",
        "self.assertEqual(self.helper, 1)\n",
        "self.assertEqual(self.value, 1)\n",
    ],
)
def test_selected_method_cannot_depend_on_unused_helpers_or_state(body: str) -> None:
    with pytest.raises(ReferenceError):
        extract_reference(_source("def helper(self):\n    return 1\n", body), "TestAPI::test_api")


@pytest.mark.parametrize(
    "body",
    [
        "self.subTest(value=1)\nself.assertEqual(1, 1)\n",
        "context = self.subTest\nwith context(value=1):\n    self.assertEqual(1, 1)\n",
        "context = self.subTest(value=1)\nwith context:\n    self.assertEqual(1, 1)\n",
        "with self.subTest(value=1) as captured:\n    self.assertEqual(1, 1)\n",
        "with self.subTest('message', value=1):\n    self.assertEqual(1, 1)\n",
        "with self.subTest(msg='message'):\n    self.assertEqual(1, 1)\n",
        "with self.subTest():\n    self.assertEqual(1, 1)\n",
        "with self.subTest(a=1, b=2, c=3):\n    self.assertEqual(1, 1)\n",
        "with self.subTest(**{'value': 1}):\n    self.assertEqual(1, 1)\n",
        "with self.subTest(value=1, value=2):\n    self.assertEqual(1, 1)\n",
    ],
)
def test_subtest_only_expands_direct_uncaptured_label_contexts(body: str) -> None:
    with pytest.raises(ReferenceError):
        extract_reference(_source(body=body), "TestAPI::test_api")
