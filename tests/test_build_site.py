import ast
import hashlib
import json
from pathlib import Path
from shutil import copytree
from types import ModuleType
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
PRODUCT = "more-itertools-71b76842d32c-5a1d64d1-be44ad73"
PRODUCT_SOURCE = ROOT / "cases" / PRODUCT
COPY = {"title": "Iterator input", "summary": "Input failed.", "expected": "Return 12."}


@pytest.fixture
def builder(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    monkeypatch.syspath_prepend(str(ROOT))
    from scripts import build_site

    return build_site


def refresh_manifest(case_dir: Path, *names: str) -> None:
    manifest_file = case_dir / "publication-manifest.json"
    manifest = json.loads(manifest_file.read_bytes())
    for name in names:
        raw = (case_dir / name).read_bytes()
        manifest["files"][name] = {"sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}
    manifest_file.write_text(json.dumps(manifest), encoding="utf-8")


def write_report(case_dir: Path, report: dict[str, Any]) -> None:
    report_file = case_dir / "validation.json"
    case_file = case_dir / "case.json"
    report_file.write_text(json.dumps(report), encoding="utf-8")
    case = json.loads(case_file.read_bytes())
    digest = hashlib.sha256(report_file.read_bytes()).hexdigest()
    case["validation"]["report_sha256"] = digest
    case["human_review"]["validation_report_sha256"] = digest
    case_file.write_text(json.dumps(case), encoding="utf-8")
    refresh_manifest(case_dir, report_file.name, case_file.name)


def recorded_control_source() -> str:
    """Read the probe's source literal without loading validator dependencies."""
    path = ROOT / "src/revertbench/validate/controller.py"
    module = ast.parse(path.read_text(encoding="utf-8"))
    for dictionary in (node for node in ast.walk(module) if isinstance(node, ast.Dict)):
        for key, value in zip(dictionary.keys, dictionary.values, strict=True):
            if isinstance(key, ast.Constant) and key.value == "assert_true":
                assert isinstance(value, ast.Constant) and isinstance(value.value, str)
                return value.value
    raise AssertionError("assert_true probe source not found")


def test_build_emits_only_public_assets_and_recorded_case_evidence(
    builder: ModuleType, tmp_path: Path
) -> None:
    source = tmp_path / "source"
    copytree(ROOT / "cases", source / "cases")
    site = source / "site"
    site.mkdir()
    for name in ("index.html", "styles.css", "app.js"):
        (site / name).write_text(name, encoding="utf-8")
    (site / "case-copy.json").write_bytes((ROOT / "site/case-copy.json").read_bytes())
    (site / "unpublished.txt").write_text("private", encoding="utf-8")
    output = tmp_path / "dist"

    builder.build_site(source, output)

    assert {path.name for path in output.iterdir()} == {
        "index.html",
        "styles.css",
        "app.js",
        "data.json",
    }
    raw = (output / "data.json").read_bytes()
    data = json.loads(raw)
    published = list((source / "cases").glob("*/case.json"))
    repositories = {json.loads(path.read_bytes())["repo"] for path in published}
    executions = sum(
        len(json.loads(path.with_name("validation.json").read_bytes())["reference"]["executions"])
        for path in published
    )
    assert data["stats"] == {
        "case_count": len(published),
        "repository_count": len(repositories),
        "execution_count": executions,
    }
    case = data["cases"][0]
    assert case["id"] == PRODUCT
    for field, filename in (
        ("reference", "reference.py"),
        ("change", "bad.diff"),
        ("repair", "repair.patch"),
        ("license", "upstream-license.txt"),
        ("notice", "upstream-reference-header.txt"),
    ):
        assert case[field].encode() == (PRODUCT_SOURCE / filename).read_bytes()
    assert case["runs"] == [
        {"role": "base", "seeds": [11, 23, 37], "result": "pass", "exception": None},
        {"role": "bad", "seeds": [11, 23, 37], "result": "fail", "exception": "TypeError"},
        {"role": "repaired", "seeds": [11, 23, 37], "result": "pass", "exception": None},
    ]
    assert case["control"] == {
        "source": recorded_control_source(),
        "score": 0,
        "reason": "bad_did_not_fail",
        "runs": [
            {"role": "base", "seeds": [], "result": "not_run", "exception": None},
            {"role": "bad", "seeds": [11, 23, 37], "result": "pass", "exception": None},
            {"role": "repaired", "seeds": [11, 23, 37], "result": "pass", "exception": None},
        ],
    }
    builder.build_site(source, output)
    assert (output / "data.json").read_bytes() == raw


def test_unexpected_output_file_aborts_the_build(builder: ModuleType, tmp_path: Path) -> None:
    (tmp_path / "stale.txt").write_text("preserve this", encoding="utf-8")

    with pytest.raises(ValueError, match="unexpected output"):
        builder.build_site(ROOT, tmp_path)

    assert list(tmp_path.iterdir()) == [tmp_path / "stale.txt"]


@pytest.mark.parametrize("filename", ["reference.py", "bad.diff"])
def test_export_rejects_a_changed_artifact(
    builder: ModuleType, tmp_path: Path, filename: str
) -> None:
    case_dir = tmp_path / PRODUCT
    copytree(PRODUCT_SOURCE, case_dir)
    (case_dir / filename).write_text("# changed\n", encoding="utf-8")

    with pytest.raises(ValueError, match="manifest"):
        builder.read_case(case_dir, COPY)


def test_export_requires_a_review_bound_to_the_report(builder: ModuleType, tmp_path: Path) -> None:
    case_dir = tmp_path / PRODUCT
    copytree(PRODUCT_SOURCE, case_dir)
    path = case_dir / "case.json"
    case = json.loads(path.read_bytes())
    case["human_review"]["validation_report_sha256"] = "0" * 64
    path.write_text(json.dumps(case), encoding="utf-8")
    refresh_manifest(case_dir, path.name)

    with pytest.raises(ValueError, match="review"):
        builder.read_case(case_dir, COPY)


@pytest.mark.parametrize("alteration", ["truncated", "empty", "wrong result"])
def test_export_rejects_an_invalid_execution_even_with_matching_digests(
    builder: ModuleType, tmp_path: Path, alteration: str
) -> None:
    case_dir = tmp_path / PRODUCT
    copytree(PRODUCT_SOURCE, case_dir)
    report_file = case_dir / "validation.json"
    report = json.loads(report_file.read_bytes())
    run = report["reference"]["executions"][0]
    if alteration == "truncated":
        run["truncated"] = True
    elif alteration == "empty":
        run["collected_node_ids"] = []
        run["tests"] = []
    else:
        run["exit_code"] = 1
    write_report(case_dir, report)

    with pytest.raises(ValueError, match=r"base.*execution"):
        builder.read_case(case_dir, COPY)


@pytest.mark.parametrize("alteration", ["score", "reason", "incomplete", "failed"])
def test_export_rejects_unexpected_control_evidence(
    builder: ModuleType, tmp_path: Path, alteration: str
) -> None:
    case_dir = tmp_path / PRODUCT
    copytree(PRODUCT_SOURCE, case_dir)
    report = json.loads((case_dir / "validation.json").read_bytes())
    grade = report["probes"]["assert_true"]["grade"]
    if alteration == "score":
        grade["score"] = 1
    elif alteration == "reason":
        grade["reason"] = "repair_did_not_pass"
    elif alteration == "incomplete":
        grade["executions"].pop()
    else:
        grade["executions"][0]["exit_code"] = 1
    write_report(case_dir, report)

    with pytest.raises(ValueError, match="control"):
        builder.read_case(case_dir, COPY)
