"""Offline pilot counts; incomplete infrastructure is never a model failure."""

from typing import Any


def summarize(episodes: list[dict[str, Any]], *, planned: int) -> dict[str, Any]:
    completed = [
        row
        for row in episodes
        if row.get("status") == "completed"
        and type(row.get("score")) is int
        and row["score"] in (0, 1)
    ]
    passes = sum(row["score"] == 1 for row in completed)
    invalid = sum(row.get("status") == "invalid_run" for row in episodes)
    return {
        "planned_samples": planned,
        "valid_completed_samples": len(completed),
        "model_passes": passes,
        "model_failures": len(completed) - passes,
        "invalid_samples": invalid,
        "incomplete_samples": planned - len(completed) - invalid,
        "pilot_pass_at_1": passes / len(completed) if completed else None,
        "physical_model_calls": sum(row.get("physical_calls", 0) for row in episodes),
        "replayed_model_calls": sum(row.get("replayed_calls", 0) for row in episodes),
        "prompt_tokens_reported": sum(row.get("prompt_tokens", 0) for row in episodes),
        "completion_tokens_reported": sum(row.get("completion_tokens", 0) for row in episodes),
        "unknown_usage_attempts": sum(row.get("unknown_usage_attempts", 0) for row in episodes),
    }


def render_report(manifest: dict[str, Any]) -> str:
    metrics = manifest["metrics"]
    return (
        f"Pilot pass@1: {metrics['model_passes']}/{metrics['valid_completed_samples']} "
        "valid completed; "
        f"{metrics['planned_samples']} planned.\n"
        f"Model failures: {metrics['model_failures']}; "
        f"infrastructure invalid: {metrics['invalid_samples']}; "
        f"incomplete: {metrics['incomplete_samples']}.\n"
        f"Physical model calls: {metrics['physical_model_calls']}. "
        "This is a small same-repository pilot; no pass@3 or held-out generalization claim."
    )
