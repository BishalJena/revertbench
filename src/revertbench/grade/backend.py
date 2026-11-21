"""Shared execution boundary for local trusted fixtures and isolated workers."""

from pathlib import Path
from typing import Protocol

from revertbench.schema import ExecutionResult


class ExecutionBackend(Protocol):
    """Execute one fresh tree/submission repeat and return structured evidence."""

    def __call__(
        self,
        tree: Path,
        submission: str,
        *,
        seed: int,
        timeout_seconds: float = 10,
        output_limit_bytes: int = 65536,
    ) -> ExecutionResult: ...
