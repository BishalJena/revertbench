from pathlib import Path
from typing import Any

import pytest

from revertbench.harness import experiment
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


def test_fake_five_case_experiment_resume_and_independent_ids(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = model_config()
    monkeypatch.setattr(experiment, "load_model", lambda path, alias: config)
    clients: list[FakeClient] = []

    def client_factory(value: Any) -> FakeClient:
        client = FakeClient(
            [
                answer
                for _ in range(5)
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
    cases = sorted((Path(__file__).resolve().parents[1] / "cases").glob("*/case.json"))
    common: dict[str, Any] = {
        "case_dirs": [path.parent for path in cases],
        "config_path": tmp_path / "unused.toml",
        "model_id": "test",
        "cache": tmp_path / "cache",
        "budget_usd": 0.25,
    }
    first = experiment.run_experiment(**common, output=tmp_path / "first", experiment_id="first")
    assert first["metrics"]["planned_samples"] == first["metrics"]["model_passes"] == 5
    assert len(clients[0].calls) == 10
    resumed = experiment.run_experiment(
        **common, output=tmp_path / "first", experiment_id="first", resume=True
    )
    assert resumed["metrics"]["physical_model_calls"] == 10
    assert not clients[1].calls
    second = experiment.run_experiment(**common, output=tmp_path / "second", experiment_id="second")
    assert len(clients[2].calls) == 10
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
