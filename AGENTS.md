# RevertBench: notes for coding agents

## Read first

- If `context/` exists (it is local-only and gitignored), read `context/README.md` and `context/DESIGN.md` before starting any task. `DESIGN.md` is the source of truth for architecture, grader rules and milestones.
- Work one milestone at a time. Each milestone in `DESIGN.md` section 11 has a "done when" check; don't start the next one until that check passes.

## Commands

```bash
uv sync                       # install
uv run ruff check .           # lint
uv run ruff format .          # format
uv run mypy                   # types (strict)
uv run pytest                 # tests
uv run revertbench --help     # CLI
```

A change is ready only when lint, format check, mypy and pytest all pass.

## Conventions

- Python 3.12, src layout (`src/revertbench/`), Pydantic v2 for data, Typer for the CLI.
- Write the failing test first for any behaviour change, then the code.
- The grader and sandbox are security-critical. Every rule in `DESIGN.md` section 6 needs a test that proves a cheat scores 0. If you remove a check, a test must go red.
- Never commit secrets, real model configs, run outputs, mined repos or caches. They are gitignored; keep it that way.
- Never put anything from `context/` into committed files.
- Keep functions small and typed. No broad `except Exception` in `grade/` or `sandbox/`.
