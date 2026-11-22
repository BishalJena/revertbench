# Documentation

Start with the [project README](../README.md) for what RevertBench is and how a
test is scored. These guides cover each part in depth.

| Guide | Read it when you want to know |
| --- | --- |
| [Case index](../cases/README.md) | Which nine regressions are accepted, what each proves and who reviewed it |
| [Mining and reviewing cases](cases.md) | How Git history becomes a validated, reviewed case |
| [Submission policy](grader-policy.md) | Which Python a submitted test may use, and the limits of that check |
| [Sandbox](sandbox.md) | How generated tests are isolated in Docker, and what risk remains |
| [Model harness](harness.md) | How a hosted model is run, budgeted, resumed and reported |
| [Upstream suite profiles](upstream-baselines.md) | How each project's own test suite is run during validation |

## Terms used throughout

| Term | Meaning |
| --- | --- |
| Parent, base | The commit just before the pull request that introduced the bug |
| Bad, buggy tree | The source as merged, with the regression |
| Repaired tree | The buggy tree plus only the production part of the later fix |
| Reference test | The maintainers' own regression test, taken from the fix |
| Submission | The one test file a model saves for grading |
| Policy rejection | A submission refused before it runs, because it uses unsupported Python |
| Invalid run | An infrastructure failure. It has no score and never counts against a model |
