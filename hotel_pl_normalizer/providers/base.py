"""What every JSON-returning model client shares, independent of the provider.

Why this is separate
--------------------
Two clients call typed models: Gemini for the structure stages, Fireworks for the
mapping session. They talk to different APIs, but the work either side of the
network call is the same -- embed the schema in the prompt, ask again when the
answer will not parse, and cache the result by prompt digest.

That shared half used to live on `GeminiJsonClient`, and Fireworks reached in for
it: `GeminiJsonClient._prompt_with_schema(...)`, `._read_cache(...)`,
`._write_cache(...)`, `._json_repair_prompt(...)`, all called on another class's
privates. It worked, but it made the Gemini client a dependency of the Fireworks
one for reasons that had nothing to do with Gemini.

The exception names carried the same problem further. Fireworks raised
`GeminiRunCancelled`, and because Fireworks is the production mapper, the demo
server's cancellation handler was catching a Gemini-named exception for a failure
Gemini had no part in. Anyone reading that handler would conclude the wrong thing
about which provider was involved.

So: the shared behaviour is a base class, the exceptions are named for what
happened rather than for who was being called, and each provider module keeps only
what is genuinely its own -- auth, request shape, response parsing, pricing.

Configuration errors stay per-provider (`GeminiConfigurationError`,
`FireworksConfigurationError`) because they name a specific API key or setting,
and both derive from `ProviderConfigurationError` so a caller that does not care
which provider failed can still catch one thing.
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel

from hotel_pl_normalizer.atomic import replace_atomically

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
    """A session was stopped between rounds by its caller.

    Distinct from `ProviderToolLoopError`: running out of turns is the model
    failing to converge, while this is a deadline, a spend cap or a person
    deciding the run should end. The caller keeps whatever was submitted before
    the stop.
    """


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


class JsonModelClient:
    """The provider-independent half of a typed model client.

    Subclasses own the network call and everything provider-shaped around it.
    Everything here is a pure function of its arguments, which is what lets both
    providers share it without sharing any state.
    """

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

    @staticmethod
    def _json_repair_prompt(
        invalid_text: str, response_model: type[BaseModel], error: str
    ) -> str:
        schema = response_model.model_json_schema()
        return "\n\n".join(
            [
                "Your previous response was not valid JSON for the required schema.",
                "Return exactly one corrected JSON object. Do not add markdown or explanatory text.",
                "## Validation Error",
                error,
                "## Required Schema",
                "```json",
                json.dumps(schema, indent=2),
                "```",
                "## Invalid Response To Correct",
                "```json",
                invalid_text,
                "```",
            ]
        )

    @staticmethod
    def _write_cache(cache_path: Path | None, result: BaseModel) -> None:
        """Write a cache entry atomically.

        Departments now run concurrently, and the gate can replay several
        properties at once against one shared cache directory, so two writers can
        target the same content-addressed path. A direct `write_text` is not
        atomic: a reader arriving mid-write, or a run killed part way through, sees
        a truncated file. Writing to a unique temporary name and replacing means a
        reader sees either the old entry or the whole new one.

        `os.replace` is atomic on Windows and POSIX alike. The temp name carries
        the pid so two processes never collide on it.
        """
        if cache_path is None:
            return
        temp_path = cache_path.with_suffix(f".{os.getpid()}.tmp")
        try:
            # Inside the try: creating the directory can fail too, and a cache
            # write must never fail the call it was meant to accelerate.
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            temp_path.write_text(result.model_dump_json(indent=2), encoding="utf-8")
            replace_atomically(temp_path, cache_path)
        except OSError:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass

    @staticmethod
    def _read_cache(
        cache_path: Path | None, response_model: type[ModelT]
    ) -> ModelT | None:
        """Return a cached response, or None to mean "call the model".

        An entry that cannot be read or does not validate is treated as a miss
        rather than raising. Cache writes are not atomic, so killing a run mid
        flight can leave a truncated file, and the cache is now load-bearing --
        without this, a corrupt entry surfaces later as an unrelated crash in
        whatever stage happened to hit it.

        The response schema is already part of the key: the digest is taken over
        `full_prompt`, which `_prompt_with_schema` builds with the model's JSON
        schema embedded. So a contract change invalidates its own entries and
        cannot land here as a stale-shape mismatch.
        """
        if cache_path is None or not cache_path.exists():
            return None
        try:
            return response_model.model_validate_json(
                cache_path.read_text(encoding="utf-8")
            )
        except (OSError, ValueError):
            # ValueError covers pydantic's ValidationError and JSON decode errors.
            return None


# Keys pydantic emits that a Gemini function declaration rejects. Stripped rather
# than translated: they are documentation or JSON Schema bookkeeping, and none of
# them changes what shape of object the model should return.
_SCHEMA_KEYS_TO_DROP = frozenset(
    {"title", "default", "additionalProperties", "$schema", "discriminator"}
)


def tool_parameter_schema(model: type[BaseModel]) -> dict:
    """A pydantic model's schema, in the subset tool declarations accept.

    `model_json_schema()` describes nested models with `$ref` and collects them
    under `$defs`, and optional fields as `anyOf: [{...}, {"type": "null"}]`.
    Gemini's `types.Tool` refuses all three, which is why the mapping validator
    hand-writes its declarations instead of deriving them.

    Hand-writing means the schema and the model drift apart silently, so this
    converts instead: definitions are inlined, nullable unions collapse to the
    non-null branch, and bookkeeping keys are dropped.
    """
    schema = model.model_json_schema()
    definitions = schema.pop("$defs", {})

    def resolve(node, depth=0):
        # Depth-guarded: a self-referencing model would otherwise inline forever.
        if depth > 12:
            return {"type": "object"}
        if isinstance(node, list):
            return [resolve(item, depth + 1) for item in node]
        if not isinstance(node, dict):
            return node

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
