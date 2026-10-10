"""Validate the runtime answer schema and cross-field arithmetic before filling a bid."""
import json
from decimal import Decimal

from jsonschema import Draft202012Validator

from swarm.normalize import round_cents


def answer_issues(answer: dict, schema: dict | None = None) -> list[str]:
    issues = []
    if schema:
        issues.extend(f"{'/'.join(map(str, e.path))}: {e.message}" for e in Draft202012Validator(schema).iter_errors(answer))
    try:
        encoded = json.dumps(answer, allow_nan=False).encode("utf-8")
        if len(encoded) > 65536:
            issues.append("answer exceeds maximum size")
        positions = answer.get("positions") or []
        if len({p.get("position") for p in positions}) != len(positions):
            issues.append("duplicate position identifiers")
        total = round_cents(sum((Decimal(str(p["quantity"])) * Decimal(str(p["unit_price"])) for p in positions), Decimal(0)))
        if total != Decimal(str(answer.get("total_net"))):
            issues.append("total differs from quantity times rounded unit prices")
        if answer.get("decision") == "no_bid" and (positions or total):
            issues.append("no_bid must have no positions and zero total")
    except (ValueError, TypeError, KeyError, ArithmeticError):
        issues.append("answer contains invalid numeric values")
    if not answer.get("tender_reference"):
        issues.append("tender reference is missing")
    return issues
