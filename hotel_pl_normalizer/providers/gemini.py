from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path
from typing import Callable, TypeVar

from pydantic import BaseModel, ValidationError

from hotel_pl_normalizer.models.common import ModelInfo
from hotel_pl_normalizer.models.department_location import (
    DepartmentIdentificationPacket,
    DepartmentLocationMap,
    DepartmentLocationPatch,
)
from hotel_pl_normalizer.models.sheet_selection import (
    SheetNameSelectionResult,
    SheetNameTriagePacket,
)
from hotel_pl_normalizer.providers.base import (
    JsonModelClient,
    ProviderConfigurationError,
    ProviderRunCancelled,
    ProviderToolLoopError,
    elapsed_ms,
    extract_json_object,
    utc_now,
)
from hotel_pl_normalizer.providers.workbook_tools import (
    WorkbookToolError,
    WorkbookToolset,
)

# Measured on the four hardest properties (624 hand-verified accounts): moving the
# base tier from 3.1-flash-lite to 3.5-flash-lite is +34 accounts, 89.4% -> 94.9%,
# and 96.755% -> 99.050% dollar-weighted, for $1.77 against ~$0.77 on a cold run.
# It also beat routing 3.6-flash at the upstream stages, which cost more and gained
# a third as much. See IMPLEMENTATION_LOG.md, "Model matrix C0-C3".
DEFAULT_GEMINI_MODEL = "gemini-3.5-flash-lite"

# Department-level repairs use the base tier. Workbook-level global repair uses
# the stronger tier because it must resolve cross-department hierarchy failures
# after deterministic validation.
DEFAULT_GEMINI_REPAIR_MODEL = DEFAULT_GEMINI_MODEL
DEFAULT_GLOBAL_REPAIR_MODEL = "gemini-3.6-flash"
GEMINI_MODEL_ENV_VAR = "FRESH_START_GEMINI_MODEL"
GEMINI_API_KEY_ENV_VAR = "GEMINI_API_KEY"
GOOGLE_API_KEY_ENV_VAR = "GOOGLE_API_KEY"
GEMINI_MAX_PROMPT_CHARS_ENV_VAR = "FRESH_START_GEMINI_MAX_PROMPT_CHARS"
GEMINI_CACHE_DIR_ENV_VAR = "FRESH_START_GEMINI_CACHE_DIR"
GEMINI_REPAIR_MODEL_ENV_VAR = "FRESH_START_GEMINI_REPAIR_MODEL"
GEMINI_SEED_ENV_VAR = "FRESH_START_GEMINI_SEED"
# 300K originally, sized for a 512 MB instance where a large prompt plus the
# parsed workbook it was built from could exhaust the box. The instance now has
# 2 GB, so the old ceiling was refusing workbooks the machine can comfortably
# hold -- a guard that stops real work is worse than the risk it was guarding.
# Still bounded rather than removed: an unbounded prompt is a runaway bill and a
# request the model will reject anyway, and hitting a stated limit is a clearer
# failure than a provider-side truncation.
DEFAULT_MAX_PROMPT_CHARS = 750_000
DEFAULT_GEMINI_SEED = 0

ModelT = TypeVar("ModelT", bound=BaseModel)


class GeminiConfigurationError(ProviderConfigurationError):
    """Raised when the Gemini backend cannot be configured."""


class GeminiJsonClient(JsonModelClient):
    """Small wrapper for model calls that must return one Pydantic JSON object."""

    provider = "google"

    def __init__(
        self,
        *,
        model_name: str | None = None,
        repair_model_name: str | None = None,
    ) -> None:
        self.model_name = model_name or os.environ.get(GEMINI_MODEL_ENV_VAR, DEFAULT_GEMINI_MODEL)
        self.seed = int(os.environ.get(GEMINI_SEED_ENV_VAR, DEFAULT_GEMINI_SEED))
        self.usage_history: list[dict] = []
        # Repairs are the calls most worth spending on: something has already gone
        # wrong, and the validator has said what. None means "same model".
        self.repair_model_name = (
            repair_model_name
            or os.environ.get(GEMINI_REPAIR_MODEL_ENV_VAR)
            or DEFAULT_GEMINI_REPAIR_MODEL
        )
        self._repair_delegate: "GeminiJsonClient | None" = None

    @property
    def repair_client(self) -> "GeminiJsonClient":
        """The client repair calls go through -- possibly a stronger tier.

        The delegate **shares this client's `usage_history` list object**, because
        the orchestrator harvests usage via `backend.client.usage_history`. A
        separate list would make every repair call invisible: no tokens, no cost,
        no telemetry, while appearing to work.
        """
        if not self.repair_model_name or self.repair_model_name == self.model_name:
            return self
        if self._repair_delegate is None:
            delegate = GeminiJsonClient(model_name=self.repair_model_name)
            delegate.usage_history = self.usage_history
            self._repair_delegate = delegate
        return self._repair_delegate

    def generate_patch_model(
        self,
        prompt: str,
        response_model: type[ModelT],
        *,
        toolset: WorkbookToolset | None = None,
        trace: list[dict] | None = None,
    ) -> ModelT:
        """Run one repair call, optionally on a stronger tier and with tools.

        Repair is where evidence matters most: the first attempt was wrong, so
        re-reading the prompt that produced it is the least promising thing the
        model can do. When a toolset is supplied it can go and look instead.
        """
        client = self.repair_client
        if toolset is not None:
            return client.generate_json_model_with_tools(
                prompt, response_model, toolset=toolset, trace=trace
            )
        return client.generate_json_model(prompt, response_model)

    def generate_json_model(self, prompt: str, response_model: type[ModelT]) -> ModelT:
        self._validate_environment()
        full_prompt = self._prompt_with_schema(prompt, response_model)
        self._validate_prompt_size(full_prompt)
        cache_path = self._cache_path(full_prompt, response_model)
        cached_result = self._read_cache(cache_path, response_model)
        if cached_result is not None:
            started_at = utc_now()
            started = time.perf_counter()
            self.usage_history.append(
                {
                    "provider": "google",
                    "model_name": self.model_name,
                    "seed": self.seed,
                    "status": "pass",
                    "cache_hit": True,
                    "json_repair": False,
                    "response_model": response_model.__name__,
                    "prompt_chars": len(full_prompt),
                    "estimated_prompt_tokens": len(full_prompt) // 4,
                    "started_at": started_at,
                    "completed_at": utc_now(),
                    "duration_ms": elapsed_ms(started),
                }
            )
            return cached_result
        genai, types = self._import_google_genai()
        client = genai.Client()
        started_at = utc_now()
        started = time.perf_counter()
        try:
            response = self._generate_with_retry(
                client,
                contents=full_prompt,
                config=types.GenerateContentConfig(
                    temperature=0,
                    seed=self.seed,
                    candidate_count=1,
                    response_mime_type="application/json",
                ),
            )
        except Exception as exc:
            self.usage_history.append(
                self._failure_record(
                    response_model=response_model,
                    prompt_chars=len(full_prompt),
                    started_at=started_at,
                    started=started,
                    error=exc,
                )
            )
            raise
        self.usage_history.append(
            self._usage_record(
                response,
                response_model=response_model,
                prompt_chars=len(full_prompt),
                cache_hit=False,
                started_at=started_at,
                started=started,
            )
        )
        text = getattr(response, "text", None)
        if not text:
            raise GeminiConfigurationError("Gemini response did not contain text.")
        try:
            result_json = extract_json_object(text)
            result = response_model.model_validate_json(result_json)
            self._write_cache(cache_path, result)
            return result
        except (ValidationError, ValueError) as exc:
            repair_prompt = self._json_repair_prompt(
                text,
                response_model,
                str(exc),
            )
            repair_started_at = utc_now()
            repair_started = time.perf_counter()
            try:
                repair_response = self._generate_with_retry(
                    client,
                    contents=repair_prompt,
                    config=types.GenerateContentConfig(
                        temperature=0,
                        seed=self.seed,
                        candidate_count=1,
                        response_mime_type="application/json",
                    ),
                )
            except Exception as repair_exc:
                self.usage_history.append(
                    self._failure_record(
                        response_model=response_model,
                        prompt_chars=len(repair_prompt),
                        started_at=repair_started_at,
                        started=repair_started,
                        error=repair_exc,
                        json_repair=True,
                    )
                )
                raise
            self.usage_history.append(
                self._usage_record(
                    repair_response,
                    response_model=response_model,
                    prompt_chars=len(repair_prompt),
                    cache_hit=False,
                    json_repair=True,
                    started_at=repair_started_at,
                    started=repair_started,
                )
            )
            repair_text = getattr(repair_response, "text", None)
            if not repair_text:
                raise GeminiConfigurationError("Gemini JSON repair response did not contain text.") from exc
            result = response_model.model_validate_json(extract_json_object(repair_text))
            self._write_cache(cache_path, result)
            return result

    def _generate_with_retry(self, client, *, contents: str, config):
        for attempt in range(3):
            try:
                return client.models.generate_content(
                    model=self.model_name,
                    contents=contents,
                    config=config,
                )
            except Exception as exc:
                if not _is_temporary_model_error(exc) or attempt == 2:
                    raise
                time.sleep(10 * (attempt + 1))
        raise AssertionError("Unreachable Gemini retry state.")

    def _usage_record(
        self,
        response,
        *,
        response_model: type[BaseModel],
        prompt_chars: int,
        cache_hit: bool,
        started_at: str,
        started: float,
        json_repair: bool = False,
    ) -> dict:
        usage = getattr(response, "usage_metadata", None)
        record = {
            "provider": "google",
            "model_name": self.model_name,
            "seed": self.seed,
            "status": "pass",
            "cache_hit": cache_hit,
            "json_repair": json_repair,
            "response_model": response_model.__name__,
            "prompt_chars": prompt_chars,
            "estimated_prompt_tokens": prompt_chars // 4,
            "started_at": started_at,
            "completed_at": utc_now(),
            "duration_ms": elapsed_ms(started),
        }
        for attr in (
            "prompt_token_count",
            "candidates_token_count",
            "total_token_count",
            "cached_content_token_count",
            "thoughts_token_count",
        ):
            value = getattr(usage, attr, None) if usage is not None else None
            if value is not None:
                record[attr] = value
        return record

    def _failure_record(
        self,
        *,
        response_model: type[BaseModel],
        prompt_chars: int,
        started_at: str,
        started: float,
        error: Exception,
        json_repair: bool = False,
    ) -> dict:
        return {
            "provider": "google",
            "model_name": self.model_name,
            "seed": self.seed,
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

    def _validate_environment(self) -> None:
        if os.environ.get(GOOGLE_API_KEY_ENV_VAR):
            return
        if os.environ.get(GEMINI_API_KEY_ENV_VAR):
            return
        raise GeminiConfigurationError(
            f"Set {GEMINI_API_KEY_ENV_VAR} before using the Gemini backend. "
            f"{GOOGLE_API_KEY_ENV_VAR} also works, but it takes precedence in the Google SDK."
        )

    @staticmethod
    def _import_google_genai():
        try:
            from google import genai
            from google.genai import types
        except ImportError as exc:
            raise GeminiConfigurationError(
                "Install the optional model dependency first: python -m pip install -e .[models]"
            ) from exc
        return genai, types

    def _validate_prompt_size(self, prompt: str) -> None:
        max_chars = int(os.environ.get(GEMINI_MAX_PROMPT_CHARS_ENV_VAR, DEFAULT_MAX_PROMPT_CHARS))
        if len(prompt) > max_chars:
            estimated_tokens = len(prompt) // 4
            raise GeminiConfigurationError(
                f"Gemini prompt is too large: {len(prompt):,} chars, roughly {estimated_tokens:,} tokens. "
                f"Limit is {max_chars:,} chars from {GEMINI_MAX_PROMPT_CHARS_ENV_VAR}. "
                "Slim or chunk the artifact before sending it."
            )

    def generate_json_model_with_tools(
        self,
        prompt: str,
        response_model: type[ModelT],
        *,
        toolset: WorkbookToolset,
        max_iterations: int = 10,
        trace: list[dict] | None = None,
        cancel: Callable[[], str | None] | None = None,
    ) -> ModelT:
        """Let the model read the workbook before returning one typed object.

        The staged pipeline gives every stage one pre-built packet and one shot, so
        a packet that is wrong -- a boundary off by ten rows, a schedule routing
        never selected -- leaves the stage no recourse. Here the model can look
        first, which is the whole point of the tool-enabled arms.

        Caching is on the *final* result, keyed on the opening prompt plus the tool
        contract, so a repeated call skips the entire session rather than replaying
        it. That is sound because every tool is a pure read of an immutable
        workbook whose content hash is already in the prompt, so the same opening
        prompt implies the same reachable evidence. It also preserves the property
        the rest of this pipeline depends on: identical inputs reproduce identical
        output for free.

        `response_mime_type` is deliberately not set. Gemini rejects it alongside
        tool declarations, so the final answer arrives as text and is extracted the
        same way a JSON-repair response is.

        Pass `trace` to collect the tool calls for an artifact; the transcript is
        not reconstructed on a cache hit, so treat it as evidence from a live run.
        """
        full_prompt = self._prompt_with_schema(prompt, response_model)
        self._validate_prompt_size(full_prompt)
        cache_path = (
            self._cache_path(full_prompt, response_model, extra=toolset.signature())
            if getattr(toolset, "cacheable", True)
            else None
        )
        cached = self._read_cache(cache_path, response_model)
        if cached is not None:
            started_at = utc_now()
            started = time.perf_counter()
            self.usage_history.append(
                {
                    "provider": "google",
                    "model_name": self.model_name,
                    "seed": self.seed,
                    "status": "pass",
                    "cache_hit": True,
                    "json_repair": False,
                    "response_model": response_model.__name__,
                    "prompt_chars": len(full_prompt),
                    "estimated_prompt_tokens": len(full_prompt) // 4,
                    "tool_calls": 0,
                    "started_at": started_at,
                    "completed_at": utc_now(),
                    "duration_ms": elapsed_ms(started),
                }
            )
            return cached

        genai, types = self._import_google_genai()
        client = genai.Client()
        config = types.GenerateContentConfig(
            temperature=0,
            seed=self.seed,
            candidate_count=1,
            tools=[types.Tool(function_declarations=toolset.declarations())],
        )
        contents: list[dict] = [{"role": "user", "parts": [{"text": full_prompt}]}]
        tool_calls = 0

        for _ in range(max_iterations):
            # Checked before each round, never mid-call. A round is one billed
            # request, so stopping here is the last point at which no further money
            # is spent -- and the caller still keeps everything already submitted.
            if cancel is not None:
                reason = cancel()
                if reason:
                    raise ProviderRunCancelled(reason)
            started_at = utc_now()
            started = time.perf_counter()
            response = self._generate_with_retry(
                client, contents=contents, config=config
            )
            self.usage_history.append(
                self._usage_record(
                    response,
                    response_model=response_model,
                    prompt_chars=len(full_prompt),
                    cache_hit=False,
                    started_at=started_at,
                    started=started,
                )
            )
            content = response.candidates[0].content
            contents.append(content.model_dump(mode="json", exclude_none=True))
            calls = [
                part.function_call
                for part in (content.parts or [])
                if getattr(part, "function_call", None) is not None
            ]
            # Mark the record so telemetry can distinguish "tools were offered and
            # declined" from "tools were never offered". Without this, a stage that
            # ignores its tools looks identical to one that never had them, and the
            # experiment is uninterpretable.
            self.usage_history[-1]["tool_loop"] = True
            self.usage_history[-1]["tool_calls"] = len(calls)

            if calls:
                parts: list[dict] = []
                for call in calls:
                    tool_calls += 1
                    arguments = dict(call.args or {})
                    try:
                        result = toolset.dispatch(str(call.name), arguments)
                    except WorkbookToolError as exc:
                        # Handed back rather than raised: a wrong argument is
                        # something the model can correct, and killing the stage
                        # over it is what the tool loop exists to avoid.
                        result = {
                            "ok": False,
                            "error": str(exc),
                            "instruction": (
                                "Correct the arguments and call again, or read "
                                "different evidence."
                            ),
                        }
                    if trace is not None:
                        trace_item = {
                            "tool": str(call.name),
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
                                "conditional_guidance_rules": [
                                    item.get("rule")
                                    for item in result.get("rule_guidance", [])
                                    if item.get("rule")
                                ],
                            }
                        trace.append(trace_item)
                    terminal = getattr(
                        toolset, "terminal_result", lambda _name, _result: None
                    )(str(call.name), result)
                    if terminal is not None:
                        self.usage_history[-1]["terminal_tool_result"] = True
                        return response_model.model_validate(terminal)
                    payload = {"name": str(call.name), "response": {"result": result}}
                    call_id = getattr(call, "id", None)
                    if call_id:
                        payload["id"] = call_id
                    parts.append({"function_response": payload})
                contents.append({"role": "user", "parts": parts})
                continue

            text = getattr(response, "text", None)
            if not text:
                raise GeminiConfigurationError(
                    "Gemini returned neither a tool call nor text."
                )
            try:
                result = response_model.model_validate_json(extract_json_object(text))
            except (ValidationError, ValueError) as exc:
                # The one-shot path re-asks with a dedicated repair prompt. Here the
                # session is already a conversation, so handing the error back as
                # another turn is both simpler and better: the model keeps the
                # evidence it just read instead of starting over from the text.
                self.usage_history[-1]["json_repair"] = True
                contents.append(
                    {
                        "role": "user",
                        "parts": [
                            {
                                "text": (
                                    "That response did not parse as "
                                    f"{response_model.__name__}: {exc}\n\n"
                                    "Return the same answer as one valid JSON "
                                    "object, with no commentary and no markdown "
                                    "fence."
                                )
                            }
                        ],
                    }
                )
                continue
            final_response_error = getattr(
                toolset, "final_response_error", lambda _: None
            )(result)
            if final_response_error:
                self.usage_history[-1]["premature_final"] = True
                contents.append(
                    {
                        "role": "user",
                        "parts": [{"text": final_response_error}],
                    }
                )
                continue
            self._write_cache(cache_path, result)
            return result

        raise ProviderToolLoopError(
            f"{response_model.__name__} was not produced within {max_iterations} "
            f"turns after {tool_calls} tool calls. Raise max_iterations, or narrow "
            "the task so it needs less evidence."
        )

    def _cache_path(
        self,
        prompt: str,
        response_model: type[BaseModel],
        *,
        extra: str = "",
    ) -> Path | None:
        cache_dir = os.environ.get(GEMINI_CACHE_DIR_ENV_VAR, "outputs/llm_cache")
        if cache_dir.strip().lower() in {"", "0", "false", "off"}:
            return None
        digest = hashlib.sha256(
            "\n".join(
                [
                    self.model_name,
                    str(self.seed),
                    response_model.__name__,
                    # Empty for a plain call, so existing keys are unchanged. The
                    # tool contract when there is one, because a session's result
                    # depends on which tools were offered.
                    extra,
                    prompt,
                ]
            ).encode("utf-8")
        ).hexdigest()
        return Path(cache_dir) / f"{digest}.json"


class GeminiSheetNameTriageBackend:
    def __init__(
        self,
        *,
        model_name: str | None = None,
        repair_model_name: str | None = None,
    ) -> None:
        self.client = GeminiJsonClient(
            model_name=model_name, repair_model_name=repair_model_name
        )

    def run(self, packet: SheetNameTriagePacket, prompt: str) -> SheetNameSelectionResult:
        result = self.client.generate_json_model(prompt, SheetNameSelectionResult)
        return result.model_copy(
            update={
                "workbook_id": packet.workbook_id,
                "model": ModelInfo(
                    provider="google",
                    model_name=self.client.model_name,
                    prompt_version="sheet_name_triage_v1",
                ),
            }
        )


class GeminiDepartmentIdBackend:
    def __init__(
        self,
        *,
        model_name: str | None = None,
        repair_model_name: str | None = None,
        patch_client=None,
    ) -> None:
        self.client = GeminiJsonClient(
            model_name=model_name, repair_model_name=repair_model_name
        )
        self.patch_client = patch_client or self.client.repair_client
        # The orchestrator reads one usage list from backend.client. Keeping both
        # providers on that list preserves complete tokens, cost, and latency.
        self.patch_client.usage_history = self.client.usage_history

    def run(
        self,
        packet: DepartmentIdentificationPacket,
        prompt: str,
        *,
        toolset: WorkbookToolset | None = None,
        trace: list[dict] | None = None,
    ) -> DepartmentLocationMap:
        """Locate every department, optionally with the workbook to read.

        Without tools this stage sees only the compact packets that routing chose,
        which is how GRY lost all 156 accounts: routing skipped the real `EC`
        utilities schedule, and the model could name it but not confirm it. With
        tools it can check a sheet exists and read it before committing.
        """
        if toolset is not None:
            result = self.client.generate_json_model_with_tools(
                prompt, DepartmentLocationMap, toolset=toolset, trace=trace
            )
        else:
            result = self.client.generate_json_model(prompt, DepartmentLocationMap)
        return result.model_copy(
            update={
                "workbook_id": packet.workbook_id,
                "model": ModelInfo(
                    provider="google",
                    model_name=self.client.model_name,
                    prompt_version="department_id_v1",
                ),
            }
        )

    def run_patch(
        self,
        packet: DepartmentIdentificationPacket,
        prompt: str,
        target_map_id: str,
        *,
        toolset: WorkbookToolset | None = None,
        trace: list[dict] | None = None,
    ) -> DepartmentLocationPatch:
        result = self.patch_client.generate_patch_model(
            prompt, DepartmentLocationPatch, toolset=toolset, trace=trace
        )
        return result.model_copy(
            update={
                "target_map_id": target_map_id,
                "workbook_id": packet.workbook_id,
                "model": ModelInfo(
                    provider=self.patch_client.provider,
                    model_name=self.patch_client.model_name,
                    prompt_version="department_id_patch_v1",
                ),
            }
        )


def _is_temporary_model_error(exc: Exception) -> bool:
    status_code = getattr(exc, "status_code", None)
    message = str(exc).lower()
    return status_code in {429, 500, 502, 503, 504} or any(
        marker in message
        for marker in (
            "high demand",
            "temporarily unavailable",
            "service unavailable",
            "resource exhausted",
        )
    )
