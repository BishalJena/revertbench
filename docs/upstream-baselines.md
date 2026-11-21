# Fixed upstream runtime-suite profiles

`revertbench validate --baseline-profile PROFILE` selects a fixed command for the
trusted existing suite on all three source trees. It does not change the scored
submission's grader, fixtures, policy, resource limits or hidden reference.

| Profile | Runtime selection |
| --- | --- |
| `pytest` (default) | Existing controlled pytest command targeting `tests`. |
| `unittest` | `python -m unittest -q`, including upstream `load_tests` hooks and doctests they add. |
| `boltons_upstream_default_behavior_v1` | pytest package doctests in `boltons` and unit tests in `tests`. |
| `pydash_upstream_default_behavior_v1` | pytest package doctests in `src/pydash` and unit tests in `tests`, excluding only `tests/pytest_mypy_testing`. |

The two versioned profiles use this fixed prefix:

```text
/usr/local/bin/python -m pytest -q --disable-warnings
-c /opt/revertbench/pytest.ini --rootdir /workspace/repo
--basetemp=/tmp/revertbench-baseline -p no:cacheprovider
```

Boltons appends `--doctest-modules -o
"doctest_optionflags=ALLOW_UNICODE ELLIPSIS IGNORE_EXCEPTION_DETAIL NORMALIZE_WHITESPACE"
boltons tests`. This selection follows the runtime targets and doctest flags in
[Boltons' pinned tox configuration](https://github.com/mahmoud/boltons/blob/d31d821d56c8f7756af9c30a864077f2e5855321/tox.ini)
and [pyproject](https://github.com/mahmoud/boltons/blob/d31d821d56c8f7756af9c30a864077f2e5855321/pyproject.toml).
Upstream tox tests an installed package from `.tox`; this profile runs the case's
source package from the controlled workspace. Packaging and the Python version
matrix are separate checks.

Pydash appends `--doctest-modules --ignore=tests/pytest_mypy_testing src/pydash
tests`. Its [pinned default test task](https://github.com/dgilland/pydash/blob/e8ea6b6bfb99ee99f77f7f7de6406c18b09e973f/tasks.py)
selects these targets and excludes the opt-in typing suite by default. This profile
covers runtime unit and doctest behavior. Coverage percentage gates and reports,
opt-in typing tests, documentation, lint and package builds are not part of this
runtime result; it is not a claim that full upstream CI passed.

Original repository conftest support remains enabled for these trusted baselines.
Ambient plugin discovery and repository command-line configuration remain disabled.
There is no arbitrary command, target, ignore-path or shell override. Check the
historical repository's layout and test-selection contract before using a profile
for a case; a recent upstream configuration is not evidence for every old commit.
An unsupported layout or import error cannot fall back to a smaller passing suite.

Reports retain the exact profile, command and PID limit for each role, along with
all collected node IDs and outcomes. Versioned pytest profiles use bounded
compressed report transport; doctest examples and unittest subtests do not inflate
collected node counts. A machine-validated versioned profile requires matching
fixed commands and all three role reports. Injected backend mismatches remain
pending infrastructure.

`--baseline-pids-limit` still defaults to 64 and accepts 8 through 256, applying only
to the trusted existing suite. Memory, CPU, wall time and output bounds remain
unchanged. Scored submissions retain their original limits. The legacy profiles,
commands and published case bytes remain unchanged. New-profile publication also
requires its versioned reproducibility evidence and review; a successful baseline
alone is not case acceptance.
