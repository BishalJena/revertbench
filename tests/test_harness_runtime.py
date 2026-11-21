"""Host runtime is a secret-free pinned-process adapter, never a host grader."""

import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from revertbench.harness.runtime import PinnedRuntime, RuntimeFailure, worker_environment


def test_worker_environment_does_not_inherit_provider_or_python_injection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "SYNTHETIC_SECRET")
    monkeypatch.setenv("PYTHONPATH", "/synthetic/injected")
    monkeypatch.setenv("UV_PROJECT", "/synthetic/current-project")
    monkeypatch.setenv("DOCKER_HOST", "unix:///synthetic/docker.sock")
    environment = worker_environment()
    assert environment["DOCKER_HOST"] == "unix:///synthetic/docker.sock"
    assert not {"GROQ_API_KEY", "PYTHONPATH", "UV_PROJECT"} & environment.keys()
    assert "SYNTHETIC_SECRET" not in json.dumps(environment)


def test_grade_without_submission_cannot_turn_into_a_fake_model_score(tmp_path: Path) -> None:
    runtime = PinnedRuntime(
        tmp_path, cache=tmp_path / "cache", output=tmp_path / "out", episode_id="opaque"
    )
    with pytest.raises(RuntimeFailure, match="not_prepared"):
        runtime.grade()
    runtime.close()
    runtime.close()


@pytest.mark.parametrize("episode_id", ["../case", "", "case\nsecret", "a" * 80])
def test_episode_alias_is_a_bounded_opaque_identifier(tmp_path: Path, episode_id: str) -> None:
    with pytest.raises(ValueError):
        PinnedRuntime(tmp_path, cache=tmp_path / "c", output=tmp_path / "o", episode_id=episode_id)


def test_each_published_case_dispatches_to_its_original_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cases = sorted((Path(__file__).resolve().parents[1] / "cases").glob("*/case.json"))
    launched: list[str] = []

    def launch(self: PinnedRuntime) -> None:
        launched.append(
            json.loads((self.case_dir / "case.json").read_text())["global_suite_evidence"][
                "implementation_commit"
            ]
        )

    def exchange(
        self: PinnedRuntime, operation: str, payload: dict[str, Any], **kwargs: Any
    ) -> dict[str, Any]:
        case = json.loads((self.case_dir / "case.json").read_text())
        assert operation == "prepare"
        return {
            "safe_task": {
                "episode_id": self.episode_id,
                "bad_pr_title": case["bad_pr"]["title"],
                "bad_diff": "public diff",
            },
            "grader_sha256": case["validation"]["grader_sha256"],
            "policy_sha256": case["validation"]["policy_sha256"],
        }

    monkeypatch.setattr(PinnedRuntime, "_launch", launch)
    monkeypatch.setattr(PinnedRuntime, "_exchange", exchange)
    fingerprints = set()
    for index, path in enumerate(cases):
        with PinnedRuntime(
            path.parent,
            cache=tmp_path / "cache",
            output=tmp_path / str(index),
            episode_id=f"episode-{index}",
        ) as runtime:
            metadata = runtime.prepare()
            fingerprints.add(metadata["grader_sha256"])
            assert set(metadata["safe_task"]) == {"episode_id", "bad_pr_title", "bad_diff"}
    assert len(set(launched)) == 4
    assert len(fingerprints) == 4


@pytest.mark.parametrize("score", [True, 1.0, [], "1"])
def test_malformed_grade_scores_are_infrastructure_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    score: object,
) -> None:
    runtime = PinnedRuntime(
        tmp_path, cache=tmp_path / "c", output=tmp_path / "o", episode_id="opaque"
    )
    runtime._metadata = {}
    runtime._submission = b"test"
    monkeypatch.setattr(
        runtime, "_exchange", lambda *_args, **_kwargs: {"status": "passed", "score": score}
    )
    with pytest.raises(RuntimeFailure, match="grade_report_invalid"):
        runtime.grade()


def test_malformed_grade_status_is_an_infrastructure_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = PinnedRuntime(
        tmp_path, cache=tmp_path / "c", output=tmp_path / "o", episode_id="opaque"
    )
    runtime._metadata = {}
    runtime._submission = b"test"
    monkeypatch.setattr(
        runtime, "_exchange", lambda *_args, **_kwargs: {"status": [], "score": None}
    )
    with pytest.raises(RuntimeFailure, match="grade_report_invalid"):
        runtime.grade()


def _protocol_worker(tmp_path: Path, code: str) -> PinnedRuntime:
    runtime = PinnedRuntime(
        tmp_path, cache=tmp_path / "c", output=tmp_path / "o", episode_id="opaque"
    )
    runtime._metadata = {}
    runtime._process = subprocess.Popen(
        [sys.executable, "-u", "-c", code],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    return runtime


def test_legal_64k_control_character_submission_reaches_worker(tmp_path: Path) -> None:
    worker = """import json,sys
for line in sys.stdin:
 request=json.loads(line)
 result={'closed':True} if request['operation']=='close' else {'restored':True}
 print(json.dumps({**request,'result':result}),flush=True)
 if request['operation']=='close':break
"""
    with _protocol_worker(tmp_path, worker) as runtime:
        source = b"\x01" * 65536
        runtime.restore_submission(source)
        assert runtime.get_submission() == source


@pytest.mark.parametrize("bad_reply", ["wrong_episode", "extra_json", "nested_json"])
def test_protocol_fault_kills_worker_and_cannot_feed_later_tools(
    tmp_path: Path,
    bad_reply: str,
) -> None:
    worker = f"""import json,sys,time
request=json.loads(sys.stdin.readline())
reply={{**request,'result':{{'ok':True}}}}
if {bad_reply!r}=='wrong_episode': reply['episode_id']='another'
if {bad_reply!r}=='nested_json':
 sys.stdout.write('['*10000+'0'+']'*10000+'\\n')
else:
 sys.stdout.write(json.dumps(reply)+'\\n'+('{{}}\\n' if {bad_reply!r}=='extra_json' else ''))
sys.stdout.flush()
time.sleep(30)
"""
    runtime = _protocol_worker(tmp_path, worker)
    process = runtime._process
    assert process is not None
    with pytest.raises(RuntimeFailure):
        runtime.tool("list_dir", {"path": "."})
    assert process.poll() is not None
    with pytest.raises(RuntimeFailure, match="not_prepared"):
        runtime.tool("list_dir", {"path": "."})
    runtime.close()


def test_worker_not_reading_stdin_cannot_block_past_request_deadline(tmp_path: Path) -> None:
    runtime = _protocol_worker(tmp_path, "import time;time.sleep(3)")
    started = time.monotonic()
    with pytest.raises(RuntimeFailure):
        runtime._exchange("restore", {"content": "x" * 65536}, timeout=0.05)
    assert time.monotonic() - started < 1
    runtime.close()


def test_failed_restore_ack_cannot_commit_new_host_submission(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = PinnedRuntime(
        tmp_path, cache=tmp_path / "c", output=tmp_path / "o", episode_id="opaque"
    )
    runtime._metadata = {}
    runtime._submission = b"old"
    monkeypatch.setattr(runtime, "_exchange", lambda *_args, **_kwargs: {"restored": False})
    with pytest.raises(RuntimeFailure, match="restore_report_invalid"):
        runtime.restore_submission(b"new")
    assert runtime._submission == b"old"


@pytest.mark.parametrize("ack,exit_code", [(False, 0), (True, 7)])
def test_normal_close_requires_cleanup_ack_and_successful_exit(
    tmp_path: Path,
    ack: bool,
    exit_code: int,
) -> None:
    worker = f"""import json,sys
request=json.loads(sys.stdin.readline())
print(json.dumps({{**request,'result':{{'closed':{ack!r}}}}}),flush=True)
sys.exit({exit_code})
"""
    runtime = _protocol_worker(tmp_path, worker)
    process = runtime._process
    with pytest.raises(RuntimeFailure, match="worker_cleanup_failed"):
        runtime.close()
    assert process is not None and process.poll() is not None


def test_context_cleanup_preserves_an_existing_error(tmp_path: Path) -> None:
    worker = """import json,sys
request=json.loads(sys.stdin.readline())
print(json.dumps({**request,'result':{'closed':False}}),flush=True)
"""
    runtime = _protocol_worker(tmp_path, worker)
    with pytest.raises(ValueError, match="original"), runtime:
        raise ValueError("original")
    assert runtime._process is not None and runtime._process.poll() is not None


def test_policy_analysis_worker_fault_is_a_runtime_failure_not_feedback(tmp_path: Path) -> None:
    worker = """import json,os,sys,time
request=json.loads(sys.stdin.readline())
os.close(1)
os.close(2)
time.sleep(3)
"""
    runtime = _protocol_worker(tmp_path, worker)
    with pytest.raises(RuntimeFailure, match="worker_lost"):
        runtime.tool("run_tests", {"node_ids": []})
    assert runtime._process is not None and runtime._process.poll() is not None
    runtime.close()
