from __future__ import annotations

from hotel_pl_normalizer.models.common import ModelInfo
from hotel_pl_normalizer.models.period_selection import (
    PeriodCatalog,
    PeriodCatalogRepair,
    PeriodColumnPacket,
)


class PeriodCatalogBackend:
    """Run period discovery through either configured JSON model client."""

    def __init__(self, client) -> None:
        self.client = client

    def run(self, packet: PeriodColumnPacket, prompt: str) -> PeriodCatalog:
        result = self.client.generate_json_model(prompt, PeriodCatalog)
        return result.model_copy(
            update={
                "workbook_id": packet.workbook_id,
                "model": ModelInfo(
                    provider=self.client.provider,
                    model_name=self.client.model_name,
                    prompt_version="period_discovery_v1",
                ),
            }
        )

    def repair(
        self, packet: PeriodColumnPacket, prompt: str
    ) -> PeriodCatalogRepair:
        del packet
        return self.client.generate_json_model(prompt, PeriodCatalogRepair)
