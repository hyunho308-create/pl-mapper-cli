"""Canonical COA metadata and controlled accounting relationships."""

from __future__ import annotations

import csv
import math
from collections import deque
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources
from types import MappingProxyType
from typing import Iterable, Mapping

COA_FIELDS = (
    "department",
    "coa_id",
    "account_name",
    "hierarchy_path",
    "parent_coa_id",
    "is_residual",
    "mapping_note",
    "synonyms",
)
ACCOUNTING_EQUATION_FIELDS = (
    "relationship_kind",
    "target_coa_id",
    "source_coa_id",
    "coefficient",
    "term_order",
    "mapped_label",
)
RELATIONSHIP_KINDS = (
    "reported_summary_link",
    "derived_summary_link",
    "summary_equation",
    "deterministic_calculation",
)
class AccountingEquationError(ValueError):
    """The controlled accounting-equation resource is invalid."""


@dataclass(frozen=True, slots=True)
class AccountingEquationTerm:
    relationship_kind: str
    target_coa_id: str
    source_coa_id: str
    coefficient: int | float
    term_order: int
    mapped_label: str


@lru_cache(maxsize=1)
def _cached_coa_rows() -> tuple[tuple[tuple[str, str], ...], ...]:
    source = resources.files("hotel_pl_normalizer.data").joinpath("coa_v2.csv")
    rows: list[tuple[tuple[str, str], ...]] = []
    seen: set[str] = set()
    with source.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if tuple(reader.fieldnames or ()) != COA_FIELDS:
            raise ValueError(
                "coa_v2.csv columns must be " + ",".join(COA_FIELDS)
            )
        for raw in reader:
            coa_id = str(raw.get("coa_id") or "").strip()
            if not coa_id:
                raise ValueError("coa_v2.csv contains a blank coa_id")
            if coa_id in seen:
                raise ValueError(f"coa_v2.csv contains duplicate coa_id {coa_id!r}")
            seen.add(coa_id)
            normalized = {
                field: str(raw.get(field) or "") for field in COA_FIELDS
            }
            normalized["is_residual"] = (
                normalized["is_residual"].strip().lower() or "false"
            )
            rows.append(tuple(normalized.items()))
    known = {dict(row)["coa_id"] for row in rows}
    for row in rows:
        item = dict(row)
        parent = item["parent_coa_id"]
        if parent and parent not in known:
            raise ValueError(
                f"coa_v2.csv account {item['coa_id']!r} has unknown parent {parent!r}"
            )
    return tuple(rows)


def load_coa() -> dict[str, dict[str, str]]:
    """Return COA rows in CSV order without sharing mutable row dictionaries."""

    return {
        item["coa_id"]: item
        for frozen in _cached_coa_rows()
        for item in [dict(frozen)]
    }


def canonical_coa_ids() -> list[str]:
    """Return canonical IDs in the row order used by the output template."""

    return [dict(row)["coa_id"] for row in _cached_coa_rows()]


def coa_depth(
    coa_id: str,
    coa: Mapping[str, Mapping[str, str]] | None = None,
) -> int:
    """Return an account's hierarchy depth, stopping safely at a cycle."""

    metadata = coa if coa is not None else load_coa()
    depth = 0
    seen = {coa_id}
    parent = str(metadata.get(coa_id, {}).get("parent_coa_id") or "")
    while parent and parent in metadata and parent not in seen:
        seen.add(parent)
        depth += 1
        parent = str(metadata[parent].get("parent_coa_id") or "")
    return depth


def children_by_parent(
    coa: Mapping[str, Mapping[str, str]] | None = None,
) -> dict[str, list[str]]:
    """Return child IDs in canonical input order for every populated parent."""

    metadata = coa if coa is not None else load_coa()
    children: dict[str, list[str]] = {}
    for coa_id, item in metadata.items():
        parent = str(item.get("parent_coa_id") or "")
        if parent:
            children.setdefault(parent, []).append(coa_id)
    return children


def _coefficient(value: str, *, row_number: int) -> int | float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise AccountingEquationError(
            f"accounting_equations.csv row {row_number} has invalid coefficient {value!r}"
        ) from exc
    if not math.isfinite(parsed) or parsed == 0:
        raise AccountingEquationError(
            f"accounting_equations.csv row {row_number} coefficient must be finite and nonzero"
        )
    return int(parsed) if parsed.is_integer() else parsed


def _parse_accounting_equations(
    rows: Iterable[Mapping[str, str]],
    *,
    known_coa_ids: set[str],
) -> tuple[AccountingEquationTerm, ...]:
    terms: list[AccountingEquationTerm] = []
    keys: set[tuple[str, str, int]] = set()
    source_keys: set[tuple[str, str, str]] = set()
    counts = dict.fromkeys(RELATIONSHIP_KINDS, 0)
    labels: dict[str, str] = {}
    for row_number, raw in enumerate(rows, start=2):
        kind = str(raw.get("relationship_kind") or "").strip()
        target = str(raw.get("target_coa_id") or "").strip()
        source = str(raw.get("source_coa_id") or "").strip()
        label = str(raw.get("mapped_label") or "").strip()
        if kind not in RELATIONSHIP_KINDS:
            raise AccountingEquationError(
                f"accounting_equations.csv row {row_number} has unknown relationship_kind {kind!r}"
            )
        if target not in known_coa_ids or source not in known_coa_ids:
            raise AccountingEquationError(
                f"accounting_equations.csv row {row_number} cites an unknown COA id"
            )
        try:
            order = int(str(raw.get("term_order") or ""))
        except ValueError as exc:
            raise AccountingEquationError(
                f"accounting_equations.csv row {row_number} has invalid term_order"
            ) from exc
        if order < 1:
            raise AccountingEquationError(
                f"accounting_equations.csv row {row_number} term_order must be positive"
            )
        key = (kind, target, order)
        if key in keys:
            raise AccountingEquationError(
                "accounting_equations.csv contains duplicate relationship target/order "
                f"{key!r}"
            )
        keys.add(key)
        source_key = (kind, target, source)
        if source_key in source_keys:
            raise AccountingEquationError(
                "accounting_equations.csv contains duplicate relationship term "
                f"{source_key!r}"
            )
        source_keys.add(source_key)
        coefficient = _coefficient(
            str(raw.get("coefficient") or ""), row_number=row_number
        )
        if kind in {"reported_summary_link", "derived_summary_link"}:
            if order != 1 or coefficient != 1:
                raise AccountingEquationError(
                    f"accounting_equations.csv link row {row_number} must be one +1 term"
                )
        if kind == "deterministic_calculation":
            prior = labels.setdefault(target, label)
            if not label:
                raise AccountingEquationError(
                    f"accounting_equations.csv deterministic row {row_number} needs mapped_label"
                )
            if prior != label:
                raise AccountingEquationError(
                    f"accounting_equations.csv has conflicting mapped labels for {target}"
                )
        elif label:
            raise AccountingEquationError(
                f"accounting_equations.csv row {row_number} has an unexpected mapped_label"
            )
        terms.append(
            AccountingEquationTerm(
                relationship_kind=kind,
                target_coa_id=target,
                source_coa_id=source,
                coefficient=coefficient,
                term_order=order,
                mapped_label=label,
            )
        )
        counts[kind] += 1

    missing_kinds = [kind for kind in RELATIONSHIP_KINDS if not counts[kind]]
    if missing_kinds:
        raise AccountingEquationError(
            "accounting_equations.csv is missing relationship kinds: "
            + ", ".join(missing_kinds)
        )
    grouped: dict[tuple[str, str], list[AccountingEquationTerm]] = {}
    for term in terms:
        grouped.setdefault(
            (term.relationship_kind, term.target_coa_id), []
        ).append(term)
    for (kind, target), grouped_terms in grouped.items():
        orders = sorted(term.term_order for term in grouped_terms)
        if orders != list(range(1, len(grouped_terms) + 1)):
            raise AccountingEquationError(
                f"accounting_equations.csv {kind} {target} term_order is not contiguous"
            )
        if kind in {"reported_summary_link", "derived_summary_link"} and len(
            grouped_terms
        ) != 1:
            raise AccountingEquationError(
                f"accounting_equations.csv {kind} {target} must have one term"
            )
        if not grouped_terms:
            raise AccountingEquationError(
                f"accounting_equations.csv {kind} {target} has no terms"
            )
    _assert_acyclic(terms)
    return tuple(terms)


def _assert_acyclic(terms: Iterable[AccountingEquationTerm]) -> None:
    dependencies: dict[str, set[str]] = {}
    for term in terms:
        dependencies.setdefault(term.target_coa_id, set()).add(term.source_coa_id)
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(target: str) -> None:
        if target in visiting:
            raise AccountingEquationError(
                f"accounting_equations.csv contains a dependency cycle at {target}"
            )
        if target in visited:
            return
        visiting.add(target)
        for source in dependencies.get(target, ()):
            visit(source)
        visiting.remove(target)
        visited.add(target)

    for target in dependencies:
        visit(target)


@lru_cache(maxsize=1)
def load_accounting_equations() -> tuple[AccountingEquationTerm, ...]:
    """Load and validate the complete ordered accounting relationship table."""

    source = resources.files("hotel_pl_normalizer.data").joinpath(
        "accounting_equations.csv"
    )
    with source.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if tuple(reader.fieldnames or ()) != ACCOUNTING_EQUATION_FIELDS:
            raise AccountingEquationError(
                "accounting_equations.csv columns must be "
                + ",".join(ACCOUNTING_EQUATION_FIELDS)
            )
        return _parse_accounting_equations(
            reader,
            known_coa_ids=set(canonical_coa_ids()),
        )


def _terms_by_target(kind: str) -> dict[str, list[AccountingEquationTerm]]:
    grouped: dict[str, list[AccountingEquationTerm]] = {}
    for term in load_accounting_equations():
        if term.relationship_kind == kind:
            grouped.setdefault(term.target_coa_id, []).append(term)
    for terms in grouped.values():
        terms.sort(key=lambda item: item.term_order)
    return grouped


def _formula(terms: list[AccountingEquationTerm]) -> str:
    rendered: list[str] = []
    for index, term in enumerate(terms):
        coefficient = term.coefficient
        absolute = abs(coefficient)
        amount = str(absolute)
        body = (
            term.source_coa_id
            if absolute == 1
            else f"{amount} * {term.source_coa_id}"
        )
        if index == 0:
            rendered.append(body if coefficient > 0 else f"-{body}")
        else:
            rendered.append(f" {'+' if coefficient > 0 else '-'} {body}")
    return "".join(rendered)


def _build_relationships():
    reported_terms = _terms_by_target("reported_summary_link")
    derived_terms = _terms_by_target("derived_summary_link")
    summary_terms = _terms_by_target("summary_equation")
    deterministic_terms = _terms_by_target("deterministic_calculation")
    summary_links = {
        target: terms[0].source_coa_id for target, terms in reported_terms.items()
    }
    derived_summary_links = {
        **summary_links,
        **{
            target: terms[0].source_coa_id
            for target, terms in derived_terms.items()
        },
    }
    summary_equations = {
        target: [(term.coefficient, term.source_coa_id) for term in terms]
        for target, terms in summary_terms.items()
    }
    deterministic = {
        target: {
            "formula": _formula(terms),
            "mapped_label": terms[0].mapped_label,
            "dependencies": tuple(term.source_coa_id for term in terms),
        }
        for target, terms in deterministic_terms.items()
    }
    return summary_links, derived_summary_links, summary_equations, deterministic


(
    SUMMARY_LINKS,
    DERIVED_SUMMARY_LINKS,
    SUMMARY_EQUATIONS,
    DETERMINISTIC_SUMMARY_CALCULATIONS,
) = _build_relationships()
DETERMINISTIC_SUMMARY_ACCOUNTS = frozenset(DETERMINISTIC_SUMMARY_CALCULATIONS)
DETERMINISTIC_CALCULATION_TERMS = {
    target: [(term.coefficient, term.source_coa_id) for term in terms]
    for target, terms in _terms_by_target("deterministic_calculation").items()
}


def _hierarchy_signature(
    coa: Mapping[str, Mapping[str, str]],
) -> tuple[tuple[str, str], ...]:
    return tuple(
        (coa_id, str(item.get("parent_coa_id") or ""))
        for coa_id, item in coa.items()
    )


@lru_cache(maxsize=8)
def _cached_signed_dependency_graph(
    hierarchy: tuple[tuple[str, str], ...],
) -> Mapping[str, tuple[tuple[str, int | float], ...]]:
    edges: dict[str, list[tuple[str, int | float]]] = {}
    for coa_id, parent in hierarchy:
        if parent:
            edges.setdefault(coa_id, []).append((parent, 1))
    for target, source in DERIVED_SUMMARY_LINKS.items():
        edges.setdefault(source, []).append((target, 1))
    for target, terms in SUMMARY_EQUATIONS.items():
        for coefficient, source in terms:
            edges.setdefault(source, []).append((target, coefficient))
    return MappingProxyType(
        {source: tuple(targets) for source, targets in edges.items()}
    )


def _dependency_graph(
    coa: Mapping[str, Mapping[str, str]] | None,
) -> Mapping[str, tuple[tuple[str, int | float], ...]]:
    metadata = coa if coa is not None else load_coa()
    return _cached_signed_dependency_graph(_hierarchy_signature(metadata))


def signed_dependency_graph(
    coa: Mapping[str, Mapping[str, str]] | None = None,
) -> dict[str, list[tuple[str, int | float]]]:
    """Return isolated child/component-to-parent accounting edges."""

    return {
        source: list(targets)
        for source, targets in _dependency_graph(coa).items()
    }


def dependency_coefficient(
    source: str | None,
    target: str | None,
    coa: Mapping[str, Mapping[str, str]] | None = None,
) -> int | float | None:
    """Return the signed coefficient along a known dependency path."""

    if not source or not target:
        return None
    if source == target:
        return 1
    queue = deque([(source, 1, 0)])
    seen: set[tuple[str, int | float]] = {(source, 1)}
    edges = _dependency_graph(coa)
    while queue:
        current, coefficient, depth = queue.popleft()
        if depth >= 10:
            continue
        for next_target, sign in edges.get(current, []):
            next_coefficient = coefficient * sign
            if next_target == target:
                return next_coefficient
            state = (next_target, next_coefficient)
            if state not in seen:
                seen.add(state)
                queue.append((next_target, next_coefficient, depth + 1))
    return None


def accounts_share_dependency_path(
    left: str,
    right: str,
    coa: Mapping[str, Mapping[str, str]],
) -> bool:
    """Return whether either account can feed the other through known edges."""

    edges = _dependency_graph(coa)

    def reaches(source: str, target: str) -> bool:
        pending = [source]
        visited = {source}
        while pending:
            current = pending.pop()
            for candidate, _coefficient in edges.get(current, ()):
                if candidate == target:
                    return True
                if candidate not in visited:
                    visited.add(candidate)
                    pending.append(candidate)
        return False

    return reaches(left, right) or reaches(right, left)
