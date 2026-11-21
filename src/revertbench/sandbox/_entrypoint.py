"""Trusted container entrypoint; production source stays on its read-only managed volume."""

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from revertbench.grade.runner import _execute
from revertbench.schema import ExecutionResult


def _emit(job: dict[str, Any], event: str, result: ExecutionResult | None = None) -> None:
    packet = {**job, "event": event}
    if result is not None:
        packet["result"] = json.loads(result.model_dump_json())
    print(json.dumps(packet, ensure_ascii=False, separators=(",", ":")), flush=True)


def main() -> None:
    job = json.loads(os.environ["REVERTBENCH_JOB"])
    seed = int(job["seed"])
    _emit(job, "started")
    root = Path("/work/tree")
    submission = root / "tests" / "test_revertbench_submission.py"
    if hashlib.sha256(submission.read_bytes()).hexdigest() != job["submission_sha256"]:
        result = ExecutionResult(
            status="invalid_run", seed=seed, reason="submission_identity_mismatch"
        )
    else:
        scratch = Path("/run/revertbench")
        scratch.mkdir(mode=0o700)
        result = _execute(
            root,
            Path("/opt/revertbench/pytest.ini"),
            scratch / "report.json",
            seed,
            float(job["timeout_seconds"]),
            int(job["output_limit_bytes"]),
        )
    _emit(job, "finished", result)


if __name__ == "__main__":
    main()
