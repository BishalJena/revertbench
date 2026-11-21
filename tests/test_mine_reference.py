from pathlib import Path

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


def test_unittest_method_lift_preserves_body_and_qualified_changed_identity() -> None:
    import ast

    source = (
        "from unittest import TestCase\nimport pkg\n"
        "class ProductTests(TestCase):\n"
        "    def test_iterator_input(self):\n"
        "        self.assertEqual(pkg.run(iter(['a', 'b'])), ['a', 'b'])\n"
        "    def test_other(self):\n        self.assertEqual(1, 1)\n"
    )
    assert changed_test_functions("", source) == (
        "ProductTests::test_iterator_input",
        "ProductTests::test_other",
    )
    extracted = extract_reference(source, "ProductTests::test_iterator_input")
    old_class = next(node for node in ast.parse(source).body if isinstance(node, ast.ClassDef))
    old_method = next(node for node in old_class.body if isinstance(node, ast.FunctionDef))
    lifted = next(node for node in ast.parse(extracted).body if isinstance(node, ast.FunctionDef))
    assert lifted.name == old_method.name and not lifted.args.args
    assert isinstance(lifted.body[0], ast.Assign)
    assert ast.dump(ast.Module(body=old_method.body, type_ignores=[])) == ast.dump(
        ast.Module(body=lifted.body[1:], type_ignores=[])
    )
    assert "from unittest import TestCase as _revertbench_test_case" in extracted
    assert "def test_other" not in extracted
    assert "class ProductTests" not in extracted


@pytest.mark.parametrize(
    "binding,base",
    [
        ("from unittest import TestCase as UnitCase", "UnitCase"),
        ("import unittest as unit", "unit.TestCase"),
    ],
)
def test_unittest_base_requires_exact_statically_bound_stdlib_identity(
    binding: str, base: str
) -> None:
    source = (
        f"{binding}\n"
        f"class ProductTests({base}):\n"
        f"    def test_api(self):\n"
        f"        self.assertEqual(1, 1)\n"
    )
    assert "self.assertEqual(1, 1)" in extract_reference(source, "ProductTests::test_api")


@pytest.mark.parametrize(
    "context",
    [
        (
            "class ProductTests(TestCase, object):\n"
            "    def test_api(self):\n"
            "        self.assertEqual(1, 1)\n"
        ),
        (
            "@decorate\n"
            "class ProductTests(TestCase):\n"
            "    def test_api(self):\n"
            "        self.assertEqual(1, 1)\n"
        ),
        (
            "class ProductTests(TestCase):\n"
            "    VALUE = 1\n"
            "    def test_api(self):\n"
            "        self.assertEqual(1, 1)\n"
        ),
        (
            "class ProductTests(TestCase):\n"
            "    def setUp(self):\n"
            "        pass\n"
            "    def test_api(self):\n"
            "        self.assertEqual(1, 1)\n"
        ),
        (
            "class ProductTests(TestCase):\n"
            "    def tearDown(self):\n"
            "        pass\n"
            "    def test_api(self):\n"
            "        self.assertEqual(1, 1)\n"
        ),
        (
            "class ProductTests(TestCase):\n"
            "    def helper(self):\n"
            "        return 1\n"
            "    def test_api(self):\n"
            "        self.assertEqual(self.helper(), 1)\n"
        ),
        (
            "class ProductTests(TestCase):\n"
            "    def assertEqual(self, *args):\n"
            "        pass\n"
            "    def test_api(self):\n"
            "        self.assertEqual(1, 1)\n"
        ),
        (
            "class ProductTests(TestCase):\n"
            "    @decorate\n"
            "    def test_api(self):\n"
            "        self.assertEqual(1, 1)\n"
        ),
        (
            "class ProductTests(TestCase):\n"
            "    def test_api(self, fixture):\n"
            "        self.assertEqual(1, 1)\n"
        ),
        (
            "class ProductTests(TestCase):\n"
            "    def test_api(self, value=1):\n"
            "        self.assertEqual(1, 1)\n"
        ),
        (
            "class ProductTests(TestCase):\n"
            "    def test_api(self):\n"
            "        self.value = 1\n"
            "        self.assertEqual(self.value, 1)\n"
        ),
        (
            "class ProductTests(TestCase):\n"
            "    def test_api(self):\n"
            "        self.assertEqual(self.__class__, TestCase)\n"
        ),
        (
            "class ProductTests(TestCase):\n"
            "    def test_api(self):\n"
            "        self.assertEqual(1, 1)\n"
            "    def test_other(self, value=run()):\n"
            "        pass\n"
        ),
    ],
)
def test_unittest_lift_rejects_class_context_or_unpreserved_dependencies(context: str) -> None:
    with pytest.raises(ReferenceError):
        extract_reference("from unittest import TestCase\n" + context, "ProductTests::test_api")


@pytest.mark.parametrize(
    "binding",
    [
        "from pkg import TestCase\n",
        "from unittest import TestCase\nTestCase = object\n",
        "from unittest import TestCase\nif True:\n    TestCase = object\n",
        "import unittest as unit\nunit = object\n",
    ],
)
def test_unittest_base_alias_cannot_be_rebound_or_come_from_project(binding: str) -> None:
    base = "unit.TestCase" if "unit =" in binding else "TestCase"
    source = (
        binding
        + f"class ProductTests({base}):\n    def test_api(self):\n        self.assertEqual(1, 1)\n"
    )
    with pytest.raises(ReferenceError):
        extract_reference(source, "ProductTests::test_api")


def test_unittest_lift_uses_collision_free_alias_and_excludes_unused_testcase_import() -> None:
    source = (
        "from unittest import TestCase\n"
        "import pkg\n"
        "_revertbench_test_case = 3\n"
        "class ProductTests(TestCase):\n"
        "    def test_api(self):\n"
        "        _revertbench_test_case_1 = 4\n"
        "        self.assertEqual(pkg.run(), _revertbench_test_case)\n"
    )
    extracted = extract_reference(source, "ProductTests::test_api")
    assert "from unittest import TestCase as _revertbench_test_case_2" in extracted
    assert "self = _revertbench_test_case_2()" in extracted
    assert "from unittest import TestCase\n" not in extracted
    assert "_revertbench_test_case = 3" in extracted


@pytest.mark.parametrize(
    "extra",
    [
        "    def test_api(self) -> None:\n        self.assertEqual(1, 1)\n",
        "    def test_api(self: object):\n        self.assertEqual(1, 1)\n",
        "    def test_api(self):\n        self = object()\n        self.assertEqual(1, 1)\n",
        "    def test_api(self):\n        inspect(self)\n        self.assertEqual(1, 1)\n",
        "    def test_api(self):\n        equal = self.assertEqual\n        equal(1, 1)\n",
        (
            "    def test_api(self):\n"
            "        self.assertEqual(1, 1)\n"
            "    def test_other(self) -> initialize():\n"
            "        pass\n"
        ),
    ],
)
def test_unittest_lift_rejects_annotation_and_identity_context(extra: str) -> None:
    source = "from unittest import TestCase\nclass ProductTests(TestCase):\n" + extra
    with pytest.raises(ReferenceError):
        extract_reference(source, "ProductTests::test_api")


@pytest.mark.parametrize(
    "prefix",
    [
        "from unittest import TestCase\nfrom pkg import setUpModule\n",
        "from unittest import TestCase\nsetUpModule = initialize\n",
        "from unittest import TestCase\ndef load_tests(*args):\n    pass\n",
        "from unittest import TestCase\nTestCase.assertEqual = replacement\n",
        "import unittest as unit\nunit.TestCase = replacement\n",
    ],
)
def test_unittest_lift_rejects_module_hooks_and_base_monkeypatches(prefix: str) -> None:
    base = "unit.TestCase" if "import unittest as unit" in prefix else "TestCase"
    source = (
        prefix
        + f"class ProductTests({base}):\n    def test_api(self):\n        self.assertEqual(1, 1)\n"
    )
    with pytest.raises(ReferenceError):
        extract_reference(source, "ProductTests::test_api")


def test_unittest_lift_rejects_standalone_helper_dependencies() -> None:
    source = (
        "from unittest import TestCase\n"
        "def expected():\n"
        "    return 1\n"
        "class ProductTests(TestCase):\n"
        "    def test_api(self):\n"
        "        self.assertEqual(1, expected())\n"
    )
    with pytest.raises(ReferenceError, match="helper"):
        extract_reference(source, "ProductTests::test_api")


def test_unrelated_nested_testcase_name_does_not_rebind_stdlib_base() -> None:
    source = (
        "from unittest import TestCase\n"
        "class OtherTests(TestCase):\n"
        "    def test_other(self):\n"
        "        class TestCase:\n"
        "            pass\n"
        "class ProductTests(TestCase):\n"
        "    def test_api(self):\n"
        "        self.assertEqual(1, 1)\n"
    )
    assert "self.assertEqual(1, 1)" in extract_reference(source, "ProductTests::test_api")


def test_unittest_lift_cannot_shadow_original_module_value_with_generated_function() -> None:
    source = (
        "from unittest import TestCase\n"
        "test_api = 1\n"
        "class ProductTests(TestCase):\n"
        "    def test_api(self):\n"
        "        self.assertEqual(test_api, 1)\n"
    )
    with pytest.raises(ReferenceError, match="collision"):
        extract_reference(source, "ProductTests::test_api")


def test_unittest_lift_accepts_exact_additive_upstream_doctest_loader() -> None:
    # Exact hook and selected method body from more-itertools PR1082.
    source = (
        "from doctest import DocTestSuite\nfrom unittest import TestCase, mock\n"
        "import more_itertools as mi\n"
        "class ProductIndexTests(TestCase):\n"
        "    def test_iterator_input(self):\n"
        "        self.assertEqual(\n"
        "            mi.product_index(iter(['i', 'a']), iter('snicker'), iter('snack')),\n"
        "            12,\n        )\n"
        "def load_tests(loader, tests, ignore):\n"
        "    # Add the doctests\n"
        "    tests.addTests(DocTestSuite('more_itertools.more'))\n"
        "    return tests\n"
    )
    extracted = extract_reference(source, "ProductIndexTests::test_iterator_input")
    assert "mi.product_index(iter(['i', 'a']), iter('snicker'), iter('snack'))" in extracted
    assert "load_tests" not in extracted and "DocTestSuite" not in extracted


@pytest.mark.parametrize(
    "change",
    [
        "    tests.modified = True\n",
        "    tests.addTests(other())\n",
        "    tests.clear()\n",
    ],
)
def test_unittest_loader_exemption_cannot_hide_other_mutations(change: str) -> None:
    source = (
        "from doctest import DocTestSuite\nfrom unittest import TestCase\n"
        "class ProductTests(TestCase):\n"
        "    def test_api(self):\n        self.assertEqual(1, 1)\n"
        "def load_tests(loader, tests, ignore):\n"
        "    tests.addTests(DocTestSuite('pkg'))\n" + change + "    return tests\n"
    )
    with pytest.raises(ReferenceError, match="module_context"):
        extract_reference(source, "ProductTests::test_api")


@pytest.mark.parametrize(
    "binding",
    [
        "from pkg import DocTestSuite\n",
        "from doctest import DocTestSuite\nDocTestSuite = other\n",
    ],
)
def test_unittest_loader_requires_exact_unrebound_doctest_import(binding: str) -> None:
    source = binding + (
        "from unittest import TestCase\nclass ProductTests(TestCase):\n"
        "    def test_api(self):\n        self.assertEqual(1, 1)\n"
        "def load_tests(loader, tests, ignore):\n"
        "    tests.addTests(DocTestSuite('pkg'))\n    return tests\n"
    )
    with pytest.raises(ReferenceError, match="module_context"):
        extract_reference(source, "ProductTests::test_api")


@pytest.mark.parametrize(
    "prefix,body,required",
    [
        (
            "from pkg import c\n",
            "actual = [c for c in [1]]\n    assert c() == 2",
            "from pkg import c",
        ),
        ("c = [1]\n", "assert [c for c in c] == [1]", "c = [1]"),
        (
            "from pkg import run as list\n",
            "actual = [list for list in [1]]\n    assert list() == 2",
            "from pkg import run as list",
        ),
        (
            "def c():\n    return 2\n",
            "actual = [c for c in []]\n    assert c() == 2",
            "def c():",
        ),
    ],
)
def test_comprehension_local_binding_does_not_drop_outer_dependencies(
    prefix: str, body: str, required: str
) -> None:
    extracted = extract_reference(prefix + f"def test_behavior():\n    {body}\n", "test_behavior")
    assert required in extracted


def test_comprehension_target_is_not_a_spurious_module_dependency() -> None:
    source = (
        "UNUSED = object()\ndef test_behavior():\n    assert [UNUSED for UNUSED in [1]] == [1]\n"
    )
    assert "UNUSED = object()" not in extract_reference(source, "test_behavior")


def test_missing_outer_name_is_not_hidden_by_a_comprehension_target() -> None:
    source = "def test_behavior():\n    values = [c for c in []]\n    assert c()\n"
    with pytest.raises(ReferenceError, match="unresolved_module_dependency:c"):
        extract_reference(source, "test_behavior")


def test_qualified_method_list_comprehensions_preserve_all_original_observations() -> None:
    import ast

    source = (
        "from unittest import TestCase\nimport pkg as mi\n"
        "class IchunkedTests(TestCase):\n"
        "    def test_zero_nonempty(self):\n"
        "        iterable = [1, 2, 3]\n"
        "        actual = [list(c) for c in mi.ichunked(iterable, 0)]\n"
        "        self.assertEqual(actual, [])\n"
        "        source = iter([1, 2, 3])\n"
        "        self.assertEqual([list(c) for c in mi.ichunked(source, 0)], [])\n"
        "        self.assertEqual(list(source), [1, 2, 3])\n"
    )
    extracted = extract_reference(source, "IchunkedTests::test_zero_nonempty")
    original_class = ast.parse(source).body[-1]
    assert isinstance(original_class, ast.ClassDef)
    original = original_class.body[0]
    lifted = ast.parse(extracted).body[-1]
    assert isinstance(original, ast.FunctionDef) and isinstance(lifted, ast.FunctionDef)
    assert ast.dump(ast.Module(body=original.body, type_ignores=[])) == ast.dump(
        ast.Module(body=lifted.body[1:], type_ignores=[])
    )
    assert sum(isinstance(node, ast.Assert) for node in ast.walk(lifted)) == 0
    assert (
        sum(
            isinstance(node, ast.Attribute) and node.attr == "assertEqual"
            for node in ast.walk(lifted)
        )
        == 3
    )
    assert "import pkg as mi" in extracted


def test_comprehension_shadow_cannot_hide_a_qualified_method_helper_dependency() -> None:
    source = (
        "from unittest import TestCase\ndef c():\n    return 1\n"
        "class TestAPI(TestCase):\n    def test_behavior(self):\n"
        "        values = [c for c in []]\n        self.assertEqual(c(), 1)\n"
    )
    with pytest.raises(ReferenceError, match="helper_dependency"):
        extract_reference(source, "TestAPI::test_behavior")


@pytest.mark.parametrize(
    "expression",
    [
        "[x for x in [] if x]",
        "[x for x in [] for y in []]",
        "[x for x, y in []]",
        "[x async for x in []]",
        "[[y for y in []] for x in []]",
        "[x for x in [y for y in []]]",
        "[(y := x) for x in []]",
        "(x for x in [])",
        "{x for x in []}",
        "{x: x for x in []}",
        "[lambda: x for x in []]",
    ],
)
def test_reference_does_not_admit_other_comprehension_forms(
    tmp_path: Path, expression: str
) -> None:
    with pytest.raises(ReferenceError):
        extract_reference(
            f"def test_behavior():\n    assert {expression} == []\n",
            "test_behavior",
            bad_tree=tmp_path,
        )


def test_imported_first_iterable_is_loaded_before_its_same_named_target() -> None:
    source = "from pkg import c\ndef test_behavior():\n    assert [c for c in c()] == [1]\n"
    assert "from pkg import c" in extract_reference(source, "test_behavior")
