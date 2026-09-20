"""Batch data-plan search: global missing-field sets covering many cases.

A batch request carries multiple cases, each with its own ``case_id`` and
``facts``.  Every case is adjudicated independently with the existing
priority / three-valued engine.  Cases already decidable (``decided``,
``conflict`` or ``no_match``) need nothing; only ``unknown`` cases contribute
their per-case ``minimal_missing_field_sets``.

A *data plan* is one inclusion-minimal global set of fields that, once
supplied, makes **every** unknown case decidable: it must contain at least one
per-case minimal set for each unknown case.  Enumerating those plans amounts
to the cross-product of the per-case minimal-set families followed by field
union, deduplication and strict-superset pruning.  Each returned plan also
lists, per unknown case, the minimal sets of that case which the plan covers.

The cross-product expansion is capped: when the accumulated number of
generated combinations exceeds the configurable limit the search aborts with
:class:`BatchDataPlanError` so results are never truncated.

Results are sorted stably: plans by field count then by field names, and the
per-case coverage lists by ``case_id`` (field sets are already sorted by the
engine).
"""
from __future__ import annotations

import os
from typing import Optional

from .engine import adjudicate

# Default safety cap on cross-product combinations generated while combining
# per-case minimal sets; overridable through the environment.
DEFAULT_MAX_BATCH_COMBINATIONS = 100_000
MAX_BATCH_COMBINATIONS_ENV = "MAX_BATCH_DATA_PLAN_COMBINATIONS"


class BatchDataPlanError(ValueError):
    """Raised when the global combination space exceeds the configured cap."""


def combination_cap() -> int:
    """Resolve the batch combination cap from the environment."""
    raw = os.environ.get(MAX_BATCH_COMBINATIONS_ENV)
    if raw is None:
        return DEFAULT_MAX_BATCH_COMBINATIONS
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_MAX_BATCH_COMBINATIONS
    if value < 1:
        return DEFAULT_MAX_BATCH_COMBINATIONS
    return value


def _minimize(family: set[frozenset[str]]) -> list[frozenset[str]]:
    """Deduplicate and drop every strict superset; stable order."""
    minimal: list[frozenset[str]] = []
    for field_set in sorted(family, key=lambda s: (len(s), tuple(sorted(s)))):
        if not any(kept < field_set for kept in minimal):
            minimal.append(field_set)
    return minimal


def _combine(
    families: list[list[frozenset[str]]], cap: int
) -> list[frozenset[str]]:
    """Cross-product of per-case minimal sets, unioned and pruned at each step.

    Deduplication and strict-superset pruning run after every merged case so
    the carried family stays small.  ``cap`` bounds the total number of raw
    unions materialized; exceeding it raises :class:`BatchDataPlanError`.
    """
    minimal: list[frozenset[str]] = [frozenset()]
    generated = 0
    for family in families:
        options = [frozenset(option) for option in family]
        next_sets: set[frozenset[str]] = set()
        for carried in minimal:
            for option in options:
                next_sets.add(carried | option)
                generated += 1
                if generated > cap:
                    raise BatchDataPlanError(
                        "batch data-plan combination space exceeds "
                        f"{cap} generated combinations; reduce the number of "
                        "unknown cases or narrow their minimal missing field "
                        "sets (raise "
                        f"{MAX_BATCH_COMBINATIONS_ENV} to search further)"
                    )
        minimal = _minimize(next_sets)
    return minimal


def batch_data_plans(
    policy: dict,
    cases: list[tuple[str, dict]],
    cap: Optional[int] = None,
) -> dict:
    """Adjudicate every case and build the global inclusion-minimal plans.

    ``cases`` is the validated ``[(case_id, facts), ...]`` list in request
    order.  Returns a dict with per-case verdicts and the sorted ``plans``;
    when no case is unknown a single empty plan is returned.
    """
    if cap is None:
        cap = combination_cap()

    case_reports: list[dict] = []
    unknown_cases: list[tuple[str, list[frozenset[str]]]] = []

    for case_id, facts in cases:
        verdict = adjudicate(policy, facts)
        report = {
            "case_id": case_id,
            "status": verdict["status"],
            "conclusion": verdict.get("conclusion"),
            "decided_priority": verdict.get("decided_priority"),
        }
        if verdict["status"] == "unknown":
            minimal_sets = [
                frozenset(field_set)
                for field_set in verdict["minimal_missing_field_sets"]
            ]
            report["minimal_missing_field_sets"] = [
                sorted(field_set) for field_set in minimal_sets
            ]
            unknown_cases.append((case_id, minimal_sets))
        case_reports.append(report)

    if not unknown_cases:
        return {
            "cases": case_reports,
            "unknown_case_ids": [],
            "plans": [{"fields": [], "covered": []}],
        }

    families = [family for _, family in unknown_cases]
    global_sets = _combine(families, cap)

    unknown_by_id = {
        case_id: family for case_id, family in unknown_cases
    }
    unknown_ids_sorted = sorted(unknown_by_id)

    plans: list[dict] = []
    for global_set in global_sets:
        covered = []
        for case_id in unknown_ids_sorted:
            covered_sets = [
                sorted(option)
                for option in unknown_by_id[case_id]
                if option <= global_set
            ]
            covered.append(
                {"case_id": case_id, "minimal_missing_field_sets": covered_sets}
            )
        plans.append({"fields": sorted(global_set), "covered": covered})

    plans.sort(
        key=lambda plan: (
            len(plan["fields"]),
            tuple(plan["fields"]),
            tuple(item["case_id"] for item in plan["covered"]),
        )
    )

    return {
        "cases": case_reports,
        "unknown_case_ids": unknown_ids_sorted,
        "plans": plans,
    }
