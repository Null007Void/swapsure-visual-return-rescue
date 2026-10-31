"""Load the frozen specification from this repository.

The normative artefacts are files, not constants.  Every rule module imports
these loaders so that behaviour cannot drift from `contracts/` and `fixtures/`
without a visible, hash-changing edit (see ``tools/verify_spec_copy.py``).

All loaders are cached and return read-only views: nothing in the application may
mutate the specification at runtime.
"""

from __future__ import annotations

import json
import pathlib
from types import MappingProxyType
from typing import Any, Mapping

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent
CONTRACTS = REPO_ROOT / "contracts"
FIXTURES = REPO_ROOT / "fixtures"
DOCS = REPO_ROOT / "docs"


class SpecError(RuntimeError):
    """The repository spec is missing, unparsable or internally inconsistent."""


def _read_json(path: pathlib.Path) -> Any:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError as exc:  # pragma: no cover - repository integrity
        raise SpecError(f"missing specification file: {path}") from exc
    except json.JSONDecodeError as exc:
        raise SpecError(f"invalid JSON in {path}: {exc}") from exc


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def _frozen_json(path: pathlib.Path) -> Mapping[str, Any]:
    data = _freeze(_read_json(path))
    if not isinstance(data, Mapping):
        raise SpecError(f"expected a JSON object in {path}")
    return data


def _frozen_json_list(path: pathlib.Path) -> tuple[Any, ...]:
    data = _freeze(_read_json(path))
    if not isinstance(data, tuple):
        raise SpecError(f"expected a JSON array in {path}")
    return data


_cache: dict[str, Any] = {}


def policy() -> Mapping[str, Any]:
    """`contracts/policy.json` v1.0 — offline defaults and the reason table."""
    if "policy" not in _cache:
        _cache["policy"] = _frozen_json(CONTRACTS / "policy.json")
    return _cache["policy"]


def state_machine() -> Mapping[str, Any]:
    """`contracts/state-machine.json` v1.0 — the authoritative edge set."""
    if "state_machine" not in _cache:
        _cache["state_machine"] = _frozen_json(CONTRACTS / "state-machine.json")
    return _cache["state_machine"]


def eligibility_schema() -> Mapping[str, Any]:
    """`contracts/eligibility.schema.json` v1.0 — the decision output contract."""
    if "eligibility_schema" not in _cache:
        _cache["eligibility_schema"] = _frozen_json(CONTRACTS / "eligibility.schema.json")
    return _cache["eligibility_schema"]


def eligibility_cases() -> tuple[Mapping[str, Any], ...]:
    """`fixtures/eligibility-cases.json` — 23 spec-oracle rows E01..E23."""
    if "eligibility_cases" not in _cache:
        _cache["eligibility_cases"] = _frozen_json_list(FIXTURES / "eligibility-cases.json")
    return _cache["eligibility_cases"]


def catalog() -> Mapping[str, Any]:
    """`fixtures/catalog.json` — synthetic catalog and the accounting oracle."""
    if "catalog" not in _cache:
        _cache["catalog"] = _frozen_json(FIXTURES / "catalog.json")
    return _cache["catalog"]
