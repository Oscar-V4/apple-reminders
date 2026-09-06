#!/usr/bin/env python3
"""Build and exercise the installed signed MCP launcher without Reminders access.

macOS 14+ only. Uses the normal private runtime cache and signature checks.
The only tools/call deliberately fails schema validation before backend dispatch;
it never requests access, runs diagnostics, or reads Reminders content.
"""
from __future__ import annotations

import argparse
import json
import sys
import tempfile
import zipfile
from pathlib import Path

from build_source_package import PLUGIN_ROOT, build_package, sha256

sys.path.insert(0, str(PLUGIN_ROOT / "scripts"))
from bounded_process import run  # noqa: E402


class SmokeError(RuntimeError):
    pass


def probe_wire() -> bytes:
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-11-25", "capabilities": {},
            "clientInfo": {"name": "installed-package-smoke", "version": "1"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
            "name": "diagnose_reminders", "arguments": ["invalid-object-type"]}},
    ]
    return ("\n".join(json.dumps(item) for item in requests) + "\n").encode()


def validate_responses(stdout: str, expected_count: int) -> None:
    try:
        responses = [json.loads(line) for line in stdout.splitlines()]
        if [item.get("id") for item in responses] != [1, 2, 3]:
            raise ValueError("unexpected response sequence")
        if any(item.get("jsonrpc") != "2.0" or "error" in item for item in responses):
            raise ValueError("JSON-RPC error")
        initialized = responses[0]["result"]
        if not initialized["protocolVersion"] or "tools" not in initialized["capabilities"]:
            raise ValueError("initialization contract")
        tools = responses[1]["result"]["tools"]
        names = [tool["name"] for tool in tools]
        if len(names) != expected_count or len(set(names)) != expected_count:
            raise ValueError("tool inventory count or uniqueness")
        diagnostic = next(tool for tool in tools if tool["name"] == "diagnose_reminders")
        if diagnostic["inputSchema"]["type"] != "object":
            raise ValueError("probe no longer guaranteed invalid")
        result = responses[2]["result"]
        payload = result["structuredContent"]
        if (result["isError"] is not True or payload["ok"] is not False
                or payload["status"] != "failed_no_mutation"
                or payload["error"]["code"] != "invalid_input"
                or payload["error"]["reason_code"] != "invalid_arguments"):
            raise ValueError("validation-only call contract")
    except (KeyError, TypeError, ValueError, StopIteration) as exc:
        raise SmokeError(f"installed MCP contract failed: {exc}") from exc


def smoke(plugin: Path, *, default_count: int, experimental_count: int,
          core_count: int | None = None) -> dict:
    with tempfile.TemporaryDirectory(prefix="apple reminders installed smoke ") as temporary:
        root = Path(temporary)
        archive = build_package(plugin, root / "package output")
        installed = root / "installed plugin"
        # The builder audits the allowlisted member paths before extraction.
        with zipfile.ZipFile(archive) as handle:
            handle.extractall(installed)
        plugin_name = json.loads((plugin / ".codex-plugin/plugin.json").read_text())["name"]
        installed_plugin = installed / plugin_name
        modes = [("default", [], default_count),
                 ("experimental", ["--experimental"], experimental_count)]
        if core_count is not None:
            modes.append(("core-only", ["--core-only"], core_count))
        for mode, arguments, expected_count in modes:
            result = run(["/bin/sh", str(installed_plugin / "scripts/launch_bundled_mcp.sh"),
                          *arguments], input=probe_wire(), cwd=installed_plugin,
                         timeout_s=60, stdout_limit=1024 * 1024, stderr_limit=64 * 1024)
            if result.returncode:
                raise SmokeError(f"{mode} installed launcher failed (exit {result.returncode})")
            validate_responses(result.stdout, expected_count)
        return {"ok": True, "archive_sha256": sha256(archive),
                "modes": {mode: count for mode, _, count in modes},
                "tools_call": "schema-validation-only"}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plugin", type=Path, default=PLUGIN_ROOT)
    parser.add_argument("--default-count", type=int, default=15)
    parser.add_argument("--experimental-count", type=int, default=15)
    parser.add_argument("--core-count", type=int, help="Also probe explicit --core-only")
    args = parser.parse_args()
    try:
        print(json.dumps(smoke(args.plugin, default_count=args.default_count,
                               experimental_count=args.experimental_count,
                               core_count=args.core_count), sort_keys=True))
    except (OSError, RuntimeError) as exc:
        parser.exit(1, f"installed package smoke failed: {exc}\n")


if __name__ == "__main__":
    main()
