from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import ConfigDict

from .common import StrictModel


class PipelineStatus(str, Enum):
    PASS = "pass"
    WARNING = "warning"
    NEEDS_REVIEW = "needs_review"
    FAIL = "fail"


class StageStatus(str, Enum):
    PASS = "pass"
    WARNING = "warning"
    SKIPPED = "skipped"
    FAIL = "fail"


class StageRun(StrictModel):
    stage_name: str
    status: StageStatus
    started_at: str | None = None
    completed_at: str | None = None
    duration_ms: int | None = None
    repair_passes: int = 0
    usage: list[dict[str, Any]] = []
    artifact_paths: dict[str, str] = {}
    messages: list[str] = []


class ModelCallTrace(StrictModel):
    sequence: int
    stage_name: str
    provider: str | None = None
    model_name: str | None = None
    response_model: str | None = None
    status: str = "pass"
    started_at: str | None = None
    completed_at: str | None = None
    duration_ms: int = 0
    cache_hit: bool = False
    json_repair: bool = False
    prompt_chars: int = 0
    estimated_prompt_tokens: int = 0
    prompt_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    cached_tokens: int = 0
    cache_write_tokens: int = 0
    thoughts_tokens: int = 0
    # Whether this call ran through the tool loop, and how many tools it asked
    # for. Both are needed to tell "tools were offered and declined" apart from
    # "tools were never offered" -- without them a stage that ignores its tools is
    # indistinguishable from one that never had any, and the tool arms cannot be
    # read at all.
    tool_loop: bool = False
    tool_calls: int = 0
    error_type: str | None = None
    error_message: str | None = None


class StageTrace(StrictModel):
    stage_name: str
    status: StageStatus
    started_at: str | None = None
    completed_at: str | None = None
    duration_ms: int = 0
    model_duration_ms: int = 0
    model_calls: int = 0
    cache_hits: int = 0
    repair_passes: int = 0
    prompt_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    artifact_paths: dict[str, str] = {}


class RunMetrics(StrictModel):
    duration_ms: int
    stage_count: int
    model_call_count: int
    failed_model_call_count: int
    cache_hit_count: int
    json_repair_count: int
    repair_pass_count: int
    prompt_chars: int
    estimated_prompt_tokens: int
    prompt_tokens: int
    output_tokens: int
    total_tokens: int
    cached_tokens: int
    cache_write_tokens: int = 0
    thoughts_tokens: int
    model_duration_ms: int
    # None means "not measured here", and that is what these always are: they
    # describe the mapping stage's output -- facts extracted, review items raised,
    # validator checks by outcome -- while this telemetry only ever sees the
    # structure stages. They used to be written as 0, which reads as "no review
    # items" rather than "nobody counted", and those are different claims.
    # `run_log.py` already reports the real figures where they exist, under
    # `outcome.review_items` and `outcome.checks_raised`.
    #
    # Kept rather than deleted because saved `run.json` artifacts carry these keys
    # and this model forbids extras, so removing them makes every existing
    # artifact unloadable. Accepting `int` keeps those readable.
    fact_count: int | None = None
    review_issue_count: int | None = None
    validation_pass_count: int | None = None
    validation_warning_count: int | None = None
    validation_fail_count: int | None = None
    validation_skipped_count: int | None = None


class RunTelemetry(StrictModel):
    run_id: str
    started_at: str
    completed_at: str
    metrics: RunMetrics
    stages: list[StageTrace]
    model_calls: list[ModelCallTrace]


class StructureRun(StrictModel):
    # Old saved full-pipeline runs contain additional mapping fields. Ignoring
    # them keeps those runs usable as upstream inputs during the transition.
    model_config = ConfigDict(extra="ignore")

    run_id: str
    workbook_id: str
    source_filename: str
    property_code: str | None = None
    requested_period: str
    status: PipelineStatus
    stages: list[StageRun]
    telemetry: RunTelemetry | None = None
    artifact_paths: dict[str, str] = {}
