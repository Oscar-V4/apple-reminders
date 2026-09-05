from __future__ import annotations

import ast
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

    def test_readme_separates_released_setup_from_development_behavior(self) -> None:
        readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
        setup = readme.split("## Get started in three steps", 1)[1].split("## Everyday use", 1)[0]
        self.assertIn("https://www.python.org/downloads/macos/", setup)
        self.assertIn("Python 3.11 or newer", setup)
        self.assertIn("codex plugin marketplace add Oscar-V4/apple-reminders --ref v0.5.2", setup)
        self.assertIn("codex plugin add apple-reminders@oscar-v4-reminders", setup)
        self.assertIn("new task", setup)
        self.assertNotIn("python3 -c", setup)
        self.assertNotIn("xcode-select", setup)
        self.assertNotIn("diagnose_reminders", setup)
        self.assertIn("Published v0.5.2", readme)
        self.assertIn("Unreleased development branch", readme)
        self.assertIn("9 Core and diagnostic tools", readme)
        self.assertIn("## Upgrade", readme)
        self.assertIn("## Uninstall", readme)
        self.assertIn("vX.Y.Z", readme)
        self.assertIn("PRIVACY.md#user-control", readme)
        self.assertEqual(readme, (PLUGIN_ROOT / "README.md").read_text(encoding="utf-8"))

    def test_public_release_version_is_coherent(self) -> None:
        manifest = json.loads(
            (PLUGIN_ROOT / ".codex-plugin" / "plugin.json").read_text(
                encoding="utf-8"
            )
        )
        version = manifest["version"]
        server_tree = ast.parse(
            (PLUGIN_ROOT / "mcp" / "server.py").read_text(encoding="utf-8")
        )
        server_versions = [
            node.value.value
            for node in server_tree.body
            if isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "SERVER_VERSION"
                for target in node.targets
            )
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ]
        readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
        changelog = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        decision = (
            REPO_ROOT
            / "docs"
            / "decisions"
            / "0019-prebuilt-signed-eventkit-core-helper.md"
        ).read_text(encoding="utf-8")

        self.assertEqual(server_versions, [version])
        self.assertIn(f"--ref v{version}", readme)
        self.assertIn(f"## {version} —", changelog)
        self.assertIn("implemented in 0.5.0", decision)

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

    def test_manifest_leads_with_realistic_public_workflows(self) -> None:
        manifest = json.loads(
            (PLUGIN_ROOT / ".codex-plugin" / "plugin.json").read_text(
                encoding="utf-8"
            )
        )
        interface = manifest["interface"]

        self.assertEqual(interface["displayName"], "Apple Reminders for Codex")
        self.assertIn("notes and screenshots", interface["shortDescription"])
        self.assertIn("meeting notes", interface["defaultPrompt"][0])
        self.assertIn("owners and deadlines", interface["defaultPrompt"][0])
        self.assertIn("screenshot", interface["defaultPrompt"][1])
        self.assertIn("useful details", interface["defaultPrompt"][1])
        self.assertIn("overdue", interface["defaultPrompt"][2])
        self.assertIn("without Xcode", interface["longDescription"])
        self.assertIn(
            "Tag assignments and native URL attachments",
            interface["longDescription"],
        )
        self.assertIn("avoid compilation", interface["longDescription"])
        self.assertIn(
            "Section writes, image-attachment changes",
            interface["longDescription"],
        )
        self.assertIn(
            "require Xcode Command Line Tools",
            interface["longDescription"],
        )
        self.assertNotIn(
            "Optional section, tag, attachment",
            interface["longDescription"],
        )
        for prompt in interface["defaultPrompt"]:
            normalized = prompt.casefold()
            for advanced_term in (
                "link",
                "section",
                "tag",
                "attachment",
                "recently deleted",
                "recover",
            ):
                with self.subTest(prompt=prompt, advanced_term=advanced_term):
                    self.assertNotIn(advanced_term, normalized)

    def test_issue_templates_match_the_current_public_product_boundaries(self) -> None:
        templates = REPO_ROOT / ".github" / "ISSUE_TEMPLATE"
        feature = (templates / "feature_request.yml").read_text(encoding="utf-8")
        bug = (templates / "bug_report.yml").read_text(encoding="utf-8")

        self.assertIn("existing 15 tools and five skills", feature)
        self.assertIn("        - Recovery", feature)
        self.assertNotIn("Optional Maintenance", feature)
        self.assertIn("Recently Deleted or recovery", bug)

    def test_native_syntax_checks_cover_every_runtime_helper_source(self) -> None:
        ci = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(
            encoding="utf-8"
        )
        contributing = (REPO_ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8")

        for source in (
            "remkit_attach_image.m",
            "remkit_sections.m",
            "remkit_recover.m",
            "reminders_eventkit.m",
        ):
            with self.subTest(source=source):
                self.assertIn(source, ci)
                self.assertIn(source, contributing)

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
