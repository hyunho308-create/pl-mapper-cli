from __future__ import annotations

import json
from dataclasses import dataclass
from importlib import resources
from typing import Protocol

from hotel_pl_normalizer.models.common import ModelInfo
from hotel_pl_normalizer.models.sheet_selection import (
    SheetNameSelectionResult,
    SheetNameSelectionValidation,
    SheetNameTriagePacket,
)
from hotel_pl_normalizer.structure.routing.sheet_name_validation import (
    normalize_sheet_name_selection_result,
    validate_sheet_name_selection_result,
)
from hotel_pl_normalizer.structure.routing.sheet_names import (
    build_local_sheet_name_selection,
)


class SheetNameTriageBackend(Protocol):
    def run(self, packet: SheetNameTriagePacket, prompt: str) -> SheetNameSelectionResult:
        """Return a model-shaped SheetNameSelectionResult."""


class LocalSheetNameTriageBackend:
    def run(self, packet: SheetNameTriagePacket, prompt: str) -> SheetNameSelectionResult:
        result = build_local_sheet_name_selection(packet)
        return result.model_copy(
            update={
                "model": ModelInfo(
                    provider="local",
                    model_name="sheet_name_heuristic",
                    prompt_version="sheet_name_triage_v1",
                )
            }
        )


@dataclass(frozen=True)
class SheetNameTriageAgentOutput:
    selection: SheetNameSelectionResult
    validation: SheetNameSelectionValidation
    prompt: str


class SheetNameTriageAgent:
    def __init__(self, backend: SheetNameTriageBackend | None = None) -> None:
        self.backend = backend or LocalSheetNameTriageBackend()

    def run(self, packet: SheetNameTriagePacket) -> SheetNameTriageAgentOutput:
        prompt = render_sheet_name_triage_prompt(packet)
        raw_selection = self.backend.run(packet, prompt)
        selection = normalize_sheet_name_selection_result(packet, raw_selection)
        validation = validate_sheet_name_selection_result(packet, selection)
        return SheetNameTriageAgentOutput(selection=selection, validation=validation, prompt=prompt)


def render_sheet_name_triage_prompt(packet: SheetNameTriagePacket, *, include_skill: bool = True) -> str:
    sections: list[str] = []
    if include_skill:
        sections.append(_load_sheet_name_triage_skill())
    sections.append("## Input SheetNameTriagePacket")
    sections.append("```json")
    sections.append(
        json.dumps(
            {
                "source_filename": packet.source_filename,
                "sheet_count": packet.sheet_count,
                "sheets": [
                    {
                        "sheet_name": sheet.sheet_name,
                        "visible": sheet.visible,
                        "header_cells": sheet.header_cells,
                    }
                    for sheet in packet.sheets
                ],
            },
            separators=(",", ": "),
        )
    )
    sections.append("```")
    sections.append("Return only JSON that conforms to SheetNameSelectionResult.")
    return "\n\n".join(sections)


def _load_sheet_name_triage_skill() -> str:
    return resources.files("hotel_pl_normalizer.prompts").joinpath("sheet_name_triage.md").read_text(encoding="utf-8")
