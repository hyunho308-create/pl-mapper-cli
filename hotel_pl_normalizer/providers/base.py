"""Supplier-neutral contract for the model behind the normalization workflow.

The workflow depends on typed tool calls, usage telemetry and cost estimation;
it does not depend on a supplier's request or response shape. OpenAI Luna is the
only adapter currently shipped. A future Fireworks or Gemini adapter implements
this same contract and can replace Luna without adding another pipeline.
"""

from __future__ import annotations

import json
import re
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any, Protocol, TypeVar

from pydantic import BaseModel

ModelT = TypeVar("ModelT", bound=BaseModel)


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def elapsed_ms(started: float) -> int:
    return round((time.perf_counter() - started) * 1000)


class ProviderConfigurationError(RuntimeError):
    """A model client cannot be configured, or answered unusably."""


class ProviderResponseTruncated(ProviderConfigurationError):
    """A model exhausted its completion-token allowance before finishing."""


class ProviderToolLoopError(RuntimeError):
    """A tool-calling session ended without producing its typed result."""


class ProviderRunCancelled(RuntimeError):
    """A caller stopped a model session between billed requests."""


class ModelToolError(ValueError):
    """A model supplied arguments that do not describe a valid tool call."""


def extract_json_object(text: str) -> str:
    """Return the first complete JSON object in a model response."""
    stripped = text.strip()
    first = stripped.find("{")
    if first >= 0:
        try:
            value, _ = json.JSONDecoder().raw_decode(stripped[first:])
            if isinstance(value, dict):
                return json.dumps(value)
        except json.JSONDecodeError:
            pass
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", stripped, flags=re.DOTALL)
    if fenced:
        return fenced.group(1)
    last = stripped.rfind("}")
    if first >= 0 and last > first:
        return stripped[first : last + 1]
    raise ProviderConfigurationError("Model response did not contain a JSON object.")


class ModelToolset(Protocol):
    """Tools exposed to one model session."""

    def declarations(self) -> list[dict[str, Any]]: ...

    def dispatch(self, name: str, arguments: dict[str, Any]) -> dict: ...


class ModelClient(ABC):
    """The complete model surface required by the current workflow."""

    provider: str
    model_name: str
    usage_history: list[dict]
    last_tool_trace: list[dict] | None
    last_validation_result: dict | None

    @staticmethod
    def _prompt_with_schema(prompt: str, response_model: type[BaseModel]) -> str:
        schema = response_model.model_json_schema()
        return "\n\n".join(
            [
                prompt,
                "## Output Schema",
                "Return exactly one JSON object. Do not wrap it in markdown.",
                "```json",
                json.dumps(schema, indent=2),
                "```",
            ]
        )

    @abstractmethod
    def generate_json_model_with_tools(
        self,
        prompt: str,
        response_model: type[ModelT],
        *,
        toolset: ModelToolset,
        max_iterations: int,
        trace: list[dict] | None = None,
        on_activity: Callable[[str], None] | None = None,
        cancel: Callable[[], str | None] | None = None,
    ) -> ModelT:
        """Run a typed tool-calling session."""

    @abstractmethod
    def estimate_cost(self, calls: list[dict]) -> float:
        """Estimate the cost of supplier-returned usage records."""

    @abstractmethod
    def pricing_details(self) -> dict[str, Any]:
        """Describe the rates used by :meth:`estimate_cost`."""


# Documentation and JSON Schema bookkeeping that tool APIs do not need.
_SCHEMA_KEYS_TO_DROP = frozenset(
    {"title", "default", "additionalProperties", "$schema", "discriminator"}
)


def tool_parameter_schema(model: type[BaseModel]) -> dict:
    """A pydantic model's schema, in the subset tool declarations accept.

    `model_json_schema()` describes nested models with `$ref` and collects them
    under `$defs`, and optional fields as `anyOf: [{...}, {"type": "null"}]`.
    Definitions are inlined, nullable unions collapse to the non-null branch,
    and bookkeeping keys are dropped so adapters can translate one stable form
    into their supplier's declaration format.
    """
    schema = model.model_json_schema()
    definitions = schema.pop("$defs", {})

    def resolve(node, depth=0):
        if not isinstance(node, (dict, list)):
            return node
        # Depth-guarded: a self-referencing model would otherwise inline forever.
        if depth > 12:
            return {"type": "object"}
        if isinstance(node, list):
            return [resolve(item, depth + 1) for item in node]

        ref = node.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/$defs/"):
            target = definitions.get(ref.rsplit("/", 1)[-1], {})
            merged = dict(target)
            # Anything alongside the $ref (a description, usually) wins.
            merged.update({k: v for k, v in node.items() if k != "$ref"})
            return resolve(merged, depth + 1)

        options = node.get("anyOf") or node.get("oneOf")
        if options:
            concrete = [
                option
                for option in options
                if not (isinstance(option, dict) and option.get("type") == "null")
            ]
            rest = {
                k: v for k, v in node.items() if k not in {"anyOf", "oneOf"}
            }
            if len(concrete) == 1:
                merged = dict(concrete[0])
                merged.update(rest)
                resolved = resolve(merged, depth + 1)
                if len(concrete) != len(options):
                    resolved["nullable"] = True
                return resolved
            # A genuine union of shapes has no tool-schema equivalent; describe
            # it as an open object rather than emitting something rejected.
            return {**resolve(rest, depth + 1), "type": "object"}

        return {
            key: resolve(value, depth + 1)
            for key, value in node.items()
            if key not in _SCHEMA_KEYS_TO_DROP
        }

    return resolve(schema)
