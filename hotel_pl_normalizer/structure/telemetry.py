from __future__ import annotations

from datetime import datetime

from hotel_pl_normalizer.models.run import (
    ModelCallTrace,
    RunMetrics,
    RunTelemetry,
    StageRun,
    StageTrace,
)


def build_run_telemetry(
    *,
    run_id: str,
    started_at: str,
    completed_at: str,
    duration_ms: int,
    stages: list[StageRun],
) -> RunTelemetry:
    calls: list[ModelCallTrace] = []
    stage_traces: list[StageTrace] = []
    for stage in stages:
        stage_calls = [
            _call_trace(len(calls) + index, stage.stage_name, usage)
            for index, usage in enumerate(stage.usage, start=1)
        ]
        calls.extend(stage_calls)
        stage_started = stage.started_at or _first_timestamp(
            stage_calls,
            "started_at",
        )
        stage_completed = stage.completed_at or _last_timestamp(
            stage_calls,
            "completed_at",
        )
        stage_duration = stage.duration_ms
        if stage_duration is None:
            stage_duration = _duration_between(stage_started, stage_completed)
        stage_traces.append(
            StageTrace(
                stage_name=stage.stage_name,
                status=stage.status,
                started_at=stage_started,
                completed_at=stage_completed,
                duration_ms=stage_duration,
                model_duration_ms=sum(item.duration_ms for item in stage_calls),
                model_calls=len(stage_calls),
                cache_hits=sum(item.cache_hit for item in stage_calls),
                repair_passes=stage.repair_passes,
                prompt_tokens=sum(item.prompt_tokens for item in stage_calls),
                output_tokens=sum(item.output_tokens for item in stage_calls),
                total_tokens=sum(item.total_tokens for item in stage_calls),
                artifact_paths=stage.artifact_paths,
            )
        )

    return RunTelemetry(
        run_id=run_id,
        started_at=started_at,
        completed_at=completed_at,
        metrics=RunMetrics(
            duration_ms=duration_ms,
            stage_count=len(stages),
            model_call_count=len(calls),
            failed_model_call_count=sum(
                item.status == "fail" for item in calls
            ),
            cache_hit_count=sum(item.cache_hit for item in calls),
            json_repair_count=sum(item.json_repair for item in calls),
            repair_pass_count=sum(item.repair_passes for item in stages),
            prompt_chars=sum(item.prompt_chars for item in calls),
            estimated_prompt_tokens=sum(
                item.estimated_prompt_tokens for item in calls
            ),
            prompt_tokens=sum(item.prompt_tokens for item in calls),
            output_tokens=sum(item.output_tokens for item in calls),
            total_tokens=sum(item.total_tokens for item in calls),
            cached_tokens=sum(item.cached_tokens for item in calls),
            cache_write_tokens=sum(item.cache_write_tokens for item in calls),
            thoughts_tokens=sum(item.thoughts_tokens for item in calls),
            model_duration_ms=sum(item.duration_ms for item in calls),
            # Deliberately not passed: see RunMetrics. This function is given the
            # structure stages and nothing else, so it cannot count facts, review
            # items or validator outcomes, and writing 0 would claim it had.
        ),
        stages=stage_traces,
        model_calls=calls,
    )


def _call_trace(
    sequence: int,
    stage_name: str,
    usage: dict,
) -> ModelCallTrace:
    return ModelCallTrace(
        sequence=sequence,
        stage_name=stage_name,
        provider=usage.get("provider"),
        model_name=usage.get("model_name"),
        response_model=usage.get("response_model"),
        status=usage.get("status", "pass"),
        started_at=usage.get("started_at"),
        completed_at=usage.get("completed_at"),
        duration_ms=usage.get("duration_ms", 0),
        cache_hit=usage.get("cache_hit", False),
        json_repair=usage.get("json_repair", False),
        prompt_chars=usage.get("prompt_chars", 0),
        estimated_prompt_tokens=usage.get("estimated_prompt_tokens", 0),
        prompt_tokens=usage.get("prompt_token_count", 0),
        output_tokens=usage.get("candidates_token_count", 0),
        total_tokens=usage.get("total_token_count", 0),
        cached_tokens=usage.get("cached_content_token_count", 0),
        cache_write_tokens=usage.get("cache_write_token_count", 0),
        thoughts_tokens=usage.get("thoughts_token_count", 0),
        tool_loop=bool(usage.get("tool_loop", False)),
        tool_calls=int(usage.get("tool_calls", 0) or 0),
        error_type=usage.get("error_type"),
        error_message=usage.get("error_message"),
    )


def _first_timestamp(
    calls: list[ModelCallTrace],
    field: str,
) -> str | None:
    return next(
        (
            value
            for item in calls
            if (value := getattr(item, field)) is not None
        ),
        None,
    )


def _last_timestamp(
    calls: list[ModelCallTrace],
    field: str,
) -> str | None:
    return next(
        (
            value
            for item in reversed(calls)
            if (value := getattr(item, field)) is not None
        ),
        None,
    )


def _duration_between(
    started_at: str | None,
    completed_at: str | None,
) -> int:
    if started_at is None or completed_at is None:
        return 0
    return round(
        (
            datetime.fromisoformat(completed_at)
            - datetime.fromisoformat(started_at)
        ).total_seconds()
        * 1000
    )
