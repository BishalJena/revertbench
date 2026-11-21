# Mining and reviewing cases

RevertBench mines historical Python regression tests, reconstructs a controlled
bad/repaired pair, and records machine evidence before human review. A discovered
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
evidence, and clears the current human decision so the new proof needs review.

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
`pending_human_review` after successful machine validation. `machine_validated`
is also understood as a stored machine state. Successful validation never marks
a case `accepted`. Infrastructure uncertainty remains distinct from a negative
benchmark result.

An actual human reviewer can record a decision explicitly:

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
verified Git lineage, merged bad-PR provenance, reviewer identity, notes and an
explicit human attestation. The review records bind report bytes and immutable
case inputs; later notes preserve the original decision. An automation agent must
not manufacture an approval, identity or attestation on behalf of an absent human.
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
