"""Normative-schema validation helpers for decision output."""

from __future__ import annotations

import json
from typing import Any, Mapping

import jsonschema

from specs import eligibility_schema


def thaw(value: Any) -> Any:
    """Convert read-only spec views into plain JSON-compatible containers."""

    if isinstance(value, Mapping):
        return {str(key): thaw(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [thaw(item) for item in value]
    return value


def schema() -> dict[str, Any]:
    return thaw(eligibility_schema())


def validate_decision(payload: Mapping[str, Any]) -> None:
    """Raise ``jsonschema.ValidationError`` if the payload violates the contract."""

    jsonschema.Draft202012Validator(schema()).validate(thaw(payload))


def validate_decision_json(payload: Mapping[str, Any]) -> str:
    """Round-trip through JSON first: the schema sees exactly what a client would."""

    encoded = json.dumps(payload, sort_keys=True)
    validate_decision(json.loads(encoded))
    return encoded
