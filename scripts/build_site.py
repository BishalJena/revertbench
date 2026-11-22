"""Build the portfolio site from the public, accepted case bundles."""

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
GITHUB = "https://github.com"
REPOSITORY = f"{GITHUB}/BishalJena/revertbench"
ASSETS = ("index.html", "styles.css", "app.js")
CASE_FILES = (
    "case.json",
    "validation.json",
    "reference.py",
    "bad.diff",
    "repair.patch",
    "upstream-license.txt",
    "upstream-reference-header.txt",
)
REFERENCE_OUTCOMES = (("base", "pass"), ("bad", "fail"), ("repaired", "pass"))
CONTROL_OUTCOMES = (("bad", "pass"), ("repaired", "pass"))
# Display copy of the validator's assert_true probe source.
CONTROL_SOURCE = "def test_cheat():\n    assert True\n"


def read_regular(path: Path) -> bytes:
    if not path.is_file() or path.is_symlink():
        raise ValueError(f"expected a regular source file at {path}")
    return path.read_bytes()


def read_case_files(case_dir: Path) -> dict[str, bytes]:
    manifest = json.loads(read_regular(case_dir / "publication-manifest.json"))
    if manifest["case_id"] != case_dir.name or manifest["manifest_version"] not in {"1", "2"}:
        raise ValueError(f"{case_dir.name}: invalid publication manifest")
    files = {}
    for name in CASE_FILES:
        contents = read_regular(case_dir / name)
        identity = {"sha256": hashlib.sha256(contents).hexdigest(), "bytes": len(contents)}
        if manifest["files"].get(name) != identity:
            raise ValueError(f"{case_dir.name}: {name} differs from its publication manifest")
        files[name] = contents
    return files


def run_outcome(
    execution: dict[str, Any], role: str, expected_nodes: set[str]
) -> tuple[tuple[str, str, str], ...]:
    nodes = execution["collected_node_ids"]
    tests = execution["tests"]
    if (
        execution["status"] != "completed"
        or execution["truncated"] is not False
        or execution["collection_errors"]
        or not expected_nodes
        or len(nodes) != len(expected_nodes)
        or set(nodes) != expected_nodes
        or len(tests) != len(expected_nodes)
        or {test["node_id"] for test in tests} != expected_nodes
    ):
        raise ValueError(f"{role} execution is incomplete or has a different test collection")

    results = []
    for test in tests:
        call = test["call"]
        outcome = call["outcome"]
        exception = call["exception_type"]
        if (
            any(test[phase]["was_xfail"] for phase in ("setup", "call", "teardown"))
            or any(test[phase]["outcome"] != "passed" for phase in ("setup", "teardown"))
            or outcome not in {"passed", "failed"}
            or (outcome == "failed" and (not isinstance(exception, str) or not exception))
            or (outcome == "passed" and exception is not None)
        ):
            raise ValueError(f"{role} execution has an invalid test result")
        results.append((test["node_id"], outcome, exception or ""))
    return tuple(sorted(results))


def summarize_runs(
    executions: list[dict[str, Any]], expected: tuple[tuple[str, str], ...]
) -> list[dict[str, Any]]:
    if len(executions) != 3 * len(expected):
        raise ValueError("expected three recorded executions on each tree")
    expected_nodes = set(executions[0]["collected_node_ids"])
    expected_seeds = [execution["seed"] for execution in executions[:3]]
    if any(type(seed) is not int for seed in expected_seeds) or len(set(expected_seeds)) != 3:
        raise ValueError("expected three distinct recorded seeds")

    summaries = []
    for index, (role, expected_result) in enumerate(expected):
        group = executions[index * 3 : (index + 1) * 3]
        seeds = [execution["seed"] for execution in group]
        outcomes = {run_outcome(execution, role, expected_nodes) for execution in group}
        if seeds != expected_seeds or len(outcomes) != 1:
            raise ValueError(f"{role} executions do not agree on their seeds or outcomes")
        results = outcomes.pop()
        exceptions = sorted({exception for _, outcome, exception in results if outcome == "failed"})
        result = "fail" if exceptions else "pass"
        expected_exit = 1 if result == "fail" else 0
        if result != expected_result or any(
            type(execution["exit_code"]) is not int or execution["exit_code"] != expected_exit
            for execution in group
        ):
            raise ValueError(f"{role} execution outcome differs from the recorded validation")
        summaries.append(
            {
                "role": role,
                "seeds": seeds,
                "result": result,
                "exception": ", ".join(exceptions) or None,
            }
        )
    return summaries


def summarize_control(probe: dict[str, Any]) -> dict[str, Any]:
    grade = probe.get("grade")
    if (
        probe.get("status") != "passed"
        or probe.get("reason") != "bad_did_not_fail"
        or not isinstance(grade, dict)
        or grade.get("status") != "rejected"
        or type(grade.get("score")) is not int
        or grade["score"] != 0
        or grade.get("reason") != probe["reason"]
    ):
        raise ValueError("expected the rejected always-passing control grade")
    # The paired grader records buggy-tree runs first, followed by the repaired tree.
    try:
        runs = summarize_runs(grade["executions"], CONTROL_OUTCOMES)
    except ValueError as error:
        raise ValueError(f"invalid control evidence: {error}") from error
    return {
        "source": CONTROL_SOURCE,
        "score": grade["score"],
        "reason": grade["reason"],
        "runs": [
            {"role": "base", "seeds": [], "result": "not_run", "exception": None},
            *runs,
        ],
    }


def pull_request(repository: str, evidence: dict[str, Any]) -> dict[str, Any]:
    number = evidence["number"]
    url = f"{GITHUB}/{repository}/pull/{number}"
    if type(number) is not int or number < 1 or evidence["url"] != url:
        raise ValueError(f"{repository}: invalid pull request evidence")
    return {"number": number, "url": url}


def read_case(case_dir: Path, copy: dict[str, str]) -> dict[str, Any]:
    case_id = case_dir.name
    if case_dir.is_symlink() or not re.fullmatch(r"[a-z0-9-]+", case_id):
        raise ValueError(f"invalid case directory {case_dir}")
    files = read_case_files(case_dir)
    case = json.loads(files["case.json"])
    report = json.loads(files["validation.json"])
    reference = report["reference"]
    validation = case["validation"]
    report_digest = hashlib.sha256(files["validation.json"]).hexdigest()
    if (
        case["id"] != case_id
        or report["case_id"] != case_id
        or validation["status"] != "accepted"
        or validation["machine_passed"] is not True
        or validation["report_sha256"] != report_digest
        or report["status"] != "machine_validated"
        or reference["status"] != "passed"
        or reference["score"] != 1
        or not reference["conditions"]
        or not all(value is True for value in reference["conditions"].values())
    ):
        raise ValueError(f"{case_id}: expected the accepted machine validation report")
    human_review = case.get("human_review")
    agent_review = case.get("agent_review")
    review = human_review or agent_review
    if (
        (human_review is None) == (agent_review is None)
        or not isinstance(review, dict)
        or review.get("decision") != "accept"
        or review.get("validation_report_sha256") != report_digest
        or (human_review is not None and review.get("attested") is not True)
        or (agent_review is not None and review.get("reviewer_kind") != "agent")
    ):
        raise ValueError(f"{case_id}: expected an accepting review bound to the validation report")
    for name, reported in (("reference.py", "reference_sha256"), ("repair.patch", "patch_sha256")):
        if report[reported] != hashlib.sha256(files[name]).hexdigest():
            raise ValueError(f"{case_id}: {name} differs from the validation report")
    bad_diff = case["bad_diff"]
    if (
        bad_diff["path"] != "bad.diff"
        or bad_diff["sha256"] != hashlib.sha256(files["bad.diff"]).hexdigest()
    ):
        raise ValueError(f"{case_id}: bad.diff differs from the approved case")

    repository = case["repo"]
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
        raise ValueError(f"{case_id}: invalid repository name")
    if set(copy) != {"title", "summary", "expected"} or any(
        not isinstance(value, str) or not value.strip() for value in copy.values()
    ):
        raise ValueError(f"{case_id}: missing case title, summary, or expected behavior")
    return {
        "id": case_id,
        "repository": repository,
        **copy,
        "reference": files["reference.py"].decode("utf-8"),
        "change": files["bad.diff"].decode("utf-8"),
        "repair": files["repair.patch"].decode("utf-8"),
        "license": files["upstream-license.txt"].decode("utf-8"),
        "notice": files["upstream-reference-header.txt"].decode("utf-8"),
        "introduced": pull_request(repository, case["bad_pr"]),
        "fixed": pull_request(repository, case["fix_pr"]),
        "bundle_url": f"{REPOSITORY}/tree/main/cases/{case_id}",
        "validation_url": f"{REPOSITORY}/blob/main/cases/{case_id}/validation.json",
        # Reference validation adds the base runs before the paired grader's evidence.
        "runs": summarize_runs(reference["executions"], REFERENCE_OUTCOMES),
        "control": summarize_control(report["probes"]["assert_true"]),
    }


def build_site(root: Path, output: Path) -> None:
    if output.is_symlink() or (output.exists() and not output.is_dir()):
        raise ValueError(f"expected a regular output directory at {output}")
    if output.exists() and any(
        path.name not in {*ASSETS, "data.json"} or path.is_symlink() or not path.is_file()
        for path in output.iterdir()
    ):
        raise ValueError(f"unexpected output files at {output}")

    site = root / "site"
    copy = json.loads(read_regular(site / "case-copy.json"))
    case_dirs = {path.parent.name: path.parent for path in (root / "cases").glob("*/case.json")}
    if not copy or set(copy) != set(case_dirs):
        raise ValueError("site/case-copy.json must cover every published case exactly once")
    cases = [read_case(case_dirs[case_id], description) for case_id, description in copy.items()]
    data = {
        "stats": {
            "case_count": len(cases),
            "repository_count": len({case["repository"] for case in cases}),
            "execution_count": sum(len(run["seeds"]) for case in cases for run in case["runs"]),
        },
        "cases": cases,
    }
    assets = {name: read_regular(site / name) for name in ASSETS}
    output.mkdir(parents=True, exist_ok=True)
    for name, contents in assets.items():
        (output / name).write_bytes(contents)
    (output / "data.json").write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="directory for static assets")
    args = parser.parse_args()
    try:
        build_site(ROOT, args.output)
    except (OSError, ValueError, KeyError, TypeError) as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
