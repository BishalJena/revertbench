# Accepted cases

This directory contains owner-reviewed, accepted benchmark cases. Each bundle
retains its original case JSON, human approval, historical reference, controlled
repair, upstream notices and machine evidence byte for byte. Its explicit
`publication-manifest.json` identifies every published file and the pinned Python
base used to reproduce its environment. Exploratory runs and local research are
excluded.

The first case is
[more-itertools product-index iterator regression](more-itertools-71b76842d32c-5a1d64d1-be44ad73/case.json):
merged PR1079 broke iterator inputs; PR1082 restored them. The unchanged original
test passes on the introducing commit's parent, fails on the bad tree with
`TypeError`, and passes after the historical production repair. Each condition
was repeated three times. The configured pytest subset passed 690 tests per tree;
the official unittest supplement, including doctests, passed 850 per tree.

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
