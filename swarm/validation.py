"""Validate the runtime answer schema and cross-field arithmetic before filling a bid."""
import json
from copy import deepcopy
from decimal import Decimal

from jsonschema import Draft202012Validator

from swarm.normalize import round_cents


def prepare_answer(answer: dict, schema: dict | None = None) -> tuple[dict, list[str]]:
    """Keep independent valid fields, omit malformed lines, and recompute the submitted total.

    Never invent replacement business values. The runtime schema is the final authority.
    """
    answer = deepcopy(answer)
    changes = []
    item_schema = ((schema or {}).get('properties', {}).get('positions', {}).get('items') or {})
    required = set(item_schema.get('required') or ('position', 'material_number', 'quantity', 'unit_price', 'lead_time_weeks'))
    kept, seen = [], set()
    for entry in answer.get('positions') or []:
        pos = entry.get('position')
        malformed = not pos or pos in seen
        for name, value in list(entry.items()):
            prop = item_schema.get('properties', {}).get(name)
            bad = bool(prop and list(Draft202012Validator(prop).iter_errors(value)))
            if name in ('quantity', 'lead_time_weeks'):
                bad |= isinstance(value, bool) or not isinstance(value, int) or value < 0
            elif name in ('unit_price','base_unit_price','surcharge_per_unit','packaging_per_unit','rebate_total'):
                try:
                    number = Decimal(str(value))
                    bad |= not number.is_finite() or number < 0
                except (ValueError, TypeError, ArithmeticError):
                    bad = True
            if bad:
                if name in required:
                    malformed = True
                else:
                    entry.pop(name)
                    changes.append(f'{pos}: omitted invalid optional {name}')
        malformed |= not required.issubset(entry)
        if item_schema and list(Draft202012Validator(item_schema).iter_errors(entry)):
            malformed = True
        if malformed:
            changes.append(f'{pos}: omitted malformed or duplicate position')
        else:
            kept.append(entry)
            seen.add(pos)
    if answer.get('decision') == 'no_bid':
        kept = []
    answer['positions'] = kept
    answer['total_net'] = float(round_cents(sum((Decimal(e['quantity']) * Decimal(str(e['unit_price'])) for e in kept), Decimal(0))))
    return answer, changes


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
