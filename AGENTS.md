# RevertBench: notes for coding agents

## Read first

- If `context/` exists (it is local-only and gitignored), read `context/README.md` and `context/DESIGN.md` before starting any task. `DESIGN.md` is the source of truth for architecture, grader rules and milestones.
- Read `context/PROGRESS.md` when present to recover the current milestone, assignments, checks and next action.
- This checkout is the project home. Keep project notes here; do not search external vaults or synchronize project files to them.
- Work one milestone at a time. Each milestone in `DESIGN.md` section 11 has a "done when" check; don't start the next one until that check passes.

## Scope checks with interview-me

- Load and apply `interview-me` before each new milestone or substantial section, and revisit it when scope changes or material uncertainty appears. Skip mechanical edits and already-clear routine tasks.
- Follow the user's requested self-answering adaptation: the orchestrator works through focused questions sequentially on the user's behalf using the conversation, project evidence and existing authorization. Start with a one-sentence hypothesis, an honest rough confidence estimate (0–100%) and the reason for any uncertainty; pair each question with a best guess and its evidence before deciding whether another question is needed.
- Distinguish user-stated facts, agent assumptions and genuine unknowns. Do not invent user preferences, quotes, answers or confirmations. Cover outcome, who benefits, why now, success criteria, binding constraints and out-of-scope work. Challenge vague or convention-driven goals until they map to a concrete authorized result.
- Stop when scope and acceptance criteria are sufficiently clear. The user's self-answering instruction overrides the skill's repeated live-interview and explicit-yes loop for already-authorized routine decisions; do not create a fresh approval ritual. Escalate only material unresolved questions that require the user, one at a time with a best guess where appropriate, while continuing independent authorized work.
- The orchestrator records a concise scope summary, supporting evidence, assumptions and unresolved questions in local-only `context/PROGRESS.md`, then delegates with the resulting scope and acceptance contracts. Keep user-facing output to that concise decision summary rather than a full interview transcript. A self-interview never expands authorization, changes the budget or bypasses milestone, security or verification gates.

## Orchestration and parallel work

- The main agent in the user's conversation is the orchestrator. Keep it available for conversation by delegating substantial implementation, testing and research to capable subagents. Handle user questions and steering promptly while delegated work continues.
- Divide independent work within the current milestone and available agent slots. Give each subagent a concrete objective, owned files, interface expectations, dependencies and acceptance checks. Run dependent work in order; parallelism must not bypass milestone gates.
- Avoid overlapping writes. Agree on shared interfaces first, assign separate files or use isolated worktrees where needed, and never reset or overwrite another agent's or the user's changes in the shared checkout.
- Subagents return their changes, checks, findings and remaining issues. The orchestrator reviews and integrates their work, resolves conflicts, runs the final required checks and owns milestone completion. Delegation does not transfer accountability.
- Keep orchestration and waits responsive. Do not leave the main agent occupied with long implementation subtasks or blocking waits when it can coordinate workers and respond to the user.

## Progress at a glance

- The orchestrator maintains local-only `context/PROGRESS.md` as the single current progress board. Keep it concise and easy to scan: current milestone and gate, active tasks with owners/files/status, completed work, checks and results, blockers or user inputs, and the next action.
- Update it when work is assigned, completed or blocked, after meaningful verification, and before a context handoff. Include an update timestamp and evidence for completion; do not invent percentages or mark a milestone done before its gate passes.
- Subagents report progress to the orchestrator rather than editing the board concurrently. Keep private status, credentials and planning details out of tracked files.

## Prerequisites and development choices

- Before substantial work, inspect available tools, dependencies and authenticated access without printing secrets. Identify presently known user-only inputs, credentials or decisions needed to finish the authorized scope.
- Identify genuinely blocking inputs early, before dependent work begins; explain what each is needed for and ask only the essential unresolved question at a time. Continue independent work while waiting. Reuse existing authorization and configuration, and do not ask for keys that the task does not require.
- Choose routine implementation details and the development approach autonomously. Use the required failing-test-first workflow for behavior changes, with adversarial tests for grader and sandbox rules. Documentation and reversible maintenance changes need proportionate verification rather than artificial behavior tests.

## Context checkpoints and compaction

- Target compaction at 50% of the effective context window when the runtime exposes usage and supports threshold configuration. Configure the supported runtime controls; an instruction in this file cannot itself force compaction or provide unavailable token measurements.
- Before compaction, checkpoint the objective, decisions, active agent/file ownership, checks, blockers and next steps in `context/PROGRESS.md` so work resumes without losing scope or duplicating completed tasks.
- If usage or compaction controls are unavailable, state that limitation and checkpoint at natural work boundaries. Do not claim an exact threshold was reached or compaction occurred without evidence.

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
