from typing import Any

from revertbench.harness.metrics import render_report, summarize


def test_invalid_and_incomplete_samples_stay_out_of_model_denominator() -> None:
    rows: list[dict[str, Any]] = [
        {"status": "completed", "score": 1, "physical_calls": 2},
        {"status": "completed", "score": 0, "physical_calls": 15},
        {"status": "invalid_run", "score": None, "physical_calls": 1},
        {"status": "incomplete", "score": None, "physical_calls": 0},
    ]
    result = summarize(rows, planned=5)
    assert result["planned_samples"] == 5 and result["valid_completed_samples"] == 2
    assert (
        result["model_passes"] == 1
        and result["model_failures"] == 1
        and result["pilot_pass_at_1"] == 0.5
    )
    assert result["invalid_samples"] == 1 and result["incomplete_samples"] == 2
    assert result["physical_model_calls"] == 18 and "pass_at_3" not in result
    report = render_report({"metrics": result})
    assert "1/2" in report and "5 planned" in report
