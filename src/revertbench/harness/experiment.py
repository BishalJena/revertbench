"""Host-only experiment manifests and sequential, independently identified samples."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

from revertbench.harness.episode import EpisodeIdentity, run_episode
from revertbench.harness.metrics import summarize
from revertbench.harness.prompt import (
    MAX_MODEL_CALLS,
    PR_BODY_POLICY,
    TOOL_DEFINITIONS,
    CompletionPolicy,
    prompt_version,
)
from revertbench.harness.runtime import PinnedRuntime
from revertbench.models.budget import CostBudget
from revertbench.models.client import ProviderClient, config_digest
from revertbench.models.config import load_model
from revertbench.models.ledger import JsonlLedger, atomic_json
from revertbench.models.scheduler import RateScheduler
from revertbench.schema import Case


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _code_identity() -> dict[str, Any]:
    root = Path(__file__).resolve().parents[3]
    commit = (
        subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, check=True, capture_output=True)
        .stdout.decode()
        .strip()
    )
    patch = subprocess.run(
        [
            "git",
            "diff",
            "--binary",
            "--no-ext-diff",
            "--no-textconv",
            "HEAD",
            "--",
            "src",
            "scripts",
            "tests",
            "pyproject.toml",
            "uv.lock",
        ],
        cwd=root,
        check=True,
        capture_output=True,
    ).stdout
    untracked = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    files = {
        name: _sha((root / name).read_bytes())
        for name in untracked
        if Path(name).suffix == ".py" and Path(name).parts[0] in {"src", "scripts", "tests"}
    }
    return {
        "commit": commit,
        "dirty_patch_sha256": _sha(patch),
        "untracked_code_sha256": files,
        "dependency_lock_sha256": _sha((root / "uv.lock").read_bytes()),
        "project_config_sha256": _sha((root / "pyproject.toml").read_bytes()),
        "host_python": sys.version,
    }


def _case_inputs(case_dirs: list[Path]) -> list[dict[str, Any]]:
    if not case_dirs or len(case_dirs) > 5:
        raise ValueError("M4 requires one to five accepted cases")
    rows = []
    for directory in case_dirs:
        path = directory / "case.json"
        if path.stat().st_size > 2 * 1024 * 1024:
            raise ValueError("Case metadata exceeds supported bounds")
        raw = path.read_bytes()
        case = Case.model_validate_json(raw)
        if case.validation.status != "accepted":
            raise ValueError("Experiments require accepted cases")
        rows.append(
            {
                "case_dir": str(directory.resolve()),
                "case_sha256": _sha(raw),
                "case_id": case.id,
                "case_inputs_sha256": case.review_input_digest(),
                "regression_group_id": case.regression_group_id,
                "dataset_split": case.dataset_split,
                "repo": case.repo,
                "private_pins": case.model_dump(
                    mode="json",
                    include={
                        "base_commit",
                        "bad_commit",
                        "fix_commit",
                        "repair",
                        "repair_derivation",
                        "reference_test",
                        "env",
                        "source_hashes",
                        "global_suite_evidence",
                        "validation",
                    },
                ),
            }
        )
    if len({row["regression_group_id"] for row in rows}) != len(rows):
        raise ValueError("Pilot cases must represent distinct regression groups")
    return rows


def _completion_identity(policy: CompletionPolicy) -> str:
    return _sha(
        json.dumps(
            {
                "completion_policy": policy,
                "prompt_version": prompt_version(policy),
            },
            sort_keys=True,
        ).encode()
    )


def _manifest(
    *,
    experiment_id: str,
    inputs: list[dict[str, Any]],
    model: dict[str, Any],
    model_hash: str,
    budget: Decimal,
    completion_policy: CompletionPolicy,
) -> dict[str, Any]:
    episodes = [
        {
            **row,
            "episode_id": f"episode-{index + 1}",
            "sample_id": uuid.uuid4().hex,
            "status": "planned",
            "score": None,
            "publication_window_label": "unknown",
            "publication_window_reason": "hosted revision/cutoff mapping is not verified",
        }
        for index, row in enumerate(inputs)
    ]
    return {
        "manifest_version": "1",
        "experiment_id": experiment_id,
        "created_at": datetime.now(UTC).isoformat(),
        "host_code": _code_identity(),
        "model_config": model,
        "config_sha256": model_hash,
        "completion_policy": completion_policy,
        "prompt_version": prompt_version(completion_policy),
        "completion_identity_sha256": _completion_identity(completion_policy),
        "pr_body_policy": PR_BODY_POLICY,
        "tools_sha256": _sha(json.dumps(TOOL_DEFINITIONS, sort_keys=True).encode()),
        "max_physical_calls_per_sample": MAX_MODEL_CALLS,
        "samples_per_case": 1,
        "budget_usd": str(budget),
        "project_budget_usd": "5",
        "episodes": episodes,
        "metrics": summarize(episodes, planned=len(episodes)),
    }


def run_experiment(
    *,
    case_dirs: list[Path],
    config_path: Path,
    model_id: str,
    output: Path,
    cache: Path,
    experiment_id: str,
    budget_usd: float,
    resume: bool = False,
    completion_policy: CompletionPolicy = "explicit_submit",
) -> dict[str, Any]:
    selected_prompt = prompt_version(completion_policy)
    if not experiment_id.strip() or len(experiment_id) > 128:
        raise ValueError("Experiment identity must be nonempty and bounded")
    output, cache = output.resolve(), cache.resolve()
    config = load_model(config_path, model_id)
    if completion_policy == "saved_test_on_stop_v1" and config.tool_choice != "auto":
        raise ValueError("Saved-test completion policy requires tool_choice=auto")
    model_hash = config_digest(config)
    inputs = _case_inputs(case_dirs)
    limit = Decimal(str(budget_usd))
    manifest_path = output / "manifest.json"
    if manifest_path.exists() and not resume:
        raise ValueError("Existing experiment requires explicit resume")
    if resume and not manifest_path.exists():
        raise ValueError("Experiment checkpoint missing; explicit recovery required")
    if resume:
        checkpoint = json.loads(manifest_path.read_bytes())
        if (
            checkpoint.get("completion_policy", "explicit_submit") != completion_policy
            or checkpoint.get("prompt_version") != selected_prompt
            or checkpoint.get("completion_identity_sha256", _completion_identity("explicit_submit"))
            != _completion_identity(completion_policy)
        ):
            raise ValueError("Experiment completion policy or prompt changed on resume")
    output.mkdir(parents=True, exist_ok=True)
    with (
        JsonlLedger(cache / "project-ledger.jsonl") as project,
        JsonlLedger(output / "trajectory.jsonl") as ledger,
    ):
        budget = CostBudget(project, experiment_id, limit)
        registrations = [
            row
            for row in project.events
            if row["event"] == "experiment_registered" and row["experiment_id"] == experiment_id
        ]
        if resume:
            manifest = cast(dict[str, Any], json.loads(manifest_path.read_bytes()))
            if (
                manifest["host_code"] != _code_identity()
                or manifest["prompt_version"] != selected_prompt
                or manifest.get("completion_policy", "explicit_submit") != completion_policy
                or manifest.get(
                    "completion_identity_sha256", _completion_identity("explicit_submit")
                )
                != _completion_identity(completion_policy)
                or manifest["pr_body_policy"] != PR_BODY_POLICY
                or manifest["tools_sha256"]
                != _sha(json.dumps(TOOL_DEFINITIONS, sort_keys=True).encode())
            ):
                raise ValueError("Experiment implementation or prompt changed on resume")
            expected = [{key: row[key] for key in inputs[0]} for row in manifest["episodes"]]
            if (
                manifest["experiment_id"] != experiment_id
                or manifest["config_sha256"] != model_hash
                or manifest["budget_usd"] != str(limit)
            ):
                raise ValueError("Experiment configuration changed on resume")
            if (
                expected != inputs
                or not registrations
                or registrations[-1]["output"] != str(output)
                or registrations[-1].get("completion_policy", "explicit_submit")
                != completion_policy
                or registrations[-1].get("prompt_version", prompt_version("explicit_submit"))
                != selected_prompt
                or registrations[-1].get(
                    "completion_identity_sha256", _completion_identity("explicit_submit")
                )
                != _completion_identity(completion_policy)
            ):
                raise ValueError("Experiment inputs or durable project accounting changed")
        else:
            if registrations:
                raise ValueError(
                    "Experiment identity already registered; recover its original output"
                )
            manifest = _manifest(
                experiment_id=experiment_id,
                inputs=inputs,
                model=config.model_dump(mode="json"),
                model_hash=model_hash,
                budget=limit,
                completion_policy=completion_policy,
            )
            project.append(
                "experiment_registered",
                experiment_id=experiment_id,
                output=str(output),
                config_sha256=model_hash,
                completion_policy=completion_policy,
                prompt_version=selected_prompt,
                completion_identity_sha256=_completion_identity(completion_policy),
            )
            atomic_json(manifest_path, manifest)
        scheduler = RateScheduler(
            project,
            f"{config.provider.id}:{config.model_id}",
            rpm=config.quotas.rpm,
            tpm=config.quotas.tpm,
            rpd=config.quotas.rpd,
            tpd=config.quotas.tpd,
        )
        client = ProviderClient(config)
        for index, row in enumerate(manifest["episodes"]):
            identity = EpisodeIdentity(experiment_id, row["episode_id"], row["sample_id"])
            runtime = PinnedRuntime(
                Path(row["case_dir"]),
                cache=cache / "engines",
                output=output / "episodes" / row["episode_id"],
                episode_id=row["episode_id"],
            )
            result = run_episode(
                identity=identity,
                config=config,
                client=client,
                runtime=runtime,
                ledger=ledger,
                budget=budget,
                scheduler=scheduler,
                completion_policy=completion_policy,
            )
            manifest["episodes"][index] = {**row, **result}
            manifest["metrics"] = summarize(manifest["episodes"], planned=len(manifest["episodes"]))
            manifest["cost_accounting"] = {
                "experiment": {key: str(value) for key, value in budget.totals().items()},
                "project": {key: str(value) for key, value in budget.totals(project=True).items()},
                "billing_note": (
                    "Usage times recorded unit prices is an estimate, not a provider invoice; "
                    "uncertain usage remains reserved."
                ),
            }
            manifest["updated_at"] = datetime.now(UTC).isoformat()
            atomic_json(manifest_path, manifest)
        return manifest
