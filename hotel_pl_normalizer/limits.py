"""Stop a run when it takes too long, costs too much, or is told to.

Why this is not just a timer on the job record
----------------------------------------------
Marking a job failed does not stop it. The worker thread keeps going and keeps
issuing billed requests, so a cap enforced only on the job record caps the *label*
and not the spend. This is checked inside the model loop, between rounds, which is
the last moment at which no further money is committed.

Three reasons to stop, one interface. Whichever trips first wins, and the reason
travels back to the client so a person who pressed a button and a run that blew its
budget do not read as the same event.

Pricing
-------
Cost is computed from rates supplied by the caller, never guessed. Model pricing
changes and a hardcoded rate silently becomes a lie -- a budget that thinks it is
enforcing $3 while enforcing $7 is worse than no budget, because it is trusted.
With no rates configured the spend cap is inert and `cost_usd` returns None, which
callers must report rather than render as $0.00.
"""

from __future__ import annotations

import threading
import time
from typing import Callable


class RunLimiter:
    """One callable answering "should this run stop, and why?"."""

    def __init__(
        self,
        *,
        deadline_seconds: float | None = None,
        max_cost_usd: float | None = None,
        price_input_per_mtok: float | None = None,
        price_output_per_mtok: float | None = None,
        cost_estimator: Callable[[list[dict]], float] | None = None,
        stop_requested: Callable[[], bool] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.deadline_seconds = deadline_seconds
        self.max_cost_usd = max_cost_usd
        self.price_input_per_mtok = price_input_per_mtok
        self.price_output_per_mtok = price_output_per_mtok
        self.cost_estimator = cost_estimator
        self.stop_requested = stop_requested
        self._clock = clock
        self._started = clock()
        self._usage: list[dict] | None = None
        self.stopped_reason: str | None = None

    # -- wiring ---------------------------------------------------------------

    def watch(self, client) -> "RunLimiter":
        """Read spend from this client's usage history as the run proceeds."""
        self._usage = client.usage_history
        return self

    @property
    def priced(self) -> bool:
        return self.cost_estimator is not None or (
            self.price_input_per_mtok is not None
            and self.price_output_per_mtok is not None
        )

    # -- measurement ----------------------------------------------------------

    def elapsed_seconds(self) -> float:
        return self._clock() - self._started

    def cost_usd(self) -> float | None:
        """Spend so far, or None when no rates were supplied.

        A provider-specific estimator can account for cached input, cache writes,
        reasoning tokens, and long-context pricing. The simple input/output-rate
        fallback remains for providers whose usage has no richer breakdown.
        """
        if not self.priced or self._usage is None:
            return None
        if self.cost_estimator is not None:
            return self.cost_estimator(self._usage)
        input_tokens = 0
        output_tokens = 0
        for call in self._usage:
            if call.get("cache_hit"):
                continue  # served from disk, never sent, never billed
            input_tokens += int(call.get("prompt_token_count") or 0)
            output_tokens += int(call.get("candidates_token_count") or 0)
            output_tokens += int(call.get("thoughts_token_count") or 0)
        return (
            input_tokens * self.price_input_per_mtok / 1_000_000
            + output_tokens * self.price_output_per_mtok / 1_000_000
        )

    # -- the check ------------------------------------------------------------

    def __call__(self) -> str | None:
        """Return a human-readable reason to stop, or None to continue."""
        if self.stopped_reason is not None:
            return self.stopped_reason

        if self.stop_requested is not None and self.stop_requested():
            self.stopped_reason = "Stopped at your request."
        elif (
            self.deadline_seconds is not None
            and self.elapsed_seconds() > self.deadline_seconds
        ):
            self.stopped_reason = (
                f"This run passed its {int(self.deadline_seconds // 60)}-minute "
                "limit and was stopped."
            )
        else:
            spent = self.cost_usd()
            if (
                self.max_cost_usd is not None
                and spent is not None
                and spent >= self.max_cost_usd
            ):
                self.stopped_reason = (
                    f"This run reached its ${self.max_cost_usd:.2f} processing "
                    f"budget (${spent:.2f} spent) and was stopped."
                )
        return self.stopped_reason


def stop_flag(event: threading.Event) -> Callable[[], bool]:
    """Adapt a threading.Event to the `stop_requested` interface."""
    return event.is_set
