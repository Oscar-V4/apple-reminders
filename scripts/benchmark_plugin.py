#!/usr/bin/env python3
"""Benchmark data-free Apple Reminders plugin startup and packaging paths.

The benchmark never loads EventKit, requests TCC access, opens the Reminders
application, or reads reminder rows.  Doctor runs use an isolated temporary
HOME, and EventKit requests stop after Python-side validation.
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


@dataclass(frozen=True)
class CommandCase:
    name: str
    command: tuple[str, ...]
    stdin: str = ""
    allowed_returncodes: tuple[int, ...] = (0,)
    environment: dict[str, str] | None = None


def percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    index = max(0, math.ceil(len(ordered) * fraction) - 1)
    return ordered[index]


def summarize(samples_ms: list[float]) -> dict[str, Any]:
    return {
        "samples": len(samples_ms),
        "min_ms": round(min(samples_ms), 3),
        "median_ms": round(statistics.median(samples_ms), 3),
        "mean_ms": round(statistics.fmean(samples_ms), 3),
        "p95_ms": round(percentile(samples_ms, 0.95), 3),
        "max_ms": round(max(samples_ms), 3),
    }


def run_command(case: CommandCase, *, root: Path = ROOT) -> None:
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    if case.environment:
        environment.update(case.environment)
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

    with tempfile.TemporaryDirectory(prefix="apple-reminders-benchmark-home-") as home:
        cases = [
            CommandCase(
                "mcp_initialize_tools_list",
                (PYTHON, str(root / "mcp" / "server.py")),
                stdin=mcp_stdin,
            ),
            CommandCase(
                "eventkit_validate_create",
                (PYTHON, str(root / "scripts" / "eventkit_bridge.py"), "--validate-only"),
                stdin=eventkit_stdin,
            ),
            CommandCase(
                "doctor_isolated_home",
                (
                    PYTHON,
                    str(root / "scripts" / "reminders_doctor.py"),
                    "--skip-helper-syntax-check",
                    "--compact",
                ),
                allowed_returncodes=(0, 1),
                environment={"HOME": home},
            ),
            CommandCase(
                "source_package_audit",
                (
                    PYTHON,
                    str(root / "scripts" / "audit_source_package.py"),
                    str(root),
                    "--json",
                ),
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
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
            "python": platform.python_version(),
        },
        "safety": {
            "isolated_home": True,
            "eventkit_loaded": False,
            "tcc_requested": False,
            "reminder_rows_read": False,
        },
        "package": package_snapshot(root),
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
