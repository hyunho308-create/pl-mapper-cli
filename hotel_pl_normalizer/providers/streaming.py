"""Reassemble a streamed chat completion into the shape a whole one has.

Why the mapping call is streamed
--------------------------------
A non-streamed request sends the prompt and then nothing travels over the
connection until the model has finished -- for these workbooks, up to ten
minutes. Whatever sits in front of the model cannot tell that apart from a dead
backend, and cuts the connection with a 504.

Measured across one corpus sweep: 154 mapping requests, median 35s, p90 426s, and
then a wall. The slowest request that ever completed took 603.6s and nothing
landed beyond it, against a client timeout of 1800s. That shape -- a tail that
stops dead rather than thinning out -- is a server-side ceiling at about 600
seconds, and five runs died on it after their structure stages had finished.

A streamed reply arrives continuously, so the connection never looks idle. The
model is not faster; the bytes just keep moving.

What this module does
---------------------
Streaming changes the shape of the reply, so something has to put it back. The
pieces arrive as deltas:

    content      one fragment at a time, concatenated in order
    tool calls   fragmented *and interleaved*, keyed by `index`; the function
                 name usually completes long before its JSON arguments
    usage        absent unless `stream_options={"include_usage": True}` is sent,
                 and then it arrives in a final chunk carrying no choices
    finish_reason on the last chunk that carries a choice

`accumulate_stream` rebuilds all four into plain dicts and small objects that
answer to the same attribute access as the SDK's own response, so the tool loop
that consumes it needs no changes.

The usage part is not a detail. The spend cap in `limits.py` and every cost
figure in the run log are computed from provider-returned token counts; without
that stream option they would silently become zero, and a cap that reads zero is
a cap that never fires.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable


@dataclass
class StreamedChoice:
    """One choice, shaped like the SDK's so `_field` and the tool loop work."""

    message: dict[str, Any]
    finish_reason: str | None = None


@dataclass
class StreamedResponse:
    """A reassembled completion.

    `message` is a plain dict rather than a model object on purpose:
    `_assistant_message` passes dicts through untouched and calls `model_dump`
    only on objects that have it, so tool calls survive the round trip back into
    the message list without a conversion step.
    """

    choices: list[StreamedChoice]
    usage: Any = None
    model: str | None = None
    # True when the provider never sent a usage chunk. The caller reports the
    # call rather than inventing zeros for it.
    usage_missing: bool = False


@dataclass
class _PartialToolCall:
    """One tool call being assembled across chunks."""

    call_id: str | None = None
    name_parts: list[str] = field(default_factory=list)
    argument_parts: list[str] = field(default_factory=list)
    announced: bool = False

    @property
    def name(self) -> str:
        return "".join(self.name_parts)

    def finish(self) -> dict[str, Any]:
        return {
            "id": self.call_id or "",
            "type": "function",
            "function": {
                "name": self.name,
                # Empty arguments must stay a valid JSON object: the loop parses
                # this with json.loads and reports a parse failure back to the
                # model, which would be a confusing way to say "it sent none".
                "arguments": "".join(self.argument_parts) or "{}",
            },
        }


def accumulate_stream(
    chunks: Iterable[Any],
    *,
    on_progress: Callable[[float, int, bool], None] | None = None,
    progress_interval_seconds: float = 15.0,
    on_tool_call_named: Callable[[str], None] | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> StreamedResponse:
    """Fold a stream of chunks into one response.

    `on_progress(elapsed_seconds, output_tokens, thinking)` is called at most
    once per `progress_interval_seconds`. Throttled because every activity line
    rewrites the job's `state.json`, which the browser is polling; an unthrottled
    feed would rewrite it hundreds of times a round for no extra information.

    `on_tool_call_named(name)` fires the first time a tool call's name is
    complete, which is typically well before its arguments are.

    `clock` is injectable so the throttle can be tested without waiting.
    """
    started = clock()
    last_progress = started

    content_parts: list[str] = []
    reasoning_parts: list[str] = []
    tool_calls: dict[int, _PartialToolCall] = {}
    finish_reason: str | None = None
    usage: Any = None
    model: str | None = None
    # A count of streamed fragments, not real tokens. Real counts come from the
    # usage chunk; this exists only so the progress line can say something is
    # happening, and it is never recorded as usage.
    fragments = 0

    for chunk in chunks:
        if model is None:
            model = getattr(chunk, "model", None)

        chunk_usage = getattr(chunk, "usage", None)
        if chunk_usage is not None:
            usage = chunk_usage

        choices = getattr(chunk, "choices", None) or []
        if choices:
            choice = choices[0]
            delta = getattr(choice, "delta", None)

            if delta is not None:
                text = getattr(delta, "content", None)
                if text:
                    content_parts.append(text)
                    fragments += 1

                reasoning = getattr(delta, "reasoning_content", None)
                if reasoning:
                    reasoning_parts.append(reasoning)
                    fragments += 1

                for delta_call in getattr(delta, "tool_calls", None) or []:
                    index = getattr(delta_call, "index", 0) or 0
                    partial = tool_calls.setdefault(index, _PartialToolCall())
                    call_id = getattr(delta_call, "id", None)
                    if call_id:
                        partial.call_id = call_id
                    function = getattr(delta_call, "function", None)
                    if function is not None:
                        name_piece = getattr(function, "name", None)
                        if name_piece:
                            partial.name_parts.append(name_piece)
                        argument_piece = getattr(function, "arguments", None)
                        if argument_piece:
                            partial.argument_parts.append(argument_piece)
                            fragments += 1
                    # Announce once the name is present and arguments have begun,
                    # which is the point at which the name is known to be whole.
                    if (
                        not partial.announced
                        and partial.name
                        and partial.argument_parts
                        and on_tool_call_named is not None
                    ):
                        partial.announced = True
                        on_tool_call_named(partial.name)

            reason = getattr(choice, "finish_reason", None)
            if reason:
                finish_reason = reason

        if on_progress is not None:
            now = clock()
            if now - last_progress >= progress_interval_seconds:
                last_progress = now
                on_progress(
                    now - started,
                    fragments,
                    # Still thinking while nothing but reasoning has arrived.
                    bool(reasoning_parts) and not content_parts and not tool_calls,
                )

    message: dict[str, Any] = {
        "role": "assistant",
        "content": "".join(content_parts) or None,
    }
    if reasoning_parts:
        message["reasoning_content"] = "".join(reasoning_parts)
    if tool_calls:
        message["tool_calls"] = [
            tool_calls[index].finish() for index in sorted(tool_calls)
        ]

    return StreamedResponse(
        choices=[StreamedChoice(message=message, finish_reason=finish_reason)],
        usage=usage,
        model=model,
        usage_missing=usage is None,
    )
