# Accepted cases

This directory contains explicitly reviewed, accepted benchmark cases. Each bundle
retains its original case JSON, reviewer identity and kind, historical reference, controlled
repair, upstream notices and machine evidence byte for byte. Its explicit
`publication-manifest.json` identifies every published file and the pinned Python
base used to reproduce its environment. Exploratory runs and local research are
excluded.

Reviews record either an actual human attestation or an agent's own decision under
explicit delegated authorization. Agent review is stored separately and never
presented as human approval. Both require the same machine, provenance and CI evidence.

The first case is
[more-itertools product-index iterator regression](more-itertools-71b76842d32c-5a1d64d1-be44ad73/case.json):
merged PR1079 broke iterator inputs; PR1082 restored them. The unchanged original
test passes on the introducing commit's parent, fails on the bad tree with
`TypeError`, and passes after the historical production repair. Each condition
was repeated three times. The configured pytest subset passed 690 tests per tree;
the official unittest supplement, including doctests, passed 850 per tree. Bishal
Jena explicitly reviewed and approved this first case; its original human review
and published evidence remain unchanged.

The second case is
[more-itertools partition None-predicate regression](more-itertools-111d6deb8080-bf3b6673-5b49ad9c/case.json):
merged PR310 stopped treating `None` as a truth-value predicate; PR368 restored
the `bool` fallback. Both assertions in the original test remain unchanged:
`partition(None, range(3))` produces `[0]` and `[1, 2]`. The reference passes on
base, raises `TypeError` on bad, and passes on the controlled repair across three
repeats. All 445 upstream unittest tests pass on each tree. This case records
an authorized agent review, with no human attestation, and pins its own grader
and environment independently of the first case.

The third case is
[more-itertools zero-size ichunked regression](more-itertools-6a6d1364d8dc-1ea82a71-6988667e/case.json):
merged PR1124 broke zero-sized chunks for nonempty input; PR1305 restored the
input guards. The original three assertions check an empty result for list and
iterator inputs, including an unconsumed iterator. Base passes, bad raises
`ValueError` on introduced lines, and repair passes across three repeats.
The full upstream suites pass 857/863/863 tests respectively. The report records
a 256-PID upstream-suite budget; scored references retain their existing limits.
This case has an authorized agent review and its own pinned policy3 environment.

The fourth case is
[more-itertools distinct-permutations regression](more-itertools-2b91cdc175c0-17fd95a0-7f36cfaf/case.json).
PR423 made mixed, unsortable inputs raise `TypeError`; the later PR834 integration
restored them. All 13 original input sequences remain unchanged. Reference runs
pass on base, fail on bad and pass on repair three times; all 494 upstream tests
pass on each tree. Its explicit function-body repair derivation records complete
before/after AST identities and the original historical production diff.

The fifth case is
[more-itertools large numeric-range equality regression](more-itertools-f9cdcb990dbb-f42daddf-9f00df42/case.json).
PR1216 made equality overflow for ranges larger than the platform size limit;
PR1345 restores comparison using the cached length. The original six comparison
rows remain unchanged. Reference runs pass/fail/pass three times and upstream
suites pass 897/896/896 tests. The canonical related hashing repair is retained
and is not claimed as directly covered by the equality reference.

Both cases record authorized agent reviews and pin their policy4 environment.
Lifted `subTest` contexts retain one pytest node and propagate the first failing
input; individual input rows are not reported as independently failing nodes.

Inspect the packet without executing its historical code on the host:

```bash
uv run revertbench review cases/more-itertools-71b76842d32c-5a1d64d1-be44ad73/case.json --show-only
```

Reproduce it with Docker from a full-history checkout:

```bash
uv run python scripts/reproduce_case.py \
  cases/more-itertools-71b76842d32c-5a1d64d1-be44ad73 \
  --output artifacts/product-reproduction
```

The helper exports the exact approved grader implementation and frozen lock from
Git, builds that worker against the recorded immutable official Python base, and
runs the normal validator plus the declared official test command. It checks
source, repair, reference, Python, lock, grader and policy identities. It writes
the actual reproduction image ID separately from the original approved image ID
and leaves the accepted bundle unchanged. The original validated image is amd64;
the reproduction records its actual architecture rather than assuming the same
platform or Docker image ID. Future cases use their own pinned
implementation and declared baseline profile. CI keeps full Git history and
uploads the reproduction results.

The historical Python reference is intentionally excluded from automated
formatting: changing its spelling would change the approved source digest.
