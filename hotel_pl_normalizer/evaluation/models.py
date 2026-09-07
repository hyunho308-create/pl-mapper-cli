"""Local check results and evidence for a pending Codex review."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from pydantic import Field

from hotel_pl_normalizer.models.common import StrictModel


class MechanicalStatus(str, Enum):
    PASS = "pass"
    WARNING = "warning"
    FAIL = "fail"


class MechanicalCheck(StrictModel):
    code: str
    status: MechanicalStatus
    message: str
    evidence: list[str] = Field(default_factory=list)


@dataclass
class EvaluationResult:
    """Prepared evidence, never an automatic semantic approval."""

    source_name: str
    periods: list[dict[str, str]] = field(default_factory=list)
    mechanical_checks: list[MechanicalCheck] = field(default_factory=list)
    coverage: list[dict[str, Any]] = field(default_factory=list)
    children: list[dict[str, Any]] = field(default_factory=list)
    definitions: list[dict[str, str]] = field(default_factory=list)
    rows: list[dict[str, Any]] = field(default_factory=list)
    findings: list[dict[str, Any]] = field(default_factory=list)
    context_notes: list[dict[str, Any]] = field(default_factory=list)
    evidence_complete: bool = True
    elapsed_seconds: float = 0.0

    @property
    def status(self) -> str:
        if not self.evidence_complete:
            return "incomplete"
        if any(c.status == MechanicalStatus.FAIL for c in self.mechanical_checks):
            return "issues_found"
        if any(f["severity"] == "error" for f in self.findings):
            return "issues_found"
        return "ready_for_review"
