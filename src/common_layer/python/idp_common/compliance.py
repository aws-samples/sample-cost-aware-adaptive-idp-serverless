# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0

"""Pack-driven compliance evaluation and cross-document reconciliation.

Compliance rules are single comparisons (``lhs OP rhs``) between extracted
field values and/or numeric literals. We parse and evaluate them with a small,
safe evaluator — never ``eval`` — so untrusted pack content cannot execute
code.

Each field operand is classified before comparing:

* ``missing`` — the field is absent (or empty): the rule is ``NA``.
* ``invalid`` — present but not exactly one number (``"TBD"``, ``"30-35"``):
  the rule is ``NEEDS_REVIEW``. A unit exponent (``N/mm2``, ``m3``) is part of
  the unit, not a second number.
* ``unverified`` — a model-extracted value that could not be grounded in the
  OCR text (or has no BDA bounding box): a passing comparison becomes
  ``NEEDS_REVIEW``; a failing comparison stays ``FAIL``.

The overall verdict uses the precedence ``FAIL > NEEDS_REVIEW > PASS > NA``.
``force_review`` (a Bedrock Guardrail intervened) sends every rule to
``NEEDS_REVIEW`` and makes the verdict ``NEEDS_REVIEW (...; guardrail=intervened)``
even when no rule applies.
"""

import re
from dataclasses import dataclass, field
from typing import AbstractSet, Any, Dict, Iterable, List, Optional, Tuple

from idp_common.grounding import strip_unit_exponents
from idp_common.packs import ComplianceRule, IndustryPack

# Result statuses (compliance outcome labels; the string values are what land
# in the record's compliance_result column).
COMPLIANT = "PASS"
FAIL = "FAIL"
NEEDS_REVIEW = "NEEDS_REVIEW"  # a human must confirm the value(s) used
NA = "NA"  # rule could not be evaluated (operand missing)

_OPERATORS = ["<=", ">=", "==", "!=", "<", ">"]
_NUMBER_RE = re.compile(r"^-?\d+(\.\d+)?$")
_NUMBER_SEARCH_RE = re.compile(r"-?\d+(?:\.\d+)?")

# Operand states.
_OK = "ok"
_MISSING = "missing"
_INVALID = "invalid"
_UNVERIFIED = "unverified"


@dataclass
class RuleResult:
    rule_id: str
    status: str
    detail: str = ""


@dataclass
class ComplianceReport:
    overall: str
    rules: List[RuleResult] = field(default_factory=list)
    guardrail_intervened: bool = False

    def to_summary(self) -> str:
        """Compact string for the record's ``compliance_result`` column."""
        parts = [f"{r.rule_id}={r.status}" for r in self.rules]
        if self.guardrail_intervened:
            parts.append("guardrail=intervened")
        if not parts:
            return NA
        return f"{self.overall} ({'; '.join(parts)})"


def _norm_name(name: Any) -> str:
    return re.sub(r"\s+", "_", str(name).strip().lower())


def _normalize(fields: Dict[str, str]) -> Dict[str, str]:
    """Lower-cased, space->underscore field lookup for case-insensitive match."""
    out: Dict[str, str] = {}
    for name, value in fields.items():
        out[_norm_name(name)] = value
    return out


def _resolve_operand(
    token: str, fields: Dict[str, str], unverified: AbstractSet[str]
) -> Tuple[str, Optional[float]]:
    """Return ``(state, number)`` for one side of a rule expression."""
    token = token.strip()
    if _NUMBER_RE.match(token):
        return _OK, float(token)
    key = _norm_name(token)
    raw = fields.get(key)
    if raw is None or str(raw).strip() == "":
        return _MISSING, None
    # Units/symbols are allowed ("35 MPa", "35 N/mm2" - a unit exponent is not
    # a number), but the value must contain exactly one number: "TBD",
    # "approx 500 or 1000" and "30-35" are ambiguous.
    numbers = _NUMBER_SEARCH_RE.findall(strip_unit_exponents(raw).replace(",", ""))
    if len(numbers) != 1:
        return _INVALID, None
    return (_UNVERIFIED if key in unverified else _OK), float(numbers[0])


def evaluate_rule(
    rule: ComplianceRule,
    normalized_fields: Dict[str, str],
    unverified: AbstractSet[str] = frozenset(),
) -> RuleResult:
    op = next((o for o in _OPERATORS if o in rule.expr), None)
    if op is None:
        return RuleResult(rule.id, NA, f"unparseable expression: {rule.expr!r}")
    lhs_str, rhs_str = rule.expr.split(op, 1)
    lhs_state, lhs = _resolve_operand(lhs_str, normalized_fields, unverified)
    rhs_state, rhs = _resolve_operand(rhs_str, normalized_fields, unverified)
    states = (lhs_state, rhs_state)
    if _MISSING in states:
        return RuleResult(rule.id, NA, "operand missing")
    if _INVALID in states:
        return RuleResult(rule.id, NEEDS_REVIEW, "operand is not exactly one number")

    comparisons = {
        "<=": lhs <= rhs,
        ">=": lhs >= rhs,
        "==": lhs == rhs,
        "!=": lhs != rhs,
        "<": lhs < rhs,
        ">": lhs > rhs,
    }
    detail = f"{lhs} {op} {rhs}"
    if not comparisons[op]:
        return RuleResult(rule.id, FAIL, detail)
    if _UNVERIFIED in states:
        return RuleResult(rule.id, NEEDS_REVIEW, f"{detail} (unverified value)")
    return RuleResult(rule.id, COMPLIANT, detail)


def _overall(results: List[RuleResult]) -> str:
    """Precedence FAIL > NEEDS_REVIEW > PASS > NA (NA also when no rules)."""
    statuses = {r.status for r in results}
    for status in (FAIL, NEEDS_REVIEW, COMPLIANT):
        if status in statuses:
            return status
    return NA


def evaluate_compliance(
    fields: Dict[str, str],
    pack: IndustryPack,
    doc_type: Optional[str] = None,
    unverified_fields: Optional[Iterable[str]] = None,
    force_review: bool = False,
) -> ComplianceReport:
    """Run the applicable compliance rules against a record's fields.

    When ``doc_type`` is given, only the rules for that document type (plus any
    pack-wide rules) are evaluated. Without it, every rule in the pack is run
    (back-compatible behavior). ``unverified_fields`` names model-extracted
    fields that could not be grounded; ``force_review`` sends every rule to
    ``NEEDS_REVIEW`` and makes the overall verdict ``NEEDS_REVIEW`` even when
    no rule applies (a blocked document has no classification, so no rules).
    """
    normalized = _normalize(fields)
    unverified = frozenset(_norm_name(n) for n in (unverified_fields or ()))
    rules = pack.compliance_rules_for(doc_type) if doc_type else pack.compliance_rules
    if force_review:
        results = [RuleResult(rule.id, NEEDS_REVIEW, "guardrail intervened") for rule in rules]
        return ComplianceReport(overall=NEEDS_REVIEW, rules=results, guardrail_intervened=True)
    results = [evaluate_rule(rule, normalized, unverified) for rule in rules]
    return ComplianceReport(overall=_overall(results), rules=results)


def extract_linking_keys(fields: Dict[str, str], pack: IndustryPack) -> Dict[str, str]:
    """Pull the pack's linking-key values out of a record's fields."""
    normalized = _normalize(fields)
    keys: Dict[str, str] = {}
    for link_key in pack.linking_keys:
        norm = re.sub(r"\s+", "_", link_key.strip().lower())
        if norm in normalized and normalized[norm]:
            keys[link_key] = normalized[norm]
    return keys


def reconcile_records(records: List[Dict[str, Any]], pack: IndustryPack) -> Dict[str, Any]:
    """Cross-document reconciliation: group by linking keys and flag conflicts.

    Pure function (no I/O) so it can be used in tests, batch jobs, or analytics.
    Records sharing the same linking-key signature should agree on shared field
    values; disagreements are reported as conflicts.
    """
    groups: Dict[str, List[Dict[str, Any]]] = {}
    for rec in records:
        link = extract_linking_keys(rec.get("fields", {}), pack)
        signature = "|".join(f"{k}={link[k]}" for k in sorted(link)) or "_unlinked"
        groups.setdefault(signature, []).append(rec)

    conflicts: List[Dict[str, Any]] = []
    for signature, group in groups.items():
        if len(group) < 2:
            continue
        seen: Dict[str, str] = {}
        for rec in group:
            for name, value in (rec.get("fields", {}) or {}).items():
                if name in seen and seen[name] != value:
                    conflicts.append(
                        {
                            "linking_signature": signature,
                            "field": name,
                            "values": sorted({seen[name], value}),
                        }
                    )
                else:
                    seen[name] = value
    return {
        "group_count": len(groups),
        "linked_group_count": sum(1 for g in groups.values() if len(g) > 1),
        "conflicts": conflicts,
    }
