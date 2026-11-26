"""The artifact check and the stored artifact: the seam of #29 (Retro validation).

``validate_and_store_artifact`` is called for a run that ended ``done`` with the lease held, the
sandbox rule satisfied and the retro in state ``validating``. It returns the ``Mapped`` row the
run ends with: ``done`` (the artifact was checked, redacted and stored) or ``failed`` with a
validation reason (exit 22).

TODO(#29): checks 2 to 8 of Retro validation (the frozen file, the closed schema, the identity
echoes, the marker, the evidence references, redaction), the ``retro_artifacts`` row and the
``done`` state. Only check 1 is here, because it needs nothing but the status document:
``report.implicit`` true or ``report.artifact`` null is ``no_artifact``. A ``done`` run with an
artifact raises an internal error (the run stays ``validating``; recovery marks it
``runner_lost``).
"""

from __future__ import annotations

from typing import Any

from . import errors
from .retrooutcome import VALIDATION_REASONS, Collected, Mapped

__all__ = ["VALIDATION_REASONS", "validate_and_store_artifact"]


def validate_and_store_artifact(collected: Collected, **_context: Any) -> Mapped:
    report = collected.report
    if report is None or report.get("implicit") is not False or report.get("artifact") is None:
        return Mapped("failed", "no_artifact", 22, outcome="done")
    raise errors.not_implemented_artifact()
