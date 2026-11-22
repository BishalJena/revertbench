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
| `blinker_upstream_default_behavior_v1` | All pytest tests in `tests`, including async tests, with warnings treated as errors. |
| `babel_upstream_default_behavior_v1` | Babel's default Linux root suite for CLDR 45, including package doctests and locale-parametrized tests. |
| `babel_cldr44_default_behavior_v1` | Babel's default Linux root suite for CLDR 44, including package doctests and locale-parametrized tests. |
| `attrs_field_transformer_default_behavior_v1` | The native attrs pytest suite for the field-transformer case, including the YAML mypy integration tests. |

The Boltons and Pydash versioned profiles retain this fixed prefix:

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

Blinker's [pinned tox command](https://github.com/pallets-eco/blinker/blob/64423dfadc9a35000b5399ca3d94a480ec7efe99/tox.ini)
runs pytest with `-v --tb=short` and a controlled temporary directory. Its
[pinned configuration](https://github.com/pallets-eco/blinker/blob/64423dfadc9a35000b5399ca3d94a480ec7efe99/pyproject.toml)
selects `tests`, treats warnings as errors, and uses asyncio auto mode. The fixed
profile reproduces those runtime settings with trusted arguments:

```text
/usr/local/bin/python -m pytest -v --tb=short
-c /opt/revertbench/pytest.ini --rootdir /workspace/repo
--basetemp=/tmp/revertbench-baseline -p no:cacheprovider
-p pytest_asyncio.plugin -o filterwarnings=error -o asyncio_mode=auto
-o asyncio_default_fixture_loop_scope=function tests
```

The worker pins `pytest-asyncio==1.5.0` alongside the existing locked pytest. It
loads that plugin explicitly while ambient plugin discovery stays disabled.
Function-scoped fixture loops make the newer plugin's default explicit; the
original suite has no async fixtures or event-loop overrides. The profile checks
the installed plugin version, its source origin and effective settings before
running tests. It does not suppress warnings, skip async tests or execute repository
build scripts. This is a compatible locked runtime, rather than a recreation of
the historical pytest 8.1.2 / pytest-asyncio 0.23.6 dependency set. Lint, typing,
documentation, package builds and the upstream Python matrix are separate checks.

Babel's default `make test` runs `python -m pytest` at the repository root.
The profile uses the historical `setup.cfg` and root `conftest.py` so collection
includes source doctests and the native locale and timezone parametrization.
The reporting command uses `-q --tb=short`, an isolated temporary directory,
and disables pytest's persistent cache. [Historical Makefile and test command](https://github.com/python-babel/babel/blob/bef3a734fbdf1da4d1058d25a5eed785bec43a63/Makefile)

The CLDR 45 profile uses Babel 2.16.0's published locale data and records the
optional Jinja and setuptools collection skips. The CLDR 44 profile uses Babel
2.15.0's locale data and records the optional setuptools collection skip. Each
wheel supplies 1,045 data files and matches its historical upstream importer
byte for byte. Historical Python code comes from each source tree. Each profile
checks its data digest before copying the files into the baseline workspace and
uses separately pinned Freezegun and python-dateutil wheels. Coverage thresholds,
package builds and documentation builds are separate checks. [Babel 2.16.0](https://pypi.org/project/Babel/2.16.0/) [Babel 2.15.0](https://pypi.org/project/Babel/2.15.0/)

The attrs profile follows the [historical tox test environment](https://github.com/python-attrs/attrs/blob/e048efcb395443608d184a149c0adbca166cb833/tox.ini).
It rebuilds each pure-Python role wheel offline with the pinned build dependencies
and checks the wheel digest before installation into scratch. The expected versions
come from the historical release tag distance. The fixed pytest command retains the
original project configuration and uses one xdist worker. Test dependencies with
native extensions live in an immutable image environment. The report binds the
installed wheel, the complete native collection and all 85 YAML mypy checks. It
records seven native skips and one expected failure on each role. Lint, the separate
Pyright task, the coverage reporting task and the full Python-version matrix are
outside the profile.

Original repository conftest support remains enabled for these trusted baselines.
Ambient plugin discovery remains disabled. Babel and attrs use their native
configuration. The other pytest profiles use the controlled configuration.
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

`--baseline-pids-limit` defaults to 64 and accepts 8 through 256 for the trusted
existing suite. Babel uses a fixed 512 MiB baseline memory cap. The original
256 MiB cap hit memory pressure and the highest measured peak was 348 MiB.
Attrs uses a fixed 896 MiB baseline memory cap for the installed-wheel build,
the xdist test worker, and its mypy subprocess. Other profiles use the default
baseline memory cap. CPU, wall time and output
bounds remain unchanged. Scored submissions retain their original limits. The legacy profiles,
commands and published case bytes remain unchanged. New-profile publication also
requires its versioned reproducibility evidence and review; a successful baseline
alone is not case acceptance.
