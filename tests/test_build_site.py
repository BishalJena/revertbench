import hashlib
import json
from pathlib import Path
from shutil import copytree
from types import ModuleType

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
    builder.build_site(source, output)
    assert (output / "data.json").read_bytes() == raw


def test_unexpected_output_file_aborts_the_build(builder: ModuleType, tmp_path: Path) -> None:
    (tmp_path / "stale.txt").write_text("preserve this", encoding="utf-8")

    with pytest.raises(ValueError, match="unexpected output"):
        builder.build_site(ROOT, tmp_path)

    assert list(tmp_path.iterdir()) == [tmp_path / "stale.txt"]


def test_export_rejects_a_changed_reference_artifact(builder: ModuleType, tmp_path: Path) -> None:
    case_dir = tmp_path / PRODUCT
    copytree(PRODUCT_SOURCE, case_dir)
    (case_dir / "reference.py").write_text("# changed\n", encoding="utf-8")

    with pytest.raises(ValueError, match=r"reference\.py.*manifest"):
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
    report_file.write_text(json.dumps(report), encoding="utf-8")
    case_file = case_dir / "case.json"
    case = json.loads(case_file.read_bytes())
    digest = hashlib.sha256(report_file.read_bytes()).hexdigest()
    case["validation"]["report_sha256"] = digest
    case["human_review"]["validation_report_sha256"] = digest
    case_file.write_text(json.dumps(case), encoding="utf-8")
    refresh_manifest(case_dir, report_file.name, case_file.name)

    with pytest.raises(ValueError, match=r"base.*execution"):
        builder.read_case(case_dir, COPY)
