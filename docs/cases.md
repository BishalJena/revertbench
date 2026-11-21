# Mining and reviewing cases

RevertBench mines historical Python regression tests, reconstructs a controlled
bad/repaired pair, and records machine evidence before explicit review. A discovered
fix is a candidate, not an accepted benchmark example.

## Run the pipeline

Install the locked development environment with `uv sync`. Build an immutable
worker image using `revertbench.sandbox.image.build_worker_image`; the returned
`sha256:…` identity is supplied explicitly below. Docker must be running. See
[sandbox.md](sandbox.md) for the container boundary and image build API.

```bash
uv run revertbench mine owner/repository --output-dir artifacts/candidates
uv run revertbench build artifacts/candidates/CANDIDATE.json \
  --good-commit FULL_GOOD_COMMIT --image sha256:IMAGE_ID
uv run revertbench validate artifacts/candidates/CANDIDATE.json \
  --good-commit FULL_GOOD_COMMIT --image sha256:IMAGE_ID
uv run revertbench review artifacts/validated-cases/CASE_ID/case.json --show-only
```

Replace the placeholders with complete commit/image identities and the actual
candidate path. `--good-commit` selects a known passing point before the fix's
parent. History probes run in Docker and require a monotonic pass-to-fail sequence
through the bounded first-parent interval. A pre-bisected candidate that contains
both verified `base_commit` and `bad_commit` needs no `--good-commit`.

Choose the baseline profile that matches the upstream test command. The default
`--baseline-profile pytest` runs the existing `tests` directory with fixed pytest
configuration. For repositories that use unittest discovery, including historical
nested-package layouts, select it explicitly:

```bash
uv run revertbench validate artifacts/candidates/CANDIDATE.json \
  --image sha256:IMAGE_ID --baseline-profile unittest
```

Trusted existing suites default to a PID limit of 64. If a supported upstream
suite requires more simultaneous threads, select the existing bounded capacity
explicitly with `--baseline-pids-limit 256` (strict range 8–256). The same selected
limit applies to base, bad and repaired baseline trees. `validation.json` records
the top-level `baseline_pids_limit` and each baseline's effective `pids_limit`;
they must agree before machine validation can pass. The immutable report hash
binds this choice without changing `Case.env` or legacy case input digests.
Memory, CPU, command wall time and output bounds remain fixed. Scored reference
tests and adversarial probes retain their default PID limit of 64.

When no Docker client is supplied, each baseline invocation owns a client with a
70-second transport timeout and closes it after cleanup. Its independent command
wall budget remains 60 seconds, including for a quiet suite. Caller-supplied
clients keep their existing timeout and ownership. Reproduction replays the
recorded baseline limit for both validation and its official-suite supplement;
legacy reports default to 64, and an older pinned engine cannot silently ignore
a nondefault recorded limit.

The unittest profile uses the equivalent of `python -m unittest -q` from the
repository root, honors `load_tests`, and includes doctests added by that hook.
The report records `baseline_profile`, `baseline_command`, complete executed
TestCase/doctest IDs, standard outcome counts, and observed environment hashes.
Unittest subtest or class-level outcome events can exceed the number of executed
TestCases; extra skip events are identified by `extra_skip_node_ids`, while successful
parent outcomes must still account exactly for executed tests. Standard failure
details remain in the report. At least one
ordinary passing test is required. Loader/import errors stay pending, test
failures and unexpected successes fail the baseline, and an empty suite cannot
validate a case. `Case.env.test_cmd` records the effective controlled command;
a trusted reporting shim supplies the result hooks. Submitted regression tests
continue to use the same grader regardless of baseline profile.

The supported worker also pins Six 1.17.0 for older upstream snapshots that declare
Six as a runtime dependency. Its version belongs to the shared locked image used
for all three trees; no repository setup script or runtime package install runs.
New profile images have their own image/lock fingerprints, while previously
published case environments retain their recorded identities.

Use `--local-repo PATH` to inspect an existing checkout instead of cloning a public
GitHub repository. `--cache-dir`, `--work-dir` and `--output-dir` choose storage;
Git objects and source snapshots never run Python or project setup on the host.
Snapshot exports are checked for file, entry and aggregate byte limits before
checkout. The initial Git mirror download has a time limit but no disk quota;
use a disposable worker with sufficient storage for repositories you select.
Keep outputs under ignored `artifacts/`, `repos/` and `workdir/`. Each output packet
uses a fresh directory and refuses to overwrite an existing packet. Validation
also accepts a built `case.json` and verifies its original candidate artifact
before reconstructing the trees into a new output directory. Revalidation preserves
PR/publication metadata, split labels and historical review notes, replaces machine
evidence, and clears both current human and agent decisions so the new proof needs
review. Separate histories of both reviewer kinds are retained.

`mine` scans bounded commit history for supported fix/test changes (at most 10,000
commits per invocation). It retains `discovery.json` alongside candidates with
examined/matched/yield counts and bounded per-commit rejection reasons; truncation
is explicitly recorded. `build`
materializes source and reference artifacts; `validate` additionally checks the
existing suites, reference regression, stability and adversarial submissions in
fresh containers. No command prompts for a model key. There is no model generation
in this pipeline.

## What the packet contains

`case.json` uses schema version 2. `candidate.json` retains the mined historical
provenance, including upstream reference notices and license text when found.
`bad.diff` shows the introducing change; `historical_fix.diff` shows the full later
fix; `repair.patch` contains only the production change applied to the bad tree.
`reference.py` is the extracted test dependency closure used for proof.
`validation.json` records versioned machine evidence and content identities.
Upstream license and reference-header text are retained as separate text artifacts
when present; extraction does not silently prepend executable encoding headers or
change the test bytes after validation.

The reference is a standalone function with its supported imports, literals and
helper dependencies. The version 2 admission profile also supports a narrowly
verified unittest derivation, recorded as
`reference_derivation: unittest_method_dependency_closure`. Its original node
remains `tests/PATH.py::Class::test_method`. Extraction requires the class's sole
base to resolve to the standard library `unittest.TestCase`, with no rebound base
alias, class data, decorators, setup/teardown, overrides or helper dependencies.
Other independent test methods may be present. The selected method takes only
`self` and directly uses `self.assertEqual`; the extracted ordinary test constructs
an exact `TestCase` through a fresh import alias and preserves the entire original
method body AST. Assertions and input cases are never rewritten or removed.

Unittest module setup/teardown and general `load_tests` hooks are rejected. A
recognized additive hook that only adds `DocTestSuite('literal.module')` through a
verified standard library import and returns the incoming test suite is permitted;
it does not configure the selected class. This is a source-visible derivation
check, not a proof of arbitrary module initialization semantics. The original
existing-suite and three-tree reference checks remain required.

Single-argument `iter()` and direct `TestCase.assertEqual(actual, expected)` are
admitted in this profile. Public project re-exports through a bounded, literal
`__all__` can be resolved from source; dynamic exports, private access and
introspection remain outside the supported subset.

The extracted test is derived from a historical test, rather than a claim
that arbitrary upstream fixtures or collection context were preserved. Reviewers
must inspect that derivation. The historical fix can contain unrelated changes;
only the extracted production repair enters the scored repaired tree.

The environment identifies immutable worker image content, Python patch version
and dependency-lock digest when observed inside that image. Missing observations
remain unknown and prevent acceptance. Publication timestamps can be null. An
observation timestamp says when evidence was retrieved; commit dates and current
editable PR text do not prove when a particular issue/PR body became public.
PR metadata distinguishes `observed_at`, `updated_at`, `merged_at`, and whether a
body is currently observed or independently verified historical content.

Regression groups and `dataset_split` (`unassigned`, `train`, `validation`, `test`)
are dataset organization fields. They do not assert a universal model training
cutoff. Candidates default to `eligibility: pending_metadata`; acceptance requires
a verified association between the introducing commit and an actual merged bad PR.
A later fix commit may have no known fix-side PR.

## Review and decisions

Review is read-only by default; `--json` returns a versioned review packet. The
renderer verifies recorded artifact hashes, bounds reads/previews, rejects links,
private or escaping paths, and escapes terminal control characters. A truncated
preview is labeled. Review packets contain source code: inspect them in a viewer,
not by executing the reference on the host.

Machine states are `candidate`, `pending_infrastructure`, `rejected`, or
`pending_review` after successful machine validation. Legacy `pending_human_review`
and `machine_validated` are also understood as stored machine states. Successful validation never marks
a case `accepted`. Infrastructure uncertainty remains distinct from a negative
benchmark result.

The default reviewer kind is `human`. An actual human reviewer records a decision explicitly:

```bash
uv run revertbench review PATH/case.json --accept \
  --reviewer 'Human reviewer name' --notes 'What I checked and concluded' \
  --attest-human-review
uv run revertbench review PATH/case.json --reject \
  --reviewer 'Human reviewer name' --notes 'Reason for rejection'
uv run revertbench review PATH/case.json \
  --reviewer 'Human reviewer name' --notes 'Additional observation'
```

Acceptance requires a complete passing machine report, matching source, grader and
policy fingerprints and versions, matching immutable inputs,
verified Git lineage, merged bad-PR provenance, reviewer identity and notes. Human
acceptance additionally requires explicit human attestation. An agent can record
its own decision when the user has explicitly delegated case review:

```bash
uv run revertbench review PATH/case.json --accept --reviewer-kind agent \
  --reviewer 'Codex regression reviewer' \
  --authorization-note 'User explicitly delegated case review for this project' \
  --notes 'Inspected the complete original test, minimal repair and bound evidence'
```

Agent review requires a nonblank authorization note and cannot use
`--attest-human-review`. It is stored in `agent_review` and `agent_review_history`,
separately from `human_review` and `review_history`; an agent never claims the
owner's identity or fabricates a human attestation. Agent records require the
validation report digest, immutable input digest and timezone-aware review time.
Agent decisions, including rejection or notes, apply to prepared packets with an
existing validation report; they cannot be attached to an unvalidated candidate.
Exactly one reviewer kind can hold the active decision. A new accept/reject
decision clears the previous active decision of either kind while preserving both
histories; notes append to the corresponding history without replacing an active
decision. Both kinds of acceptance use the same machine and provenance gates.
Review records bind report bytes and immutable case inputs. Previously published
cases retain their original pinned engine, review records and evidence bytes.
The case-specific machine report does not prove that every global adversarial
grader/sandbox regression test passed. Acceptance additionally requires separately
verified `global_suite_evidence`: the CI run URL and implementation commit, observed
passing checks and sandbox jobs, matching grader/policy fingerprints, and the case
image to which that evidence applies. `applies_to_image` records compatibility with
the validated case image; it does not claim that a different CI platform ran that
exact Docker image ID. Missing evidence remains unknown and blocks acceptance.

The local JSON record is an audit mechanism, not a cryptographic identity service;
the operator is responsible for supplying truthful independently verified metadata.

## Supported subset and limits

The miner supports ordinary Python functions and the constrained unittest method
derivation described above, plus a small production-only repair that applies
unchanged to the bad tree. Complex fixtures, decorators,
ambiguous/nonmonotonic histories, broad
repairs, unavailable dependencies, unsupported Git objects, missing reference
coverage and inconsistent test collections are rejected or remain pending with a
reason. The fixed worker environment does not install arbitrary project build
hooks, run repository setup commands, or fetch dependencies while grading.
Projects needing additional dependencies require a separately prepared, pinned
supported image. Existing suites may exercise trusted repository `conftest.py`
inside the container; submitted tests use the stricter grader policy and controlled
collection configuration.

Run the required checks with:

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest --run-docker
```
