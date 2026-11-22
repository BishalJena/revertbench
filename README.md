# RevertBench

A benchmark for whether coding models can write tests that catch regressions in
real Python pull requests.

Each case is a pull request from open-source history that was merged, caused a regression, and was later fixed. The model plays the reviewer who should have caught it: given the repository and the PR, it writes a test that exposes the regression. A test passes only if it fails on the buggy code for the right reason and passes on a controlled repaired copy.

The [nine accepted cases across four repositories](cases/README.md) include
the original reference tests, controlled production repairs, review records and
pinned Docker reproduction evidence. GitHub Actions reproduces each case on the
parent, introducing merge and controlled repair. The hosted-model evaluation
harness supports budgeted, resumable runs. Comparative model results are pending.

Start with the [product-index iterator regression](cases/more-itertools-71b76842d32c-5a1d64d1-be44ad73/case.json).
An optimization broke iterator inputs by calling `len()` on the original iterator.
The historical reference passes on the parent, raises `TypeError` on the
introducing merge, and passes with the production repair. The [case index](cases/README.md)
includes a Docker reproduction command that requires no model credentials.

Read the [submission policy](docs/grader-policy.md),
[sandbox guarantees and limits](docs/sandbox.md), and
[case mining and review workflow](docs/cases.md), and
[model evaluation commands and reproducibility](docs/harness.md).

## Development

Run from a full-history Git checkout with [uv](https://docs.astral.sh/uv/) and Docker.

```bash
uv sync
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest
uv run pytest --run-docker     # includes real container boundary checks
uv run revertbench --help
```

Model providers are configured in `configs/models.toml` (copy `configs/models.example.toml`). Export the configured API-key environment variable using your local environment manager; `.env` is ignored and is not loaded automatically by the CLI. Real configuration, credentials and run outputs are not committed.
