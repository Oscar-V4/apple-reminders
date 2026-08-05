from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import validate_plugin  # noqa: E402


class PluginValidationTests(unittest.TestCase):
    def test_real_plugin_manifest_mcp_skills_and_evals_validate(self) -> None:
        self.assertEqual(validate_plugin.validate_root(ROOT), [])

    def test_manifest_reuses_one_reviewed_brand_asset(self) -> None:
        manifest = json.loads(
            (ROOT / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8")
        )
        interface = manifest["interface"]
        brand_paths = {
            interface["composerIcon"],
            interface["logo"],
            interface["logoDark"],
        }

        self.assertEqual(brand_paths, {"./assets/icon.png"})
        self.assertTrue((ROOT / "assets" / "icon.png").is_file())
        self.assertFalse((ROOT / "assets" / "logo.png").exists())
        self.assertFalse((ROOT / "assets" / "logo-dark.png").exists())

    def test_substantive_mcp_requires_manifest_declaration(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "assets").mkdir()
            (root / "mcp").mkdir()
            (root / "schemas").mkdir()
            (root / "assets" / "icon.png").write_bytes(b"brand")
            (root / ".mcp.json").write_text(
                json.dumps(
                    {
                        "mcpServers": {
                            "apple-reminders-local": {
                                "title": "Apple Reminders",
                                "description": "Local stdio tools",
                                "cwd": ".",
                                "command": "python3",
                                "args": ["./mcp/server.py"],
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            (root / "mcp" / "server.py").write_text(
                'SERVER_VERSION = "0.1.0"\n'
                + "# initialize tools/list tools/call\n"
                + ("x = 1\n" * 600),
                encoding="utf-8",
            )
            (root / "schemas" / "mcp-tools.json").write_text(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "tools": [
                            {
                                "name": "list_reminders",
                                "inputSchema": {
                                    "type": "object",
                                    "additionalProperties": False,
                                },
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            errors: list[str] = []
            validate_plugin.validate_mcp(root, {"version": "0.1.0"}, errors)

        self.assertTrue(any("must declare mcpServers" in error for error in errors))

    def test_mcp_plugin_semantic_core_version_drift_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "mcp").mkdir()
            (root / "schemas").mkdir()
            (root / ".mcp.json").write_text(
                json.dumps(
                    {
                        "mcpServers": {
                            "apple-reminders-local": {
                                "title": "Apple Reminders",
                                "description": "Local stdio tools",
                                "cwd": ".",
                                "command": "python3",
                                "args": ["./mcp/server.py"],
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            (root / "mcp" / "server.py").write_text(
                'SERVER_VERSION = "9.0.0"\n'
                + "# initialize tools/list tools/call\n"
                + ("x = 1\n" * 600),
                encoding="utf-8",
            )
            (root / "schemas" / "mcp-tools.json").write_text(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "tools": [
                            {
                                "name": "list_reminders",
                                "inputSchema": {
                                    "type": "object",
                                    "additionalProperties": False,
                                },
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            errors: list[str] = []
            validate_plugin.validate_mcp(
                root,
                {"version": "0.1.0", "mcpServers": "./.mcp.json"},
                errors,
            )

        self.assertTrue(any("version drift" in error for error in errors))

    def test_mcp_route_and_tool_schema_drift_is_rejected_statically(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "mcp").mkdir()
            (root / "schemas").mkdir()
            (root / ".mcp.json").write_text(
                json.dumps(
                    {
                        "mcpServers": {
                            "apple-reminders-local": {
                                "title": "Apple Reminders",
                                "description": "Local stdio tools",
                                "cwd": ".",
                                "command": "python3",
                                "args": ["./mcp/server.py"],
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            (root / "mcp" / "server.py").write_text(
                'SERVER_VERSION = "0.1.0"\n'
                'ROUTES = {"declared_route": object()}\n'
                "EVENTKIT_READ_ROUTES = {}\n"
                "EVENTKIT_CONTROL_ROUTES = {}\n"
                "EVENTKIT_MUTATION_ROUTES = {}\n"
                "DOCTOR_TOOLS = set()\n"
                + "# initialize tools/list tools/call\n"
                + ("x = 1\n" * 600),
                encoding="utf-8",
            )
            (root / "schemas" / "mcp-tools.json").write_text(
                json.dumps(
                    {
                        "schemaVersion": 1,
                        "tools": [
                            {
                                "name": "different_tool",
                                "inputSchema": {
                                    "type": "object",
                                    "additionalProperties": False,
                                },
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            errors: list[str] = []
            validate_plugin.validate_mcp(
                root,
                {"version": "0.1.0", "mcpServers": "./.mcp.json"},
                errors,
            )

        self.assertTrue(any("route/schema drift" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
