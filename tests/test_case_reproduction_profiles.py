"""Version-two publications bind complete fixed-profile suite repeats."""

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

PROFILES = ("boltons_upstream_default_behavior_v1", "pydash_upstream_default_behavior_v1")


def _helper(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1]))
    from scripts import reproduce_case

    return reproduce_case


def _write(root: Path, name: str, value: Any) -> dict[str, Any]:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(value).encode()
    path.write_bytes(raw)
    return {"sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}


def _publication(root: Path, helper: Any, profile: str) -> tuple[dict[str, Any], dict[str, Any]]:
    from revertbench.validate.baseline import baseline_command

    command = list(baseline_command(profile))  # type: ignore[arg-type]
    identity = dict(
        environment_python="3.12.16",
        grader_sha256="a" * 64,
        policy_sha256="b" * 64,
        dependency_lock_sha256="c" * 64,
    )
    case: dict[str, Any] = {
        "id": "synthetic",
        "source_hashes": dict.fromkeys(("base", "bad", "repaired"), "d" * 64),
        "env": {
            "image": "sha256:" + "e" * 64,
            "test_cmd": command,
            "python": identity["environment_python"],
            **{k: v for k, v in identity.items() if k != "environment_python"},
        },
    }
    result = {
        "status": "passed",
        "exit_code": 0,
        "profile": profile,
        "command": command,
        "pids_limit": 256,
        "collected": 2,
        "passed": 2,
        "failed": 0,
        "errors": 0,
        "skipped": 0,
        "xfailed": 0,
        "xpassed": 0,
        "deselected": 0,
        "node_ids": ["tests/t.py::test_a", "pkg/p.py::doc"],
        **identity,
    }
    report = {
        "status": "machine_validated",
        "case_id": case["id"],
        "image": case["env"]["image"],
        "baseline_profile": profile,
        "baseline_command": command,
        "baseline_pids_limit": 256,
        "source_hashes": case["source_hashes"],
        "baseline_reports": {role: result.copy() for role in case["source_hashes"]},
        **identity,
    }
    files = {
        name: _write(root, name, {})
        for name in (
            "candidate.json",
            "reference.py",
            "repair.patch",
            "bad.diff",
            "historical_fix.diff",
            "upstream-license.txt",
            "upstream-reference-header.txt",
        )
    }
    files["case.json"] = _write(root, "case.json", case)
    files["validation.json"] = _write(root, "validation.json", report)
    roles = {}
    for role in case["source_hashes"]:
        path = f"upstream-suite/{role}.json"
        files[path] = _write(root, path, result)
        roles[role] = {"path": path, **files[path], "source_sha256": case["source_hashes"][role]}
    summary = {
        "version": "1",
        "evidence_kind": "structured_baseline_repeat",
        "independent_instrumentation": False,
        "case_id": case["id"],
        "profile": profile,
        "command": command,
        "pids_limit": 256,
        "image": case["env"]["image"],
        "validation_report_sha256": files["validation.json"]["sha256"],
        "source_hashes": case["source_hashes"],
        "status": "passed",
        "roles": roles,
        "scope": helper._upstream_scope(profile),
        **identity,
    }
    files["upstream-suite/summary.json"] = _write(root, "upstream-suite/summary.json", summary)
    manifest = {
        "manifest_version": "2",
        "case_id": case["id"],
        "files": files,
        "build": {"base_image": "python@sha256:" + "f" * 64},
    }
    _write(root, "publication-manifest.json", manifest)
    return case, report


@pytest.mark.parametrize("profile", PROFILES)
def test_v2_complete_repeat_and_pinned_registry_are_required(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, profile: str
) -> None:
    from revertbench.validate.baseline import baseline_command

    helper = _helper(monkeypatch)
    case, report = _publication(tmp_path, helper, profile)
    assert helper.read_publication(tmp_path)["manifest_version"] == "2"
    evidence = helper._upstream_evidence(tmp_path, case, report, baseline_command)
    assert evidence["independent_instrumentation"] is False
    assert evidence["roles"]["base"]["source_sha256"] == case["source_hashes"]["base"]


@pytest.mark.parametrize(
    "change",
    [
        "command",
        "ignore",
        "flags",
        "profile",
        "pid",
        "source",
        "report",
        "report_case",
        "report_image",
        "environment",
        "node_ids",
        "counts",
        "role_digest",
        "independent",
        "scope",
    ],
)
def test_tampered_repeat_rejected_even_with_updated_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    from revertbench.validate.baseline import baseline_command

    helper = _helper(monkeypatch)
    case, report = _publication(tmp_path, helper, PROFILES[1])
    summary = json.loads((tmp_path / "upstream-suite/summary.json").read_bytes())
    if change == "command":
        summary["command"] = [*summary["command"][:-1], "other_tests"]
    elif change == "ignore":
        summary["command"] = [
            part for part in summary["command"] if not part.startswith("--ignore=")
        ]
    elif change == "flags":
        summary["command"].remove("--doctest-modules")
    elif change == "profile":
        summary["profile"] = "unregistered_profile"
    elif change == "pid":
        summary["pids_limit"] = 64
    elif change == "source":
        summary["source_hashes"]["base"] = "0" * 64
    elif change == "report":
        summary["validation_report_sha256"] = "0" * 64
    elif change in {"report_case", "report_image"}:
        report["case_id" if change == "report_case" else "image"] = "different"
        summary["validation_report_sha256"] = _write(tmp_path, "validation.json", report)["sha256"]
    elif change == "environment":
        summary["policy_sha256"] = "0" * 64
    elif change == "role_digest":
        summary["roles"]["bad"]["sha256"] = "0" * 64
    elif change == "independent":
        summary["independent_instrumentation"] = True
    elif change == "scope":
        summary["scope"] = {"all_repository_checks": True}
    else:
        role = json.loads((tmp_path / "upstream-suite/bad.json").read_bytes())
        role["node_ids" if change == "node_ids" else "passed"] = (
            ["other"] if change == "node_ids" else 1
        )
        descriptor = _write(tmp_path, "upstream-suite/bad.json", role)
        summary["roles"]["bad"].update(descriptor)
    _write(tmp_path, "upstream-suite/summary.json", summary)
    with pytest.raises(helper.ReproductionError):
        helper._upstream_evidence(tmp_path, case, report, baseline_command)


def test_v2_extra_artifacts_and_legacy_engine_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    helper = _helper(monkeypatch)
    case, report = _publication(tmp_path, helper, PROFILES[0])
    with pytest.raises(helper.ReproductionError, match=r"pinned|registry"):
        helper._upstream_evidence(tmp_path, case, report, None)
    (tmp_path / "official-unittest").mkdir()
    (tmp_path / "official-unittest/summary.json").write_text("{}")
    with pytest.raises(helper.ReproductionError, match="allowlist"):
        helper.read_publication(tmp_path)


def test_v1_minimal_publication_unchanged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    helper = _helper(monkeypatch)
    manifest = {
        "manifest_version": "1",
        "files": {"case.json": _write(tmp_path, "case.json", {})},
        "build": {"base_image": "python@sha256:" + "a" * 64},
    }
    _write(tmp_path, "publication-manifest.json", manifest)
    assert helper.read_publication(tmp_path) == manifest


def test_fixed_registry_cannot_be_replaced_by_consistent_tampered_commands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from revertbench.validate.baseline import baseline_command

    helper = _helper(monkeypatch)
    case, report = _publication(tmp_path, helper, PROFILES[1])
    case["env"]["test_cmd"].remove("--ignore=tests/pytest_mypy_testing")
    report["baseline_command"] = case["env"]["test_cmd"]
    for result in report["baseline_reports"].values():
        result["command"] = case["env"]["test_cmd"]
    summary = json.loads((tmp_path / "upstream-suite/summary.json").read_bytes())
    summary["command"] = case["env"]["test_cmd"]
    summary["validation_report_sha256"] = _write(tmp_path, "validation.json", report)["sha256"]
    for role in summary["roles"]:
        summary["roles"][role].update(
            _write(tmp_path, f"upstream-suite/{role}.json", report["baseline_reports"][role])
        )
    _write(tmp_path, "upstream-suite/summary.json", summary)
    with pytest.raises(helper.ReproductionError, match="registry"):
        helper._upstream_evidence(tmp_path, case, report, baseline_command)


def test_new_profile_fresh_suite_dispatch_and_full_node_comparison(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    from revertbench.validate.baseline import baseline_command

    helper = _helper(monkeypatch)
    case, report = _publication(tmp_path, helper, PROFILES[0])
    output = tmp_path / "output"
    output.mkdir()
    _write(output, "validation.json", report)
    seen: list[Path] = []
    trees = SimpleNamespace(
        **{f"{role}_tree": tmp_path / role for role in ("base", "bad", "repaired")}
    )

    class Backend:
        def __init__(self, image: str, **kwargs: Any) -> None:
            assert image == "fresh-image"
            assert kwargs["profile"] == PROFILES[0]
            assert kwargs["limits"].pids == 256

        def __call__(self, tree: Path) -> Any:
            seen.append(tree)
            return SimpleNamespace(
                model_dump=lambda **kwargs: report["baseline_reports"][tree.name]
            )

    module = SimpleNamespace(baseline_command=baseline_command, DockerBaselineRunner=Backend)
    monkeypatch.setattr(
        helper, "_official_suite", lambda *args, **kwargs: pytest.fail("legacy unittest invoked")
    )
    result = helper._reproduce_suites(
        "2", module, trees, "fresh-image", object(), case, report, output, tmp_path
    )
    assert result["suite_evidence_kind"] == "structured_baseline_repeat"
    assert result["independent_instrumentation"] is False
    assert len(seen) == 3
    assert (
        result["upstream_suites"]["bad"]["node_ids"]
        == report["baseline_reports"]["bad"]["node_ids"]
    )
    assert (output / "upstream-suite/summary.json").is_file()
    changed = {
        **report,
        "baseline_reports": {
            role: {**value, "node_ids": ["wrong", "same_count"]}
            for role, value in report["baseline_reports"].items()
        },
    }
    with pytest.raises(helper.ReproductionError):
        helper._check_fresh_upstream(changed, report)


def test_legacy_case1_can_repeat_unittest_while_primary_is_pytest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    helper = _helper(monkeypatch)
    command = ["/usr/local/bin/python", "-m", "unittest", "-q"]
    case = {"env": {"test_cmd": command}}
    report = {"baseline_profile": "pytest"}
    _write(
        tmp_path,
        "official-unittest/summary.json",
        {"roles": {role: {"tests_run": 850} for role in ("base", "bad", "repaired")}},
    )
    seen = []

    def official(
        tree: Path, image: str, argv: tuple[str, ...], client: Any, **kwargs: Any
    ) -> dict[str, Any]:
        assert argv == tuple(command) and kwargs["baseline_pids_limit"] == 64
        seen.append(tree)
        return {"tests_run": 850, "status": "passed"}

    monkeypatch.setattr(helper, "_official_suite", official)
    trees = SimpleNamespace(
        base_tree=tmp_path / "base", bad_tree=tmp_path / "bad", repaired_tree=tmp_path / "repaired"
    )
    result = helper._reproduce_suites(
        "1", None, trees, "image", object(), case, report, tmp_path, tmp_path
    )
    assert len(seen) == 3
    assert set(result) == {"official_command", "official_suites"}


@pytest.mark.parametrize("derived", [False, True])
def test_v2_exact_allowlist_optional_historical_proof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, derived: bool
) -> None:
    helper = _helper(monkeypatch)
    case, _ = _publication(tmp_path, helper, PROFILES[0])
    manifest = json.loads((tmp_path / "publication-manifest.json").read_bytes())
    descriptor = _write(tmp_path, "historical-production.diff", {"synthetic": True})
    manifest["files"]["historical-production.diff"] = descriptor
    if derived:
        case["repair_derivation"] = {
            "version": "historical_function_body_v1",
            "historical_diff": {
                "path": "historical-production.diff",
                "sha256": descriptor["sha256"],
            },
        }
        manifest["files"]["case.json"] = _write(tmp_path, "case.json", case)
    _write(tmp_path, "publication-manifest.json", manifest)
    if derived:
        assert helper.read_publication(tmp_path)["manifest_version"] == "2"
    else:
        with pytest.raises(helper.ReproductionError, match="allowlist"):
            helper.read_publication(tmp_path)


@pytest.mark.parametrize(
    "field,value",
    [
        ("exit_code", 1),
        ("failed", 1),
        ("collected", 3),
        ("node_ids", ["same", "same"]),
        ("collection_errors", ["broken"]),
    ],
)
def test_repeat_cannot_claim_passed_with_inconsistent_results(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, field: str, value: Any
) -> None:
    helper = _helper(monkeypatch)
    _, report = _publication(tmp_path, helper, PROFILES[0])
    result = {**report["baseline_reports"]["base"], field: value}
    with pytest.raises(helper.ReproductionError):
        helper._matching_suite(
            result,
            result,
            profile=PROFILES[0],
            command=report["baseline_command"],
            pids=256,
            identity=report,
        )
