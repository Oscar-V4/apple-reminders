#!/usr/bin/env python3
"""Validate this Codex plugin without importing runtime integrations.

The validator is intentionally dependency-free so CI can validate the plugin,
all bundled skills/evals, and the local stdio MCP declaration without opening
Reminders, loading EventKit/ReminderKit, or touching user data.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = REPO_ROOT / "plugins" / "apple-reminders"
SEMVER_RE = re.compile(
    r"^(0|[1-9]\d*)\."
    r"(0|[1-9]\d*)\."
    r"(0|[1-9]\d*)"
    r"(?:-(?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*)"
    r"(?:\.(?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*))*)?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)
PLUGIN_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
FRONTMATTER_RE = re.compile(r"\A---\n(?P<body>.*?)\n---(?:\n|\Z)", re.DOTALL)
MARKDOWN_LINK_RE = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
PLACEHOLDER_RE = re.compile(r"\[TODO:[^\]]*\]|\b(?:TODO|TBD)\b", re.IGNORECASE)

ALLOWED_MANIFEST_FIELDS = {
    "name",
    "version",
    "description",
    "author",
    "homepage",
    "repository",
    "license",
    "keywords",
    "skills",
    "mcpServers",
    "apps",
    "interface",
}
REQUIRED_MANIFEST_FIELDS = {
    "name",
    "version",
    "description",
    "author",
    "license",
    "skills",
    "interface",
}
REQUIRED_INTERFACE_FIELDS = {
    "displayName",
    "shortDescription",
    "longDescription",
    "developerName",
    "category",
    "capabilities",
}
PATH_INTERFACE_FIELDS = {"composerIcon", "logo", "logoDark"}
HTTPS_INTERFACE_FIELDS = {"websiteURL", "privacyPolicyURL", "termsOfServiceURL"}

PUBLIC_MCP_TOOL_NAMES = (
    "request_reminders_access",
    "list_reminder_lists",
    "fetch_reminders",
    "read_reminder",
    "create_reminder",
    "change_reminder",
    "delete_reminder",
    "inspect_reminder_native",
    "ensure_reminder_list",
    "create_reminder_section",
    "organize_reminder",
    "change_reminder_attachment",
    "diagnose_reminders",
)
PUBLIC_MCP_TOOL_NAME_SET = frozenset(PUBLIC_MCP_TOOL_NAMES)
MAX_MCP_DISCOVERY_BYTES = 32_768


def _load_json(path: Path, errors: list[str]) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        errors.append(f"missing file: {path}")
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        errors.append(f"invalid JSON {path}: {exc}")
    return None


def _nonempty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _resolve_plugin_path(
    root: Path,
    value: Any,
    label: str,
    errors: list[str],
    *,
    directory: bool = False,
) -> Path | None:
    if not _nonempty_string(value) or not value.startswith("./"):
        errors.append(f"{label} must be a non-empty plugin-relative path beginning with ./")
        return None
    raw_candidate = root / value[2:]
    candidate = raw_candidate.resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        errors.append(f"{label} escapes the plugin root: {value}")
        return None
    expected = candidate.is_dir() if directory else candidate.is_file()
    if not expected:
        kind = "directory" if directory else "file"
        errors.append(f"{label} does not reference an existing {kind}: {value}")
        return None
    if raw_candidate.is_symlink():
        errors.append(f"{label} must not be a symlink: {value}")
    return candidate


def _frontmatter_value(body: str, key: str) -> str | None:
    match = re.search(rf"(?m)^{re.escape(key)}:\s*(.*?)\s*$", body)
    if not match:
        return None
    value = match.group(1).strip().strip("\"'")
    if value in {">", "|"}:
        following = body[match.end() :].splitlines()
        folded: list[str] = []
        for line in following:
            if not line.startswith((" ", "\t")):
                break
            folded.append(line.strip())
        value = " ".join(part for part in folded if part)
    return value or None


def _validate_eval_cases(skill_name: str, path: Path, errors: list[str]) -> None:
    payload = _load_json(path, errors)
    if payload is None:
        return
    if isinstance(payload, list):
        cases = payload
    elif isinstance(payload, dict):
        if payload.get("skill_name") != skill_name:
            errors.append(f"{path}: skill_name must be {skill_name!r}")
        cases = payload.get("evals")
    else:
        cases = None
    if not isinstance(cases, list) or not 2 <= len(cases) <= 10:
        errors.append(f"{path}: evals must contain between 2 and 10 cases")
        return

    ids: set[int] = set()
    for index, item in enumerate(cases, start=1):
        prefix = f"{path}: eval {index}"
        if not isinstance(item, dict):
            errors.append(f"{prefix} must be an object")
            continue
        if not _nonempty_string(item.get("prompt")):
            errors.append(f"{prefix} needs a non-empty prompt")
        expected = item.get("expected_behavior", item.get("expected_output"))
        if not _nonempty_string(expected):
            errors.append(f"{prefix} needs expected_behavior or expected_output")
        if "id" in item:
            identifier = item["id"]
            if isinstance(identifier, bool) or not isinstance(identifier, int):
                errors.append(f"{prefix} id must be an integer")
            elif identifier in ids:
                errors.append(f"{prefix} id is duplicated: {identifier}")
            else:
                ids.add(identifier)
        if "assertions" in item:
            assertions = item["assertions"]
            if not isinstance(assertions, list) or not assertions:
                errors.append(f"{prefix} assertions must be a non-empty list")
            else:
                for assertion in assertions:
                    valid = _nonempty_string(assertion) or (
                        isinstance(assertion, dict)
                        and _nonempty_string(assertion.get("text"))
                    )
                    if not valid:
                        errors.append(f"{prefix} contains an invalid assertion")


def _validate_agent_yaml(path: Path, errors: list[str]) -> None:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        errors.append(f"unable to read {path}: {exc}")
        return
    if not re.search(r"(?m)^interface:\s*$", text):
        errors.append(f"{path}: missing interface mapping")
    for key in ("display_name", "short_description", "default_prompt"):
        if not re.search(rf"(?m)^\s+{key}:\s*\S", text):
            errors.append(f"{path}: missing interface.{key}")
    if PLACEHOLDER_RE.search(text):
        errors.append(f"{path}: unresolved placeholder found")


def _validate_skill_links(skill_path: Path, root: Path, text: str, errors: list[str]) -> None:
    for target in MARKDOWN_LINK_RE.findall(text):
        target = target.split("#", 1)[0]
        if not target or target.startswith(("https://", "http://", "mailto:")):
            continue
        candidate = (skill_path.parent / target).resolve()
        try:
            candidate.relative_to(root)
        except ValueError:
            errors.append(f"{skill_path}: link escapes plugin root: {target}")
            continue
        if not candidate.exists():
            errors.append(f"{skill_path}: broken local link: {target}")


def validate_skills(root: Path, skills_dir: Path, errors: list[str]) -> list[str]:
    skill_names: list[str] = []
    directories = sorted(path for path in skills_dir.iterdir() if path.is_dir())
    if not directories:
        errors.append(f"no skills found under {skills_dir}")
        return skill_names
    for directory in directories:
        skill_path = directory / "SKILL.md"
        if not skill_path.is_file() or skill_path.is_symlink():
            errors.append(f"{directory}: missing regular SKILL.md")
            continue
        try:
            text = skill_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            errors.append(f"unable to read {skill_path}: {exc}")
            continue
        if len(text.splitlines()) > 500:
            errors.append(f"{skill_path}: exceeds 500 lines")
        match = FRONTMATTER_RE.match(text)
        if not match:
            errors.append(f"{skill_path}: malformed or missing YAML frontmatter")
            continue
        name = _frontmatter_value(match.group("body"), "name")
        description = _frontmatter_value(match.group("body"), "description")
        if name != directory.name:
            errors.append(f"{skill_path}: frontmatter name must match {directory.name!r}")
        elif name in skill_names:
            errors.append(f"duplicate skill name: {name}")
        else:
            skill_names.append(name)
        if not description:
            errors.append(f"{skill_path}: frontmatter description is required")
        if PLACEHOLDER_RE.search(match.group("body")):
            errors.append(f"{skill_path}: unresolved frontmatter placeholder found")
        _validate_skill_links(skill_path, root, text, errors)

        agent_path = directory / "agents" / "openai.yaml"
        if not agent_path.is_file() or agent_path.is_symlink():
            errors.append(f"{directory}: missing regular agents/openai.yaml")
        else:
            _validate_agent_yaml(agent_path, errors)

        eval_path = directory / "evals" / "evals.json"
        if not eval_path.is_file() or eval_path.is_symlink():
            errors.append(f"{directory}: missing regular evals/evals.json")
        else:
            _validate_eval_cases(directory.name, eval_path, errors)
    return skill_names


def _extract_string_assignment(path: Path, variable: str, errors: list[str]) -> str | None:
    try:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
    except (OSError, UnicodeError, SyntaxError) as exc:
        errors.append(f"unable to parse {path}: {exc}")
        return None
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        value = node.value
        if any(isinstance(target, ast.Name) and target.id == variable for target in targets):
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                return value.value
    errors.append(f"{path}: missing string assignment for {variable}")
    return None


def _extract_string_collection_members(
    path: Path,
    variables: tuple[str, ...],
    errors: list[str],
) -> set[str] | None:
    """Read declared MCP tool names without importing the runtime."""

    try:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
    except (OSError, UnicodeError, SyntaxError) as exc:
        errors.append(f"unable to parse {path}: {exc}")
        return None
    found: dict[str, set[str]] = {}
    wanted = set(variables)
    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        names = {
            target.id
            for target in targets
            if isinstance(target, ast.Name) and target.id in wanted
        }
        if not names:
            continue
        values: list[ast.expr | None]
        if isinstance(node.value, ast.Dict):
            values = list(node.value.keys)
        elif isinstance(node.value, ast.Set):
            values = list(node.value.elts)
        elif (
            isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Name)
            and node.value.func.id in {"set", "frozenset"}
            and len(node.value.args) == 1
            and not node.value.keywords
            and isinstance(node.value.args[0], ast.Set)
        ):
            values = list(node.value.args[0].elts)
        elif (
            isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Name)
            and node.value.func.id in {"set", "frozenset"}
            and not node.value.args
            and not node.value.keywords
        ):
            values = []
        else:
            errors.append(
                f"{path}: MCP string collections must be literal dictionaries or sets"
            )
            return None
        members: set[str] = set()
        for value in values:
            if not isinstance(value, ast.Constant) or not isinstance(value.value, str):
                errors.append(f"{path}: MCP string collections must use literal strings")
                return None
            members.add(value.value)
        for name in names:
            found[name] = members
    missing = sorted(wanted - set(found))
    if missing:
        errors.append(f"{path}: missing literal MCP route mappings: {missing}")
        return None
    return set().union(*(found[name] for name in variables))


def _semver_core(version: str) -> str:
    return re.split(r"[-+]", version, maxsplit=1)[0]


def validate_mcp(root: Path, manifest: dict[str, Any], errors: list[str]) -> None:
    config_path = root / ".mcp.json"
    declaration = manifest.get("mcpServers")
    if config_path.exists() and declaration != "./.mcp.json":
        errors.append("plugin.json must declare mcpServers as ./.mcp.json when substantive MCP config exists")
        return
    if declaration is not None and not config_path.is_file():
        errors.append("plugin.json declares mcpServers but .mcp.json is missing")
        return
    if not config_path.is_file():
        return

    payload = _load_json(config_path, errors)
    if not isinstance(payload, dict) or set(payload) != {"mcpServers"}:
        errors.append(".mcp.json must contain only a top-level mcpServers object")
        return
    servers = payload.get("mcpServers")
    if not isinstance(servers, dict) or not servers:
        errors.append(".mcp.json mcpServers must be a non-empty object")
        return
    for name, server in servers.items():
        prefix = f".mcp.json server {name!r}"
        if not PLUGIN_NAME_RE.fullmatch(name):
            errors.append(f"{prefix} name must be kebab-case")
        if not isinstance(server, dict):
            errors.append(f"{prefix} must be an object")
            continue
        if "url" in server:
            errors.append(f"{prefix} must remain a local stdio server, not an HTTP endpoint")
        if not _nonempty_string(server.get("title")) or not _nonempty_string(server.get("description")):
            errors.append(f"{prefix} needs a title and description")
        if server.get("cwd") != ".":
            errors.append(f"{prefix} cwd must be the plugin root (.)")
        if not _nonempty_string(server.get("command")):
            errors.append(f"{prefix} command must be non-empty")
        args = server.get("args")
        if not isinstance(args, list) or not args or not all(_nonempty_string(arg) for arg in args):
            errors.append(f"{prefix} args must be a non-empty string list")
        elif args[0].startswith("./"):
            _resolve_plugin_path(root, args[0], f"{prefix} args[0]", errors)
        else:
            errors.append(f"{prefix} args[0] must be a plugin-relative executable script")
        for index, icon in enumerate(server.get("icons", [])):
            if not isinstance(icon, dict):
                errors.append(f"{prefix} icon {index} must be an object")
                continue
            _resolve_plugin_path(root, icon.get("src"), f"{prefix} icon {index}", errors)

    schema_path = root / "schemas" / "mcp-tools.json"
    schema = _load_json(schema_path, errors)
    tool_names: set[str] | None = None
    if not isinstance(schema, dict) or schema.get("schemaVersion") != 2:
        errors.append("schemas/mcp-tools.json must use schemaVersion 2")
    else:
        tools = schema.get("tools")
        if not isinstance(tools, list) or not tools:
            errors.append("schemas/mcp-tools.json must define at least one tool")
        else:
            names: set[str] = set()
            ordered_names: list[str] = []
            for index, tool in enumerate(tools):
                if not isinstance(tool, dict) or not _nonempty_string(tool.get("name")):
                    errors.append(f"MCP tool {index} needs a string name")
                    continue
                name = tool["name"]
                if name in names:
                    errors.append(f"duplicate MCP tool name: {name}")
                names.add(name)
                ordered_names.append(name)
                input_schema = tool.get("inputSchema")
                if not isinstance(input_schema, dict) or input_schema.get("type") != "object":
                    errors.append(f"MCP tool {name} needs an object inputSchema")
                elif input_schema.get("additionalProperties") is not False:
                    errors.append(f"MCP tool {name} must reject unknown input fields")
                if "outputSchema" in tool:
                    errors.append(
                        f"MCP tool {name} must keep outputSchema out of public discovery"
                    )
            tool_names = names
            if tuple(ordered_names) != PUBLIC_MCP_TOOL_NAMES:
                errors.append(
                    "public MCP tool contract drift: "
                    f"expected={list(PUBLIC_MCP_TOOL_NAMES)}, actual={ordered_names}"
                )
            compact_bytes = len(
                json.dumps(schema, ensure_ascii=False, separators=(",", ":")).encode(
                    "utf-8"
                )
            )
            if compact_bytes > MAX_MCP_DISCOVERY_BYTES:
                errors.append(
                    "public MCP discovery contract exceeds the 32 KiB budget: "
                    f"{compact_bytes} bytes"
                )

    server_path = root / "mcp" / "server.py"
    if not server_path.is_file() or server_path.stat().st_size < 1_000:
        errors.append("mcp/server.py is missing or appears to be an empty stub")
        return
    try:
        server_text = server_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        errors.append(f"unable to read {server_path}: {exc}")
        return
    for method in ("initialize", "tools/list", "tools/call"):
        if method not in server_text:
            errors.append(f"mcp/server.py is missing MCP method {method!r}")
    contract_path = root / "mcp" / "v2_contract.py"
    contract_names = _extract_string_collection_members(
        contract_path,
        ("READ_TOOLS", "MUTATION_TOOLS"),
        errors,
    )
    if contract_names is not None:
        if contract_names != PUBLIC_MCP_TOOL_NAME_SET:
            errors.append(
                "public MCP runtime contract drift: "
                f"expected={sorted(PUBLIC_MCP_TOOL_NAME_SET)}, "
                f"actual={sorted(contract_names)}"
            )
        if tool_names is not None and tool_names != contract_names:
            errors.append(
                "public MCP schema/runtime drift: "
                f"schema_only={sorted(tool_names - contract_names)}, "
                f"runtime_only={sorted(contract_names - tool_names)}"
            )
    server_version = _extract_string_assignment(server_path, "SERVER_VERSION", errors)
    plugin_version = manifest.get("version")
    if server_version and isinstance(plugin_version, str):
        if not SEMVER_RE.fullmatch(server_version):
            errors.append(f"mcp/server.py SERVER_VERSION is not strict semver: {server_version}")
        elif _semver_core(server_version) != _semver_core(plugin_version):
            errors.append(
                "MCP server/plugin version drift: "
                f"server={server_version}, plugin={plugin_version}"
            )


def validate_root(root: Path) -> list[str]:
    root = root.expanduser().resolve()
    errors: list[str] = []
    manifest_path = root / ".codex-plugin" / "plugin.json"
    manifest = _load_json(manifest_path, errors)
    if not isinstance(manifest, dict):
        return errors or ["plugin manifest must be a JSON object"]

    unknown = sorted(set(manifest) - ALLOWED_MANIFEST_FIELDS)
    if unknown:
        errors.append(f"plugin.json has unsupported fields: {unknown}")
    missing = sorted(REQUIRED_MANIFEST_FIELDS - set(manifest))
    if missing:
        errors.append(f"plugin.json is missing required fields: {missing}")
    if "hooks" in manifest:
        errors.append("plugin.json hooks is unsupported by plugin ingestion")

    name = manifest.get("name")
    if not _nonempty_string(name) or not PLUGIN_NAME_RE.fullmatch(name):
        errors.append("plugin.json name must be lower-case kebab-case")
    elif name != root.name:
        errors.append(f"plugin.json name {name!r} must match plugin directory {root.name!r}")
    version = manifest.get("version")
    if not _nonempty_string(version) or not SEMVER_RE.fullmatch(version):
        errors.append("plugin.json version must be strict semantic versioning")
    if not _nonempty_string(manifest.get("description")):
        errors.append("plugin.json description is required")
    author = manifest.get("author")
    if not isinstance(author, dict) or not _nonempty_string(author.get("name")):
        errors.append("plugin.json author.name is required")
    if not _nonempty_string(manifest.get("license")):
        errors.append("plugin.json license is required")

    for field in ("homepage", "repository"):
        if field in manifest and (
            not _nonempty_string(manifest[field]) or not manifest[field].startswith("https://")
        ):
            errors.append(f"plugin.json {field} must be an absolute https URL")

    skills_dir = _resolve_plugin_path(
        root, manifest.get("skills"), "plugin.json skills", errors, directory=True
    )
    if skills_dir:
        validate_skills(root, skills_dir, errors)

    interface = manifest.get("interface")
    if not isinstance(interface, dict):
        errors.append("plugin.json interface must be an object")
    else:
        missing_interface = sorted(REQUIRED_INTERFACE_FIELDS - set(interface))
        if missing_interface:
            errors.append(f"plugin.json interface is missing: {missing_interface}")
        for field in REQUIRED_INTERFACE_FIELDS - {"capabilities"}:
            if field in interface and not _nonempty_string(interface[field]):
                errors.append(f"plugin.json interface.{field} must be non-empty")
        capabilities = interface.get("capabilities")
        if not isinstance(capabilities, list) or not capabilities or not all(
            _nonempty_string(item) for item in capabilities
        ):
            errors.append("plugin.json interface.capabilities must be a non-empty string list")
        for field in HTTPS_INTERFACE_FIELDS:
            if field in interface and (
                not _nonempty_string(interface[field]) or not interface[field].startswith("https://")
            ):
                errors.append(f"plugin.json interface.{field} must be an absolute https URL")
        for field in PATH_INTERFACE_FIELDS:
            if field in interface:
                _resolve_plugin_path(root, interface[field], f"plugin.json interface.{field}", errors)
        screenshots = interface.get("screenshots", [])
        if not isinstance(screenshots, list):
            errors.append("plugin.json interface.screenshots must be a list")
        else:
            for index, screenshot in enumerate(screenshots):
                if not isinstance(screenshot, str) or not screenshot.startswith("./assets/") or not screenshot.endswith(".png"):
                    errors.append(f"plugin.json screenshot {index} must be a PNG under ./assets/")
                else:
                    _resolve_plugin_path(root, screenshot, f"plugin.json screenshot {index}", errors)
        prompts = interface.get("defaultPrompt", [])
        if not isinstance(prompts, list) or not prompts:
            errors.append("plugin.json interface.defaultPrompt must be a non-empty list")
        elif len(prompts) > 3:
            errors.append("plugin.json interface.defaultPrompt may contain at most 3 entries")
        else:
            for index, prompt in enumerate(prompts):
                if not _nonempty_string(prompt) or len(prompt) > 128:
                    errors.append(f"plugin.json defaultPrompt {index} must be 1-128 characters")

    if "apps" in manifest:
        _resolve_plugin_path(root, manifest["apps"], "plugin.json apps", errors)
    validate_mcp(root, manifest, errors)

    try:
        raw_manifest = manifest_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        raw_manifest = ""
    if PLACEHOLDER_RE.search(raw_manifest):
        errors.append("plugin.json contains an unresolved placeholder")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("plugin", nargs="?", type=Path, default=PLUGIN_ROOT)
    args = parser.parse_args(argv)
    root = args.plugin.expanduser().resolve()
    errors = validate_root(root)
    if errors:
        for error in errors:
            print(f"plugin validation error: {error}", file=sys.stderr)
        return 1
    skills = sorted(path.name for path in (root / "skills").iterdir() if path.is_dir())
    print(
        f"Plugin validation passed: {root} "
        f"({len(skills)} skills; substantive local MCP declared)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
