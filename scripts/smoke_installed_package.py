#!/usr/bin/env python3
"""Build and exercise the installed signed MCP launcher without Reminders access.

macOS 14+ only. Uses the normal private runtime cache and signature checks.
A tools/call fails schema validation before backend dispatch. The optional
--check-packaging probe also executes the bounded packaging-only diagnosis;
neither path requests access or reads Reminders content.
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


def probe_wire(check_packaging: bool = False) -> bytes:
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-11-25", "capabilities": {},
            "clientInfo": {"name": "installed-package-smoke", "version": "1"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
            "name": "diagnose_reminders", "arguments": ["invalid-object-type"]}},
    ]
    if check_packaging:
        requests.append({"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {
            "name": "diagnose_reminders", "arguments": {
                "scope": "packaging", "detail_level": "summary", "execution_mode": "metadata_only"}}})
    return ("\n".join(json.dumps(item) for item in requests) + "\n").encode()


def validate_responses(stdout: str, expected_names: frozenset[str], check_packaging: bool = False) -> None:
    try:
        responses = [json.loads(line) for line in stdout.splitlines()]
        if [item.get("id") for item in responses] != ([1, 2, 3, 4] if check_packaging else [1, 2, 3]):
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
        if check_packaging:
            result = responses[3]["result"]
            payload = result["structuredContent"]
            data = payload["data"]
            if (result["isError"] is not False or payload["ok"] is not True
                    or payload["status"] != "verified" or data["scope"] != "packaging"
                    or data["capabilities"] != []
                    or {check["name"] for check in data["checks"]} != {"platform", "local_artifacts", "redaction"}
                    or data["privacy"] != {"content_free": True, "reminder_content_read": False, "prompt_triggered": False}
                    or data["execution"] != {"mode": "metadata_only", "developer_tool_process_attempted": False,
                                             "compiler_process_attempted": False, "install_request_attempted": False}):
                raise ValueError("packaging diagnosis privacy or scope contract")
    except (AttributeError, KeyError, TypeError, ValueError, StopIteration) as exc:
        raise SmokeError(f"installed MCP contract failed: {exc}") from exc


def client_command(plugin: Path, client: str) -> list[str]:
    """Resolve only documented client placeholders; never evaluate shell text."""
    if client == "codex":
        manifest = json.loads((plugin / ".codex-plugin/plugin.json").read_text())
        config = json.loads((plugin / manifest["mcpServers"]).read_text())
        server = config["mcpServers"]["apple-reminders-local"]
    elif client == "claude-code":
        config = json.loads((plugin / ".mcp.json").read_text())
        server = config["mcpServers"]["apple-reminders-local"]
    elif client == "claude-desktop":
        server = json.loads((plugin / "manifest.json").read_text())["server"]["mcp_config"]
    else:
        raise ValueError(f"unsupported client: {client}")
    return [value.replace("${CLAUDE_PLUGIN_ROOT}", str(plugin))
            .replace("${__dirname}", str(plugin))
            for value in [server["command"], *server.get("args", [])]]


def smoke(plugin: Path, *, default_count: int, experimental_count: int,
          core_count: int | None = None, check_packaging: bool = False,
          check_clients: bool = False) -> dict:
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
                          *arguments], input=probe_wire(check_packaging), cwd=installed_plugin,
                         timeout_s=60, stdout_limit=1024 * 1024, stderr_limit=64 * 1024)
            if result.returncode:
                raise SmokeError(f"{mode} installed launcher failed (exit {result.returncode})")
            validate_responses(result.stdout, expected_names, check_packaging)
        report = {"ok": True, "archive_sha256": sha256(archive),
                  "modes": {mode: len(names) for mode, _, names in modes},
                  "tools_call": "schema-validation-and-packaging-metadata" if check_packaging else "schema-validation-only"}
        if check_clients:
            desktop = build_package(plugin, root / "package output", format="mcpb")
            desktop_root = root / "Claude extension with spaces"
            extract_audited_archive(desktop, desktop_root)
            report["mcpb_sha256"] = sha256(desktop)
            report["clients"] = {}
            for client, selected in (("codex", installed_plugin),
                                     ("claude-code", installed_plugin),
                                     ("claude-desktop", desktop_root)):
                # Claude runs from an unrelated working directory. This catches
                # accidental dependencies on Codex's plugin-root cwd handling.
                cwd = selected if client == "codex" else root
                for mode, arguments, names in modes:
                    result = run(client_command(selected, client) + arguments,
                                 input=probe_wire(check_packaging), cwd=cwd,
                                 timeout_s=60, stdout_limit=1024 * 1024,
                                 stderr_limit=64 * 1024)
                    if result.returncode:
                        raise SmokeError(f"{client} {mode} launcher failed (exit {result.returncode})")
                    validate_responses(result.stdout, names, check_packaging)
                report["clients"][client] = {mode: len(names) for mode, _, names in modes}
        return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plugin", type=Path, default=PLUGIN_ROOT)
    parser.add_argument("--default-count", type=int, choices=(9, 15), default=15,
                        help="Expected default profile; 9 supports older Core-default packages")
    parser.add_argument("--experimental-count", type=int, choices=(15,), default=15)
    parser.add_argument("--core-count", type=int, choices=(9,), help="Also probe explicit --core-only")
    parser.add_argument("--check-packaging", action="store_true",
                        help="Also call packaging-only diagnosis (requires scope-bounded collector)")
    parser.add_argument("--check-clients", action="store_true",
                        help="Probe Codex, Claude Code, and Desktop MCPB launch configurations")
    args = parser.parse_args()
    try:
        print(json.dumps(smoke(args.plugin, default_count=args.default_count,
                               experimental_count=args.experimental_count,
                               core_count=args.core_count, check_packaging=args.check_packaging,
                               check_clients=args.check_clients), sort_keys=True))
    except (OSError, RuntimeError) as exc:
        parser.exit(1, f"installed package smoke failed: {exc}\n")


if __name__ == "__main__":
    main()
