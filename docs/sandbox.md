# Docker execution boundary

**In short**

- Generated tests run only inside Docker containers. There is no automatic
  fallback to local execution.
- Only the bytes of the saved test file cross from the agent's workspace into
  grading. Edits to source, `conftest.py` or configuration stay behind.
- Every grading run gets a fresh read-only copy of the source, no network, a
  non-root user with all Linux capabilities dropped, and fixed CPU, memory,
  process, time and output limits.
- Infrastructure failures have no score. Timeouts and output overruns caused by
  the submission score 0.
- Containers share the host kernel. The boundary narrows exposure; it does not
  remove kernel or daemon risk.

**On this page:** [One contract, two backends](#one-grading-contract-two-execution-backends) ·
[Data crossing the boundary](#data-crossing-the-boundary) ·
[Restrictions and lifecycle](#runtime-restrictions-and-lifecycle) ·
[Verification](#verification) · [Remaining limits](#remaining-limits) ·
[Source and transport details](#source-and-transport-details)

The policy and grading rules answer whether a submission earns credit. Docker
provides the separate boundary needed to execute generated Python. Admission by
the static policy does not make a test safe to run on the host.

## One grading contract, two execution backends

`ExecutionBackend` is a callable that accepts a source tree, submission text,
seed, wall-time budget and output-byte budget, then returns `ExecutionResult`.
Both `grade_submission` and `validate_reference` accept an
`execution_backend` keyword. The selected backend handles every repeat,
including all nine base/bad/repaired runs in reference validation. The scoring,
collection, failure, coverage and stability rules remain in the same grader.

The default local `run_tree` backend is retained for explicitly trusted toy
fixtures. Callers handling generated submissions must select `DockerRunner`;
there is no automatic fallback to local execution when Docker is unavailable.
Unavailable infrastructure has no model score.

```python
import docker

from revertbench.grade.rules import grade_submission
from revertbench.sandbox.image import build_worker_image
from revertbench.sandbox.worker import DockerRunner

client = docker.from_env()
try:
    image = build_worker_image(client)
    backend = DockerRunner(image, client=client)
    result = grade_submission(
        bad_tree,
        repaired_tree,
        submission,
        changed_lines,
        execution_backend=backend,
    )
finally:
    client.close()
```

The paths and submission in this example are caller-supplied inputs, not values
read from a model command. The image is built separately from target repository
code. Its build context admits only the worker's required public files; provider
keys, private notes, Git history and unrelated local files are not build inputs.
Image building may download pinned dependencies. Grading containers have no
external network access and do not install dependencies during a test run.
The builder resolves the Python base to a registry digest, installs the hashed
lockfile export, and returns the built image's immutable content ID. Docker
proxy configuration is disabled during both building and runtime. Package
source must itself be suitable for publication; an allowlist is not a scanner
for credentials embedded inside otherwise legitimate Python files.

## Data crossing the boundary

1. An agent sandbox receives only the buggy source snapshot and writable space
   for its work. Git history, the repair and hidden reference tests are absent.
2. Source is transferred as a bounded canonical archive. Symlinks, hardlinks,
   special files and escaping paths are rejected; copying a tree does not grant
   it access to arbitrary host paths.
3. On submission, the host extracts only the bounded regular test-file bytes
   from the designated submission path. Changes to other source files,
   `conftest.py`, pytest configuration or the agent environment do not enter
   grading.
4. Each grading repeat receives a fresh source volume made from the original
   trusted tree plus those submission bytes. A stopped seeding helper fills a
   Docker-managed volume; the grading container mounts it read-only at
   `/work/tree`. No host directory is bind-mounted.
5. The grading entry point and pytest configuration belong to the worker. The
   submitted test cannot select its command, plugins, root directory or report
   destination. The worker uses the same per-test call-phase coverage and
   structured phase reports as the trusted local runner.
6. Bounded output and report data return to the host, where request identities,
   completion and report consistency are checked before scoring. A container
   exit code alone is not a passing grade.

An agent's editable tree lives in bounded tmpfs under `/workspace/repo`, copied
from a read-only seed volume. Its writes do not accumulate in an unbounded
writable host directory or grading volume. Agent execution is a separate
container lifetime from each fresh grading repeat.

## Runtime restrictions and lifecycle

Runtime limits are represented by `SandboxLimits`. Container creation fixes the
working directory, hostname, user and environment rather than forwarding the
host environment. In particular, model-provider credentials and Docker client
proxy configuration are not injected into test execution.

The defaults are 256 MiB of container memory with no extra swap, 64 tasks
(processes and threads), one CPU, 64 MiB per configured scratch tmpfs,
8 MiB shared memory, and fixed
UID/GID 10001. Tmpfs pages also consume the container's memory budget; the
individual space limits do not add extra memory beyond the cgroup limit.
The caller supplies the per-run wall-time and output budgets.

| Boundary | Enforcement |
| --- | --- |
| Privilege | Non-root user, all Linux capabilities dropped, no privilege escalation; default seccomp remains enabled. |
| Host access | No Docker socket, host PID/network namespace, privileged devices or host-directory mounts. |
| Network | An isolated `none` network; loopback is not an external network connection. |
| Filesystem | Read-only root and grading source volume; only bounded temporary/report/shared-memory areas are writable. |
| Agent writes | A bounded tmpfs workspace rather than a writable source volume. |
| Resources | CPU quota, memory/swap and PID limits, plus an external wall-time and output-byte budget. |
| Input/output | Bounded source archives, regular-file submission extraction, logs and structured reports. |
| Lifetime | Unique owned resources; container/helper/volume cleanup also runs on failure. |

Job identifiers are opaque and scoped to the resources created for that
execution. Source, submission, seed and image identity stay associated with the
host request; results from another job or an incomplete run cannot be silently
reused. Both scored trees use the same worker image and execution settings, with
only the intended production repair differing. Canonical archive metadata and
fixed paths reduce incidental differences between runs.

Timeouts and output-limit aborts attributable to the submitted process are
submission failures. PID and memory quotas bound execution independently of
grading outcomes. Image/daemon/setup/transport failures, unexplained worker loss
and a bare container OOM flag are invalid runs with no score. The OOM flag proves
that the container hit its memory limit, but cannot distinguish bootstrap usage
from the submitted test; an arbitrary signal is likewise insufficient evidence
of model fault. Resources are collected explicitly after bounded report
retrieval, rather than depending on container auto-removal to race the result
reader.

## Verification

Run the normal checks and the opted-in container suite on a Docker-enabled
Linux host:

```bash
uv sync --frozen
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest --run-docker
```

Without `--run-docker`, marked container tests are skipped for local unit work.
Those skips do not satisfy the sandbox acceptance gate. With the flag, an
unavailable daemon is a hard failure. CI runs a separate Docker job on a hosted
Linux runner using `pytest --run-docker -m docker`; an empty or unavailable
container suite cannot masquerade as successful verification.

The suite covers reference grading through the shared backend, differential and
per-node coverage rejection, policy rejection before execution, namespace and
mount boundaries, resource caps, extraction attacks, fresh state and cleanup.
Resource-exhaustion probes use bounded controlled children inside containers;
they are not executed through the host fixture backend.

## Remaining limits

Docker containers share their host kernel. These settings reduce exposure and
bound resources; they do not prove immunity to kernel or daemon vulnerabilities.
The configured Docker daemon remains a trusted component.

Static-policy limitations still apply: project APIs and Python's dynamic
behavior can reveal information indirectly. Reports produced in the test
interpreter are checked for consistency, but are not universally authentic
against arbitrary in-process tampering. Changed-line coverage proves execution,
not semantic causation. The controlled regression pair and human reference-test
review remain separate case-acceptance requirements.

## Source and transport details

The trusted grader lives in `/opt/revertbench/src`, made available to the isolated
entrypoint by a static image-owned system `.pth` file. The test process puts that
dedicated grader directory first, then the case's `src` and root directories,
ahead of ordinary installed dependencies. This prevents an installed dependency
with the target package's name from replacing the repository being evaluated.

Agent command output is read from Docker's multiplexed socket in bounded frames,
with payload reads of at most 16 KiB and a combined output budget. Submission
extraction uses a fixed isolated Python helper with directory file descriptors,
no-follow and nonblocking opens, and regular-file/link-count/size/UTF-8 checks.
The selected inode stays fixed during reading; concurrent changes need not form
an atomic semantic snapshot. The grader evaluates exactly the returned bytes.
