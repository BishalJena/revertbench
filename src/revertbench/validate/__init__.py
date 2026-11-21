"""Machine validation never substitutes for explicit human case review."""

from revertbench.validate.baseline import DockerBaselineRunner, DockerReferenceProbe
from revertbench.validate.controller import (
    MachineValidationReport,
    ProbeResult,
    validate_built_candidate,
)

__all__ = [
    "DockerBaselineRunner",
    "DockerReferenceProbe",
    "MachineValidationReport",
    "ProbeResult",
    "validate_built_candidate",
]
