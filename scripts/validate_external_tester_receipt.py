#!/usr/bin/env python3
"""Validate privacy-safe external beta receipts without echoing receipt data."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = REPO_ROOT / "docs" / "launch" / "external-tester-receipt.schema.json"


class ReceiptError(ValueError):
    """A stable, content-free receipt validation failure."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _matches_type(value: Any, expected: str) -> bool:
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    return False


def _validate_schema(value: Any, schema: dict[str, Any]) -> None:
    if "const" in schema:
        constant = schema["const"]
        if type(value) is not type(constant) or value != constant:
            raise ReceiptError("schema_error")
    if "type" in schema and not _matches_type(value, schema["type"]):
        raise ReceiptError("schema_error")
    if "enum" in schema and value not in schema["enum"]:
        raise ReceiptError("schema_error")

    if isinstance(value, str):
        if len(value) > schema.get("maxLength", len(value)):
            raise ReceiptError("schema_error")
        pattern = schema.get("pattern")
        if pattern is not None and re.search(pattern, value) is None:
            raise ReceiptError("schema_error")

    if isinstance(value, dict):
        properties = schema.get("properties", {})
        required = schema.get("required", [])
        if any(key not in value for key in required):
            raise ReceiptError("schema_error")
        if schema.get("additionalProperties") is False and any(
            key not in properties for key in value
        ):
            raise ReceiptError("privacy_error")
        for key, child in value.items():
            child_schema = properties.get(key)
            if child_schema is not None:
                _validate_schema(child, child_schema)

    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0):
            raise ReceiptError("schema_error")
        if len(value) > schema.get("maxItems", len(value)):
            raise ReceiptError("schema_error")
        if schema.get("uniqueItems"):
            normalized = [json.dumps(item, sort_keys=True) for item in value]
            if len(normalized) != len(set(normalized)):
                raise ReceiptError("schema_error")
        item_schema = schema.get("items")
        if item_schema is not None:
            for item in value:
                _validate_schema(item, item_schema)


def _require_checks(receipt: dict[str, Any], required: set[str]) -> None:
    check_ids = [check["id"] for check in receipt["checks"]]
    if len(check_ids) != len(set(check_ids)) or not required.issubset(check_ids):
        raise ReceiptError("scenario_error")


def _validate_outcomes(receipt: dict[str, Any]) -> None:
    cleanup_outcome: str | None = None
    for check in receipt["checks"]:
        outcome = check["outcome"]
        category = check["error_category"]
        if check["id"] == "exact_cleanup":
            cleanup_outcome = outcome
        if outcome == "passed" and category != "none":
            raise ReceiptError("scenario_error")
        if outcome == "not_run" and category != "not_run":
            raise ReceiptError("scenario_error")
        if outcome in {"failed", "blocked"} and category in {"none", "not_run"}:
            raise ReceiptError("scenario_error")
    expected_cleanup_outcome = {
        "passed": "passed",
        "failed": "failed",
        "not_run": "not_run",
    }[receipt["exact_cleanup"]]
    if expected_cleanup_outcome != "not_run" and cleanup_outcome is None:
        raise ReceiptError("scenario_error")
    if cleanup_outcome is not None and cleanup_outcome != expected_cleanup_outcome:
        raise ReceiptError("scenario_error")


def _validate_scenario(receipt: dict[str, Any]) -> None:
    scenario = receipt["scenario"]
    if scenario == "fresh_core_allow":
        if (
            receipt["xcode"] != "absent"
            or receipt["command_line_tools"] != "absent"
            or receipt["tcc_precondition"] != "not_determined"
            or receipt["tcc_result"] != "granted_after_prompt"
        ):
            raise ReceiptError("scenario_error")
        _require_checks(
            receipt,
            {
                "install",
                "release_verification",
                "permission_allow",
                "core_bounded_read",
                "core_synthetic_crud",
                "core_canonical_alarm",
                "exact_cleanup",
            },
        )
    elif scenario == "fresh_core_deny":
        if (
            receipt["xcode"] != "absent"
            or receipt["command_line_tools"] != "absent"
            or receipt["tcc_precondition"] != "not_determined"
            or receipt["tcc_result"] != "denied"
        ):
            raise ReceiptError("scenario_error")
        _require_checks(
            receipt,
            {"install", "release_verification", "permission_deny_no_retry"},
        )
    elif scenario == "intel_core":
        if receipt["hardware"] != "intel":
            raise ReceiptError("scenario_error")
        _require_checks(
            receipt,
            {
                "install",
                "release_verification",
                "core_bounded_read",
                "core_synthetic_crud",
                "core_canonical_alarm",
                "exact_cleanup",
            },
        )
    elif scenario == "minimum_macos_core":
        if receipt["macos_version"].split(".", 1)[0] != "14":
            raise ReceiptError("scenario_error")
        _require_checks(
            receipt,
            {
                "install",
                "release_verification",
                "core_bounded_read",
                "core_synthetic_crud",
                "core_canonical_alarm",
                "exact_cleanup",
            },
        )
    elif scenario == "upgrade_identity":
        if receipt["tcc_precondition"] != "granted":
            raise ReceiptError("scenario_error")
        _require_checks(
            receipt,
            {
                "install",
                "release_verification",
                "upgrade_identity",
                "core_bounded_read",
                "core_canonical_alarm",
                "exact_cleanup",
            },
        )
    elif scenario == "clt_only_experimental":
        if receipt["xcode"] != "absent" or receipt["command_line_tools"] != "installed":
            raise ReceiptError("scenario_error")
        _require_checks(
            receipt,
            {
                "install",
                "release_verification",
                "core_bounded_read",
                "experimental_capability",
                "experimental_synthetic_mutation",
                "exact_cleanup",
            },
        )
    _validate_outcomes(receipt)


def validate_receipt(receipt: Any, schema: dict[str, Any]) -> None:
    _validate_schema(receipt, schema)
    _validate_scenario(receipt)


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ReceiptError("invalid_json")
        result[key] = value
    return result


def _load_json(path: Path) -> Any:
    try:
        return json.loads(
            path.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
        )
    except (json.JSONDecodeError, UnicodeError, RecursionError) as exc:
        raise ReceiptError("invalid_json") from exc
    except OSError as exc:
        raise ReceiptError("unreadable_file") from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("receipts", nargs="+", type=Path)
    args = parser.parse_args(argv)

    try:
        schema = _load_json(SCHEMA_PATH)
    except ReceiptError:
        print("validator: invalid (schema_unavailable)", file=sys.stderr)
        return 2

    failed = False
    for index, path in enumerate(args.receipts, start=1):
        try:
            receipt = _load_json(path)
            validate_receipt(receipt, schema)
        except ReceiptError as exc:
            failed = True
            print(f"receipt {index}: invalid ({exc.code})", file=sys.stderr)
        else:
            print(f"receipt {index}: valid")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
