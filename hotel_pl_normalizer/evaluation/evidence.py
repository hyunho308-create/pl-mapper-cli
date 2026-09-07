"""Source evidence lookup for one version-5 normalization run log.

The run log already contains the exact rows shown to the mapper.  This module
indexes those rows without re-extracting or reinterpreting the source P&L, so
the evaluator can give a judge the cited rows plus a small amount of nearby
Excel- or PDF-specific context.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

from hotel_pl_normalizer.models.evidence import (
    EvidenceRow,
    ExcelRowLocator,
    PdfLineLocator,
    ensure_evidence_row,
)


class EvidenceIndexError(ValueError):
    """A run log cannot be indexed without guessing about its evidence."""


@dataclass(frozen=True, slots=True)
class AccountEvidence:
    """One account decision joined to its exact run-log evidence rows."""

    coa_id: str
    account_name: str | None
    department: str | None
    operation: str | None
    computed_values: Mapping[str, Any]
    source_row_keys: tuple[str, ...]
    excluded_row_keys: tuple[str, ...]
    source_rows: tuple[EvidenceRow, ...]
    excluded_rows: tuple[EvidenceRow, ...]
    missing_source_row_keys: tuple[str, ...]
    missing_excluded_row_keys: tuple[str, ...]
    payload: Mapping[str, Any] = field(repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class EvidenceIndex:
    """Typed evidence and account lookups for one P&L run.

    ``rows`` always retains the order of ``run_log["evidence_rows"]``.  Nearby
    context is selected from that same order and never crosses an Excel sheet
    or a PDF page.
    """

    rows: tuple[EvidenceRow, ...]
    accounts: tuple[AccountEvidence, ...]
    _rows_by_key: Mapping[str, EvidenceRow] = field(repr=False, compare=False)
    _accounts_by_id: Mapping[str, AccountEvidence] = field(
        repr=False,
        compare=False,
    )
    _scope_row_keys: Mapping[tuple[str, str | int], tuple[str, ...]] = field(
        repr=False,
        compare=False,
    )

    @classmethod
    def from_run_log(cls, run_log: Mapping[str, Any]) -> EvidenceIndex:
        """Build an index from the typed evidence contract introduced in v5."""

        if not isinstance(run_log, Mapping):
            raise EvidenceIndexError("run log must be an object")
        if run_log.get("log_version") != 5:
            raise EvidenceIndexError("evidence evaluation requires run-log version 5")

        raw_rows = run_log.get("evidence_rows")
        if not _is_object_sequence(raw_rows):
            raise EvidenceIndexError("run log evidence_rows must be a list")

        rows: list[EvidenceRow] = []
        rows_by_key: dict[str, EvidenceRow] = {}
        scope_row_keys: dict[tuple[str, str | int], list[str]] = {}
        for position, raw_row in enumerate(raw_rows):
            if not isinstance(raw_row, Mapping):
                raise EvidenceIndexError(
                    f"evidence_rows[{position}] must be an object"
                )
            declared_key = str(raw_row.get("row_key") or "")
            try:
                row = ensure_evidence_row(raw_row)
            except (KeyError, TypeError, ValueError) as exc:
                raise EvidenceIndexError(
                    f"invalid evidence_rows[{position}]: {exc}"
                ) from exc
            if not declared_key:
                raise EvidenceIndexError(
                    f"evidence_rows[{position}] has no row_key"
                )
            if declared_key != row.row_key:
                raise EvidenceIndexError(
                    f"evidence row {declared_key!r} disagrees with its typed locator "
                    f"{row.row_key!r}"
                )
            if row.row_key in rows_by_key:
                raise EvidenceIndexError(
                    f"duplicate evidence row_key {row.row_key!r}"
                )
            _validate_anchor_kinds(row)
            rows.append(row)
            rows_by_key[row.row_key] = row
            scope_row_keys.setdefault(_scope_key(row), []).append(row.row_key)

        raw_accounts = run_log.get("accounts")
        if not _is_object_sequence(raw_accounts):
            raise EvidenceIndexError("run log accounts must be a list")

        accounts: list[AccountEvidence] = []
        accounts_by_id: dict[str, AccountEvidence] = {}
        for position, raw_account in enumerate(raw_accounts):
            if not isinstance(raw_account, Mapping):
                raise EvidenceIndexError(
                    f"accounts[{position}] must be an object"
                )
            coa_id = str(raw_account.get("coa_id") or "").strip()
            if not coa_id:
                raise EvidenceIndexError(f"accounts[{position}] has no coa_id")
            if coa_id in accounts_by_id:
                raise EvidenceIndexError(f"duplicate account coa_id {coa_id!r}")

            source_keys = _account_row_keys(raw_account, "source_rows", position)
            excluded_keys = _account_row_keys(raw_account, "excluded_rows", position)
            source_rows = tuple(
                rows_by_key[key] for key in source_keys if key in rows_by_key
            )
            excluded_rows = tuple(
                rows_by_key[key] for key in excluded_keys if key in rows_by_key
            )
            raw_computed = raw_account.get("computed_values")
            computed_values = (
                dict(raw_computed)
                if isinstance(raw_computed, Mapping)
                else {"selected": raw_account.get("computed_value")}
            )
            account = AccountEvidence(
                coa_id=coa_id,
                account_name=_optional_text(raw_account.get("account_name")),
                department=_optional_text(raw_account.get("department")),
                operation=_optional_text(raw_account.get("operation")),
                computed_values=computed_values,
                source_row_keys=source_keys,
                excluded_row_keys=excluded_keys,
                source_rows=source_rows,
                excluded_rows=excluded_rows,
                missing_source_row_keys=tuple(
                    key for key in source_keys if key not in rows_by_key
                ),
                missing_excluded_row_keys=tuple(
                    key for key in excluded_keys if key not in rows_by_key
                ),
                payload=deepcopy(dict(raw_account)),
            )
            accounts.append(account)
            accounts_by_id[coa_id] = account

        return cls(
            rows=tuple(rows),
            accounts=tuple(accounts),
            _rows_by_key=rows_by_key,
            _accounts_by_id=accounts_by_id,
            _scope_row_keys={
                scope: tuple(keys) for scope, keys in scope_row_keys.items()
            },
        )

    def row(self, row_key: str) -> EvidenceRow | None:
        """Return an exact evidence row, or ``None`` when it is not indexed."""

        return self._rows_by_key.get(str(row_key))

    def account(self, coa_id: str) -> AccountEvidence | None:
        """Return the account evidence record for ``coa_id`` when present."""

        return self._accounts_by_id.get(str(coa_id))

    def nearby(self, row_key: str, radius: int = 2) -> tuple[EvidenceRow, ...]:
        """Return up to ``radius`` logged rows on either side of one row."""

        if radius < 0:
            raise ValueError("radius cannot be negative")
        row = self.row(row_key)
        if row is None:
            return ()
        keys = self._scope_row_keys[_scope_key(row)]
        position = keys.index(row.row_key)
        selected = keys[max(0, position - radius) : position + radius + 1]
        return tuple(self._rows_by_key[key] for key in selected)

    def rows_for_account(self, coa_id: str) -> tuple[EvidenceRow, ...]:
        """Return only the account's exact included rows, in citation order."""

        account = self.account(coa_id)
        return account.source_rows if account is not None else ()

    def missing_row_keys_for_account(self, coa_id: str) -> tuple[str, ...]:
        """Return unresolved included and excluded citations without duplicates."""

        account = self.account(coa_id)
        if account is None:
            return ()
        return tuple(
            dict.fromkeys(
                account.missing_source_row_keys
                + account.missing_excluded_row_keys
            )
        )

    def context_for_account(
        self,
        coa_id: str,
        radius: int = 2,
    ) -> tuple[EvidenceRow, ...]:
        """Return cited included/excluded rows plus same-scope nearby context.

        The result is de-duplicated in original run-log evidence order, which
        keeps prompt construction deterministic.
        """

        if radius < 0:
            raise ValueError("radius cannot be negative")
        account = self.account(coa_id)
        if account is None:
            return ()
        selected_keys: set[str] = set()
        for row in account.source_rows + account.excluded_rows:
            selected_keys.update(item.row_key for item in self.nearby(row.row_key, radius))
        return tuple(row for row in self.rows if row.row_key in selected_keys)


def build_evidence_index(run_log: Mapping[str, Any]) -> EvidenceIndex:
    """Functional entry point used by evaluator runners and judge modules."""

    return EvidenceIndex.from_run_log(run_log)


def _account_row_keys(
    account: Mapping[str, Any],
    field_name: str,
    account_position: int,
) -> tuple[str, ...]:
    raw_refs = account.get(field_name)
    if not _is_object_sequence(raw_refs):
        raise EvidenceIndexError(
            f"accounts[{account_position}].{field_name} must be a list"
        )
    keys: list[str] = []
    for position, raw_ref in enumerate(raw_refs):
        if not isinstance(raw_ref, Mapping):
            raise EvidenceIndexError(
                f"accounts[{account_position}].{field_name}[{position}] "
                "must be an object"
            )
        key = str(raw_ref.get("row_key") or "")
        if not key:
            raise EvidenceIndexError(
                f"accounts[{account_position}].{field_name}[{position}] "
                "has no row_key"
            )
        keys.append(key)
    return tuple(keys)


def _validate_anchor_kinds(row: EvidenceRow) -> None:
    expected = row.locator.kind
    for period_id, anchor in row.anchors_by_period.items():
        if anchor is not None and anchor.kind != expected:
            raise EvidenceIndexError(
                f"evidence row {row.row_key!r} has a {anchor.kind.value} anchor "
                f"for {period_id!r} but a {expected.value} locator"
            )


def _scope_key(row: EvidenceRow) -> tuple[str, str | int]:
    locator = row.locator
    if isinstance(locator, ExcelRowLocator):
        return ("excel", locator.sheet_name)
    if isinstance(locator, PdfLineLocator):
        return ("pdf", locator.page_number)
    raise EvidenceIndexError(
        f"unsupported evidence locator type {type(locator).__name__}"
    )


def _is_object_sequence(value: Any) -> bool:
    return isinstance(value, Sequence) and not isinstance(
        value,
        (str, bytes, bytearray),
    )


def _optional_text(value: Any) -> str | None:
    return None if value is None else str(value)


__all__ = [
    "AccountEvidence",
    "EvidenceIndex",
    "EvidenceIndexError",
    "build_evidence_index",
]
