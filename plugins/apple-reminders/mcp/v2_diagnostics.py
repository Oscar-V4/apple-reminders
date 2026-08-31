#!/usr/bin/env python3
"""Content-free Diagnostics Module for the public v2 Reminders interface."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping
from typing import Any

from reminders_contracts import runtime_boundary_metadata


HEX_64_PATTERN = re.compile(r"^[0-9a-f]{64}$")
DIAGNOSIS_CHECKS = {
    "core": frozenset({"platform", "reminders_app", "store_access", "command_schema"}),
    "access": frozenset({"permissions", "account_visibility", "store_access"}),
    "native_extension": frozenset(
        {"helper_toolchain", "private_frameworks", "store_access"}
    ),
    "sections": frozenset(
        {"helper_toolchain", "private_frameworks", "command_schema", "store_access"}
    ),
    "tags": frozenset({"command_schema", "store_access"}),
    "attachments": frozenset(
        {"helper_toolchain", "private_frameworks", "command_schema", "store_access"}
    ),
    "recovery": frozenset(
        {"helper_toolchain", "private_frameworks", "command_schema", "store_access"}
    ),
    "packaging": frozenset(
        {"platform", "local_artifacts", "redaction", "command_schema"}
    ),
}
EXECUTION_MODES = frozenset({"metadata_only", "experimental_toolchain"})
EXPERIMENTAL_TOOLCHAIN_SCOPES = frozenset(
    {"native_extension", "sections", "attachments", "recovery"}
)
DIAGNOSIS_CAPABILITIES = {
    "core": ("stable_core",),
    "access": ("stable_core",),
    "native_extension": (
        "section_create_mutation",
        "section_move_mutation",
        "tag_assignment_mutation",
        "image_attachment_mutation",
        "url_attachment_mutation",
        "attachment_delete_mutation",
        "recently_deleted_exact_inspection",
        "recently_deleted_recovery",
    ),
    "sections": ("section_create_mutation", "section_move_mutation"),
    "tags": ("tag_assignment_mutation",),
    "attachments": (
        "image_attachment_mutation",
        "url_attachment_mutation",
        "attachment_delete_mutation",
    ),
    "recovery": (
        "recently_deleted_inventory",
        "recently_deleted_exact_inspection",
        "recently_deleted_recovery",
    ),
    "packaging": ("stable_core",),
}
PUBLIC_ERROR_CODES = frozenset(
    {
        "invalid_input",
        "permission_denied",
        "schema_mismatch",
        "unsupported_capability",
        "unexpected_error",
    }
)
CONTENT_FREE_FALSE_FIELDS = (
    "reminder_rows_read",
    "reminder_titles_read",
    "list_section_tag_names_read",
    "journal_cache_backup_contents_read",
    "write_attempted",
    "permission_prompt_attempted",
    "application_launched",
    "private_framework_loaded",
)


class DiagnosticsError(ValueError):
    def __init__(
        self,
        code: str,
        reason_code: str,
        message: str,
        *,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.reason_code = reason_code
        self.retryable = retryable


def _stable_hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _reason_code(value: Any, fallback: str) -> str:
    candidate = value if isinstance(value, str) else fallback
    normalized = re.sub(r"[^a-z0-9_]+", "_", candidate.casefold()).strip("_")
    return (normalized or fallback)[:128]


def _public_error(exc: DiagnosticsError) -> dict[str, Any]:
    code = exc.code if exc.code in PUBLIC_ERROR_CODES else "unexpected_error"
    return {
        "code": code,
        "reason_code": _reason_code(exc.reason_code, code),
        "message": (str(exc) or "Diagnosis failed.")[:2000],
        "retryable": exc.retryable,
    }


def _project_capabilities(raw: Any, scope: str) -> list[dict[str, Any]]:
    if not isinstance(raw, Mapping):
        return []
    stable = raw.get("stable_core")
    experimental = raw.get("experimental_internals")
    selected: list[dict[str, Any]] = []
    for name in DIAGNOSIS_CAPABILITIES.get(scope, ()):
        candidate = (
            stable
            if name == "stable_core"
            else experimental.get(name)
            if isinstance(experimental, Mapping)
            else None
        )
        if not isinstance(candidate, Mapping):
            continue
        projected = {
            key: candidate[key]
            for key in (
                "capability",
                "support_tier",
                "api_boundary",
                "compiler_requirement",
                "build_compatibility",
                "schema_compatibility",
                "runtime_state",
                "reason_code",
                "available",
                "runtime_verification_required",
            )
            if isinstance(candidate.get(key), (str, bool))
        }
        if projected.get("capability") == name:
            selected.append(projected)
    return selected


class DiagnosticsFacade:
    """One-tool Interface that owns diagnosis filtering and privacy claims."""

    def __init__(
        self,
        *,
        doctor_call: Callable[[dict[str, Any]], dict[str, Any]],
        environment_fingerprint: Callable[[], str],
    ) -> None:
        self._doctor_call = doctor_call
        self._environment_fingerprint = environment_fingerprint

    def call(self, name: str, raw_arguments: Any) -> dict[str, Any]:
        try:
            if name != "diagnose_reminders":
                raise DiagnosticsError(
                    "invalid_input",
                    "unknown_diagnostics_tool",
                    f"Unknown Diagnostics tool: {name}",
                )
            if not isinstance(raw_arguments, Mapping):
                raise DiagnosticsError(
                    "invalid_input",
                    "arguments_not_object",
                    "Tool arguments must be an object.",
                )
            return self._diagnose(dict(raw_arguments))
        except DiagnosticsError as exc:
            error = _public_error(exc)
            result: dict[str, Any] = {
                "schema_version": 2,
                "ok": False,
                "status": "failed_no_mutation",
                "operation": "diagnose_reminders",
                "error": error,
            }
            if error["code"] == "permission_denied":
                result["next_action"] = {
                    "kind": "request_access",
                    "tool": "request_reminders_access",
                    "retry_original_once": True,
                    "message": "Request Reminders access, then retry diagnosis once.",
                }
            return result
        except Exception as exc:
            return {
                "schema_version": 2,
                "ok": False,
                "status": "failed_no_mutation",
                "operation": "diagnose_reminders",
                "error": {
                    "code": "unexpected_error",
                    "reason_code": "diagnostics_facade_failure",
                    "message": (
                        "The Diagnostics Module could not complete "
                        f"({type(exc).__name__})."
                    ),
                    "retryable": False,
                },
            }

    def _diagnose(self, arguments: dict[str, Any]) -> dict[str, Any]:
        unknown = sorted(
            set(arguments) - {"scope", "detail_level", "execution_mode"}
        )
        if unknown:
            raise DiagnosticsError(
                "invalid_input",
                "unknown_fields",
                f"Unsupported fields: {', '.join(unknown)}",
            )
        scope = arguments.get("scope", "core")
        if scope not in DIAGNOSIS_CHECKS:
            raise DiagnosticsError(
                "invalid_input",
                "invalid_diagnosis_scope",
                "scope is not a public diagnosis area.",
            )
        detail = arguments.get("detail_level", "summary")
        if detail not in {"summary", "full"}:
            raise DiagnosticsError(
                "invalid_input",
                "invalid_detail_level",
                "detail_level must be summary or full.",
            )
        execution_mode = arguments.get("execution_mode", "metadata_only")
        if execution_mode not in EXECUTION_MODES:
            raise DiagnosticsError(
                "invalid_input",
                "invalid_execution_mode",
                "execution_mode must be metadata_only or experimental_toolchain.",
            )
        if (
            execution_mode == "experimental_toolchain"
            and scope not in EXPERIMENTAL_TOOLCHAIN_SCOPES
        ):
            raise DiagnosticsError(
                "invalid_input",
                "experimental_toolchain_scope_required",
                "Experimental toolchain execution requires an explicitly related private-helper scope.",
            )
        raw = self._doctor_call(
            {
                "scope": scope,
                "detail_level": detail,
                "execution_mode": execution_mode,
            }
        )
        if not isinstance(raw, Mapping):
            raise DiagnosticsError(
                "unexpected_error",
                "doctor_failed",
                "The Doctor returned a non-object result.",
            )
        privacy = raw.get("privacy") if isinstance(raw.get("privacy"), Mapping) else {}
        raw_error = raw.get("error") if isinstance(raw.get("error"), Mapping) else None
        if raw.get("ok") is False and raw_error is not None and not privacy:
            backend_code = raw_error.get("code")
            public_code = (
                backend_code
                if isinstance(backend_code, str) and backend_code in PUBLIC_ERROR_CODES
                else "unexpected_error"
            )
            raise DiagnosticsError(
                public_code,
                _reason_code(backend_code, "doctor_failed"),
                str(raw_error.get("message") or "The content-free Doctor failed.")[:2000],
                retryable=raw_error.get("retryable") is True,
            )
        if (
            privacy.get("content_free") is not True
            or any(privacy.get(field) is not False for field in CONTENT_FREE_FALSE_FIELDS)
        ):
            raise DiagnosticsError(
                "schema_mismatch",
                "doctor_not_content_free",
                "The Doctor did not satisfy its content-free privacy contract.",
            )
        execution_raw = raw.get("execution")
        execution_fields = (
            "developer_tool_process_attempted",
            "compiler_process_attempted",
            "install_request_attempted",
        )
        if (
            not isinstance(execution_raw, Mapping)
            or execution_raw.get("mode") != execution_mode
            or any(
                not isinstance(execution_raw.get(field), bool)
                for field in execution_fields
            )
        ):
            raise DiagnosticsError(
                "schema_mismatch",
                "doctor_execution_unattested",
                "The Doctor did not attest its requested execution mode.",
            )
        if execution_raw.get("install_request_attempted") is not False:
            raise DiagnosticsError(
                "schema_mismatch",
                "doctor_install_request_forbidden",
                "Diagnosis must never request developer-tool installation.",
            )
        if execution_mode == "metadata_only" and any(
            execution_raw.get(field) is True
            for field in (
                "developer_tool_process_attempted",
                "compiler_process_attempted",
            )
        ):
            raise DiagnosticsError(
                "schema_mismatch",
                "metadata_diagnosis_executed_process",
                "Metadata-only diagnosis must not execute developer tools.",
            )
        expected_boundaries = runtime_boundary_metadata()
        capabilities = (
            raw.get("capabilities")
            if isinstance(raw.get("capabilities"), Mapping)
            else {}
        )
        if capabilities.get("runtime_boundaries") != expected_boundaries:
            raise DiagnosticsError(
                "schema_mismatch",
                "doctor_runtime_boundaries_mismatch",
                "The Doctor returned inconsistent runtime capability boundaries.",
            )
        checks_raw = raw.get("checks") if isinstance(raw.get("checks"), Mapping) else {}
        checks: list[dict[str, Any]] = []
        selected_names = DIAGNOSIS_CHECKS[str(scope)]
        for check_name, candidate in list(checks_raw.items()):
            if check_name not in selected_names or len(checks) >= 50:
                continue
            if not isinstance(candidate, Mapping):
                continue
            status = candidate.get("status")
            if status not in {"ok", "warning", "blocked", "unknown", "skipped"}:
                status = "unknown"
            facts: list[dict[str, Any]] = []
            details = candidate.get("details")
            if detail == "full" and isinstance(details, Mapping):
                for fact_name, fact_value in details.items():
                    if not (
                        isinstance(fact_value, (str, int, float, bool))
                        or fact_value is None
                    ):
                        continue
                    facts.append(
                        {"name": str(fact_name)[:128], "value": fact_value}
                    )
                    if len(facts) >= 20:
                        break
            checks.append(
                {
                    "name": str(check_name)[:128],
                    "status": status,
                    "code": _reason_code(candidate.get("code"), "unknown_check"),
                    "message": str(
                        candidate.get("message") or "No diagnostic message."
                    )[:2000],
                    "facts": facts,
                }
            )
        counts = {
            status: sum(1 for check in checks if check["status"] == status)
            for status in ("ok", "warning", "blocked", "unknown", "skipped")
        }
        overall = (
            "blocked"
            if counts["blocked"]
            else "degraded"
            if counts["warning"] or counts["unknown"]
            else "ready"
        )
        summary = ", ".join(
            f"{status}={count}" for status, count in counts.items() if count
        ) or "No checks were available for the requested diagnostic area."
        return {
            "schema_version": 2,
            "ok": True,
            "status": "verified",
            "operation": "diagnose_reminders",
            "data": {
                "overall": overall,
                "scope": scope,
                "execution_mode": execution_mode,
                "execution": {
                    "mode": execution_mode,
                    **{
                        field: execution_raw[field]
                        for field in execution_fields
                    },
                },
                "capability_boundaries": expected_boundaries,
                "summary": summary[:4000],
                "environment_fingerprint": self._fingerprint(),
                "checks": checks,
                "capabilities": _project_capabilities(raw.get("capabilities"), str(scope)),
                "privacy": {
                    "content_free": True,
                    "reminder_content_read": False,
                    "prompt_triggered": False,
                },
            },
        }

    def _fingerprint(self) -> str:
        value = self._environment_fingerprint()
        if isinstance(value, str) and HEX_64_PATTERN.fullmatch(value):
            return value
        return _stable_hash(str(value))
