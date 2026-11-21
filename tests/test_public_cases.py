"""Published accepted cases retain their review and immutable evidence bindings."""

import hashlib
import json
import os
import re
from pathlib import Path

import pytest

from revertbench.review import _machine_passed, build_review_packet, load_case, read_artifact

_CASES_ROOT = Path(__file__).resolve().parents[1] / "cases"
_CASE_PATHS = tuple(sorted(_CASES_ROOT.glob("*/case.json")))


def test_public_dataset_contains_an_accepted_case() -> None:
    assert _CASE_PATHS, "publish the owner-approved first case"


@pytest.mark.parametrize("case_path", _CASE_PATHS, ids=lambda path: path.parent.name)
def test_public_case_preserves_human_review_and_all_artifact_bindings(case_path: Path) -> None:
    case = load_case(case_path)
    assert case.validation.status == "accepted"
    assert case.human_review is not None and case.human_review.attested
    assert case.human_review.case_inputs_sha256 == case.review_input_digest()
    build_review_packet(case_path)
    _machine_passed(case, case_path.parent)
    manifest = json.loads((case_path.parent / "publication-manifest.json").read_text())
    assert manifest["manifest_version"] == "1"
    assert manifest["case_id"] == case.id
    expected = {
        "case.json",
        "candidate.json",
        "reference.py",
        "repair.patch",
        "bad.diff",
        "historical_fix.diff",
        "validation.json",
        "upstream-license.txt",
        "upstream-reference-header.txt",
        "official-unittest/base.json",
        "official-unittest/bad.json",
        "official-unittest/repaired.json",
        "official-unittest/summary.json",
    }
    assert set(manifest["files"]) == expected
    actual = {
        path.relative_to(case_path.parent).as_posix()
        for path in case_path.parent.rglob("*")
        if path.is_file()
    }
    assert actual == expected | {"publication-manifest.json"}
    for name, identity in manifest["files"].items():
        path = case_path.parent / name
        assert not path.is_symlink()
        raw = path.read_bytes()
        assert hashlib.sha256(raw).hexdigest() == identity["sha256"]
        assert len(raw) == identity["bytes"]
        assert b"/Users/" not in raw and b"/data/revertbench" not in raw
    summary_raw = read_artifact(case_path.parent, "official-unittest/summary.json", "report")
    summary = json.loads(summary_raw)
    assert summary["status"] == "passed"
    assert summary["validation_report_sha256"] == case.validation.report_sha256
    assert case.env is not None and summary["image"] == case.env.image
    assert tuple(summary["command"]) == case.env.test_cmd
    for role in ("base", "bad", "repaired"):
        assert summary["roles"][role]["source_sha256"] == case.source_hashes[role]
        assert summary["roles"][role]["status"] == "passed"
    assert hashlib.sha256(summary_raw).hexdigest() in case.notes


@pytest.mark.parametrize("case_path", _CASE_PATHS, ids=lambda path: path.parent.name)
def test_reproduction_helper_has_a_versioned_pinned_build_contract(
    case_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.syspath_prepend(str(_CASES_ROOT.parent))
    from scripts.reproduce_case import read_publication

    publication = read_publication(case_path.parent)
    case = load_case(case_path)
    assert re.fullmatch(r"python@sha256:[0-9a-f]{64}", publication["build"]["base_image"])
    assert case.env is not None
    assert publication["build"]["original_worker_image"] == case.env.image
    assert publication["build"]["validated_architecture"]


@pytest.mark.docker
@pytest.mark.parametrize("case_path", _CASE_PATHS, ids=lambda path: path.parent.name)
def test_accepted_case_reproduces_with_its_pinned_grader(
    case_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.syspath_prepend(str(_CASES_ROOT.parent))
    from scripts.reproduce_case import reproduce

    record_root = os.environ.get("REVERTBENCH_REPRO_OUTPUT")
    output = Path(record_root) / case_path.parent.name if record_root else tmp_path / "reproduction"
    result = reproduce(case_path.parent, output=output, cache=tmp_path / "cache")
    assert result["status"] == "reproduced"
    assert result["case_id"] == case_path.parent.name
    assert result["fresh_image"]
    assert result["reference_score"] == 1


def test_pinned_snapshot_includes_readme_required_by_hatch_build(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import io
    import subprocess
    import tarfile

    monkeypatch.syspath_prepend(str(_CASES_ROOT.parent))
    from scripts import reproduce_case

    def archive_command(
        command: list[str], *, cwd: Path, timeout: float
    ) -> subprocess.CompletedProcess[str]:
        assert command[-4:] == ["src/revertbench", "pyproject.toml", "uv.lock", "README.md"]
        archive = Path(
            next(
                value.removeprefix("--output=")
                for value in command
                if value.startswith("--output=")
            )
        )
        with tarfile.open(archive, "w") as output:
            for name, raw in {
                "src/revertbench/__init__.py": b"",
                "pyproject.toml": b"",
                "uv.lock": b"",
                "README.md": b"Pinned public README\n",
            }.items():
                member = tarfile.TarInfo(name)
                member.size = len(raw)
                output.addfile(member, io.BytesIO(raw))
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(reproduce_case, "_run", archive_command)
    snapshot = tmp_path / "snapshot"
    reproduce_case._export_grader(tmp_path, "a" * 40, snapshot)
    assert (snapshot / "README.md").read_text() == "Pinned public README\n"
