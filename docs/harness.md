# Track A harness

The harness gives a hosted model a buggy repository and the introducing PR diff.
The model can inspect files, save one test, preview it on buggy code and submit it.
The grader then checks the saved bytes against the buggy and controlled repaired
snapshots. A completed sample can score zero; infrastructure failures stay unscored.

## Run and inspect

Run from a full-history Git checkout with uv and Docker available. The runtime
exports historical grader commits from that checkout. The default `gpt_oss`
client profile supports Groq's `openai/gpt-oss-120b` and `openai/gpt-oss-20b`.
The explicit `qwen3_8_parsed` profile supports `qwen/qwen3.8-27b` with low
reasoning effort. It retains raw responses but omits top-level assistant reasoning
when sending conversation history, as required by that model's documented format.
Other hosted profiles require separate compatibility implementation and verification.

Copy `configs/models.example.toml` to the ignored `configs/models.toml`, review its
dated model, pricing and quota evidence, and export the configured API-key variable
in the invoking process. The CLI does not load `.env` automatically. Keep credentials,
real configuration and run outputs outside version control.

`tool_choice` defaults to `auto`; the example selects `required`. Required selection
asks the provider for a tool call on every turn. It does not choose `submit`, prove
test quality or guarantee completion within the call budget. Use a fresh experiment
when changing this setting or the client profile. Verify each model's actual
tool continuation, usage and quota evidence before scheduling a cohort; a compatible
API handshake is not a benchmark result.

```bash
uv run revertbench run cases \
  --model groq-120b \
  --experiment-id five-case-pilot-1 \
  --output runs/five-case-pilot-1 \
  --budget-usd 0.25
uv run revertbench report runs/five-case-pilot-1
```

Use the same arguments with `--resume` to resume an existing experiment. Keep the
same code revision, host Python, dependency lock, prompt, tools and model
configuration, plus the original case, output and shared-cache paths. The manifest
checks those identities before continuing. An
independent generation needs a new experiment identity and makes fresh requests.
A resumed completed response is not another attempt. When running across
multiple checkouts, pass the same absolute `--cache` path so all experiments share
the project budget ledger. Unresolved provider or tool
calls remain explicit; a lost response cannot be assumed free or safely repeated.

The initial run uses one sample per case. Report planned samples, valid completions,
passes, valid failures, invalid and incomplete samples together. Three grading
seeds test stability of one submission; they do not measure pass@3. The five-case
pilot shares one upstream repository and has no held-out split or contamination
claim. Hosted model aliases and backend fingerprints may not identify immutable
weights; preserve missing revision and effective-cutoff evidence as unknown.

## Model tools

| Tool | Operation |
| --- | --- |
| `list_dir(path)` | Bounded listing of one relative repository directory |
| `read_file(path, start, end)` | Inclusive, one-based line range |
| `search(pattern, path)` | Bounded literal substring search |
| `write_test(content)` | Save the UTF-8 test and report separate static policy feedback |
| `run_tests(node_ids)` | Check eligibility, then preview on buggy code; `[]` selects it all |
| `submit()` | End model interaction with the saved test |

There is no shell or arbitrary production-file write tool. Paths stay inside the
repository. Tools have bounded output, and test previews have a short time limit.
Only the saved test persists: after a preview, the next workspace is recreated
from pristine buggy source and that test. Preview output never includes a repaired
run or final score. The `preview_invoked` diagnostic records a pytest invocation;
it does not prove that a test body ran, because collection or setup can fail. Each episode allows at most 15 physical model requests,
including any recorded retries. A plain final answer is not a saved submission.

For `write_test`, `ok=true` means the bytes were saved. The separate `policy.allowed`,
`reason` and bounded `details` describe admission under the case's pinned checker,
using only the saved test and pristine buggy source. An ineligible preview returns
`policy_rejected` and `executed=false` before pytest. Revise the saved test using
that feedback; static eligibility does not predict its paired behavioral grade.

The prompt describes a conservative syntax subset shared by all published case
policies. Actual admission uses each case's pinned policy. Mutable PR bodies are
uniformly omitted under `omitted_all_cases_v1`; the introducing title and full diff
are retained. Opaque episode numbers keep internal case identifiers, which contain
fix-commit prefixes, out of model messages. Reference tests, repair patches and
private validation metadata never enter the model workspace.

## Reproducibility and costs

New harness modules change the current package fingerprint. The harness therefore
exports each accepted case's exact approved grader commit and launches a separate,
locked host interpreter. That process verifies the published inputs, rebuilds the
matching container from its immutable Python base, observes the runtime identities
and grades with the original policy and evaluator. Fresh platform-specific image
IDs are recorded separately from historical approved IDs. The current harness
never substitutes its latest grader for an older case.

API credentials stay in the provider process. The pinned host controller receives
no model credentials, and containers receive only the appropriate source tree and
fixed runtime support. Generated Python executes only in containers. Only the
saved test bytes cross from the agent workspace into fresh grading containers.

The experiment journal records logical call identities and exact request hashes
before sending, responses and usage before tools, tool transitions, submission
hashes and private final grading evidence. Manifests bind the harness, prompt,
provider configuration, case inputs, original grader, images and result counts.
Completed calls and results are reused on resume; unresolved effects are not
silently rerun.

Cost accounting reserves a conservative request ceiling before each attempt.
Missing usage or uncertain delivery retains that reservation. Published prices
are estimates of charges, not an invoice. Local quota ceilings and observed
allowlisted response headers are retained with their units and evidence dates;
missing headers do not establish a larger entitlement. No SDK retries or
provider-hosted browsing/execution tools are enabled. Experiment and shared
project budgets apply before subsequent requests.

These controls complement the [submission policy](grader-policy.md) and
[container boundary](sandbox.md); they do not prove arbitrary Python safe,
universal report authenticity or semantic relevance of every passing assertion.
