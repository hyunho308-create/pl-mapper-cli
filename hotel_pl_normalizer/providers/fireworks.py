"""Fireworks/OpenAI-compatible client for typed and tool-driven model calls."""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Callable, TypeVar

from pydantic import BaseModel, ValidationError

from hotel_pl_normalizer.activity import (
    describe_progress,
    describe_round,
    describe_tool_call,
    describe_tool_call_started,
)
from hotel_pl_normalizer.providers.base import (
    JsonModelClient,
    ProviderConfigurationError,
    ProviderResponseTruncated,
    ProviderRunCancelled,
    ProviderToolLoopError,
    elapsed_ms,
    extract_json_object,
    utc_now,
)
from hotel_pl_normalizer.providers.streaming import accumulate_stream
from hotel_pl_normalizer.providers.workbook_tools import (
    WorkbookToolError,
    WorkbookToolset,
)

# Dated, not the bare alias. `accounts/fireworks/models/deepseek-v4-flash` now
# answers 404 "Model not found, inaccessible, and/or not deployed" -- the alias
# is gone from this account and only the dated builds are served. Since this is
# the mapping session's default, the unversioned name took the whole run down on
# its first request.
DEFAULT_FIREWORKS_MODEL = "accounts/fireworks/models/deepseek-v4-flash-0731"
FIREWORKS_API_KEY_ENV_VAR = "FIREWORKS_API_KEY"
FIREWORKS_MODEL_ENV_VAR = "FRESH_START_FIREWORKS_MODEL"
FIREWORKS_CACHE_DIR_ENV_VAR = "FRESH_START_FIREWORKS_CACHE_DIR"
FIREWORKS_MAX_PROMPT_CHARS_ENV_VAR = "FRESH_START_FIREWORKS_MAX_PROMPT_CHARS"
FIREWORKS_MAX_OUTPUT_TOKENS_ENV_VAR = "FRESH_START_FIREWORKS_MAX_OUTPUT_TOKENS"
FIREWORKS_REASONING_EFFORT_ENV_VAR = "FRESH_START_FIREWORKS_REASONING_EFFORT"
FIREWORKS_TEMPERATURE_ENV_VAR = "FRESH_START_FIREWORKS_TEMPERATURE"
DEFAULT_MAX_PROMPT_CHARS = 3_000_000
DEFAULT_MAX_OUTPUT_TOKENS = 131_072
DEFAULT_REASONING_EFFORT = "max"
DEFAULT_TEMPERATURE = 0.0
FIREWORKS_BASE_URL = "https://api.fireworks.ai/inference/v1"
FIREWORKS_INPUT_USD_PER_MTOK = 0.14
FIREWORKS_CACHED_INPUT_USD_PER_MTOK = 0.028
FIREWORKS_OUTPUT_USD_PER_MTOK = 0.28

ModelT = TypeVar("ModelT", bound=BaseModel)


class FireworksConfigurationError(ProviderConfigurationError):
    """The Fireworks client cannot make a valid request."""


class FireworksResponseTruncated(
    FireworksConfigurationError, ProviderResponseTruncated
):
    """Fireworks exhausted the configured completion-token allowance."""


def estimate_fireworks_cost(calls: list[dict]) -> float:
    """Estimate billed Fireworks usage from provider-returned token counters."""
    regular_input = cached_input = output = 0
    for call in calls:
        if call.get("cache_hit"):
            continue
        prompt = int(call.get("prompt_token_count") or 0)
        cached = min(prompt, int(call.get("cached_content_token_count") or 0))
        regular_input += prompt - cached
        cached_input += cached
        output += int(call.get("candidates_token_count") or 0)
        output += int(call.get("thoughts_token_count") or 0)
    return round(
        regular_input * FIREWORKS_INPUT_USD_PER_MTOK / 1_000_000
        + cached_input * FIREWORKS_CACHED_INPUT_USD_PER_MTOK / 1_000_000
        + output * FIREWORKS_OUTPUT_USD_PER_MTOK / 1_000_000,
        6,
    )


def _field(value, name: str, default=None):
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


class FireworksJsonClient(JsonModelClient):
    """Fireworks half of the shared `JsonModelClient` surface."""

    provider = "fireworks"

    def __init__(
        self,
        *,
        model_name: str | None = None,
        repair_model_name: str | None = None,
        reasoning_effort: str | None = None,
    ) -> None:
        self.model_name = model_name or os.environ.get(
            FIREWORKS_MODEL_ENV_VAR, DEFAULT_FIREWORKS_MODEL
        )
        # Retained for interface parity. The first experiment intentionally uses
        # one Fireworks model for both the initial decision and any repair turn.
        self.repair_model_name = repair_model_name or self.model_name
        # Per client, not per process. `max` is the mapping session's setting and
        # suits one long call over the whole workbook; a stage that instead makes
        # twenty short tool round-trips pays that thinking on every one of them,
        # so it needs to be able to ask for less without changing what the mapper
        # does. The env var stays the default for callers that do not care.
        self.reasoning_effort = reasoning_effort or os.environ.get(
            FIREWORKS_REASONING_EFFORT_ENV_VAR, DEFAULT_REASONING_EFFORT
        )
        self.temperature = float(
            os.environ.get(FIREWORKS_TEMPERATURE_ENV_VAR, DEFAULT_TEMPERATURE)
        )
        self.max_output_tokens = int(
            os.environ.get(
                FIREWORKS_MAX_OUTPUT_TOKENS_ENV_VAR, DEFAULT_MAX_OUTPUT_TOKENS
            )
        )
        self.usage_history: list[dict] = []
        self._client_instance = None

    @property
    def repair_client(self) -> "FireworksJsonClient":
        return self

    def generate_patch_model(
        self,
        prompt: str,
        response_model: type[ModelT],
        *,
        toolset: WorkbookToolset | None = None,
        trace: list[dict] | None = None,
    ) -> ModelT:
        if toolset is not None:
            return self.generate_json_model_with_tools(
                prompt, response_model, toolset=toolset, trace=trace
            )
        return self.generate_json_model(prompt, response_model)

    def generate_json_model(self, prompt: str, response_model: type[ModelT]) -> ModelT:
        self._validate_environment()
        full_prompt = self._prompt_with_schema(prompt, response_model)
        self._validate_prompt_size(full_prompt)
        cache_path = self._cache_path(full_prompt, response_model)
        cached = self._read_cache(cache_path, response_model)
        if cached is not None:
            self._record_cache_hit(response_model, len(full_prompt))
            return cached

        client = self._client()
        prompt_cache_key = self._prompt_cache_key(full_prompt, response_model)
        response = self._call(
            client,
            messages=[{"role": "user", "content": full_prompt}],
            response_format=self._response_format(response_model),
            response_model=response_model,
            prompt_chars=len(full_prompt),
            prompt_cache_key=prompt_cache_key,
        )
        text = self._message_text(response)
        try:
            result = response_model.model_validate_json(extract_json_object(text))
        except (ValidationError, ValueError) as exc:
            repair_prompt = self._json_repair_prompt(
                text, response_model, str(exc)
            )
            response = self._call(
                client,
                messages=[{"role": "user", "content": repair_prompt}],
                response_format=self._response_format(response_model),
                response_model=response_model,
                prompt_chars=len(repair_prompt),
                prompt_cache_key=prompt_cache_key,
                json_repair=True,
            )
            result = response_model.model_validate_json(
                extract_json_object(self._message_text(response))
            )
        self._write_cache(cache_path, result)
        return result

    def generate_json_model_with_tools(
        self,
        prompt: str,
        response_model: type[ModelT],
        *,
        toolset: WorkbookToolset,
        max_iterations: int = 10,
        trace: list[dict] | None = None,
        cancel: Callable[[], str | None] | None = None,
        on_activity: Callable[[str], None] | None = None,
    ) -> ModelT:
        self._validate_environment()
        full_prompt = self._prompt_with_schema(prompt, response_model)
        self._validate_prompt_size(full_prompt)
        cache_path = (
            self._cache_path(full_prompt, response_model, extra=toolset.signature())
            if getattr(toolset, "cacheable", True)
            else None
        )
        cached = self._read_cache(cache_path, response_model)
        if cached is not None:
            self._record_cache_hit(response_model, len(full_prompt), tool_calls=0)
            return cached

        client = self._client()
        prompt_cache_key = self._prompt_cache_key(
            full_prompt, response_model, extra=toolset.signature()
        )
        tools = [
            {"type": "function", "function": declaration}
            for declaration in toolset.declarations()
        ]
        messages: list = [{"role": "user", "content": full_prompt}]
        total_tool_calls = 0

        def announce(line: str) -> None:
            # Never let the progress feed take the run down with it: this is
            # commentary, and a failed write to it must not lose a paid round.
            if on_activity is None:
                return
            try:
                on_activity(line)
            except Exception:  # noqa: BLE001 - reporting must not break the run
                pass

        for round_number in range(1, max_iterations + 1):
            if cancel is not None:
                reason = cancel()
                if reason:
                    raise ProviderRunCancelled(reason)
            response = self._call(
                client,
                messages=messages,
                tools=tools,
                response_model=response_model,
                prompt_chars=len(full_prompt),
                prompt_cache_key=prompt_cache_key,
                announce=announce,
            )
            choice = response.choices[0]
            message = choice.message
            messages.append(self._assistant_message(message))
            calls = list(_field(message, "tool_calls", None) or [])
            self.usage_history[-1]["tool_loop"] = True
            self.usage_history[-1]["tool_calls"] = len(calls)
            announce(describe_round(round_number, max_iterations, len(calls)))

            if calls:
                for call in calls:
                    total_tool_calls += 1
                    function = _field(call, "function")
                    name = str(_field(function, "name", ""))
                    raw_arguments = _field(function, "arguments", "{}") or "{}"
                    try:
                        arguments = json.loads(raw_arguments)
                        if not isinstance(arguments, dict):
                            raise ValueError("tool arguments must be an object")
                        try:
                            result = toolset.dispatch(name, arguments)
                        except WorkbookToolError as exc:
                            result = {
                                "ok": False,
                                "error": str(exc),
                                "instruction": (
                                    "Correct the arguments and call again, or read "
                                    "different evidence."
                                ),
                            }
                    except (json.JSONDecodeError, ValueError) as exc:
                        arguments = {"raw_arguments": str(raw_arguments)}
                        result = {
                            "ok": False,
                            "error": f"Invalid tool arguments: {exc}",
                            "instruction": "Return one valid JSON object as arguments.",
                        }
                    if trace is not None:
                        trace_item = {
                            "tool": name,
                            "arguments": arguments,
                            "ok": bool(result.get("ok", False)),
                        }
                        if not trace_item["ok"] and result.get("error"):
                            trace_item["error"] = str(result["error"])
                        if "accepted" in result:
                            trace_item["validation"] = {
                                "accepted": result.get("accepted"),
                                "error_count": result.get("error_count"),
                                "warning_count": result.get("warning_count"),
                                "validation_attempt": result.get("validation_attempt"),
                                "findings": result.get("findings"),
                                "repair_tracking": result.get("repair_tracking"),
                                "changed_coa_ids": result.get(
                                    "changed_coa_ids_since_prior_validation"
                                ),
                                "conditional_guidance_rules": [
                                    item.get("rule")
                                    for item in result.get("rule_guidance", [])
                                    if item.get("rule")
                                ],
                            }
                        trace.append(trace_item)
                    announce(
                        describe_tool_call(
                            name, arguments, bool(result.get("ok", False))
                        )
                    )
                    terminal = getattr(
                        toolset, "terminal_result", lambda _name, _result: None
                    )(name, result)
                    if terminal is not None:
                        self.usage_history[-1]["terminal_tool_result"] = True
                        return response_model.model_validate(terminal)
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": str(_field(call, "id", "")),
                            "content": json.dumps({"result": result}),
                        }
                    )
                continue

            text = _field(message, "content") or ""
            if not text:
                raise FireworksConfigurationError(
                    "Fireworks returned neither a tool call nor text."
                )
            try:
                result = response_model.model_validate_json(extract_json_object(text))
            except (ValidationError, ValueError) as exc:
                self.usage_history[-1]["json_repair"] = True
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "That response did not parse as "
                            f"{response_model.__name__}: {exc}\n\n"
                            "Return the same answer as one valid JSON object, with "
                            "no commentary and no markdown fence."
                        ),
                    }
                )
                continue
            final_response_error = getattr(
                toolset, "final_response_error", lambda _: None
            )(result)
            if final_response_error:
                self.usage_history[-1]["premature_final"] = True
                messages.append({"role": "user", "content": final_response_error})
                continue
            self._write_cache(cache_path, result)
            return result

        raise ProviderToolLoopError(
            f"{response_model.__name__} was not produced within {max_iterations} "
            f"turns after {total_tool_calls} tool calls. Raise max_iterations, or "
            "narrow the task so it needs less evidence."
        )

    def _client(self):
        if self._client_instance is None:
            try:
                from openai import OpenAI
            except ImportError as exc:
                raise FireworksConfigurationError(
                    "Install the optional model dependencies first: "
                    "python -m pip install -e .[models]"
                ) from exc
            self._client_instance = OpenAI(
                api_key=os.environ[FIREWORKS_API_KEY_ENV_VAR],
                base_url=FIREWORKS_BASE_URL,
                timeout=1800.0,
                # Mapping requests can already run for 30 minutes. Retrying an
                # unanswered request can multiply latency and cost without any
                # application-level progress or tool trace.
                max_retries=0,
            )
        return self._client_instance

    def _call(
        self,
        client,
        *,
        messages,
        response_model: type[BaseModel],
        prompt_chars: int,
        tools=None,
        response_format=None,
        prompt_cache_key: str | None = None,
        json_repair: bool = False,
        announce: Callable[[str], None] | None = None,
    ):
        started_at = utc_now()
        started = time.perf_counter()
        kwargs = {
            "model": self.model_name,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": self.max_output_tokens,
        }
        extra_body = {}
        if self.reasoning_effort:
            extra_body["reasoning_effort"] = self.reasoning_effort
        if prompt_cache_key:
            # Fireworks prompt caching is replica-local. A stable affinity key
            # keeps every turn in one repair/tool session on the same replica,
            # while the isolation key prevents one property's financial context
            # from sharing a cache namespace with another property's session.
            extra_body["prompt_cache_key"] = prompt_cache_key
            extra_body["prompt_cache_isolation_key"] = prompt_cache_key
        if extra_body:
            kwargs["extra_body"] = extra_body
        if tools is not None:
            kwargs["tools"] = tools
        if response_format is not None:
            kwargs["response_format"] = response_format
        # Streamed so the connection never looks idle. See providers/streaming.py
        # for the measurement that made this necessary; without it, a round that
        # runs past about ten minutes is cut with a 504 having produced nothing.
        kwargs["stream"] = True
        # Usage is omitted from a stream unless it is asked for, and it is not
        # optional here: the spend cap and every cost figure are computed from
        # provider-returned counts.
        kwargs["stream_options"] = {"include_usage": True}
        try:
            response = accumulate_stream(
                client.chat.completions.create(**kwargs),
                on_progress=(
                    (
                        lambda elapsed, tokens, thinking: announce(
                            describe_progress(elapsed, tokens, thinking=thinking)
                        )
                    )
                    if announce is not None
                    else None
                ),
                on_tool_call_named=(
                    (lambda name: announce(describe_tool_call_started(name)))
                    if announce is not None
                    else None
                ),
            )
        except Exception as exc:
            self.usage_history.append(
                self._failure_record(
                    response_model=response_model,
                    prompt_chars=prompt_chars,
                    started_at=started_at,
                    started=started,
                    error=exc,
                    prompt_cache_key=prompt_cache_key,
                    json_repair=json_repair,
                )
            )
            raise
        self.usage_history.append(
            self._usage_record(
                response,
                response_model=response_model,
                prompt_chars=prompt_chars,
                started_at=started_at,
                started=started,
                prompt_cache_key=prompt_cache_key,
                json_repair=json_repair,
            )
        )
        if getattr(response, "usage_missing", False):
            # The provider streamed no usage chunk. Flagged rather than left to
            # read as a call that cost nothing: the spend cap sums these, and a
            # silent zero would quietly raise the ceiling on the whole run.
            self.usage_history[-1]["usage_missing"] = True
        finish_reason = _field(response.choices[0], "finish_reason")
        if finish_reason == "length":
            self.usage_history[-1].update(
                {
                    "status": "fail",
                    "error_type": "ResponseTruncated",
                    "error_message": "Output token limit reached.",
                }
            )
            raise FireworksResponseTruncated(
                "Fireworks truncated the response at the configured output-token "
                f"limit ({self.max_output_tokens:,})."
            )
        return response

    def _usage_record(
        self,
        response,
        *,
        response_model: type[BaseModel],
        prompt_chars: int,
        started_at: str,
        started: float,
        prompt_cache_key: str | None = None,
        json_repair: bool = False,
    ) -> dict:
        usage = _field(response, "usage")
        prompt_tokens = _field(usage, "prompt_tokens")
        completion_tokens = _field(usage, "completion_tokens")
        total_tokens = _field(usage, "total_tokens")
        prompt_details = _field(usage, "prompt_tokens_details")
        completion_details = _field(usage, "completion_tokens_details")
        cached_tokens = _field(prompt_details, "cached_tokens")
        reasoning_tokens = _field(completion_details, "reasoning_tokens")
        answer_tokens = completion_tokens
        if completion_tokens is not None and reasoning_tokens is not None:
            answer_tokens = max(0, int(completion_tokens) - int(reasoning_tokens))
        record = {
            "provider": "fireworks",
            "model_name": self.model_name,
            "reasoning_effort": self.reasoning_effort,
            "temperature": self.temperature,
            "status": "pass",
            "cache_hit": False,
            "json_repair": json_repair,
            "response_model": response_model.__name__,
            "prompt_chars": prompt_chars,
            "estimated_prompt_tokens": prompt_chars // 4,
            "started_at": started_at,
            "completed_at": utc_now(),
            "duration_ms": elapsed_ms(started),
        }
        if prompt_cache_key:
            record["prompt_cache_key"] = prompt_cache_key
            record["prompt_cache_enabled"] = True
        for name, value in (
            ("prompt_token_count", prompt_tokens),
            ("candidates_token_count", answer_tokens),
            ("total_token_count", total_tokens),
            ("cached_content_token_count", cached_tokens),
            ("thoughts_token_count", reasoning_tokens),
        ):
            if value is not None:
                record[name] = int(value)
        return record

    def _failure_record(
        self,
        *,
        response_model: type[BaseModel],
        prompt_chars: int,
        started_at: str,
        started: float,
        error: Exception,
        prompt_cache_key: str | None = None,
        json_repair: bool = False,
    ) -> dict:
        record = {
            "provider": "fireworks",
            "model_name": self.model_name,
            "reasoning_effort": self.reasoning_effort,
            "temperature": self.temperature,
            "status": "fail",
            "cache_hit": False,
            "json_repair": json_repair,
            "response_model": response_model.__name__,
            "prompt_chars": prompt_chars,
            "estimated_prompt_tokens": prompt_chars // 4,
            "started_at": started_at,
            "completed_at": utc_now(),
            "duration_ms": elapsed_ms(started),
            "error_type": type(error).__name__,
            "error_message": str(error),
        }
        if prompt_cache_key:
            record["prompt_cache_key"] = prompt_cache_key
            record["prompt_cache_enabled"] = True
        return record

    def _record_cache_hit(
        self,
        response_model: type[BaseModel],
        prompt_chars: int,
        *,
        tool_calls: int | None = None,
    ) -> None:
        record = {
            "provider": "fireworks",
            "model_name": self.model_name,
            "reasoning_effort": self.reasoning_effort,
            "temperature": self.temperature,
            "status": "pass",
            "cache_hit": True,
            "json_repair": False,
            "response_model": response_model.__name__,
            "prompt_chars": prompt_chars,
            "estimated_prompt_tokens": prompt_chars // 4,
            "started_at": utc_now(),
            "completed_at": utc_now(),
            "duration_ms": 0,
        }
        if tool_calls is not None:
            record["tool_calls"] = tool_calls
        self.usage_history.append(record)

    def _cache_path(
        self,
        prompt: str,
        response_model: type[BaseModel],
        *,
        extra: str = "",
    ) -> Path | None:
        cache_dir = os.environ.get(
            FIREWORKS_CACHE_DIR_ENV_VAR, "outputs/llm_cache"
        )
        if cache_dir.strip().lower() in {"", "0", "false", "off"}:
            return None
        digest = hashlib.sha256(
            "\n".join(
                [
                    "fireworks",
                    self.model_name,
                    self.reasoning_effort,
                    str(self.temperature),
                    str(self.max_output_tokens),
                    response_model.__name__,
                    extra,
                    prompt,
                ]
            ).encode("utf-8")
        ).hexdigest()
        return Path(cache_dir) / f"{digest}.json"

    def _prompt_cache_key(
        self,
        prompt: str,
        response_model: type[BaseModel],
        *,
        extra: str = "",
    ) -> str:
        """Return a stable, non-sensitive Fireworks affinity/isolation key."""
        digest = hashlib.sha256(
            "\n".join(
                [
                    "hotel-pl-normalizer",
                    self.model_name,
                    self.reasoning_effort,
                    str(self.temperature),
                    response_model.__name__,
                    extra,
                    prompt,
                ]
            ).encode("utf-8")
        ).hexdigest()
        return f"hotel-pl-{digest[:32]}"

    def _validate_environment(self) -> None:
        if not os.environ.get(FIREWORKS_API_KEY_ENV_VAR):
            raise FireworksConfigurationError(
                f"Set {FIREWORKS_API_KEY_ENV_VAR} before using Fireworks."
            )

    def _validate_prompt_size(self, prompt: str) -> None:
        max_chars = int(
            os.environ.get(
                FIREWORKS_MAX_PROMPT_CHARS_ENV_VAR, DEFAULT_MAX_PROMPT_CHARS
            )
        )
        if len(prompt) > max_chars:
            raise FireworksConfigurationError(
                f"Fireworks prompt is too large: {len(prompt):,} chars. Limit is "
                f"{max_chars:,} from {FIREWORKS_MAX_PROMPT_CHARS_ENV_VAR}."
            )

    @staticmethod
    def _response_format(response_model: type[BaseModel]) -> dict:
        return {
            "type": "json_schema",
            "json_schema": {
                "name": response_model.__name__,
                "schema": response_model.model_json_schema(),
            },
        }

    @staticmethod
    def _message_text(response) -> str:
        text = _field(response.choices[0].message, "content")
        if not text:
            raise FireworksConfigurationError(
                "Fireworks response did not contain text."
            )
        return str(text)

    @staticmethod
    def _assistant_message(message) -> dict:
        payload = {"role": "assistant", "content": _field(message, "content")}
        reasoning = _field(message, "reasoning_content")
        if reasoning:
            payload["reasoning_content"] = reasoning
        calls = _field(message, "tool_calls") or []
        if calls:
            payload["tool_calls"] = [
                call.model_dump(mode="json", exclude_none=True)
                if hasattr(call, "model_dump")
                else call
                for call in calls
            ]
        return payload
