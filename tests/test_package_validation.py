from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = REPO_ROOT / "plugins" / "apple-reminders"
SCRIPTS = REPO_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import validate_plugin  # noqa: E402


class PluginValidationTests(unittest.TestCase):
    def test_real_plugin_manifest_mcp_skills_and_evals_validate(self) -> None:
        self.assertEqual(validate_plugin.validate_root(PLUGIN_ROOT), [])

    def test_repo_marketplace_exposes_the_runtime_subtree_with_install_policy(self) -> None:
        manifest = json.loads(
            (PLUGIN_ROOT / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8")
        )
        marketplace = json.loads(
            (REPO_ROOT / ".agents/plugins/marketplace.json").read_text(encoding="utf-8")
        )

        self.assertEqual(marketplace["name"], "oscar-v4-reminders")
        self.assertEqual(marketplace["interface"]["displayName"], "Oscar V4 Reminders")
        self.assertEqual(len(marketplace["plugins"]), 1)
        entry = marketplace["plugins"][0]
        self.assertEqual(entry["name"], manifest["name"])
        self.assertEqual(
            entry["source"],
            {"source": "local", "path": "./plugins/apple-reminders"},
        )
        self.assertEqual(
            entry["policy"],
            {"installation": "AVAILABLE", "authentication": "ON_INSTALL"},
        )
        self.assertEqual(entry["category"], "Productivity")

    def test_readme_has_a_doctor_free_install_upgrade_and_uninstall_path(self) -> None:
        readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
        quick_start = readme.split("## Quick Start", 1)[1].split("## First permission", 1)[0]

        self.assertIn(
            "codex plugin marketplace add Oscar-V4/apple-reminders --ref v0.4.0",
            quick_start,
        )
        self.assertIn("codex plugin add apple-reminders@oscar-v4-reminders", quick_start)
        self.assertIn("오늘 할 일 보여줘", quick_start)
        self.assertNotIn("doctor", quick_start.casefold())
        self.assertIn("## Upgrade", readme)
        upgrade = readme.split("## Upgrade", 1)[1].split(
            "## Temporarily disable", 1
        )[0]
        self.assertIn(
            "codex plugin remove apple-reminders@oscar-v4-reminders",
            upgrade,
        )
        self.assertIn(
            "codex plugin marketplace remove oscar-v4-reminders",
            upgrade,
        )
        self.assertIn(
            "codex plugin marketplace add Oscar-V4/apple-reminders --ref vX.Y.Z",
            upgrade,
        )
        self.assertIn(
            "codex plugin add apple-reminders@oscar-v4-reminders",
            upgrade,
        )
        self.assertNotIn("marketplace upgrade", upgrade)
        self.assertIn("## Uninstall", readme)
        self.assertIn("codex plugin remove apple-reminders@oscar-v4-reminders", readme)
        self.assertIn("macOS 14 or newer", readme)
        self.assertIn("Python 3.11 or newer", readme)

    def test_manifest_reuses_one_reviewed_brand_asset(self) -> None:
        manifest = json.loads(
            (PLUGIN_ROOT / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8")
        )
        interface = manifest["interface"]
        brand_paths = {
            interface["composerIcon"],
            interface["logo"],
            interface["logoDark"],
        }

        self.assertEqual(brand_paths, {"./assets/icon.png"})
        self.assertTrue((PLUGIN_ROOT / "assets" / "icon.png").is_file())
        self.assertFalse((PLUGIN_ROOT / "assets" / "logo.png").exists())
        self.assertFalse((PLUGIN_ROOT / "assets" / "logo-dark.png").exists())

    def test_manifest_declares_read_and_write_capabilities(self) -> None:
        manifest = json.loads(
            (PLUGIN_ROOT / ".codex-plugin" / "plugin.json").read_text(
                encoding="utf-8"
            )
        )

        self.assertEqual(
            manifest["interface"]["capabilities"],
            ["Interactive", "Read", "Write"],
        )

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

    def test_legacy_internal_routes_are_not_treated_as_public_tools(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir).resolve()
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
            (root / "mcp" / "v2_contract.py").write_text(
                (PLUGIN_ROOT / "mcp" / "v2_contract.py").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            (root / "schemas" / "mcp-tools.json").write_text(
                (PLUGIN_ROOT / "schemas" / "mcp-tools.json").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            errors: list[str] = []
            validate_plugin.validate_mcp(
                root,
                {"version": "0.1.0", "mcpServers": "./.mcp.json"},
                errors,
            )

        self.assertEqual(errors, [])

    def test_public_v2_runtime_contract_drift_is_rejected_statically(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir).resolve()
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
                + "# initialize tools/list tools/call\n"
                + ("x = 1\n" * 600),
                encoding="utf-8",
            )
            contract = (PLUGIN_ROOT / "mcp" / "v2_contract.py").read_text(encoding="utf-8")
            contract = contract.replace('        "diagnose_reminders",\n', "", 1)
            (root / "mcp" / "v2_contract.py").write_text(
                contract,
                encoding="utf-8",
            )
            (root / "schemas" / "mcp-tools.json").write_text(
                (PLUGIN_ROOT / "schemas" / "mcp-tools.json").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            errors: list[str] = []
            validate_plugin.validate_mcp(
                root,
                {"version": "0.1.0", "mcpServers": "./.mcp.json"},
                errors,
            )

        self.assertTrue(any("runtime contract drift" in error for error in errors))
        self.assertTrue(any("schema/runtime drift" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
