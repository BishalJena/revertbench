"""A derived repair copies verified historical statements, never an invented algorithm."""

import difflib
import hashlib
import json
from dataclasses import replace
from pathlib import Path

import docker
import pytest
from pydantic import ValidationError

from revertbench.build.derivation import DerivedFunctionRepair
from revertbench.build.patch import BuildError
from test_build_candidate import _Repository

BEFORE = (
    'OFFSET = 0\n\ndef normalize(value):\n    """Historical description."""\n'
    "    result = value.strip()\n    return result\n\ndef untouched():\n    return 1\n"
)
AFTER = BEFORE.replace("Historical description.", "Updated documentation.").replace(
    "return result\n", "return result.lower()\n"
)
BAD = (
    "# Older formatting and location.\nOFFSET=0\n\ndef normalize( value ):\n"
    '    """Older description."""\n    result=value.strip()\n    return result\n'
    "\n# Keep these bad-tree bytes.\ndef untouched():\n    return 2\n"
)


def _diff(before: str, after: str) -> str:
    return "diff --git a/toy.py b/toy.py\n" + "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile="a/toy.py",
            tofile="b/toy.py",
        )
    )


def _derive(before: str = BEFORE, after: str = AFTER, bad: str = BAD) -> DerivedFunctionRepair:
    from revertbench.build.derivation import derive_function_body_patch

    return derive_function_body_patch(
        before,
        after,
        bad,
        path="toy.py",
        function_name="normalize",
        historical_patch=_diff(before, after),
    )


def test_derivation_preserves_bad_outside_bytes_and_copies_full_historical_algorithm() -> None:
    result = _derive()
    assert result.repaired_source == BAD.replace(
        "    return result\n", "    return result.lower()\n"
    )
    assert result.patch == _diff(BAD, result.repaired_source)
    proof = result.proof
    assert (
        proof.version == "historical_function_body_v1"
        and proof.path == "toy.py"
        and proof.function == "normalize"
    )
    assert proof.derived_patch_sha256 == hashlib.sha256(result.patch.encode()).hexdigest()
    assert proof.historical_diff.sha256 == hashlib.sha256(_diff(BEFORE, AFTER).encode()).hexdigest()
    assert proof.before_body_sha256 != proof.after_body_sha256
    assert _derive() == result


@pytest.mark.parametrize(
    "kind", ["one_line", "closing_header_line", "global_default", "decorator_drift", "annotation"]
)
def test_derived_profile_rejects_definition_time_dependencies_and_shared_header_lines(
    kind: str,
) -> None:
    before = "def normalize(value):\n    return value\n"
    after = before.replace("return value", "return value + 1")
    bad = before
    if kind == "one_line":
        before = "def normalize(value): return value\n"
        after = "def normalize(value): return value + 1\n"
        bad = "def normalize( value ): return value\n"
    elif kind == "closing_header_line":
        bad = "def normalize(\n value\n): return value\n"
    elif kind == "global_default":
        before = "DEFAULT=1\n" + before.replace("value):", "value=DEFAULT):")
        after = "DEFAULT=1\n" + after.replace("value):", "value=DEFAULT):")
        bad = before.replace("DEFAULT=1", "DEFAULT=10")
    elif kind == "decorator_drift":
        decorator = "def decorate(function):\n    return function\n"
        before = decorator + "@decorate\n" + before
        after = decorator + "@decorate\n" + after
        bad = before.replace("return function", "return None")
    else:
        before = "DEFAULT=1\n" + before.replace("value):", "value: DEFAULT):")
        after = "DEFAULT=1\n" + after.replace("value):", "value: DEFAULT):")
        bad = before.replace("DEFAULT=1", "DEFAULT=10")
    with pytest.raises(BuildError):
        _derive(before, after, bad)


def test_derived_profile_accepts_literal_none_default_without_reformatting_header() -> None:
    before = BEFORE.replace("normalize(value)", "normalize(value=None)")
    after = AFTER.replace("normalize(value)", "normalize(value=None)")
    bad = BAD.replace("normalize( value )", "normalize( value=None )")
    result = _derive(before, after, bad)
    assert "def normalize( value=None ):\n" in result.repaired_source


@pytest.mark.parametrize(
    "binding",
    [
        "try:\n    int('bad')\nexcept ValueError as normalize:\n    pass\n",
        "match 1:\n    case normalize:\n        pass\n",
        "match [1]:\n    case [*normalize]:\n        pass\n",
        "match {}:\n    case {**normalize}:\n        pass\n",
    ],
)
def test_derived_target_rejects_module_exception_and_pattern_rebinding(binding: str) -> None:
    with pytest.raises(BuildError, match="ambiguous"):
        _derive(BEFORE + binding, AFTER + binding, BAD + binding)


@pytest.mark.parametrize(
    "mutation",
    [
        "bad_algorithm",
        "historical_signature",
        "bad_signature",
        "historical_decorator",
        "extra_function",
        "outside_import",
        "new_function_import",
        "new_unbound_global",
        "changed_dependency",
        "duplicate_target",
        "target_rebinding",
        "no_algorithm_change",
        "unsupported_encoding",
    ],
)
def test_derivation_rejects_drift_ambiguous_targets_and_new_dependencies(mutation: str) -> None:
    before, after, bad = BEFORE, AFTER, BAD
    if mutation == "bad_algorithm":
        bad = bad.replace("return result\n", 'return result + "drift"\n')
    elif mutation == "historical_signature":
        after = after.replace("normalize(value)", "normalize(value, extra=1)")
    elif mutation == "bad_signature":
        bad = bad.replace("normalize( value )", "normalize( value, extra=1 )")
    elif mutation == "historical_decorator":
        after = after.replace("def normalize", "@decorator\ndef normalize")
    elif mutation == "extra_function":
        after = after.replace("return 1", "return 3")
    elif mutation == "outside_import":
        after = "import os\n" + after
    elif mutation == "new_function_import":
        after = after.replace("return result.lower()", "import os\n    return result.lower()")
    elif mutation == "new_unbound_global":
        after = after.replace("return result.lower()", "return external(result)")
    elif mutation == "changed_dependency":
        after = after.replace("return result.lower()", "return OFFSET + len(result)")
        bad = bad.replace("OFFSET=0", "OFFSET=8")
    elif mutation == "duplicate_target":
        bad += "\ndef normalize(value):\n    return value\n"
    elif mutation == "target_rebinding":
        bad += "\nnormalize = lambda x: x\n"
    elif mutation == "no_algorithm_change":
        after = before.replace("Historical description.", "New docs only.")
    else:
        before = "# coding: unicode_escape\n" + before
    with pytest.raises(BuildError):
        _derive(before, after, bad)


def test_default_candidate_mode_stays_native_and_explicit_target_is_validated() -> None:
    from revertbench.mine.models import candidate_from_json, candidate_to_json
    from test_build_candidate import _candidate

    candidate = _candidate()
    assert candidate.repair_derivation == "historical_diff" and candidate.repair_target is None
    derived = replace(
        candidate,
        repair_derivation="historical_function_body_v1",
        repair_target="normalize",
        production_patch=_derive().patch,
    )
    assert candidate_from_json(candidate_to_json(derived)) == derived
    for changes in (
        {"repair_derivation": "invented"},
        {"repair_target": "normalize"},
        {"repair_derivation": "historical_function_body_v1"},
        {"repair_derivation": "historical_function_body_v1", "repair_target": "a.b"},
    ):
        with pytest.raises(ValueError):
            candidate_from_json(json.dumps({**json.loads(candidate_to_json(candidate)), **changes}))


def _repository() -> _Repository:

    class Repository(_Repository):
        def file_text(self, commit: str, path: str) -> str:
            if path == "toy.py":
                return (
                    AFTER
                    if commit == "4" * 40
                    else BEFORE
                    if commit == "3" * 40
                    else BAD.replace("return result\n", "return result.lower()\n")
                    if commit == "1" * 40
                    else BAD
                )
            return super().file_text(commit, path)

        def export_tree(self, commit: str, destination: Path) -> Path:
            destination.mkdir()
            (destination / "toy.py").write_text(self.file_text(commit, "toy.py"))
            return destination

        def diff(self, before: str, after: str, paths: tuple[str, ...] = ()) -> str:
            return (
                _diff(BEFORE, AFTER)
                if before == "3" * 40
                else _diff(BAD.replace("return result\n", "return result.lower()\n"), BAD)
            )

        def apply_patch(self, tree: Path, patch: str, *, check: bool = False) -> None:
            assert patch == _derive().patch
            if not check:
                source = _derive().repaired_source
                if self.extra_change:
                    source += "\nUNRELATED = 1\n"
                (tree / "toy.py").write_text(source)

    return Repository()


def test_builder_recomputes_proof_and_rejects_forged_candidate_patch(tmp_path: Path) -> None:
    from revertbench.build import materialize_candidate
    from test_build_candidate import _candidate

    candidate = replace(
        _candidate(),
        repair_derivation="historical_function_body_v1",
        repair_target="normalize",
        production_patch=_derive().patch,
    )
    built = materialize_candidate(_repository(), candidate, tmp_path / "built")
    assert built.repair_derivation == _derive().proof
    assert built.historical_production_diff == _diff(BEFORE, AFTER)
    assert (built.repaired_tree / "toy.py").read_text() == _derive().repaired_source
    with pytest.raises(BuildError, match=r"deriv|historical"):
        materialize_candidate(
            _repository(),
            replace(
                candidate,
                production_patch=candidate.production_patch.replace(
                    "result.lower()", "result.upper()"
                ),
            ),
            tmp_path / "forged",
        )
    with pytest.raises(BuildError):
        materialize_candidate(
            _repository(),
            replace(candidate, repair_derivation="historical_diff", repair_target=None),
            tmp_path / "not_explicit",
        )
    repo = _repository()
    repo.extra_change = True
    with pytest.raises(BuildError, match=r"deriv|unexpected"):
        materialize_candidate(repo, candidate, tmp_path / "extra")


def test_materialization_passes_absolute_owned_snapshot_paths_to_git(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from revertbench.build import materialize_candidate
    from test_build_candidate import _candidate

    monkeypatch.chdir(tmp_path)
    repository = _repository()
    original = repository.apply_patch

    def apply(tree: Path, patch: str, *, check: bool = False) -> None:
        assert tree.is_absolute()
        original(tree, patch, check=check)

    monkeypatch.setattr(repository, "apply_patch", apply)
    candidate = replace(
        _candidate(),
        repair_derivation="historical_function_body_v1",
        repair_target="normalize",
        production_patch=_derive().patch,
    )
    materialize_candidate(repository, candidate, Path("relative-build"))


def test_optional_case_proof_binds_inputs_without_changing_legacy_digest() -> None:
    from revertbench.review import load_case
    from revertbench.schema import Case
    from test_schema import _reviewable_case

    root = Path(__file__).resolve().parents[1] / "cases"
    for path in root.glob("*/case.json"):
        case = load_case(path)
        review = case.human_review or case.agent_review
        assert review is not None and case.review_input_digest() == review.case_inputs_sha256
    case = _reviewable_case()
    proof = _derive().proof.model_copy(update={"derived_patch_sha256": case.repair.patch_sha256})
    with_proof = Case.model_validate({**case.model_dump(mode="python"), "repair_derivation": proof})
    assert with_proof.review_input_digest() != case.review_input_digest()
    assert (
        Case.model_validate(
            {**case.model_dump(mode="python"), "repair_derivation": None}
        ).review_input_digest()
        == case.review_input_digest()
    )
    with pytest.raises(ValidationError):
        Case.model_validate(
            {**case.model_dump(mode="python"), "repair_derivation": _derive().proof}
        )


def test_review_requires_machine_report_to_bind_case_repair_proof(tmp_path: Path) -> None:
    from revertbench.review import ReviewError, load_case, record_review, write_case
    from revertbench.schema import Case
    from test_review import _packet

    path = _packet(tmp_path)
    case = load_case(path)
    historical = _diff(BEFORE, AFTER).encode()
    (tmp_path / "historical-production.diff").write_bytes(historical)
    proof = _derive().proof.model_copy(update={"derived_patch_sha256": case.repair.patch_sha256})
    case = Case.model_validate({**case.model_dump(mode="python"), "repair_derivation": proof})
    write_case(case, path)
    with pytest.raises(ReviewError, match=r"machine|deriv"):
        record_review(
            path,
            decision="accept",
            reviewer="Codex",
            notes="Checked",
            reviewer_kind="agent",
            authorization_note="Delegated",
        )
    report = json.loads((tmp_path / "validation.json").read_bytes())
    report["repair_derivation_sha256"] = proof.content_digest()
    raw = json.dumps(report).encode()
    (tmp_path / "validation.json").write_bytes(raw)
    data = case.model_dump(mode="python")
    data["validation"] = case.validation.model_copy(
        update={"report_sha256": hashlib.sha256(raw).hexdigest()}
    )
    write_case(Case.model_validate(data), path)
    accepted = record_review(
        path,
        decision="accept",
        reviewer="Codex",
        notes="Checked historical derivation",
        reviewer_kind="agent",
        authorization_note="Delegated",
    )
    assert accepted.validation.status == "accepted"


def test_controller_binds_and_checks_full_body_proof_before_running_tests(tmp_path: Path) -> None:
    from revertbench.build import materialize_candidate
    from revertbench.validate import validate_built_candidate
    from revertbench.validate.baseline import BaselineResult
    from test_build_candidate import _candidate

    candidate = replace(
        _candidate(),
        repair_derivation="historical_function_body_v1",
        repair_target="normalize",
        production_patch=_derive().patch,
    )
    built = materialize_candidate(_repository(), candidate, tmp_path / "built")
    report = validate_built_candidate(
        built,
        image="sha256:" + "a" * 64,
        baseline_backend=lambda tree: BaselineResult(status="invalid_run"),
    )
    assert built.repair_derivation is not None
    assert report.repair_derivation_sha256 == built.repair_derivation.content_digest()
    assert report.status == "pending_infrastructure"
    forged = built.repair_derivation.model_copy(update={"after_body_sha256": "0" * 64})

    def not_run(tree: Path) -> BaselineResult:
        pytest.fail("forged proof reached execution")

    rejected = validate_built_candidate(
        replace(built, repair_derivation=forged),
        image="sha256:" + "a" * 64,
        baseline_backend=not_run,
    )
    assert rejected.status == "rejected" and rejected.reason == "repair_derivation_mismatch"


def test_derived_publication_permits_only_its_single_declared_extra_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1]))
    from scripts import reproduce_case

    native = (
        Path(__file__).resolve().parents[1] / "cases/more-itertools-71b76842d32c-5a1d64d1-be44ad73"
    )
    import shutil

    shutil.copytree(native, tmp_path / "case")
    folder = tmp_path / "case"
    case = json.loads((folder / "case.json").read_bytes())
    case["repair_derivation"] = _derive().proof.model_dump(mode="json")
    (folder / "case.json").write_text(json.dumps(case))
    (folder / "historical-production.diff").write_text(_diff(BEFORE, AFTER))
    manifest = json.loads((folder / "publication-manifest.json").read_bytes())
    for name in ["case.json", "historical-production.diff"]:
        data = (folder / name).read_bytes()
        manifest["files"][name] = {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
    (folder / "publication-manifest.json").write_text(json.dumps(manifest))
    assert reproduce_case.read_publication(folder)["files"]["historical-production.diff"]
    (folder / "extra.txt").write_text("unapproved")
    with pytest.raises(reproduce_case.ReproductionError, match=r"allowlist|artifact"):
        reproduce_case.read_publication(folder)
    manifest["files"]["extra.txt"] = {
        "sha256": hashlib.sha256(b"unapproved").hexdigest(),
        "bytes": 10,
    }
    (folder / "publication-manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(reproduce_case.ReproductionError, match=r"allowlist|artifact"):
        reproduce_case.read_publication(folder)


@pytest.mark.docker
def test_explicit_derived_repair_runs_normal_docker_case_validation(
    tmp_path: Path, docker_client: docker.DockerClient, worker_image: str
) -> None:
    from revertbench.build import canonical_tree_digest, materialize_candidate
    from revertbench.validate import validate_built_candidate
    from test_build_candidate import _candidate

    candidate = replace(
        _candidate(),
        repair_derivation="historical_function_body_v1",
        repair_target="normalize",
        production_patch=_derive().patch,
    )
    built = materialize_candidate(_repository(), candidate, tmp_path / "built")
    trees = {
        "base": built.base_tree,
        "bad": built.bad_tree,
        "historical_fix": built.historical_fix_tree,
        "repaired": built.repaired_tree,
    }
    for tree in trees.values():
        (tree / "tests").mkdir()
        (tree / "tests/test_existing.py").write_text(
            'from toy import normalize\ndef test_existing():\n    assert normalize("x") == "x"\n'
        )
    built = replace(
        built, source_hashes={role: canonical_tree_digest(tree) for role, tree in trees.items()}
    )
    report = validate_built_candidate(built, image=worker_image, client=docker_client)
    assert report.status == "machine_validated", report.model_dump_json()
    assert built.repair_derivation is not None
    assert report.repair_derivation_sha256 == built.repair_derivation.content_digest()
    assert report.reference is not None and report.reference.score == 1
