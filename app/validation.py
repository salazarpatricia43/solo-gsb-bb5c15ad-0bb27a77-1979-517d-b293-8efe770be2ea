"""Structural validation for submitted policies.

Validation is deliberately strict: unknown keys, wrong types or recursive
expressions deeper than :data:`MAX_EXPR_DEPTH` are rejected with a list of
human-readable errors (one error per detected problem where practical).
"""
from __future__ import annotations

from collections import Counter
from typing import Any

from .logic import COMPARISONS

MAX_EXPR_DEPTH = 64
MAX_RULES = 1000
MAX_PRIORITY = 10**9
MAX_ID_LENGTH = 200
MAX_CONCLUSION_LENGTH = 500
MAX_MAX_CHANGES = 100


class PolicyValidationError(ValueError):
    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("; ".join(errors))


def validate_policy(payload: Any) -> list[dict]:
    """Validate a policy payload. Returns the normalized ``rules`` list."""
    errors: list[str] = []

    if not isinstance(payload, dict):
        raise PolicyValidationError(["policy body must be an object"])

    rules = payload.get("rules")
    if not isinstance(rules, list) or not rules:
        errors.append("rules: must be a non-empty array")
        raise PolicyValidationError(errors)
    if len(rules) > MAX_RULES:
        errors.append(f"rules: at most {MAX_RULES} rules are allowed")

    seen_ids: set[str] = set()
    normalized: list[dict] = []

    # Reserve every effective id (explicit or default) before validating so an
    # explicit id cannot shadow an auto-generated one.
    effective_ids = [
        rule.get("id", f"rule-{index + 1}")
        if isinstance(rule, dict) else None
        for index, rule in enumerate(rules)
    ]
    id_counts = Counter(i for i in effective_ids if isinstance(i, str))

    for index, rule in enumerate(rules):
        prefix = f"rules[{index}]"
        if not isinstance(rule, dict):
            errors.append(f"{prefix}: must be an object")
            continue

        rule_id = effective_ids[index]
        if not isinstance(rule_id, str) or not rule_id.strip():
            errors.append(f"{prefix}.id: must be a non-empty string")
        elif len(rule_id) > MAX_ID_LENGTH:
            errors.append(f"{prefix}.id: must be at most {MAX_ID_LENGTH} characters")
        elif id_counts[rule_id] > 1:
            errors.append(f"{prefix}.id: duplicate rule id {rule_id!r}")
        else:
            seen_ids.add(rule_id)

        priority = rule.get("priority")
        if isinstance(priority, bool) or not isinstance(priority, int):
            errors.append(f"{prefix}.priority: must be an integer")
        elif not -MAX_PRIORITY <= priority <= MAX_PRIORITY:
            errors.append(
                f"{prefix}.priority: must be between -{MAX_PRIORITY} and "
                f"{MAX_PRIORITY}"
            )

        conclusion = rule.get("conclusion")
        if not isinstance(conclusion, str) or not conclusion.strip():
            errors.append(f"{prefix}.conclusion: must be a non-empty string")
        elif len(conclusion) > MAX_CONCLUSION_LENGTH:
            errors.append(
                f"{prefix}.conclusion: must be at most "
                f"{MAX_CONCLUSION_LENGTH} characters"
            )

        if "expr" not in rule:
            errors.append(f"{prefix}.expr: expression is required")
        else:
            errors.extend(_validate_expr(rule["expr"], f"{prefix}.expr", 1))

        normalized_rule = dict(rule)
        if "id" not in normalized_rule and isinstance(rule_id, str) and rule_id:
            normalized_rule["id"] = rule_id
        normalized.append(normalized_rule)

    if errors:
        raise PolicyValidationError(errors)
    return normalized


def _validate_expr(node: Any, path: str, depth: int) -> list[str]:
    errors: list[str] = []
    if depth > MAX_EXPR_DEPTH:
        return [f"{path}: expression nesting exceeds {MAX_EXPR_DEPTH} levels"]
    if not isinstance(node, dict):
        return [f"{path}: expression node must be an object"]

    keys = set(node)
    connective_keys = keys & {"all", "any"}
    if "not" in keys:
        if connective_keys or "field" in keys:
            errors.append(f"{path}: 'not' cannot combine with other operators")
        if set(node) != {"not"}:
            unknown = set(node) - {"not"}
            errors.append(f"{path}: unknown keys {sorted(unknown)}")
        if "not" in node:
            errors.extend(_validate_expr(node["not"], f"{path}.not", depth + 1))
        return errors

    if connective_keys:
        if len(connective_keys) > 1:
            errors.append(f"{path}: 'all' and 'any' are mutually exclusive")
        if "field" in keys:
            errors.append(f"{path}: connective node cannot contain 'field'")
        key = next(iter(connective_keys))
        children = node.get(key)
        if not isinstance(children, list) or not children:
            errors.append(f"{path}.{key}: must be a non-empty array")
        else:
            for i, child in enumerate(children):
                errors.extend(
                    _validate_expr(child, f"{path}.{key}[{i}]", depth + 1)
                )
        unknown = keys - {key}
        if unknown:
            errors.append(f"{path}: unknown keys {sorted(unknown)}")
        return errors

    # Leaf comparison node.
    required = {"field", "op", "value"}
    missing = required - keys
    if missing:
        errors.append(f"{path}: comparison node missing keys {sorted(missing)}")
    unknown = keys - required
    if unknown:
        errors.append(f"{path}: unknown keys {sorted(unknown)}")
    if missing or unknown:
        return errors

    field_name = node["field"]
    if not isinstance(field_name, str) or not field_name:
        errors.append(f"{path}.field: must be a non-empty string")

    op = node["op"]
    if op not in COMPARISONS:
        errors.append(
            f"{path}.op: must be one of {sorted(COMPARISONS)}"
        )

    value = node["value"]
    if not _json_literal(value):
        errors.append(
            f"{path}.value: must be a JSON literal "
            "(null, bool, number, string, array or object of literals)"
        )
    return errors


def _json_literal(value: Any) -> bool:
    if value is None or isinstance(value, (bool, int, float, str)):
        return True
    if isinstance(value, list):
        return all(_json_literal(v) for v in value)
    if isinstance(value, dict):
        return all(
            isinstance(k, str) and _json_literal(v) for k, v in value.items()
        )
    return False


def validate_counterfactual_request(payload: Any) -> tuple[dict, str, dict, int]:
    """Validate a counterfactual request body.

    Returns the normalized ``(facts, target_conclusion, candidates,
    max_changes)`` tuple; raises :class:`PolicyValidationError` with one error
    string per detected problem.
    """
    errors: list[str] = []

    if not isinstance(payload, dict):
        raise PolicyValidationError(["request body must be an object"])

    facts = payload.get("facts", {})
    if not isinstance(facts, dict) or not all(
        isinstance(k, str) for k in facts
    ):
        errors.append("facts: must be an object with string keys")

    target = payload.get("target_conclusion")
    if not isinstance(target, str) or not target.strip():
        errors.append("target_conclusion: must be a non-empty string")
    elif len(target) > MAX_CONCLUSION_LENGTH:
        errors.append(
            "target_conclusion: must be at most "
            f"{MAX_CONCLUSION_LENGTH} characters"
        )

    candidates = payload.get("candidates")
    normalized_candidates: dict[str, list] = {}
    if not isinstance(candidates, dict) or not candidates:
        errors.append("candidates: must be a non-empty object")
    else:
        for field_name, values in candidates.items():
            prefix = f"candidates.{field_name}"
            if not isinstance(field_name, str) or not field_name:
                errors.append(f"{prefix}: candidate field name must be a non-empty string")
                continue
            if not isinstance(values, list) or not values:
                errors.append(f"{prefix}: must be a non-empty array")
                continue
            normalized_candidates[field_name] = list(values)

    max_changes = payload.get("max_changes")
    if isinstance(max_changes, bool) or not isinstance(max_changes, int):
        errors.append("max_changes: must be a positive integer")
    elif not 1 <= max_changes <= MAX_MAX_CHANGES:
        errors.append(
            f"max_changes: must be between 1 and {MAX_MAX_CHANGES}"
        )

    if errors:
        raise PolicyValidationError(errors)

    return facts, target, normalized_candidates, max_changes
