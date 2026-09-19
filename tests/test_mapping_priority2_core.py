import hashlib
import json
from datetime import datetime

import pytest

import hotel_pl_normalizer.mapping.mapper as mapper_module
from hotel_pl_normalizer.mapping.findings import Finding
from hotel_pl_normalizer.mapping.mapper import (
    DETERMINISTIC_SUMMARY_ACCOUNTS,
    WORKBOOK_DYNAMIC_DATA_MARKER,
    AccountSourceDecision,
    OodMiscSummaryMode,
    SourceOperation,
    WorkbookMappingValidator,
    WorkbookSourcePlan,
    WorkbookStrategy,
    _coa_lines,
    _execute,
    _group_rows,
    _hierarchy_equations,
    _load_coa,
    _primary_prompt,
    _stable_mapping_prompt_prefix,
    _stable_mapping_prompt_telemetry,
    _summary_equation_lines,
    map_workbook,
)
from hotel_pl_normalizer.mapping.repair import (
    AUTO_REPAIR_RULES,
    RepairContext,
    repair,
)
from hotel_pl_normalizer.mapping.rules import rule_registry
from hotel_pl_normalizer.models.binding import WorkbookBindings
from hotel_pl_normalizer.models.period_selection import (
    PeriodColumnSelection,
    PeriodColumnSelectionMap,
    PeriodOption,
    PeriodScenario,
)
from hotel_pl_normalizer.models.workbook import (
    CellRecord,
    FileType,
    MergedRange,
    WorkbookRecord,
    WorkbookRow,
    WorkbookSheet,
    WorkbookSource,
)
from hotel_pl_normalizer.structure.binding.agent import bind_periods
from hotel_pl_normalizer.structure.binding.checks import check_bindings
from hotel_pl_normalizer.structure.binding.toolset import PeriodBindingToolset
from hotel_pl_normalizer.structure.representation.builder import (
    LabelLayout,
    select_row_label,
)


def _strategy() -> WorkbookStrategy:
    return WorkbookStrategy(
        reporting_layout="test",
        summary_source="test",
        ood_misc_summary_mode=OodMiscSummaryMode.SEPARATE,
    )


def _plan(*decisions: AccountSourceDecision) -> WorkbookSourcePlan:
    return WorkbookSourcePlan(
        plan_id="plan",
        workbook_id="wb",
        strategy=_strategy(),
        decisions=list(decisions),
    )


def _finding(rule: str, target: str = "S1.test") -> Finding:
    severity = "warning" if rule == "coverage_unspecified" else "error"
    return Finding(severity, rule, target)


def _repair_evidence() -> list[dict]:
    return [
        {
            "row_key": f"Sheet!{index}",
            "label": f"Row {index}",
            "selected_value": actual,
            "selected_values": {"actual": actual, "prior": prior},
        }
        for index, (actual, prior) in enumerate(
            ((6.0, 12.0), (3.0, 4.0), (2.0, 5.0)),
            start=1,
        )
    ]


@pytest.mark.parametrize(
    ("operation", "source_rows", "scale_factor"),
    [
        (SourceOperation.DIRECT, ["Sheet!1"], None),
        (SourceOperation.SUM, ["Sheet!1", "Sheet!2"], None),
        (SourceOperation.NEGATE, ["Sheet!1", "Sheet!2"], None),
        (SourceOperation.RATIO, ["Sheet!1", "Sheet!2"], None),
        (SourceOperation.PRODUCT, ["Sheet!1", "Sheet!2"], None),
        (SourceOperation.SCALE, ["Sheet!1", "Sheet!2"], 2.5),
    ],
)
def test_overlap_cleanup_preserves_values_for_every_eligible_operation(
    operation,
    source_rows,
    scale_factor,
):
    decision = AccountSourceDecision(
        coa_id="S1.test",
        operation=operation,
        source_rows=source_rows,
        excluded_rows=[source_rows[0], "Sheet!3"],
        scale_factor=scale_factor,
    )
    plan = _plan(decision)
    evidence = _repair_evidence()
    coa = {"S1.test": {"coa_id": "S1.test", "parent_coa_id": ""}}
    before = {
        period_id: _execute(
            plan.decisions,
            evidence,
            coa,
            period_id=period_id,
            preserve_blanks=True,
        )[0]["S1.test"]
        for period_id in ("actual", "prior")
    }

    result = repair(
        plan,
        [_finding("source_row_included_and_excluded")],
        RepairContext(revalidate=lambda candidate: ()),
    )
    after = {
        period_id: _execute(
            result.plan.decisions,
            evidence,
            coa,
            period_id=period_id,
            preserve_blanks=True,
        )[0]["S1.test"]
        for period_id in ("actual", "prior")
    }

    repaired = result.plan.decisions[0]
    assert before == after
    assert repaired.source_rows == source_rows
    assert repaired.excluded_rows == ["Sheet!3"]
    assert result.changed
    assert result.residual_findings == ()


@pytest.mark.parametrize(
    "rule",
    ["source_row_repeated", "coverage_unspecified"],
)
def test_repeated_rows_and_coverage_stay_model_owned(rule):
    plan = _plan(
        AccountSourceDecision(
            coa_id="S1.test",
            operation=SourceOperation.SUM,
            source_rows=["Sheet!1", "Sheet!1"],
        )
    )
    finding = _finding(rule)

    result = repair(plan, [finding], RepairContext())

    assert not result.changed
    assert result.plan is plan
    assert result.residual_findings == (finding,)
    assert result.before_plan_digest == result.after_plan_digest


def test_adjusted_subtotal_overlap_stays_model_owned():
    plan = _plan(
        AccountSourceDecision(
            coa_id="S1.test",
            operation=SourceOperation.ADJUSTED_SUBTOTAL,
            source_rows=["Sheet!1"],
            excluded_rows=["Sheet!1"],
        )
    )
    finding = _finding("source_row_included_and_excluded")

    result = repair(plan, [finding], RepairContext())

    assert not result.changed
    assert result.plan is plan
    assert result.residual_findings == (finding,)


def test_repair_audit_digest_chain_full_revalidation_and_idempotence():
    plan = _plan(
        AccountSourceDecision(
            coa_id="S1.one",
            operation=SourceOperation.SUM,
            source_rows=["Sheet!1"],
            excluded_rows=["Sheet!1"],
        ),
        AccountSourceDecision(
            coa_id="S1.two",
            operation=SourceOperation.NEGATE,
            source_rows=["Sheet!2"],
            excluded_rows=["Sheet!2"],
        ),
    )
    findings = [
        _finding("source_row_included_and_excluded", "S1.one"),
        _finding("source_row_included_and_excluded", "S1.two"),
    ]
    revalidated = _finding("source_detail_incomplete", "S1.one")
    callbacks = []

    def revalidate(candidate):
        callbacks.append(candidate)
        return (revalidated,)

    result = repair(plan, findings, RepairContext(revalidate=revalidate))

    assert callbacks == [result.plan]
    assert result.residual_findings == (revalidated,)
    assert result.applied[0].before_plan_digest == result.before_plan_digest
    assert result.applied[0].after_plan_digest == result.applied[1].before_plan_digest
    assert result.applied[-1].after_plan_digest == result.after_plan_digest
    assert all(item.before != item.after and item.reason for item in result.applied)

    second = repair(result.plan, findings, RepairContext(revalidate=revalidate))
    assert not second.changed
    assert second.plan is result.plan
    assert second.before_plan_digest == second.after_plan_digest
    assert callbacks == [result.plan]


def test_live_validator_rechecks_overlap_cleanup_at_a_fixed_point():
    validator = WorkbookMappingValidator(
        "wb",
        [
            {
                "row_key": "Sheet!1",
                "label": "Account",
                "selected_value": 10.0,
                "selected_values": {"actual": 10.0},
            }
        ],
        {
            "S1.test": {
                "coa_id": "S1.test",
                "parent_coa_id": "",
                "is_residual": "false",
            }
        },
        period_labels={"actual": "2025 Actual"},
    )
    plan = _plan(
        AccountSourceDecision(
            coa_id="S1.test",
            operation=SourceOperation.SUM,
            source_rows=["Sheet!1"],
            excluded_rows=["Sheet!1"],
        )
    )

    result = validator.dispatch("validate_mapping", plan.model_dump(mode="json"))

    assert result["accepted"]
    assert len(validator.history) == 1
    assert validator.current_plan.decisions[0].source_rows == ["Sheet!1"]
    assert validator.current_plan.decisions[0].excluded_rows == []
    assert result["deterministic_repair_count"] == 1
    assert result["deterministic_repairs"][0]["rule"] == (
        "source_row_included_and_excluded"
    )
    assert result["deterministic_repairs"][0]["model_validation_attempt"] == 1


def test_live_validator_noop_keeps_legacy_result_shape():
    validator = WorkbookMappingValidator(
        "wb",
        [{"row_key": "Sheet!1", "selected_value": 10.0}],
        {"S1.test": {"coa_id": "S1.test", "parent_coa_id": ""}},
    )
    result = validator.dispatch(
        "validate_mapping",
        _plan(
            AccountSourceDecision(
                coa_id="S1.test",
                operation=SourceOperation.SUM,
                source_rows=["Sheet!1"],
            )
        ).model_dump(mode="json"),
    )

    assert result["accepted"]
    assert "deterministic_repair_count" not in result
    assert "deterministic_repairs" not in result
    assert validator.deterministic_repairs == []


def test_registry_enables_only_value_preserving_overlap_cleanup():
    enabled = {
        rule for rule, policy in rule_registry().items() if policy.auto_repairable
    }
    assert enabled == AUTO_REPAIR_RULES == {
        "source_row_included_and_excluded"
    }


def _period_map(workbook_id: str = "wb-a") -> PeriodColumnSelectionMap:
    return PeriodColumnSelectionMap(
        selection_map_id="selection",
        workbook_id=workbook_id,
        requested_period="2025 Actual",
        sheet_selections=[
            PeriodColumnSelection(
                sheet_name="Summary",
                value_column=2,
                excel_column="B",
                period_label="2025 Actual",
            )
        ],
    )


def test_canonical_mapping_prompt_prefix_has_golden_bytes_and_hash():
    coa = _load_coa()
    model_coa = {
        coa_id: metadata
        for coa_id, metadata in coa.items()
        if coa_id not in DETERMINISTIC_SUMMARY_ACCOUNTS
    }
    encoded = _stable_mapping_prompt_prefix(model_coa).encode("utf-8")

    assert len(encoded) == 105475
    assert hashlib.sha256(encoded).hexdigest() == (
        "a4bbe934eab83a6b3144a97ac82ac5d28055f155a796dab3ccb05e36ed3f9c12"
    )


def test_mapper_tool_declarations_have_golden_bytes_and_no_private_review_ids():
    declarations = WorkbookMappingValidator("wb", [], _load_coa()).declarations()
    encoded = json.dumps(
        declarations,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")

    assert len(encoded) == 38763
    assert hashlib.sha256(encoded).hexdigest() == (
        "2d78d7abe66f0b87185674f89f8f9aaebab596a6937ee122cf5ce98ecae35152"
    )
    assert b"review_item_id" not in encoded


def test_reordered_prompt_is_semantically_the_old_combined_json_payload():
    coa = _load_coa()
    period = _period_map()
    evidence = [
        {
            "row_key": "Summary!1",
            "label": "DynamicEvidenceAlphaToken",
            "selected_value": 10.0,
        }
    ]
    prompt = _primary_prompt(
        workbook_id="wb-a",
        requested_period="2025 Actual",
        periods={"actual": period},
        period_labels={"actual": "2025 Actual"},
        evidence=evidence,
        coa=coa,
        excluded_sheets=["Notes"],
        sheet_routing_context=[{"sheet_name": "Summary"}],
    )
    payload = json.loads(prompt.split(f"{WORKBOOK_DYNAMIC_DATA_MARKER}\n\n", 1)[1])
    model_coa = {
        coa_id: metadata
        for coa_id, metadata in coa.items()
        if coa_id not in DETERMINISTIC_SUMMARY_ACCOUNTS
    }
    expected = {
        "workbook_id": "wb-a",
        "requested_period": "2025 Actual",
        "period_columns": ["Summary|column=2|2025 Actual"],
        "coa": _coa_lines(model_coa),
        "coa_hierarchy_equations": _hierarchy_equations(model_coa),
        "summary_equations": _summary_equation_lines(),
        "sheets_excluded_as_nonfinancial": ["Notes"],
        "sheet_routing_context": [{"sheet_name": "Summary"}],
        "workbook_rows": _group_rows(evidence, {"actual": "2025 Actual"}),
    }

    assert payload == expected
    assert list(payload)[:3] == [
        "coa",
        "coa_hierarchy_equations",
        "summary_equations",
    ]
    assert "mapping_rules" not in payload
    assert prompt.startswith(_stable_mapping_prompt_prefix(model_coa))


def test_distant_source_caption_reaches_mapper_without_changing_values():
    row = WorkbookRow(row_index=185, cells=[
        CellRecord(row=185, column=column, address=f"R185C{column}", raw_value=value)
        for column, value in [(2, "KITCHEN"), (3, "FDBP"), (4, "DPRM"),
                              (5, "Kitchen Management"), (6, 397092.87)]
    ])
    selected = select_row_label(row, LabelLayout(primary_column=3))
    evidence = [{"row_key": "Summary!185", "label": selected.cell.raw_value,
                 "label_context": [cell.raw_value for cell in selected.context],
                 "selected_values": {"actual": 397092.87, "prior": 310737.37}}]
    prompt = _primary_prompt("wb-a", "2025 Actual", {"actual": _period_map()},
        {"actual": "2025 Actual", "prior": "2024 Actual"}, evidence, _load_coa(), [])
    payload = json.loads(prompt.split(f"{WORKBOOK_DYNAMIC_DATA_MARKER}\n\n", 1)[1])
    assert selected.cell.raw_value == "FDBP"
    assert payload["workbook_rows"][0]["rows"] == [
        "185|FDBP / KITCHEN / DPRM / Kitchen Management|397092.87|310737.37"
    ]


def test_mapping_selection_records_stable_prefix_telemetry(monkeypatch):
    coa = {
        "S1.test": {
            "coa_id": "S1.test",
            "parent_coa_id": "",
            "is_residual": "false",
        }
    }
    monkeypatch.setattr(mapper_module, "_load_coa", lambda: coa)

    class Client:
        usage_history = []

        def generate_json_model_with_tools(
            self,
            prompt,
            response_model,
            *,
            toolset,
            **kwargs,
        ):
            del prompt, kwargs
            validation = toolset.dispatch(
                "validate_mapping",
                _plan(
                    AccountSourceDecision(
                        coa_id="S1.test",
                        operation=SourceOperation.SUM,
                        source_rows=["Summary!1"],
                    )
                ).model_dump(mode="json"),
            )
            return response_model.model_validate(
                {
                    "workbook_id": "wb",
                    "status": "accepted",
                    "outcome": validation["outcome"],
                    "validation_attempt": validation["validation_attempt"],
                }
            )

    result = map_workbook(
        workbook_id="wb",
        requested_period="2025 Actual",
        periods=_period_map("wb"),
        evidence=[
            {
                "row_key": "Summary!1",
                "label": "Revenue",
                "selected_value": 10.0,
            }
        ],
        excluded_sheets=[],
        client=Client(),
    )

    assert result.mapping_selection["stable_prompt_prefix"] == (
        _stable_mapping_prompt_telemetry(coa)
    )


def _sheet(*headers: str) -> WorkbookSheet:
    header_cells = [
        CellRecord(row=1, column=1, address="A1", raw_value="Account")
    ]
    value_cells = [
        CellRecord(row=2, column=1, address="A2", raw_value="Rooms Revenue")
    ]
    for index, header in enumerate(headers, start=2):
        column = chr(64 + index)
        header_cells.append(
            CellRecord(
                row=1,
                column=index,
                address=f"{column}1",
                raw_value=header,
            )
        )
        value_cells.append(
            CellRecord(
                row=2,
                column=index,
                address=f"{column}2",
                raw_value=100.0 + index,
            )
        )
    return WorkbookSheet(
        sheet_id="sheet",
        sheet_name="Summary",
        max_row=2,
        max_column=len(headers) + 1,
        rows=[
            WorkbookRow(row_index=1, cells=header_cells),
            WorkbookRow(row_index=2, cells=value_cells),
        ],
    )


def _period(
    scenario=PeriodScenario.ACTUAL,
    start="2025-01",
    end="2025-06",
) -> PeriodOption:
    return PeriodOption(scenario=scenario, start_month=start, end_month=end)


def _binding_check(sheet, period):
    return check_bindings(
        WorkbookBindings.model_validate(
            {
                "bindings": [
                    {
                        "period_id": period.period_id,
                        "sheet_name": sheet.sheet_name,
                        "excel_column": "B",
                    }
                ]
            }
        ),
        {sheet.sheet_name: sheet},
        period_ids=[period.period_id],
        financial_sheets=[sheet.sheet_name],
        periods=[period],
    )


def test_stacked_headers_are_not_unioned_at_the_final_boundary():
    sheet = WorkbookSheet(
        sheet_id="stacked",
        sheet_name="Summary",
        max_row=21,
        max_column=2,
        rows=[
            WorkbookRow(
                1,
                [CellRecord(1, 2, "B1", "June 2025 YTD Actual")],
            ),
            WorkbookRow(
                2,
                [
                    CellRecord(2, 1, "A2", "Rooms Revenue"),
                    CellRecord(2, 2, "B2", 100.0),
                ],
            ),
            WorkbookRow(20, [CellRecord(20, 2, "B20", "Budget")]),
            WorkbookRow(
                21,
                [
                    CellRecord(21, 1, "A21", "Rooms Revenue"),
                    CellRecord(21, 2, "B21", 110.0),
                ],
            ),
        ],
    )

    result = _binding_check(sheet, _period(PeriodScenario.BUDGET))

    assert not result.accepted
    assert any("stacked-header scenario" in item for item in result.rejections)


def test_generic_total_is_insufficient_and_remains_model_owned():
    result = _binding_check(_sheet("2025 Actual Total"), _period(end="2025-12"))

    assert result.accepted


def test_merged_header_is_used_by_final_compatibility_validation():
    sheet = WorkbookSheet(
        sheet_id="merged",
        sheet_name="Summary",
        max_row=2,
        max_column=3,
        rows=[
            WorkbookRow(
                1,
                [
                    CellRecord(
                        1,
                        2,
                        "B1",
                        "June 2025 YTD Actual",
                        is_merged=True,
                        merged_parent="B1",
                    )
                ],
            ),
            WorkbookRow(
                2,
                [
                    CellRecord(2, 1, "A2", "Rooms Revenue"),
                    CellRecord(2, 2, "B2", 100.0),
                    CellRecord(2, 3, "C2", 110.0),
                ],
            ),
        ],
        merged_ranges=[
            MergedRange("B1:C1", 1, 2, "June 2025 YTD Actual")
        ],
    )

    assert _binding_check(sheet, _period()).accepted
    assert not _binding_check(sheet, _period(PeriodScenario.BUDGET)).accepted


def test_current_actual_and_relative_prior_are_compatible():
    sheet = WorkbookSheet(
        sheet_id="actual-prior",
        sheet_name="Summary",
        max_row=3,
        max_column=3,
        rows=[
            WorkbookRow(
                1,
                [
                    CellRecord(
                        1,
                        2,
                        "B1",
                        "June 2025 YTD",
                        is_merged=True,
                        merged_parent="B1",
                    )
                ],
            ),
            WorkbookRow(
                2,
                [
                    CellRecord(2, 2, "B2", "Actual"),
                    CellRecord(2, 3, "C2", "Prior Year"),
                ],
            ),
            WorkbookRow(
                3,
                [
                    CellRecord(3, 1, "A3", "Rooms Revenue"),
                    CellRecord(3, 2, "B3", 100.0),
                    CellRecord(3, 3, "C3", 90.0),
                ],
            ),
        ],
        merged_ranges=[MergedRange("B1:C1", 1, 2, "June 2025 YTD")],
    )
    actual = _period()
    prior = _period(start="2024-01", end="2024-06")
    result = check_bindings(
        WorkbookBindings.model_validate(
            {
                "bindings": [
                    {
                        "period_id": actual.period_id,
                        "sheet_name": "Summary",
                        "excel_column": "B",
                    },
                    {
                        "period_id": prior.period_id,
                        "sheet_name": "Summary",
                        "excel_column": "C",
                    },
                ]
            }
        ),
        {"Summary": sheet},
        period_ids=[actual.period_id, prior.period_id],
        financial_sheets=["Summary"],
        periods=[actual, prior],
    )

    assert result.accepted, result.rejections


@pytest.mark.parametrize(
    ("header", "period"),
    [
        ("June 2025 Actual", _period(start="2025-06", end="2025-06")),
        ("June 2025 YTD Actual", _period()),
        ("June 2025 TTM Actual", _period(start="2024-07", end="2025-06")),
    ],
)
def test_monthly_ytd_and_ttm_compatible_bindings(header, period):
    assert _binding_check(_sheet(header), period).accepted


@pytest.mark.parametrize(
    ("header", "period", "expected"),
    [
        (
            "June 2025 YTD Actual",
            _period(start="2025-06", end="2025-06"),
            "monthly",
        ),
        ("June 2025 Actual", _period(PeriodScenario.BUDGET), "scenario"),
        ("June 2024 YTD Actual", _period(), "year"),
        ("June 2025 TTM Actual", _period(), "YTD"),
    ],
)
def test_final_binding_boundary_rejects_wrong_scenario_year_or_grain(
    header,
    period,
    expected,
):
    result = _binding_check(_sheet(header), period)

    assert not result.accepted
    assert any(expected.casefold() in item.casefold() for item in result.rejections)


@pytest.mark.parametrize("header", ["Amount", "June 2025 YTD Actual Budget"])
def test_insufficient_or_ambiguous_identity_remains_model_owned(header):
    assert _binding_check(_sheet(header), _period()).accepted


def _workbook(sheet: WorkbookSheet) -> WorkbookRecord:
    return WorkbookRecord(
        workbook_id="wb",
        source=WorkbookSource(
            source_id="source",
            original_filename="test.xlsx",
            file_type=FileType.XLSX,
            ingested_at=datetime(2025, 7, 1),
        ),
        sheets=[sheet],
    )


def test_provider_final_object_cannot_bypass_the_tool_read_gate():
    period = _period()

    class Client:
        prompt = ""

        def generate_json_model_with_tools(self, prompt, response_model, **kwargs):
            del kwargs
            self.prompt = prompt
            return response_model.model_validate(
                {
                    "bindings": [
                        {
                            "period_id": period.period_id,
                            "sheet_name": "Summary",
                            "excel_column": "B",
                        }
                    ]
                }
            )

    client = Client()
    with pytest.raises(RuntimeError, match="uninspected sheet"):
        bind_periods(
            _workbook(_sheet("June 2025 YTD Actual")),
            client=client,
            periods=[period],
            financial_sheets=["Summary"],
        )
    assert "Deterministic proposals" not in client.prompt


def test_inspected_provider_final_object_still_passes_model_owned_boundary():
    period = _period()

    class Client:
        def generate_json_model_with_tools(
            self,
            prompt,
            response_model,
            *,
            toolset,
            **kwargs,
        ):
            del prompt, kwargs
            toolset.dispatch(
                "read_rows",
                {"sheet_name": "Summary", "start_row": 1, "end_row": 2},
            )
            return response_model.model_validate(
                {
                    "bindings": [
                        {
                            "period_id": period.period_id,
                            "sheet_name": "Summary",
                            "excel_column": "B",
                        }
                    ]
                }
            )

    output = bind_periods(
        _workbook(_sheet("Amount")),
        client=Client(),
        periods=[period],
        financial_sheets=["Summary"],
    )

    assert output.structure.bindings[0].excel_column == "B"
    assert output.reads == 1


def test_out_of_patience_salvage_drops_explicitly_wrong_binding():
    period = _period(PeriodScenario.BUDGET)
    toolset = PeriodBindingToolset(
        _workbook(_sheet("June 2025 YTD Actual")),
        period_ids=[period.period_id],
        periods=[period],
        financial_sheets=["Summary"],
    )
    toolset.dispatch(
        "read_rows",
        {"sheet_name": "Summary", "start_row": 1, "end_row": 2},
    )
    payload = {
        "bindings": [
            {
                "period_id": period.period_id,
                "sheet_name": "Summary",
                "excel_column": "B",
            }
        ]
    }

    assert not toolset.dispatch("submit_bindings", payload)["accepted"]
    assert not toolset.dispatch("submit_bindings", payload)["accepted"]
    salvaged = toolset.dispatch("submit_bindings", payload)

    assert salvaged["accepted"]
    assert salvaged["structure"]["bindings"] == []
    assert len(salvaged["structure"]["unavailable"]) == 1
