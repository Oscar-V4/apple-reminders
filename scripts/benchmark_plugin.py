#!/usr/bin/env python3
"""Benchmark data-free Apple Reminders plugin startup and packaging paths.

The benchmark never runs the native helper or calls EventKit APIs, requests TCC
access, opens the Reminders application, or reads reminder rows. Doctor runs use
the real MCP route with an isolated temporary HOME. On macOS, EventKit helper
tests compile but never execute the helper; request tests stop after Python-side
validation.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import platform
import statistics
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable


ROOT = Path(__file__).resolve().parents[1]
PYTHON = sys.executable
P95_BUDGETS_MS = {
    "mcp_initialize_tools_list": 300.0,
    "mcp_doctor_route": 1_500.0,
    "eventkit_helper_build_fresh": 2_500.0,
    "eventkit_helper_build_cached": 350.0,
}
BENCHMARK_ENV_DENYLIST = {
    "APPLE_REMINDERS_MCP_TEST_MODE",
    "APPLE_REMINDERS_ADAPTER_PATH",
    "APPLE_REMINDERS_EVENTKIT_BRIDGE_PATH",
    "APPLE_REMINDERS_DOCTOR_PATH",
}


@dataclass(frozen=True)
class CommandCase:
    name: str
    command: tuple[str, ...]
    stdin: str = ""
    allowed_returncodes: tuple[int, ...] = (0,)
    environment: dict[str, str] | None = None
    output_contract: str | None = None


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = max(0, math.ceil(len(ordered) * fraction) - 1)
    return ordered[index]


def summarize(samples_ms: list[float]) -> dict[str, Any]:
    median_ms = statistics.median(samples_ms)
    return {
        "samples": len(samples_ms),
        "min_ms": round(min(samples_ms), 3),
        "median_ms": round(median_ms, 3),
        "mean_ms": round(statistics.fmean(samples_ms), 3),
        "stdev_ms": round(statistics.stdev(samples_ms), 3) if len(samples_ms) > 1 else 0.0,
        "mad_ms": round(statistics.median(abs(value - median_ms) for value in samples_ms), 3),
        "p95_ms": round(percentile(samples_ms, 0.95), 3),
        "max_ms": round(max(samples_ms), 3),
    }


def evaluate_performance_gates(
    measurements: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    cases: dict[str, dict[str, Any]] = {}
    passed = True
    complete = True
    for name, budget_ms in P95_BUDGETS_MS.items():
        measurement = measurements.get(name, {})
        if measurement.get("status") == "skipped":
            cases[name] = {
                "status": "skipped",
                "reason": measurement.get("reason", "unsupported"),
                "budget_ms": budget_ms,
            }
            complete = False
            passed = False
            continue
        actual_ms = measurement.get("p95_ms")
        if not isinstance(actual_ms, (int, float)) or isinstance(actual_ms, bool):
            cases[name] = {
                "status": "invalid",
                "budget_ms": budget_ms,
            }
            complete = False
            passed = False
            continue
        case_passed = actual_ms <= budget_ms
        cases[name] = {
            "status": "passed" if case_passed else "failed",
            "actual_ms": actual_ms,
            "budget_ms": budget_ms,
        }
        passed = passed and case_passed
    return {
        "metric": "p95_ms",
        "complete": complete,
        "passed": passed,
        "cases": cases,
    }


def run_command(case: CommandCase, *, root: Path = ROOT) -> None:
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    if case.environment:
        environment.update(case.environment)
    for name in BENCHMARK_ENV_DENYLIST:
        environment.pop(name, None)
    completed = subprocess.run(
        case.command,
        cwd=root,
        env=environment,
        input=case.stdin,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
        check=False,
    )
    if completed.returncode not in case.allowed_returncodes:
        raise RuntimeError(
            f"{case.name} exited {completed.returncode}: {completed.stderr.strip()}"
        )
    if case.output_contract == "mcp_success":
        try:
            responses = [
                json.loads(line)
                for line in completed.stdout.splitlines()
                if line.strip()
            ]
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"{case.name} emitted invalid MCP JSON") from exc
        if not responses:
            raise RuntimeError(f"{case.name} emitted no MCP responses")
        for response in responses:
            if not isinstance(response, dict) or "error" in response:
                raise RuntimeError(f"{case.name} reported an MCP error")
            result = response.get("result")
            if not isinstance(result, dict) or result.get("isError") is True:
                raise RuntimeError(f"{case.name} reported an MCP error")
        requests = [
            json.loads(line)
            for line in case.stdin.splitlines()
            if line.strip()
        ]
        expected_ids = [request["id"] for request in requests if "id" in request]
        response_ids = [response.get("id") for response in responses]
        if len(response_ids) != len(expected_ids) or set(response_ids) != set(expected_ids):
            raise RuntimeError(f"{case.name} MCP response IDs did not match requests")
    elif case.output_contract == "json_ok":
        try:
            payload = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"{case.name} emitted invalid JSON") from exc
        if not isinstance(payload, dict) or payload.get("ok") is not True:
            raise RuntimeError(f"{case.name} did not report ok=true")


def benchmark_action(
    action: Callable[[], None], *, warmups: int, samples: int
) -> dict[str, Any]:
    for _ in range(warmups):
        action()
    elapsed_ms: list[float] = []
    for _ in range(samples):
        started = time.perf_counter_ns()
        action()
        elapsed_ms.append((time.perf_counter_ns() - started) / 1_000_000)
    return summarize(elapsed_ms)


def git_value(*args: str, root: Path = ROOT) -> str | None:
    completed = subprocess.run(
        ["git", *args],
        cwd=root,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    value = completed.stdout.strip()
    return value if completed.returncode == 0 and value else None


def package_snapshot(root: Path = ROOT) -> dict[str, Any]:
    completed = subprocess.run(
        [PYTHON, str(root / "scripts" / "audit_source_package.py"), str(root), "--json"],
        cwd=root,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"source audit failed: {completed.stderr.strip()}")
    audit = json.loads(completed.stdout)
    files = [root / relative for relative in audit["package_files"]]
    with tempfile.TemporaryDirectory(prefix="apple-reminders-benchmark-snapshot-") as output:
        built = subprocess.run(
            [
                PYTHON,
                str(root / "scripts" / "build_source_package.py"),
                str(root),
                "--output-directory",
                output,
            ],
            cwd=root,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
            check=False,
        )
        if built.returncode != 0:
            raise RuntimeError(f"snapshot package build failed: {built.stderr.strip()}")
        build_result = json.loads(built.stdout)
        archive_value = build_result.get("archive") if isinstance(build_result, dict) else None
        if not isinstance(archive_value, str) or not archive_value:
            raise RuntimeError("snapshot package build did not report an archive path")
        archive = Path(archive_value).resolve()
        output_root = Path(output).resolve()
        try:
            archive.relative_to(output_root)
        except ValueError as exc:
            raise RuntimeError("snapshot package escaped its temporary output directory") from exc
        archive_bytes = archive.stat().st_size
    return {
        "allowlisted_files": len(files),
        "allowlisted_source_bytes": sum(path.stat().st_size for path in files),
        "archive_bytes": archive_bytes,
    }


def collect_payload(args: argparse.Namespace) -> dict[str, Any]:
    root = args.plugin_root.expanduser().resolve()
    system = platform.system()
    if not (root / ".codex-plugin" / "plugin.json").is_file():
        raise RuntimeError(f"plugin root is missing its manifest: {root}")
    initialize = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-11-25",
            "capabilities": {},
            "clientInfo": {"name": "benchmark", "version": "1"},
        },
    }
    tools_list = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}
    mcp_stdin = json.dumps(initialize) + "\n" + json.dumps(tools_list) + "\n"
    doctor_call = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {"name": "reminders_plugin_doctor", "arguments": {}},
    }
    doctor_stdin = json.dumps(initialize) + "\n" + json.dumps(doctor_call) + "\n"
    eventkit_stdin = json.dumps(
        {
            "schema_version": 1,
            "operation": "create_reminder",
            "calendar_id": "BENCHMARK-CALENDAR",
            "title": "Benchmark reminder",
            "due": {
                "kind": "timed",
                "date_time": "2026-08-06T14:30:00+09:00",
                "time_zone": "Asia/Seoul",
            },
            "alarms": [
                {"kind": "absolute", "date_time": "2026-08-06T14:00:00+09:00"}
            ],
            "recurrence_rules": [{"frequency": "weekly", "interval": 1}],
        }
    )

    with (
        tempfile.TemporaryDirectory(prefix="apple-reminders-benchmark-home-") as home,
        tempfile.TemporaryDirectory(
            prefix="apple-reminders-benchmark-eventkit-cache-"
        ) as eventkit_cache,
    ):
        cases = [
            CommandCase(
                "mcp_initialize_tools_list",
                (PYTHON, str(root / "mcp" / "server.py")),
                stdin=mcp_stdin,
                output_contract="mcp_success",
            ),
            CommandCase(
                "eventkit_validate_create",
                (PYTHON, str(root / "scripts" / "eventkit_bridge.py"), "--validate-only"),
                stdin=eventkit_stdin,
                output_contract="json_ok",
            ),
            CommandCase(
                "mcp_doctor_route",
                (PYTHON, str(root / "mcp" / "server.py")),
                stdin=doctor_stdin,
                environment={"HOME": home},
                output_contract="mcp_success",
            ),
            CommandCase(
                "source_package_audit",
                (
                    PYTHON,
                    str(root / "scripts" / "audit_source_package.py"),
                    str(root),
                    "--json",
                ),
                output_contract="json_ok",
            ),
        ]
        measurements = {
            case.name: benchmark_action(
                lambda selected=case: run_command(selected, root=root),
                warmups=args.warmups,
                samples=args.samples,
            )
            for case in cases
        }
        eventkit_build_cases = (
            CommandCase(
                "eventkit_helper_build_fresh",
                (
                    PYTHON,
                    str(root / "scripts" / "eventkit_bridge.py"),
                    "--build-only",
                    "--cache-dir",
                    eventkit_cache,
                    "--force-build",
                ),
                output_contract="json_ok",
            ),
            CommandCase(
                "eventkit_helper_build_cached",
                (
                    PYTHON,
                    str(root / "scripts" / "eventkit_bridge.py"),
                    "--build-only",
                    "--cache-dir",
                    eventkit_cache,
                ),
                output_contract="json_ok",
            ),
        )
        if system == "Darwin":
            measurements.update(
                {
                    case.name: benchmark_action(
                        lambda selected=case: run_command(selected, root=root),
                        warmups=args.build_warmups,
                        samples=args.build_samples,
                    )
                    for case in eventkit_build_cases
                }
            )
        else:
            for case in eventkit_build_cases:
                measurements[case.name] = {
                    "status": "skipped",
                    "reason": "requires_macos",
                }

    def build_once() -> None:
        with tempfile.TemporaryDirectory(prefix="apple-reminders-benchmark-build-") as output:
            run_command(
                CommandCase(
                    "deterministic_package_build",
                    (
                        PYTHON,
                        str(root / "scripts" / "build_source_package.py"),
                        str(root),
                        "--output-directory",
                        output,
                    ),
                ),
                root=root,
            )

    measurements["deterministic_package_build"] = benchmark_action(
        build_once,
        warmups=args.build_warmups,
        samples=args.build_samples,
    )
    performance_gates = evaluate_performance_gates(measurements)
    return {
        "schema_version": 1,
        "label": args.label,
        "captured_at": datetime.now(UTC).isoformat(),
        "git": {
            "branch": git_value("branch", "--show-current", root=root),
            "commit": git_value("rev-parse", "HEAD", root=root),
            "dirty": bool(git_value("status", "--porcelain", root=root)),
        },
        "platform": {
            "system": system,
            "release": platform.release(),
            "machine": platform.machine(),
            "python": platform.python_version(),
        },
        "measurement_semantics": {
            "timing": "end_to_end_subprocess_wall_clock",
            "process_startup_included": True,
            "host_load_controlled": False,
            "cross_machine_comparable": False,
            "doctor_home_state": "isolated_empty_temporary_home",
        },
        "safety": {
            "isolated_home": True,
            "doctor_isolated_home": True,
            "doctor_via_mcp_route": True,
            "eventkit_validation_only": True,
            "eventkit_helper_compiled": system == "Darwin",
            "eventkit_live_invocation": False,
            "eventkit_loaded": False,
            "eventkit_api_called": False,
            "native_helper_executed": False,
            "tcc_requested": False,
            "reminder_rows_read": False,
        },
        "package": package_snapshot(root),
        "performance_gates": performance_gates,
        "measurements": measurements,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--label", required=True, help="Human-readable run label")
    parser.add_argument(
        "--plugin-root",
        type=Path,
        default=ROOT,
        help="Plugin checkout to benchmark (defaults to this script's repository)",
    )
    parser.add_argument("--samples", type=int, default=15)
    parser.add_argument("--warmups", type=int, default=3)
    parser.add_argument("--build-samples", type=int, default=5)
    parser.add_argument("--build-warmups", type=int, default=1)
    parser.add_argument(
        "--no-enforce-performance-gates",
        action="store_true",
        help="Report p95 budget failures without returning exit status 2",
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    if min(args.samples, args.build_samples) < 1 or min(args.warmups, args.build_warmups) < 0:
        parser.error("sample counts must be positive and warmup counts non-negative")
    try:
        payload = collect_payload(args)
        rendered = json.dumps(payload, indent=2, sort_keys=True) + "\n"
        if args.output:
            output = args.output.expanduser().resolve()
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(rendered, encoding="utf-8")
    except (OSError, RuntimeError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        sys.stderr.write(f"benchmark failed: {exc}\n")
        return 1
    sys.stdout.write(rendered)
    gates = payload.get("performance_gates", {})
    if not args.no_enforce_performance_gates and gates.get("passed") is False:
        failed = [
            name
            for name, result in gates.get("cases", {}).items()
            if result.get("status") in {"failed", "invalid"}
        ]
        sys.stderr.write(f"benchmark performance gate failed: {', '.join(failed)}\n")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
