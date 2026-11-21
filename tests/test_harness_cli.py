"""Explicit run identities and budgets dispatch without hidden provider calls."""

from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from revertbench import cli


def test_run_and_report_help_need_no_provider_or_docker() -> None:
    for command in ("run", "report"):
        result = CliRunner().invoke(cli.app, [command, "--help"])
        assert result.exit_code == 0, result.output


def test_run_dispatches_explicit_model_identity_and_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cases = tmp_path / "cases"
    for name in ("second", "first"):
        case = cases / name
        case.mkdir(parents=True)
        (case / "case.json").write_text("{}")
    calls: list[dict[str, Any]] = []

    def run(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return {"planned": 2, "valid_completed": 1, "passed": 0, "invalid": 1}

    monkeypatch.setattr(cli, "_run_experiment", run)
    output = tmp_path / "experiment"
    result = CliRunner().invoke(
        cli.app,
        [
            "run",
            str(cases),
            "--model",
            "selected-model",
            "--experiment-id",
            "pilot-1",
            "--output",
            str(output),
            "--budget-usd",
            "0.25",
            "--resume",
        ],
    )
    assert result.exit_code == 0, result.output
    assert calls[0]["case_dirs"] == [cases / "first", cases / "second"]
    assert calls[0]["model_id"] == "selected-model"
    assert calls[0]["experiment_id"] == "pilot-1"
    assert calls[0]["budget_usd"] == 0.25
    assert calls[0]["resume"] is True
    assert calls[0]["output"] == output
    assert calls[0]["cache"] == Path("cache")


@pytest.mark.parametrize(
    "extra",
    [["--budget-usd", value] for value in ("5.01", "0", "nan", "inf")],
)
def test_run_rejects_invalid_budget_before_dispatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, extra: list[str]
) -> None:
    calls: list[object] = []
    monkeypatch.setattr(cli, "_run_experiment", lambda **kwargs: calls.append(kwargs))
    result = CliRunner().invoke(
        cli.app,
        [
            "run",
            str(tmp_path),
            "--model",
            "selected",
            "--experiment-id",
            "pilot-1",
            "--output",
            str(tmp_path / "out"),
            *extra,
        ],
    )
    assert result.exit_code != 0 and not calls


def test_run_errors_do_not_expose_provider_exception_payload(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = tmp_path / "case"
    case.mkdir()
    (case / "case.json").write_text("{}")

    def fail(**kwargs: Any) -> dict[str, Any]:
        raise ValueError("SYNTHETIC_PROVIDER_SECRET")

    monkeypatch.setattr(cli, "_run_experiment", fail)
    result = CliRunner().invoke(
        cli.app,
        [
            "run",
            str(tmp_path),
            "--model",
            "selected",
            "--experiment-id",
            "pilot-1",
            "--output",
            str(tmp_path / "out"),
        ],
    )
    assert result.exit_code != 0
    assert "SYNTHETIC_PROVIDER_SECRET" not in result.output


def test_report_displays_saved_denominators_without_running_model(tmp_path: Path) -> None:
    import json

    metrics = {
        "planned": 5,
        "valid_completed": 3,
        "passed": 1,
        "invalid": 1,
        "incomplete": 1,
        "pass_at_1": 1 / 3,
    }
    (tmp_path / "manifest.json").write_text(json.dumps({"metrics": metrics}))
    result = CliRunner().invoke(cli.app, ["report", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == metrics


def test_report_rejects_missing_metrics_without_printing_private_data(tmp_path: Path) -> None:
    (tmp_path / "manifest.json").write_text('{"private":"SYNTHETIC_SECRET"}')
    result = CliRunner().invoke(cli.app, ["report", str(tmp_path)])
    assert result.exit_code != 0
    assert "SYNTHETIC_SECRET" not in result.output
