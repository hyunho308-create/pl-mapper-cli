"""OpenAI adapter for the existing OpenAI-compatible mapping tool loop."""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from typing import Callable, TypeVar

from pydantic import BaseModel, ValidationError

from hotel_pl_normalizer.activity import describe_round, describe_tool_call
from hotel_pl_normalizer.providers.base import (
    ProviderRunCancelled,
    ProviderToolLoopError,
    elapsed_ms,
    extract_json_object,
    utc_now,
)
from hotel_pl_normalizer.providers.fireworks import (
    FireworksConfigurationError,
    FireworksJsonClient,
    FireworksResponseTruncated,
)
from hotel_pl_normalizer.providers.workbook_tools import (
    WorkbookToolError,
    WorkbookToolset,
)

ModelT = TypeVar("ModelT", bound=BaseModel)

DEFAULT_OPENAI_MODEL = "gpt-5.6-luna"
OPENAI_API_KEY_ENV_VAR = "OPENAI_API_KEY"
OPENAI_MODEL_ENV_VAR = "FRESH_START_OPENAI_MODEL"
OPENAI_REASONING_EFFORT_ENV_VAR = "FRESH_START_OPENAI_REASONING_EFFORT"
OPENAI_REPAIR_REASONING_EFFORT_ENV_VAR = (
    "FRESH_START_OPENAI_REPAIR_REASONING_EFFORT"
)



@dataclass(frozen=True)
class OpenAIModelPricing:
    """The token rates that belong to one OpenAI model."""

    input_usd_per_mtok: float
    cached_input_usd_per_mtok: float
    output_usd_per_mtok: float
    cache_write_multiplier: float = 1.25
    long_context_threshold: int = 272_000
    long_input_multiplier: float = 2.0
    long_output_multiplier: float = 1.5


OPENAI_MODEL_PRICING = {
    "gpt-5.6-luna": OpenAIModelPricing(
        input_usd_per_mtok=0.20,
        cached_input_usd_per_mtok=0.02,
        output_usd_per_mtok=1.20,
    ),
}

# Kept as the public shorthand used by pipeline telemetry. Cost calculation
# itself resolves the actual model through OPENAI_MODEL_PRICING.
OPENAI_INPUT_USD_PER_MTOK = OPENAI_MODEL_PRICING[DEFAULT_OPENAI_MODEL].input_usd_per_mtok
OPENAI_CACHED_INPUT_USD_PER_MTOK = (
    OPENAI_MODEL_PRICING[DEFAULT_OPENAI_MODEL].cached_input_usd_per_mtok
)
OPENAI_OUTPUT_USD_PER_MTOK = OPENAI_MODEL_PRICING[DEFAULT_OPENAI_MODEL].output_usd_per_mtok


def openai_model_pricing(model_name: str | None) -> OpenAIModelPricing:
    """Return explicit pricing, never silently price another model as Luna."""
    selected = model_name or DEFAULT_OPENAI_MODEL
    if selected in OPENAI_MODEL_PRICING:
        return OPENAI_MODEL_PRICING[selected]
    # A dated Luna snapshot has the same product pricing as its alias. Keep the
    # match narrow so Terra, Sol, and future unrelated models fail visibly.
    if selected.startswith(f"{DEFAULT_OPENAI_MODEL}-"):
        return OPENAI_MODEL_PRICING[DEFAULT_OPENAI_MODEL]
    raise ValueError(f"No OpenAI pricing is configured for model {selected!r}.")


def estimate_openai_cost(
    calls: list[dict], *, pricing: OpenAIModelPricing | None = None
) -> float:
    """Estimate OpenAI cost from provider token counts and actual model names."""
    total = 0.0
    for call in calls:
        if call.get("cache_hit"):
            continue
        rates = pricing or openai_model_pricing(call.get("model_name"))
        prompt = int(call.get("prompt_token_count") or 0)
        cached = min(prompt, int(call.get("cached_content_token_count") or 0))
        cache_write = min(
            prompt - cached,
            int(call.get("cache_write_token_count") or 0),
        )
        regular = prompt - cached - cache_write
        output = int(call.get("candidates_token_count") or 0)
        output += int(call.get("thoughts_token_count") or 0)
        long_context = prompt > rates.long_context_threshold
        input_multiplier = rates.long_input_multiplier if long_context else 1.0
        output_multiplier = rates.long_output_multiplier if long_context else 1.0
        total += input_multiplier * (
            regular * rates.input_usd_per_mtok
            + cached * rates.cached_input_usd_per_mtok
            + cache_write
            * rates.input_usd_per_mtok
            * rates.cache_write_multiplier
        ) / 1_000_000
        total += (
            output * rates.output_usd_per_mtok * output_multiplier / 1_000_000
        )
    return round(total, 6)


class OpenAIJsonClient(FireworksJsonClient):
    """Run the unchanged mapper session against an OpenAI model."""

    provider = "openai"

    def __init__(
        self,
        *,
        model_name: str | None = None,
        reasoning_effort: str | None = None,
        repair_reasoning_effort: str | None = None,
    ) -> None:
        selected_repair_reasoning = repair_reasoning_effort or os.environ.get(
            OPENAI_REPAIR_REASONING_EFFORT_ENV_VAR, "medium"
        )
        super().__init__(
            model_name=model_name
            or os.environ.get(OPENAI_MODEL_ENV_VAR, DEFAULT_OPENAI_MODEL),
            reasoning_effort=reasoning_effort
            or os.environ.get(OPENAI_REASONING_EFFORT_ENV_VAR, "medium"),
        )
        # Kept on this adapter rather than requiring the optional Fireworks
        # fallback to share Luna's repair settings.
        self.repair_reasoning_effort = selected_repair_reasoning
        # Luna supports 128k output. Keep repairs at the same ceiling so a valid
        # accepted checkpoint is not lost merely because it occurred after turn 1.
        self.max_output_tokens = min(self.max_output_tokens, 128_000)
        self.repair_max_output_tokens = self.max_output_tokens

    def _client(self):
        if self._client_instance is None:
            from openai import OpenAI

            client = OpenAI(
                api_key=os.environ[OPENAI_API_KEY_ENV_VAR],
                timeout=1800.0,
                # A multi-turn mapper can legitimately cross a low account TPM
                # window. Honor OpenAI's Retry-After response instead of losing
                # the completed plan and repair context.
                max_retries=2,
            )
            self._client_instance = client
        return self._client_instance

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
        """Run the existing mapping contract through the Responses API."""
        self._validate_environment()
        full_prompt = self._prompt_with_schema(prompt, response_model)
        self._validate_prompt_size(full_prompt)
        client = self._client()
        tools = [
            {
                "type": "function",
                "name": declaration["name"],
                "description": declaration.get("description", ""),
                "parameters": declaration["parameters"],
                "strict": False,
            }
            for declaration in toolset.declarations()
        ]
        current_input: str | list[dict] = full_prompt
        previous_response_id: str | None = None
        total_tool_calls = 0
        self.last_tool_trace = trace
        self.last_validation_result = None

        def announce(line: str) -> None:
            if on_activity is None:
                return
            try:
                on_activity(line)
            except Exception:  # noqa: BLE001 - progress must not break a run
                pass

        for round_number in range(1, max_iterations + 1):
            if cancel is not None:
                reason = cancel()
                if reason:
                    raise ProviderRunCancelled(reason)

            started_at = utc_now()
            started = time.perf_counter()
            request = {
                "model": self.model_name,
                "input": current_input,
                "tools": tools,
                # Repairs need the submitted plan and deterministic validator
                # result, not the prior turn's private chain of thought. Keeping
                # reasoning turn-local prevents a large first analysis from
                # consuming the account's entire TPM allowance on every repair.
                "reasoning": {
                    "effort": self.reasoning_effort,
                    "context": "current_turn",
                },
                "max_output_tokens": self.max_output_tokens,
                "store": True,
            }
            if previous_response_id:
                request["previous_response_id"] = previous_response_id
            try:
                response = client.responses.create(**request)
            except Exception as exc:
                self.usage_history.append(
                    {
                        "provider": self.provider,
                        "model_name": self.model_name,
                        "reasoning_effort": self.reasoning_effort,
                        "status": "fail",
                        "cache_hit": False,
                        "response_model": response_model.__name__,
                        "prompt_chars": len(full_prompt),
                        "started_at": started_at,
                        "completed_at": utc_now(),
                        "duration_ms": elapsed_ms(started),
                        "error_type": type(exc).__name__,
                        "error_message": str(exc),
                    }
                )
                raise

            usage = getattr(response, "usage", None)
            input_details = getattr(usage, "input_tokens_details", None)
            output_details = getattr(usage, "output_tokens_details", None)
            output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
            reasoning_tokens = int(
                getattr(output_details, "reasoning_tokens", 0) or 0
            )
            record = {
                "provider": self.provider,
                "model_name": self.model_name,
                "reasoning_effort": self.reasoning_effort,
                "temperature": None,
                "status": "pass",
                "cache_hit": False,
                "response_model": response_model.__name__,
                "prompt_chars": len(full_prompt),
                "started_at": started_at,
                "completed_at": utc_now(),
                "duration_ms": elapsed_ms(started),
                "prompt_token_count": int(getattr(usage, "input_tokens", 0) or 0),
                "candidates_token_count": max(0, output_tokens - reasoning_tokens),
                "cached_content_token_count": int(
                    getattr(input_details, "cached_tokens", 0) or 0
                ),
                "cache_write_token_count": int(
                    getattr(input_details, "cache_write_tokens", 0) or 0
                ),
                "thoughts_token_count": reasoning_tokens,
                "total_token_count": int(getattr(usage, "total_tokens", 0) or 0),
                "tool_loop": True,
            }
            self.usage_history.append(record)

            if getattr(response, "status", None) == "incomplete":
                details = getattr(response, "incomplete_details", None)
                reason = getattr(details, "reason", "incomplete")
                record.update(
                    status="fail",
                    error_type="ResponseTruncated",
                    error_message=str(reason),
                )
                raise FireworksResponseTruncated(
                    f"OpenAI response was incomplete: {reason}."
                )

            calls = [
                item
                for item in (getattr(response, "output", None) or [])
                if getattr(item, "type", None) == "function_call"
            ]
            record["tool_calls"] = len(calls)
            announce(describe_round(round_number, max_iterations, len(calls)))

            if calls:
                next_input: list[dict] = []
                for call in calls:
                    total_tool_calls += 1
                    name = str(getattr(call, "name", ""))
                    raw_arguments = getattr(call, "arguments", "{}") or "{}"
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
                                "validation_attempt": result.get(
                                    "validation_attempt"
                                ),
                                "findings": result.get("findings"),
                                "repair_tracking": result.get("repair_tracking"),
                                "changed_coa_ids": result.get(
                                    "changed_coa_ids_since_prior_validation"
                                ),
                            }
                        trace.append(trace_item)

                    announce(
                        describe_tool_call(
                            name, arguments, bool(result.get("ok", False))
                        )
                    )
                    if "accepted" in result:
                        self.last_validation_result = {
                            "accepted": result.get("accepted"),
                            "error_count": result.get("error_count"),
                            "warning_count": result.get("warning_count"),
                            "validation_attempt": result.get("validation_attempt"),
                            "findings": result.get("findings"),
                        }
                    if result.get("accepted") is False:
                        self.reasoning_effort = self.repair_reasoning_effort
                        self.max_output_tokens = self.repair_max_output_tokens
                    terminal = getattr(
                        toolset, "terminal_result", lambda _name, _result: None
                    )(name, result)
                    if terminal is not None:
                        record["terminal_tool_result"] = True
                        return response_model.model_validate(terminal)
                    next_input.append(
                        {
                            "type": "function_call_output",
                            "call_id": str(getattr(call, "call_id", "")),
                            "output": json.dumps({"result": result}),
                        }
                    )
                previous_response_id = response.id
                current_input = next_input
                continue

            text = getattr(response, "output_text", "") or ""
            if not text:
                raise FireworksConfigurationError(
                    "OpenAI returned neither a tool call nor text."
                )
            try:
                result = response_model.model_validate_json(extract_json_object(text))
            except (ValidationError, ValueError) as exc:
                previous_response_id = response.id
                current_input = (
                    f"That response did not parse as {response_model.__name__}: "
                    f"{exc}\n\nReturn the same answer as one valid JSON object."
                )
                continue
            final_response_error = getattr(
                toolset, "final_response_error", lambda _: None
            )(result)
            if final_response_error:
                previous_response_id = response.id
                current_input = final_response_error
                continue
            return result

        raise ProviderToolLoopError(
            f"{response_model.__name__} was not produced within {max_iterations} "
            f"turns after {total_tool_calls} tool calls."
        )

    def _usage_record(self, *args, **kwargs) -> dict:
        record = super()._usage_record(*args, **kwargs)
        record["provider"] = self.provider
        return record

    def _failure_record(self, *args, **kwargs) -> dict:
        record = super()._failure_record(*args, **kwargs)
        record["provider"] = self.provider
        return record

    def _record_cache_hit(self, *args, **kwargs) -> None:
        super()._record_cache_hit(*args, **kwargs)
        self.usage_history[-1]["provider"] = self.provider

    def _validate_environment(self) -> None:
        if not os.environ.get(OPENAI_API_KEY_ENV_VAR):
            raise RuntimeError(
                f"Set {OPENAI_API_KEY_ENV_VAR} before using the OpenAI provider."
            )
