"""Execute only trusted toy behavior to verify standalone subTest exception semantics."""

from pathlib import Path

from revertbench.grade.rules import grade_submission


def test_subtest_does_not_swallow_changed_project_failure(tmp_path: Path) -> None:
    bad, repaired = tmp_path / "bad", tmp_path / "repaired"
    for tree, result in ((bad, "value"), (repaired, "value.strip()")):
        tree.mkdir()
        (tree / "toy.py").write_text(f"def normalize(value):\n    return {result}\n")
    source = (
        "from unittest import TestCase\nfrom toy import normalize\ndef test_behavior():\n"
        "    case = TestCase()\n    values = [' x ', ' y ']\n"
        "    for value in values:\n        with case.subTest(value=value):\n"
        "            case.assertEqual(normalize(value), str(value).strip())\n"
    )
    grade = grade_submission(bad, repaired, source, {"toy.py": {2}})
    assert grade.status == "passed" and grade.score == 1, grade.model_dump_json()
    assert len(grade.executions) == 6
    for execution in grade.executions:
        assert len(execution.tests) == 1


def test_subtest_continues_after_success_and_checks_the_later_input(tmp_path: Path) -> None:
    bad, repaired = tmp_path / "bad", tmp_path / "repaired"
    for tree, body in (
        (bad, "return value"),
        (repaired, "return value.strip() if value == ' x ' else value"),
    ):
        tree.mkdir()
        (tree / "toy.py").write_text(f"def normalize(value):\n    {body}\n")
    source = (
        "from unittest import TestCase\nfrom toy import normalize\ndef test_behavior():\n"
        "    case = TestCase()\n    values = [' x ', ' y ']\n"
        "    for value in values:\n        with case.subTest(value=value):\n"
        "            case.assertEqual(normalize(value), str(value).strip())\n"
    )
    grade = grade_submission(bad, repaired, source, {"toy.py": {2}})
    assert grade.status == "rejected" and grade.score == 0
    assert grade.reason == "repair_did_not_pass"
