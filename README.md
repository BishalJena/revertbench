# RevertBench

An RL environment and eval built from real engineering decisions that went wrong.

Each case is a pull request from open-source history that was merged, caused a regression, and was later fixed. The model plays the reviewer who should have caught it: given the repository and the PR, it writes a test that exposes the regression. A test passes only if it fails on the buggy code for the right reason and passes on the fixed code.

Status: the differential grader, Docker sandbox and case pipeline are implemented.
The [five accepted regression cases](cases/README.md) include their original
reference tests, controlled repairs, review provenance and pinned Docker
reproduction evidence. One case has an owner review; four record delegated agent
reviews. The model harness is next; no model results are claimed yet.

Read the [submission policy](docs/grader-policy.md),
[sandbox guarantees and limits](docs/sandbox.md), and
[case mining and review workflow](docs/cases.md).

## Development

Requires [uv](https://docs.astral.sh/uv/) and Docker.

```bash
uv sync
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest
uv run pytest --run-docker     # includes real container boundary checks
uv run revertbench --help
```

Model providers are configured in `configs/models.toml` (copy `configs/models.example.toml`). API keys go in `.env` (copy `.env.example`). Neither file is committed.
