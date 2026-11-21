from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from revertbench import __version__
from revertbench.cli import app


def test_version_command_prints_package_version() -> None:
    result = CliRunner().invoke(app, ["version"])

    assert result.exit_code == 0
    assert result.output.strip() == __version__


def test_pipeline_commands_and_review_help_are_discoverable() -> None:
    runner = CliRunner()
    for command in ("mine", "build", "validate", "review"):
        result = runner.invoke(app, [command, "--help"])
        assert result.exit_code == 0, result.output


def test_mine_dispatches_without_credentials_or_prompts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from revertbench import cli

    calls: list[dict[str, Any]] = []

    def capture(**kwargs: Any) -> int:
        calls.append(kwargs)
        return 2

    monkeypatch.setattr(cli, "_mine_candidates", capture)
    result = CliRunner().invoke(app, ["mine", "owner/toy", "--output-dir", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert calls[0]["repo"] == "owner/toy"
    assert "2 candidate" in result.output


def test_build_and_validate_dispatch_explicit_images(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from revertbench import cli

    candidate = tmp_path / "candidate.json"
    candidate.write_text("{}")
    calls: list[dict[str, Any]] = []

    def capture(**kwargs: Any) -> Path:
        calls.append(kwargs)
        return tmp_path / "case.json"

    monkeypatch.setattr(cli, "_process_candidate", capture)
    runner = CliRunner()
    for command in ("build", "validate"):
        result = runner.invoke(app, [command, str(candidate), "--image", "sha256:" + "a" * 64])
        assert result.exit_code == 0, result.output
    assert [call["validate"] for call in calls] == [False, True]


def test_review_is_read_only_by_default_and_human_flags_are_explicit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from revertbench import cli
    from revertbench.review import ReviewPacket

    path = tmp_path / "case.json"
    path.write_text("{}")
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        cli,
        "build_review_packet",
        lambda path: ReviewPacket(
            case_id="toy", status="pending_human_review", case_inputs_sha256="a" * 64, sections=()
        ),
    )
    monkeypatch.setattr(cli, "record_review", lambda *args, **kwargs: calls.append(kwargs))
    result = CliRunner().invoke(app, ["review", str(path)])
    assert result.exit_code == 0 and not calls
    for flags in (
        ["--accept"],
        ["--accept", "--reviewer", "Reviewer", "--notes", "Inspected"],
        ["--accept", "--reject"],
    ):
        result = CliRunner().invoke(app, ["review", str(path), *flags])
        assert result.exit_code != 0 and not calls
    result = CliRunner().invoke(
        app,
        [
            "review",
            str(path),
            "--accept",
            "--reviewer",
            "Reviewer",
            "--notes",
            "Inspected",
            "--attest-human-review",
        ],
    )
    assert result.exit_code == 0, result.output
    assert calls[0]["attested"] is True


def test_agent_review_cli_requires_explicit_kind_and_authorization(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from revertbench import cli
    from revertbench.review import ReviewPacket

    path = tmp_path / "case.json"
    path.write_text("{}")
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(cli, "record_review", lambda *args, **kwargs: calls.append(kwargs))
    monkeypatch.setattr(
        cli,
        "build_review_packet",
        lambda path: ReviewPacket(
            case_id="toy", status="pending_review", case_inputs_sha256="a" * 64, sections=()
        ),
    )
    base = [
        "review",
        str(path),
        "--accept",
        "--reviewer-kind",
        "agent",
        "--reviewer",
        "Codex reviewer",
        "--notes",
        "Inspected",
    ]
    for extra in ([], ["--authorization-note", "User delegated review", "--attest-human-review"]):
        result = CliRunner().invoke(app, base + extra)
        assert result.exit_code != 0 and not calls
    result = CliRunner().invoke(app, [*base, "--authorization-note", "User delegated review"])
    assert result.exit_code == 0, result.output
    assert (
        calls[0]["reviewer_kind"] == "agent"
        and calls[0]["authorization_note"] == "User delegated review"
    )
    assert calls[0]["attested"] is False


def test_cli_errors_do_not_print_untrusted_exception_payload(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from revertbench import cli

    def fail(**kwargs: Any) -> int:
        raise ValueError("SYNTHETIC_PRIVATE_SENTINEL")

    monkeypatch.setattr(cli, "_mine_candidates", fail)
    result = CliRunner().invoke(app, ["mine", "owner/toy"])
    assert result.exit_code != 0
    assert "SYNTHETIC_PRIVATE_SENTINEL" not in result.output


@pytest.mark.parametrize("reviewer_kind", ["human", "agent"])
@pytest.mark.parametrize("legacy_history", ["present", "empty", "missing"])
def test_pipeline_persists_machine_pending_packet_without_review_decision(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, reviewer_kind: str, legacy_history: str
) -> None:
    import hashlib

    from revertbench import build, cli, validate
    from revertbench.build import BuiltCandidate
    from revertbench.mine.models import MiningCandidate, candidate_to_json
    from revertbench.review import load_case
    from revertbench.schema import Case, GradeResult
    from revertbench.validate import MachineValidationReport, ProbeResult
    from revertbench.validate.baseline import BaselineResult, baseline_command

    source = "def test_api():\n    assert True\n"
    patch = "trusted synthetic patch\n"
    digest = hashlib.sha256(patch.encode()).hexdigest()
    candidate = MiningCandidate(
        repo="owner/toy",
        fix_commit="3" * 40,
        fix_parent="4" * 40,
        base_commit="1" * 40,
        bad_commit="2" * 40,
        reference_path="tests/test_api.py",
        reference_node_id="tests/test_api.py::test_api",
        reference_source=source,
        production_patch=patch,
        modified_source_paths=("toy.py",),
        license_path="LICENSE",
        license_text="Synthetic upstream license",
    )
    candidate_path = tmp_path / "candidate.json"
    candidate_path.write_text(candidate_to_json(candidate))
    built = BuiltCandidate(
        case_id="toy",
        repo=candidate.repo,
        base_commit="1" * 40,
        bad_commit="2" * 40,
        fix_commit="3" * 40,
        fix_parent="4" * 40,
        base_tree=tmp_path,
        bad_tree=tmp_path,
        historical_fix_tree=tmp_path,
        repaired_tree=tmp_path,
        reference_source=source,
        reference_path=candidate.reference_path,
        reference_node_id=candidate.reference_node_id,
        production_patch=patch,
        patch_sha256=digest,
        source_hashes={role: "a" * 64 for role in ("base", "bad", "historical_fix", "repaired")},
        changed_lines={"toy.py": (2,)},
        candidate=candidate,
    )

    class Repository:
        def diff(self, *args: str) -> str:
            return "synthetic diff\n"

    monkeypatch.setattr(cli, "_repository", lambda *args: Repository())
    monkeypatch.setattr(build, "materialize_candidate", lambda *args: built)
    observed: list[dict[str, Any]] = []

    def validate_fake(*args: Any, **kwargs: Any) -> MachineValidationReport:
        observed.append(kwargs)
        return MachineValidationReport(
            status="machine_validated",
            reason="all_machine_checks_passed",
            case_id="toy",
            image="sha256:" + "b" * 64,
            baseline_profile=kwargs["baseline_profile"],
            baseline_command=baseline_command(kwargs["baseline_profile"]),
            environment_python="3.12.15",
            dependency_lock_sha256="c" * 64,
            source_hashes=built.source_hashes,
            patch_sha256=digest,
            reference_sha256=hashlib.sha256(source.encode()).hexdigest(),
            baseline_reports={
                role: BaselineResult(status="passed") for role in ("base", "bad", "repaired")
            },
            reference=GradeResult(status="passed", score=1, reason="passed"),
            probes={"empty": ProbeResult(status="passed", reason="checked")},
        )

    monkeypatch.setattr(validate, "validate_built_candidate", validate_fake)
    path = cli._process_candidate(
        candidate_path=candidate_path,
        cache_dir=tmp_path / "cache",
        work_dir=tmp_path / "work",
        output_dir=tmp_path / "packets",
        local_repo=tmp_path,
        image="sha256:" + "b" * 64,
        good_commit=None,
        validate=True,
        baseline_profile="unittest",
    )
    case = load_case(path)
    assert case.validation.status == "pending_review"
    assert case.human_review is None and case.agent_review is None
    assert case.eligibility == "pending_metadata"
    assert case.lineage_verified is True
    assert case.env is not None and case.env.python == "3.12.15"
    assert case.env.test_cmd == baseline_command("unittest")
    assert (path.parent / "upstream-license.txt").read_text() == "Synthetic upstream license"
    assert len(observed) == 1

    from datetime import UTC, datetime

    from revertbench.review import write_case
    from revertbench.schema import (
        AgentReview,
        HumanReview,
        PublicationEvidence,
        PullRequestMetadata,
    )

    prior = HumanReview(
        decision="reject",
        reviewer="Actual reviewer",
        notes="Historical note.",
        reviewed_at=datetime.now(UTC),
    )
    assert case.validation.report_sha256 is not None
    agent_prior = AgentReview(
        decision="reject",
        reviewer="Codex",
        notes="Historical agent observation.",
        authorization_note="User delegated review",
        reviewed_at=datetime.now(UTC),
        validation_report_sha256=case.validation.report_sha256,
        case_inputs_sha256=case.review_input_digest(),
    )
    original = Case.model_validate(
        {
            **case.model_dump(mode="python"),
            "bad_pr": PullRequestMetadata(number=7, url="https://github.com/owner/toy/pull/7"),
            "publication_evidence": (
                PublicationEvidence(
                    artifact="issue",
                    url="https://github.com/owner/toy/issues/8",
                    observed_at=datetime.now(UTC),
                ),
            ),
            "dataset_split": "validation",
            "notes": "User-maintained provenance note",
            "human_review": prior if reviewer_kind == "human" else None,
            "agent_review": agent_prior if reviewer_kind == "agent" else None,
            "review_history": (prior,) if legacy_history == "present" else (),
            "agent_review_history": (agent_prior,) if legacy_history == "present" else (),
        }
    )
    write_case(original, path)
    if legacy_history == "missing":
        import json

        old_data = json.loads(path.read_text())
        old_data.pop("review_history")
        old_data.pop("agent_review_history")
        path.write_text(json.dumps(old_data))
    revalidated_path = cli._process_candidate(
        candidate_path=path,
        cache_dir=tmp_path / "cache",
        work_dir=tmp_path / "work",
        output_dir=tmp_path / "new-packets",
        local_repo=tmp_path,
        image="sha256:" + "b" * 64,
        good_commit=None,
        validate=True,
    )
    revalidated = load_case(revalidated_path)
    assert revalidated.bad_pr == original.bad_pr
    assert revalidated.publication_evidence == original.publication_evidence
    assert revalidated.dataset_split == "validation" and revalidated.notes == original.notes
    assert (
        revalidated.human_review is None
        and revalidated.agent_review is None
        and revalidated.validation.status == "pending_review"
    )
    expected_human = original.review_history
    expected_agent = original.agent_review_history
    if reviewer_kind == "human" and prior not in expected_human:
        expected_human = (*expected_human, prior)
    if reviewer_kind == "agent" and agent_prior not in expected_agent:
        expected_agent = (*expected_agent, agent_prior)
    assert revalidated.review_history == expected_human
    assert revalidated.agent_review_history == expected_agent


def test_mine_retains_bounded_discovery_funnel(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import json

    from revertbench import cli
    from revertbench.mine import candidates

    report = candidates.DiscoveryReport(
        candidates=(),
        examined_commits=7,
        matched_fixes=3,
        rejections=(("a" * 40, "unsupported_reference_fixture"),),
    )
    monkeypatch.setattr(cli, "_repository", lambda *args: object())
    monkeypatch.setattr(candidates, "discover_report", lambda *args, **kwargs: report)
    count = cli._mine_candidates(
        repo="owner/toy",
        cache_dir=tmp_path / "cache",
        output_dir=tmp_path / "out",
        local_repo=tmp_path,
        limit=20,
        max_commits=10,
    )
    assert count == 0
    stored = json.loads((tmp_path / "out" / "discovery.json").read_text())
    assert stored["discovery_version"] == "1"
    assert stored["examined_commits"] == 7 and stored["matched_fixes"] == 3
    assert stored["candidate_count"] == 0
    assert stored["rejections"][0]["commit"] == "a" * 40
    assert stored["rejections"][0]["reason"] == "unsupported_reference_fixture"


def test_cli_history_limit_matches_miner_maximum(monkeypatch: pytest.MonkeyPatch) -> None:
    from revertbench import cli

    called: list[object] = []
    monkeypatch.setattr(cli, "_mine_candidates", lambda **kwargs: called.append(kwargs))
    result = CliRunner().invoke(app, ["mine", "owner/toy", "--max-commits", "10001"])
    assert result.exit_code != 0 and not called


def test_validate_selects_explicit_fixed_baseline_profile(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from revertbench import cli

    candidate = tmp_path / "candidate.json"
    candidate.write_text("{}")
    calls: list[dict[str, Any]] = []

    def capture(**kwargs: Any) -> Path:
        calls.append(kwargs)
        return tmp_path / "case.json"

    monkeypatch.setattr(cli, "_process_candidate", capture)
    result = CliRunner().invoke(
        app,
        [
            "validate",
            str(candidate),
            "--image",
            "sha256:" + "a" * 64,
            "--baseline-profile",
            "unittest",
        ],
    )
    assert result.exit_code == 0, result.output
    assert calls[0]["baseline_profile"] == "unittest"
    bad = CliRunner().invoke(
        app,
        [
            "validate",
            str(candidate),
            "--image",
            "sha256:" + "a" * 64,
            "--baseline-profile",
            "arbitrary",
        ],
    )
    assert bad.exit_code != 0
    assert len(calls) == 1


def test_validate_selects_bounded_upstream_pid_limit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from revertbench import cli

    calls: list[dict[str, Any]] = []

    def capture(**kwargs: Any) -> Path:
        calls.append(kwargs)
        return tmp_path / "case.json"

    monkeypatch.setattr(cli, "_process_candidate", capture)
    base = ["validate", str(tmp_path / "candidate.json"), "--image", "sha256:" + "a" * 64]
    for extra, expected in (([], 64), (["--baseline-pids-limit", "256"], 256)):
        result = CliRunner().invoke(app, base + extra)
        assert result.exit_code == 0, result.output
        assert calls[-1]["baseline_pids_limit"] == expected
    for value in ["7", "257", "3.5"]:
        assert CliRunner().invoke(app, [*base, "--baseline-pids-limit", value]).exit_code != 0
    assert len(calls) == 2
