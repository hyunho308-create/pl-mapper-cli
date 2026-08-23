# Mapping Validation Feedback

Use this guidance only after a mapping validation attempt.

Return the existing raw errors and warnings. For each finding, also identify the
affected COA account or accounts in `coa_ids`. Include the implicated row keys in
`source_rows`, including rows cited by the affected accounts for structural
findings. Use an empty list only when no relevant cited rows can be identified.
Include definitions only for the distinct validation rules that failed or warned
on the current attempt. Do not add source-row packets, repeated workbook
evidence, or guidance for rules that did not fire.

Preserve the validator's numeric fields in `details`. For hierarchy, Summary to
department, and Summary equation findings, also state the comparison explicitly in
`validation_math` so the repair model knows the exact amount and direction of
the mismatch without having to reconstruct it from omitted context.

## Feedback Shape

```json
{
  "errors": ["existing raw errors"],
  "warnings": ["existing raw warnings"],
  "findings": [
    {
      "rule": "hierarchy_complete",
      "coa_ids": ["S1.labor_costs_and_related_expenses"],
      "source_rows": ["Sheet1!121", "Sheet1!144", "Sheet1!154"],
      "details": {"parent": "100.00", "children": "90.00", "variance": "10.00"},
      "validation_math": "children are 10.00 less than parent"
    },
    {
      "rule": "source_row_repeated",
      "coa_ids": ["S1.salaries_and_wages"],
      "source_rows": ["Sheet1!121"]
    }
  ],
  "rule_guidance": [
    {
      "rule": "hierarchy_complete",
      "description": "A parent marked complete does not equal the sum of its immediate COA children.",
      "resolution": "Review the full subtree for a misplaced child, omitted sibling, subtotal overlap, double counting, or a source hierarchy that does not match the COA. Do not force unsupported detail."
    }
  ],
  "instruction": "Repair only the implicated decisions."
}
```

## Repair Tracking

Every `patch_mapping` response must include:

```json
{
  "repair_hypothesis": "Supplemental Pay is assigned to the wrong payroll level, causing both hierarchy errors.",
  "expected_fix": "Moving the cited Supplemental Pay row should reconcile the labor and payroll-related expense parents."
}
```

Keep both fields to one concise sentence each. They should explain why the
proposed changes address the current findings, not restate every replacement.

After validating the patch, return a compact result with the next validator
response:

```json
{
  "repair_tracking": {
    "repair_hypothesis": "Supplemental Pay is assigned to the wrong payroll level, causing both hierarchy errors.",
    "expected_fix": "Moving the cited Supplemental Pay row should reconcile the labor and payroll-related expense parents.",
    "resolved_rules": ["hierarchy_complete"],
    "remaining_rules": [],
    "new_rules": []
  }
}
```

The conversation already contains prior repair attempts. Do not resend a
growing repair-history block or repeat source evidence. Persist the structured
repair fields in telemetry so later review can see what was tried and why.

## Rule Guidance

### `execution`

Description: The submitted mapping cannot be executed because a required
decision, source row, operation field, workbook ID, review citation, or venue
name is invalid.

Resolution: Correct the specific mechanical issue named in the error. Do not
change unrelated mappings.

### `coverage`

Description: A COA parent account is missing its required mapping decision.

Resolution: Add a supported mapping or `no_value` decision and state whether
child detail is complete, partial, or absent.

### `hierarchy_complete`

Description: A parent marked `complete` does not equal the sum of its immediate
COA children.

Resolution: Review the full subtree for a misplaced child, omitted sibling,
subtotal overlap, double counting, or a source hierarchy that does not match the
COA. Do not force unsupported detail. If children are ambiguous or there is a
mismatch in COA, it is preferred to leave a correct parent and set coverage to
partial.

### `hierarchy_partial_with_residual`

Description: A partial child mapping does not reconcile to its parent even
though the hierarchy has a residual account.

Resolution: First consider whether amounts belong in specific sibling accounts.
Keep the reconciled parent fixed, map every positively identifiable child, and
use the legitimate residual for remaining supported accounts. A plug of 5% or
more remains blocking during repair; never invent source detail.

### `coverage_inconsistent`

Description: A parent is marked as having no child detail, but one or more
children are mapped.

Resolution: Inspect the children accounts to make sure they belong. If they do
update the coverage to partial.

### `coverage_unspecified`

Description: A parent does not state whether its child detail is complete,
partial, or absent.

Resolution: Set coverage based on the source structure.

### `parent_no_value_with_children`

Description: Child accounts have values while their parent is marked `no_value`.

Resolution: Map the supported parent total or remove children that do not belong
beneath it.

### `summary_department`

Description: A Summary total does not match the independently mapped department
total.

Resolution: Validate the Summary revenue-to-EBITDA structure first. Look for
missing or duplicated schedules, summary-only lines, separately presented
expenses, or department expenses that must be derived from revenue less profit.

If the same mismatch remains after this structural repair and both source layers
are independently supported and disjoint, review every offset candidate supplied
in the validation feedback. Apply a candidate only when its label and surrounding
structure support the affected department. Otherwise preserve both reported
layers and add one `source_discrepancy` review item citing both COA IDs and all
rows from both layers. Explain briefly why the candidates do not resolve the
difference. Python will keep the finding blocking unless every other
qualification check passes.

### `source_discrepancy`

Description: Independently reported Summary and department values still differ
after a structural repair pass, and deterministic qualification checks passed.

Resolution: Preserve both reported source layers and present the computed
difference for human review. Do not alter either value merely to force agreement.

### `routed_department_not_present`

Description: Upstream routing found usable source coverage for a department, but
the department root was marked `no_value` with `not_present` child coverage.

Resolution: Inspect the routed primary, summary, and detail locations and map the
department root from the best supported total. Do not use `not_present` to bypass
a routed department.

### `summary_combined_ood_misc`

Description: The selected combined OOD and miscellaneous-income presentation
does not reconcile to the detailed schedules.

Resolution: Confirm whether Summary combines or separates OOD and miscellaneous
income, then check the two detailed sections for omissions or duplication.

### `summary_combined_ood_misc_inactive_bucket`

Description: The selected combined OOD/Misc presentation populated both Summary
buckets. A combined-in-OOD presentation requires Summary Misc to be zero, while a
combined-in-Misc presentation requires Summary OOD to be zero.

Resolution: Put the combined amount in only the selected Summary bucket. Preserve
the separate S3 OOD and S4 Misc department totals for the combined reconciliation.

### `summary_math`

Description: A Summary subtotal does not satisfy its required
revenue-to-EBITDA equation.

Resolution: Review the complete Summary structure for missing lines, duplicated
lines, sign errors, or items presented separately below GOP.

### `kpi_math`

Description: A reported KPI does not match the deterministic calculation from
its mapped inputs.

Resolution: Check the KPI's units and the mapped rooms or revenue inputs. Use
the supported source convention.

### `non_operating_sign`

Description: Non-operating income has the wrong normalized sign.

Resolution: Confirm whether the source presents income as a positive offset and
negate it when required by the COA convention.

### `source_row_repeated`

Description: The same source row appears more than once within one account
calculation.

Resolution: Remove the duplicate occurrence from that account.

### `source_row_included_and_excluded`

Description: One account both includes and excludes the same source row.

Resolution: Keep the row only on the side required by the selected operation.

### `source_row_double_count`

Description: The same source row is assigned to two unrelated COA accounts.

Resolution: Choose the correct account or replace an overlapping subtotal with
non-overlapping detail. Related parent/child and standard derived reuse is
allowed. Before deriving an expense as revenue less profit, use a directly
reported expense total when one exists. For a rare nonstandard adjustment, use
one connected path, an actual subtotal adjustment, and an `unusual_convention`
review item citing the row and every affected account.

### `small_source_reconciliation_difference`

Description: Directly reported parent and child values differ slightly even
though both are supported by the source.

Resolution: Preserve the reported source values and leave the difference for
review. Do not create a plug merely to eliminate a small source inconsistency.

### `source_detail_incomplete`

Description: The source supports the parent and some children but lacks enough
detail to complete the COA hierarchy.

Resolution: Preserve the reconciled parent as an anchor and map every positively
identifiable child without changing it. Do not clear supported children merely
because coverage is incomplete. Use `not_present` only when the source contains
no usable evidence for this child hierarchy. After validation has no blocking
errors, use one focused enrichment pass for all such parents, then finish with
the warnings preserved rather than guessing or repeatedly revisiting unavailable
detail.

### `unresolved_negative_residual`

Description: Final presentation found a material negative remainder that Python
did not force into a residual account.

Resolution: Check duplicated children, an offset already netted into a subtotal,
a missed allocation or credit, and source sign conventions. Preserve supported
values and leave the warning when the negative remainder cannot be resolved.

### `ood_misc_summary_mode_unknown`

Description: The mapping does not state whether Summary combines or separates
OOD and miscellaneous income.

Resolution: Inspect the Summary presentation and select the matching combined
or separate treatment.
