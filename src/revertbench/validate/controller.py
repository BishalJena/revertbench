"""Machine evidence for a built case; human acceptance is deliberately separate."""

from __future__ import annotations

import ast
import hashlib
import re
from pathlib import Path
from typing import Literal, Protocol

import docker
from pydantic import Field

from revertbench import __version__
from revertbench.build import BuildError, BuiltCandidate, canonical_tree_digest
from revertbench.grade.backend import ExecutionBackend
from revertbench.grade.policy import POLICY_VERSION, check_submission
from revertbench.grade.rules import grade_submission
from revertbench.grade.validation import validate_reference
from revertbench.sandbox.agent import AgentSandbox
from revertbench.sandbox.worker import DockerRunner, SandboxError
from revertbench.schema import ContentDigest, EvidenceModel, GradeResult
from revertbench.validate.baseline import BaselineBackend, BaselineResult, DockerBaselineRunner


class ProbeResult(EvidenceModel):
    status: Literal["passed", "failed", "invalid", "not_applicable"]
    reason: str
    grade: GradeResult | None = None


class MachineValidationReport(EvidenceModel):
    report_version: Literal["1"] = "1"
    status: Literal["machine_validated", "rejected", "pending_infrastructure"]
    case_id: str
    reason: str
    image: str
    environment_python: str | None = None
    dependency_lock_sha256: ContentDigest | None = None
    grader_sha256: ContentDigest | None = None
    policy_sha256: ContentDigest | None = None
    grader_version: str = __version__
    policy_version: str = POLICY_VERSION
    validator_version: Literal["1"] = "1"
    probe_scope: Literal["case_specific"] = "case_specific"
    global_suite_evidence: Literal["separate_ci_required"] = "separate_ci_required"
    source_hashes: dict[str, str]
    patch_sha256: str
    reference_sha256: str
    baseline_reports: dict[str, BaselineResult] = Field(default_factory=dict)
    reference: GradeResult | None = None
    probes: dict[str, ProbeResult] = Field(default_factory=dict)
    human_review: Literal["pending"] = "pending"


class TransferBackend(Protocol):
    def __call__(self, tree: Path, source: str) -> str: ...


def _transfer_probe(image: str, client: docker.DockerClient | None, tree: Path, source: str) -> str:
    """Alter only agent workspace, then retrieve only the fixed submission artifact."""
    with AgentSandbox(image, tree, client=client) as box:
        script = (
            "from pathlib import Path; "
            "p=Path('/workspace/repo'); "
            "(p/'pytest.ini').write_text('[pytest]\\naddopts=--collect-only\\n'); "
            "[(q.write_text('# agent changed source\\n')) "
            "for q in p.rglob('*.py') if 'tests' not in q.parts]; "
            "t=p/'tests/test_revertbench_submission.py'; t.parent.mkdir(exist_ok=True); "
            f"t.write_text({source!r}, encoding='utf-8')"
        )
        result = box.exec(["/usr/local/bin/python", "-I", "-c", script], timeout_seconds=10)
        if result.status != "completed" or result.exit_code != 0:
            raise SandboxError("transfer_probe_failed")
        return box.submission_bytes().decode("utf-8")


def _symbols(path: Path) -> set[str]:
    if not path.is_file():
        return set()
    data = path.read_bytes()
    cookie_pattern = rb"^[ \t\f]*\#.*?coding[:=][ \t]*([-\w.]+)"
    for line in data.splitlines()[:2]:
        cookie = re.match(cookie_pattern, line)
        if cookie and cookie.group(1).lower().replace(b"_", b"-") not in {b"utf-8", b"utf8"}:
            raise BuildError("unsupported_production_source")
    try:
        if data.startswith(b"\xef\xbb\xbf") or len(data) > 4_194_304:
            raise BuildError("unsupported_production_source")
        module = ast.parse(data.decode("utf-8"))
    except (SyntaxError, ValueError, RecursionError, UnicodeError) as error:
        raise BuildError("unsupported_production_source") from error
    result: set[str] = set()
    for node in module.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if not node.name.startswith("_"):
                result.add(node.name)
                if isinstance(node, ast.ClassDef):
                    result.update(
                        f"{node.name}.{child.name}"
                        for child in node.body
                        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                        and not child.name.startswith("_")
                    )
        elif isinstance(node, ast.Assign):
            result.update(
                target.id
                for target in node.targets
                if isinstance(target, ast.Name) and not target.id.startswith("_")
            )
    return result


def _fix_only_source(built: BuiltCandidate) -> str | None:
    for name in built.candidate.modified_source_paths:
        added = _symbols(built.repaired_tree / name) - _symbols(built.bad_tree / name)
        parts = list(Path(name).with_suffix("").parts)
        if parts and parts[0] == "src":
            parts.pop(0)
        if parts and parts[-1] == "__init__":
            parts.pop()
        if added and parts and all(part.isidentifier() for part in parts):
            module, symbol = ".".join(parts), sorted(added)[0]
            return f"import {module}\ndef test_fix_only():\n    assert {module}.{symbol}\n"
    return None


def _probe_grade(built: BuiltCandidate, source: str, execute: ExecutionBackend) -> ProbeResult:
    result = grade_submission(
        built.bad_tree, built.repaired_tree, source, built.changed_lines, execution_backend=execute
    )
    status: Literal["passed", "failed", "invalid"] = (
        "invalid" if result.status == "invalid_run" else "passed" if result.score == 0 else "failed"
    )
    return ProbeResult(status=status, reason=result.reason, grade=result)


def validate_built_candidate(
    built: BuiltCandidate,
    *,
    image: str,
    client: docker.DockerClient | None = None,
    baseline_backend: BaselineBackend | None = None,
    execution_backend: ExecutionBackend | None = None,
    transfer_backend: TransferBackend | None = None,
) -> MachineValidationReport:
    """Require existing suites, differential reference, and explicit adversarial probes."""
    baselines: dict[str, BaselineResult] = {}
    probes: dict[str, ProbeResult] = {}
    reference: GradeResult | None = None

    def report(
        status: Literal["machine_validated", "rejected", "pending_infrastructure"], reason: str
    ) -> MachineValidationReport:
        return MachineValidationReport(
            status=status,
            reason=reason,
            case_id=built.case_id,
            image=image,
            environment_python=next(
                (value.environment_python for value in baselines.values()), None
            ),
            dependency_lock_sha256=next(
                (value.dependency_lock_sha256 for value in baselines.values()), None
            ),
            grader_sha256=next((value.grader_sha256 for value in baselines.values()), None),
            policy_sha256=next((value.policy_sha256 for value in baselines.values()), None),
            source_hashes=built.source_hashes,
            patch_sha256=built.patch_sha256,
            reference_sha256=hashlib.sha256(built.reference_source.encode("utf-8")).hexdigest(),
            baseline_reports=baselines,
            reference=reference,
            probes=probes,
        )

    if re.fullmatch(r"(?:sha256:[0-9a-f]{64}|[^\s@]+@sha256:[0-9a-f]{64})", image) is None:
        return report("pending_infrastructure", "unpinned_image")
    trees = {
        "base": built.base_tree,
        "bad": built.bad_tree,
        "historical_fix": built.historical_fix_tree,
        "repaired": built.repaired_tree,
    }
    try:
        if any(
            canonical_tree_digest(tree) != built.source_hashes[role] for role, tree in trees.items()
        ):
            return report("rejected", "source_identity_changed")
        if hashlib.sha256(built.production_patch.encode("utf-8")).hexdigest() != built.patch_sha256:
            return report("rejected", "patch_identity_changed")
        policy = check_submission(built.reference_source, built.bad_tree)
        if not policy.allowed:
            return report("rejected", "reference_policy_rejected")
        baseline = baseline_backend or DockerBaselineRunner(image, client=client)
        for role in ("base", "bad", "repaired"):
            baselines[role] = baseline(trees[role])
        if any(value.status == "invalid_run" for value in baselines.values()):
            return report("pending_infrastructure", "baseline_infrastructure")
        if any(value.status != "passed" for value in baselines.values()):
            reason = (
                "unsupported_environment"
                if any(value.reason == "unsupported_environment" for value in baselines.values())
                else "existing_suite_failed"
            )
            return report("rejected", reason)
        identities = {
            (
                value.environment_python,
                value.dependency_lock_sha256,
                value.grader_sha256,
                value.policy_sha256,
            )
            for value in baselines.values()
        }
        if len(identities) != 1:
            return report("pending_infrastructure", "baseline_environment_mismatch")
        execute = execution_backend or DockerRunner(image, client=client)
        reference = validate_reference(
            built.base_tree,
            built.bad_tree,
            built.repaired_tree,
            built.reference_source,
            built.changed_lines,
            execution_backend=execute,
        )
        if reference.status == "invalid_run":
            return report("pending_infrastructure", "reference_infrastructure")
        if reference.status != "passed":
            return report("rejected", "reference_not_a_regression")
        sources = {
            "empty": "",
            "assert_true": "def test_cheat():\n    assert True\n",
            "assert_false": "def test_cheat():\n    assert False\n",
            "import_error": (
                "import revertbench_nonexistent_target\ndef test_cheat():\n    assert True\n"
            ),
        }
        fix_only = _fix_only_source(built)
        if fix_only is None:
            probes["fix_only"] = ProbeResult(status="not_applicable", reason="no_added_symbols")
        else:
            sources["fix_only"] = fix_only
        for name, source in sources.items():
            probes[name] = _probe_grade(built, source, execute)
        probes["stability"] = ProbeResult(
            status="passed", reason="reference_three_seeds_identical", grade=reference
        )
        transfer = transfer_backend or (
            lambda tree, source: _transfer_probe(image, client, tree, source)
        )
        transferred = transfer(built.bad_tree, built.reference_source)
        if transferred != built.reference_source:
            probes["config_tamper"] = ProbeResult(
                status="failed", reason="submission_transfer_changed"
            )
        else:
            checked = validate_reference(
                built.base_tree,
                built.bad_tree,
                built.repaired_tree,
                transferred,
                built.changed_lines,
                execution_backend=execute,
            )
            probes["config_tamper"] = ProbeResult(
                status="passed"
                if checked.status == "passed"
                else "invalid"
                if checked.status == "invalid_run"
                else "failed",
                reason="only_submission_transferred",
                grade=checked,
            )
        if any(probe.status == "invalid" for probe in probes.values()):
            return report("pending_infrastructure", "probe_infrastructure")
        if any(probe.status == "failed" for probe in probes.values()):
            return report("rejected", "adversarial_probe_failed")
        if any(
            canonical_tree_digest(tree) != built.source_hashes[role] for role, tree in trees.items()
        ):
            return report("rejected", "source_identity_changed")
        return report("machine_validated", "all_machine_checks_passed")
    except BuildError:
        return report("rejected", "unsupported_production_source")
    except (OSError, SandboxError) as error:
        probes["transport"] = ProbeResult(status="invalid", reason=type(error).__name__)
        return report("pending_infrastructure", "validation_transport_failed")
