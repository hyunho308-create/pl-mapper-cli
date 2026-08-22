from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict


class StrictModel(BaseModel):
    """Base contract: unexpected fields fail rather than disappearing."""

    model_config = ConfigDict(extra="forbid")


class Severity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
