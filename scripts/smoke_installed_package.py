#!/usr/bin/env python3
"""Build and exercise the installed signed MCP launcher without Reminders access.

macOS 14+ only. Uses the normal private runtime cache and signature checks.
The only tools/call deliberately fails schema validation before backend dispatch;
it never requests access, runs diagnostics, or reads Reminders content.
"""
from __future__ import annotations

import argparse
import json
import shutil
import stat
import sys
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

from build_source_package import PLUGIN_ROOT, build_package, sha256

sys.path.insert(0, str(PLUGIN_ROOT / "scripts"))
from bounded_process import run  # noqa: E402


class SmokeError(RuntimeError):
    pass


# Deliberate release expectations, independent of the installed server catalog.
CORE_TOOL_NAMES = frozenset({
    "request_reminders_access", "list_reminder_lists", "fetch_reminders",
    "read_reminder", "create_reminder", "change_reminder", "delete_reminder",
    "ensure_reminder_list", "diagnose_reminders",
})
PUBLIC_TOOL_NAMES = CORE_TOOL_NAMES | {
    "inspect_reminder_native", "change_reminder_attachment",
    "create_reminder_section", "organize_reminder",
    "inspect_recently_deleted", "recover_deleted_reminder",
}


def extract_audited_archive(archive: Path, destination: Path) -> None:
    """Extract the builder's audited regular-file ZIP with faithful Unix modes."""
    destination.mkdir(mode=0o755)
    destination.chmod(0o755)
    with zipfile.ZipFile(archive) as handle:
        for info in handle.infolist():
            relative = PurePosixPath(info.filename)
            mode = info.external_attr >> 16
            if (relative.is_absolute() or ".." in relative.parts
                    or not relative.parts or "\\" in info.filename
                    or mode not in {stat.S_IFREG | 0o644, stat.S_IFREG | 0o755}):
                raise SmokeError("audited package contains an unsupported member")
            target = destination.joinpath(*relative.parts)
            for directory in reversed(target.parents):
                if directory == destination or destination in directory.parents:
                    directory.mkdir(mode=0o755, exist_ok=True)
                    directory.chmod(0o755)
            with target.open("xb") as output, handle.open(info) as source:
                shutil.copyfileobj(source, output, length=1024 * 1024)
            target.chmod(stat.S_IMODE(mode))


def profile_modes(default_count: int, experimental_count: int,
                  core_count: int | None) -> list[tuple[str, list[str], frozenset[str]]]:
    if default_count not in {9, 15} or experimental_count != 15 or core_count not in {None, 9}:
        raise SmokeError("supported profiles are default 9 or 15, experimental 15, and Core-only 9")
    modes = [("default", [], CORE_TOOL_NAMES if default_count == 9 else PUBLIC_TOOL_NAMES),
             ("experimental", ["--experimental"], PUBLIC_TOOL_NAMES)]
    if core_count is not None:
        modes.append(("core-only", ["--core-only"], CORE_TOOL_NAMES))
    return modes


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


def validate_responses(stdout: str, expected_names: frozenset[str]) -> None:
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
        if len(names) != len(expected_names) or set(names) != expected_names:
            raise ValueError("tool inventory names or uniqueness")
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
    except (AttributeError, KeyError, TypeError, ValueError, StopIteration) as exc:
        raise SmokeError(f"installed MCP contract failed: {exc}") from exc


def smoke(plugin: Path, *, default_count: int, experimental_count: int,
          core_count: int | None = None) -> dict:
    modes = profile_modes(default_count, experimental_count, core_count)
    with tempfile.TemporaryDirectory(prefix="apple reminders installed smoke ") as temporary:
        root = Path(temporary)
        archive = build_package(plugin, root / "package output")
        installed = root / "installed plugin"
        # The builder audits the allowlisted member paths and modes.
        extract_audited_archive(archive, installed)
        plugin_name = json.loads((plugin / ".codex-plugin/plugin.json").read_text())["name"]
        installed_plugin = installed / plugin_name
        for mode, arguments, expected_names in modes:
            result = run(["/bin/sh", str(installed_plugin / "scripts/launch_bundled_mcp.sh"),
                          *arguments], input=probe_wire(), cwd=installed_plugin,
                         timeout_s=60, stdout_limit=1024 * 1024, stderr_limit=64 * 1024)
            if result.returncode:
                raise SmokeError(f"{mode} installed launcher failed (exit {result.returncode})")
            validate_responses(result.stdout, expected_names)
        return {"ok": True, "archive_sha256": sha256(archive),
                "modes": {mode: len(names) for mode, _, names in modes},
                "tools_call": "schema-validation-only"}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plugin", type=Path, default=PLUGIN_ROOT)
    parser.add_argument("--default-count", type=int, choices=(9, 15), default=15,
                        help="Expected default profile; 9 supports older Core-default packages")
    parser.add_argument("--experimental-count", type=int, choices=(15,), default=15)
    parser.add_argument("--core-count", type=int, choices=(9,), help="Also probe explicit --core-only")
    args = parser.parse_args()
    try:
        print(json.dumps(smoke(args.plugin, default_count=args.default_count,
                               experimental_count=args.experimental_count,
                               core_count=args.core_count), sort_keys=True))
    except (OSError, RuntimeError) as exc:
        parser.exit(1, f"installed package smoke failed: {exc}\n")


if __name__ == "__main__":
    main()
