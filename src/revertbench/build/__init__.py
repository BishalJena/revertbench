"""Materialize controlled regression pairs without executing repository code."""

from revertbench.build.candidate import BuiltCandidate, canonical_tree_digest, materialize_candidate
from revertbench.build.patch import BuildError

__all__ = ["BuildError", "BuiltCandidate", "canonical_tree_digest", "materialize_candidate"]
