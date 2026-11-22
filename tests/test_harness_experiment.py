from pathlib import Path
from typing import Any, Final, cast

import pytest

from revertbench.harness import experiment
from revertbench.harness.prompt import CompletionPolicy
from test_harness_episode import FakeClient, FakeRuntime, model_config, response


class ExperimentRuntime(FakeRuntime):
    def __init__(self, case_dir: Path, *, cache: Path, output: Path, episode_id: str) -> None:
        super().__init__()
        self.episode_id = episode_id

    def prepare(self) -> dict[str, Any]:
        result = super().prepare()
        result["safe_task"]["episode_id"] = self.episode_id
        return result

    def tool(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        self.tools.append(name)
        if name == "write_test":
            self.source = args["content"].encode()
            return {"ok": True, "bytes": len(self.source)}
        return {"submitted": True} if name == "submit" else {"ok": True}


@pytest.mark.parametrize("case_count", [5, 6])
def test_fake_experiment_resume_and_independent_ids(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case_count: int
) -> None:
    case_root = Path(__file__).resolve().parents[1] / "cases"
    case_dirs = [path.parent for path in sorted(case_root.glob("*/case.json"))][:case_count]
    assert len(case_dirs) == case_count
    config = model_config()
    monkeypatch.setattr(experiment, "load_model", lambda path, alias: config)
    clients: list[FakeClient] = []

    def client_factory(value: Any) -> FakeClient:
        client = FakeClient(
            [
                answer
                for _ in range(case_count)
                for answer in (
                    response("write_test", {"content": "def test_ok():\n    assert True\n"}),
                    response("submit"),
                )
            ]
        )
        clients.append(client)
        return client

    monkeypatch.setattr(experiment, "ProviderClient", client_factory)
    monkeypatch.setattr(experiment, "PinnedRuntime", ExperimentRuntime)
    common: dict[str, Any] = {
        "case_dirs": case_dirs,
        "config_path": tmp_path / "unused.toml",
        "model_id": "test",
        "cache": tmp_path / "cache",
        "budget_usd": 0.25,
    }
    first = experiment.run_experiment(**common, output=tmp_path / "first", experiment_id="first")
    assert first["metrics"]["planned_samples"] == first["metrics"]["model_passes"] == case_count
    assert len(clients[0].calls) == 2 * case_count
    resumed = experiment.run_experiment(
        **common, output=tmp_path / "first", experiment_id="first", resume=True
    )
    assert resumed["metrics"]["physical_model_calls"] == 2 * case_count
    assert not clients[1].calls
    second = experiment.run_experiment(**common, output=tmp_path / "second", experiment_id="second")
    assert len(clients[2].calls) == 2 * case_count
    assert {row["sample_id"] for row in first["episodes"]}.isdisjoint(
        row["sample_id"] for row in second["episodes"]
    )
    for row in first["episodes"]:
        assert row["publication_window_label"] == "unknown"
        assert row["dataset_split"] == "unassigned"


def test_existing_experiment_requires_resume_and_unchanged_model_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = model_config()
    monkeypatch.setattr(experiment, "load_model", lambda path, alias: config)
    monkeypatch.setattr(experiment, "ProviderClient", lambda value: FakeClient([response()]))
    monkeypatch.setattr(experiment, "PinnedRuntime", ExperimentRuntime)
    case = next((Path(__file__).resolve().parents[1] / "cases").glob("*/case.json")).parent
    common: dict[str, Any] = {
        "case_dirs": [case],
        "config_path": tmp_path / "unused.toml",
        "model_id": "test",
        "cache": tmp_path / "cache",
        "budget_usd": 0.25,
        "output": tmp_path / "run",
        "experiment_id": "first",
    }
    experiment.run_experiment(**common)
    with pytest.raises(ValueError, match="resume"):
        experiment.run_experiment(**common)
    changed = config.model_copy(update={"temperature": 0.1})
    monkeypatch.setattr(experiment, "load_model", lambda path, alias: changed)
    with pytest.raises(ValueError, match="configuration"):
        experiment.run_experiment(**common, resume=True)


def test_resume_rejects_changed_host_prompt_or_tool_implementation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = model_config()
    monkeypatch.setattr(experiment, "load_model", lambda path, alias: config)
    monkeypatch.setattr(experiment, "ProviderClient", lambda value: FakeClient([response()]))
    monkeypatch.setattr(experiment, "PinnedRuntime", ExperimentRuntime)
    monkeypatch.setattr(experiment, "_code_identity", lambda: {"commit": "first"})
    case = next((Path(__file__).resolve().parents[1] / "cases").glob("*/case.json")).parent
    args: dict[str, Any] = {
        "case_dirs": [case],
        "config_path": tmp_path / "unused.toml",
        "model_id": "test",
        "cache": tmp_path / "cache",
        "budget_usd": 0.25,
        "output": tmp_path / "run",
        "experiment_id": "first",
    }
    experiment.run_experiment(**args)
    monkeypatch.setattr(experiment, "_code_identity", lambda: {"commit": "changed"})
    with pytest.raises(ValueError, match="implementation"):
        experiment.run_experiment(**args, resume=True)


@pytest.fixture
def policy_experiment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Dispatch-only fixture isolates experiment contracts from episode semantics."""
    monkeypatch.setattr(experiment, "load_model", lambda *args: model_config())
    monkeypatch.setattr(experiment, "_code_identity", lambda: {"commit": "fixed"})
    monkeypatch.setattr(experiment, "ProviderClient", lambda config: object())
    monkeypatch.setattr(experiment, "PinnedRuntime", lambda *args, **kwargs: object())
    calls: list[dict[str, Any]] = []

    def episode(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return {"status": "completed", "score": 0, "physical_calls": 0}

    monkeypatch.setattr(experiment, "run_episode", episode)
    case = sorted((Path(__file__).parents[1] / "cases").glob("*/case.json"))[0].parent
    return {
        "case_dirs": [case],
        "config_path": tmp_path / "unused.toml",
        "model_id": "test",
        "output": tmp_path / "out",
        "cache": tmp_path / "cache",
        "experiment_id": "completion-policy-test",
        "budget_usd": 0.25,
        "_calls": calls,
    }


@pytest.mark.parametrize("policy", ["explicit_submit", "saved_test_on_stop_v1"])
def test_experiment_binds_and_dispatches_completion_policy(
    policy_experiment: dict[str, Any], policy: CompletionPolicy
) -> None:
    from revertbench.harness.prompt import prompt_version

    calls = policy_experiment.pop("_calls")
    manifest = experiment.run_experiment(**policy_experiment, completion_policy=policy)
    assert manifest["completion_policy"] == policy
    assert manifest["prompt_version"] == prompt_version(policy)
    assert calls[0]["completion_policy"] == policy
    experiment.run_experiment(**policy_experiment, completion_policy=policy, resume=True)
    assert calls[-1]["completion_policy"] == policy


@pytest.mark.parametrize("policy", ["unknown", "saved_test_on_stop_v1"])
def test_completion_policy_rejected_before_outputs_or_side_effects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, policy: str
) -> None:
    config = model_config().model_copy(update={"tool_choice": "required"})
    monkeypatch.setattr(experiment, "load_model", lambda *args: config)

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("invalid completion policy reached an experiment effect")

    for name in ("_case_inputs", "JsonlLedger", "ProviderClient", "PinnedRuntime"):
        monkeypatch.setattr(experiment, name, forbidden)
    with pytest.raises(ValueError, match=r"completion|auto"):
        experiment.run_experiment(
            case_dirs=[],
            config_path=tmp_path / "unused",
            model_id="test",
            output=tmp_path / "out",
            cache=tmp_path / "cache",
            experiment_id="policy-test",
            budget_usd=0.25,
            completion_policy=cast(CompletionPolicy, policy),
        )
    assert not (tmp_path / "out").exists()
    assert not (tmp_path / "cache").exists()


@pytest.mark.parametrize("stored", [None, "explicit_submit", "unknown"])
def test_saved_stop_cannot_resume_another_or_legacy_policy(
    policy_experiment: dict[str, Any], stored: str | None
) -> None:
    import json

    calls = policy_experiment.pop("_calls")
    experiment.run_experiment(**policy_experiment)
    path = policy_experiment["output"] / "manifest.json"
    manifest = json.loads(path.read_bytes())
    if stored is None:
        manifest.pop("completion_policy", None)
    else:
        manifest["completion_policy"] = stored
    path.write_text(json.dumps(manifest))
    calls.clear()
    with pytest.raises(ValueError, match=r"completion|prompt"):
        experiment.run_experiment(
            **policy_experiment, completion_policy="saved_test_on_stop_v1", resume=True
        )
    assert not calls


def test_legacy_missing_completion_policy_is_explicit_only_and_keeps_source_guard(
    policy_experiment: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    import json

    calls = policy_experiment.pop("_calls")
    manifest = experiment.run_experiment(**policy_experiment)
    assert manifest["completion_policy"] == "explicit_submit"
    path = policy_experiment["output"] / "manifest.json"
    for key in ("completion_policy", "completion_identity_sha256"):
        manifest.pop(key)
    path.write_text(json.dumps(manifest))
    project = policy_experiment["cache"] / "project-ledger.jsonl"
    rows = [json.loads(line) for line in project.read_text().splitlines()]
    for row in rows:
        if row["event"] == "experiment_registered":
            for key in ("completion_policy", "prompt_version", "completion_identity_sha256"):
                row.pop(key)
    project.write_text("".join(json.dumps(row) + "\n" for row in rows))
    experiment.run_experiment(**policy_experiment, resume=True)
    assert calls[-1]["completion_policy"] == "explicit_submit"
    calls.clear()
    monkeypatch.setattr(experiment, "_code_identity", lambda: {"commit": "changed"})
    with pytest.raises(ValueError, match="implementation"):
        experiment.run_experiment(**policy_experiment, resume=True)
    assert not calls


def test_completion_prompt_change_cannot_replay_completed_episode(
    policy_experiment: dict[str, Any],
) -> None:
    import json

    calls = policy_experiment.pop("_calls")
    experiment.run_experiment(**policy_experiment, completion_policy="saved_test_on_stop_v1")
    path = policy_experiment["output"] / "manifest.json"
    manifest = json.loads(path.read_bytes())
    manifest["prompt_version"] = "other-prompt"
    path.write_text(json.dumps(manifest))
    calls.clear()
    with pytest.raises(ValueError, match="prompt"):
        experiment.run_experiment(
            **policy_experiment, completion_policy="saved_test_on_stop_v1", resume=True
        )
    assert not calls


def test_project_registration_prevents_coherent_manifest_policy_rebinding(
    policy_experiment: dict[str, Any],
) -> None:
    import json

    from revertbench.harness.prompt import prompt_version

    calls = policy_experiment.pop("_calls")
    experiment.run_experiment(**policy_experiment)
    path = policy_experiment["output"] / "manifest.json"
    manifest = json.loads(path.read_bytes())
    policy: Final[CompletionPolicy] = "saved_test_on_stop_v1"
    manifest.update(
        {
            "completion_policy": policy,
            "prompt_version": prompt_version(policy),
            "completion_identity_sha256": experiment._completion_identity(policy),
        }
    )
    path.write_text(json.dumps(manifest))
    calls.clear()
    with pytest.raises(ValueError, match=r"accounting|completion"):
        experiment.run_experiment(**policy_experiment, completion_policy=policy, resume=True)
    assert not calls


def test_saved_stop_resume_keeps_model_configuration_guard(
    policy_experiment: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = policy_experiment.pop("_calls")
    experiment.run_experiment(**policy_experiment, completion_policy="saved_test_on_stop_v1")
    calls.clear()
    changed = model_config().model_copy(update={"temperature": 0.7})
    monkeypatch.setattr(experiment, "load_model", lambda *args: changed)
    with pytest.raises(ValueError, match="configuration"):
        experiment.run_experiment(
            **policy_experiment, completion_policy="saved_test_on_stop_v1", resume=True
        )
    assert not calls
