"""Current model adapter and the neutral contract future adapters implement."""

from .base import ModelClient, ModelToolset, ProviderRunCancelled
from .openai_api import OpenAIModelClient


def create_model_client(
    *,
    reasoning_effort: str = "medium",
    repair_reasoning_effort: str = "medium",
) -> ModelClient:
    """Build the model used by the supported workflow.

    A future supplier adapter replaces this factory result while preserving the
    :class:`ModelClient` contract; the pipeline itself does not branch.
    """
    return OpenAIModelClient(
        reasoning_effort=reasoning_effort,
        repair_reasoning_effort=repair_reasoning_effort,
    )


__all__ = [
    "ModelClient",
    "ModelToolset",
    "ProviderRunCancelled",
    "create_model_client",
]
