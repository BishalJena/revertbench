"""Canonical fingerprints shared by host admission and immutable image evidence."""

import hashlib
from pathlib import Path


def package_identity(package_root: Path) -> tuple[str, str]:
    """Hash sorted package Python paths and exact bytes, then the policy separately."""
    digest = hashlib.sha256()
    for path in sorted(package_root.rglob("*.py")):
        name = path.relative_to(package_root).as_posix().encode("utf-8")
        data = path.read_bytes()
        digest.update(len(name).to_bytes(8, "big") + name)
        digest.update(len(data).to_bytes(8, "big") + data)
    policy = hashlib.sha256((package_root / "grade/policy.py").read_bytes()).hexdigest()
    return digest.hexdigest(), policy


def host_code_identity() -> tuple[str, str]:
    """Identify this controller's trusted package snapshot, which must remain fixed during a run."""
    return package_identity(Path(__file__).resolve().parents[1])
