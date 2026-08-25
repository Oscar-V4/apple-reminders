from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_ROOT = REPO_ROOT / "plugins" / "apple-reminders"
SCRIPTS = REPO_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import audit_source_package  # noqa: E402
import build_source_package  # noqa: E402


# The deterministic allowlist is the primary content boundary. This hard ceiling
# catches gross package growth while leaving room for reviewed runtime modules;
# exact source/archive bytes remain visible in every benchmark result.
RELEASE_ARCHIVE_HARD_CEILING_BYTES = 1_048_576
PUBLIC_MCP_TOOL_NAMES = [
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
]


class SourcePackagePolicyTests(unittest.TestCase):
    def test_real_source_package_allowlist_passes(self) -> None:
        result = audit_source_package.audit_source(PLUGIN_ROOT)
        self.assertEqual(result.errors, ())
        self.assertGreater(len(result.files), 20)

    def test_marketplace_runtime_subtree_is_closed_and_dev_free(self) -> None:
        files, errors = audit_source_package.package_files(PLUGIN_ROOT)

        self.assertEqual(errors, [])
        self.assertEqual(
            audit_source_package.unallowlisted_runtime_files(PLUGIN_ROOT, files),
            [],
        )
        self.assertEqual(
            audit_source_package.scan_worktree_for_forbidden(PLUGIN_ROOT),
            [],
        )
        for excluded in ("tests", "docs", ".github", "screenshots", "dist"):
            self.assertFalse((PLUGIN_ROOT / excluded).exists(), excluded)

    def test_install_local_public_docs_match_canonical_github_docs(self) -> None:
        self.assertEqual(
            audit_source_package.validate_document_mirrors(REPO_ROOT, PLUGIN_ROOT),
            [],
        )

    def test_public_release_documents_are_packaged_and_manifest_links_match(self) -> None:
        required = {
            Path("CHANGELOG.md"),
            Path("SECURITY.md"),
            Path("SUPPORT.md"),
            Path("TERMS.md"),
        }
        files, errors = audit_source_package.package_files(PLUGIN_ROOT)
        self.assertEqual(errors, [])
        self.assertTrue(required.issubset(files))
        for relative in required:
            text = (PLUGIN_ROOT / relative).read_text(encoding="utf-8")
            self.assertTrue(text.strip(), relative)
            self.assertNotIn("TODO", text)

        manifest = json.loads(
            (PLUGIN_ROOT / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            manifest["interface"]["termsOfServiceURL"],
            "https://github.com/Oscar-V4/apple-reminders/blob/main/TERMS.md",
        )

    def test_public_runtime_modules_are_in_the_package(self) -> None:
        files, errors = audit_source_package.package_files(PLUGIN_ROOT)

        self.assertEqual(errors, [])
        self.assertTrue(
            {
                Path("mcp/v2_contract.py"),
                Path("mcp/v2_core.py"),
                Path("mcp/v2_core_backend.py"),
                Path("mcp/v2_diagnostics.py"),
                Path("mcp/v2_native.py"),
                Path("mcp/v2_native_backend.py"),
                Path("scripts/reminders_image_input.py"),
                Path("scripts/reminders_service.py"),
            }.issubset(files)
        )

    def test_extracted_package_initializes_and_lists_exact_public_tools(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            archive = build_source_package.build_package(PLUGIN_ROOT, base / "build")
            with zipfile.ZipFile(archive) as handle:
                handle.extractall(base / "extracted")
            manifest = json.loads(
                (PLUGIN_ROOT / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8")
            )
            plugin_root = base / "extracted" / manifest["name"]
            requests = [
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-11-25",
                        "capabilities": {},
                        "clientInfo": {"name": "package-test", "version": "1"},
                    },
                },
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/list",
                    "params": {},
                },
            ]
            completed = subprocess.run(
                [sys.executable, "./mcp/server.py"],
                cwd=plugin_root,
                input="".join(
                    json.dumps(request, separators=(",", ":")) + "\n"
                    for request in requests
                ),
                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=10,
                check=False,
            )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        responses = [json.loads(line) for line in completed.stdout.splitlines()]
        self.assertEqual(len(responses), 2, completed.stdout)
        self.assertEqual(responses[0]["result"]["serverInfo"]["version"], "0.3.0")
        tools = responses[1]["result"]["tools"]
        self.assertEqual([tool["name"] for tool in tools], PUBLIC_MCP_TOOL_NAMES)
        self.assertTrue(all("outputSchema" not in tool for tool in tools))

    def test_recursive_marketplace_source_copy_initializes_with_exact_public_tools(self) -> None:
        marketplace = json.loads(
            (REPO_ROOT / ".agents/plugins/marketplace.json").read_text(encoding="utf-8")
        )
        source_path = marketplace["plugins"][0]["source"]["path"]
        source = (REPO_ROOT / source_path).resolve()
        self.assertEqual(source, PLUGIN_ROOT.resolve())

        with tempfile.TemporaryDirectory() as temp_dir:
            installed = Path(temp_dir) / "apple-reminders"
            shutil.copytree(source, installed)
            requests = [
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-11-25",
                        "capabilities": {},
                        "clientInfo": {"name": "marketplace-copy-test", "version": "1"},
                    },
                },
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            ]
            completed = subprocess.run(
                [sys.executable, "./mcp/server.py"],
                cwd=installed,
                input="".join(
                    json.dumps(request, separators=(",", ":")) + "\n"
                    for request in requests
                ),
                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=10,
                check=False,
            )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        responses = [json.loads(line) for line in completed.stdout.splitlines()]
        tools = responses[1]["result"]["tools"]
        self.assertEqual([tool["name"] for tool in tools], PUBLIC_MCP_TOOL_NAMES)
        self.assertTrue(all("outputSchema" not in tool for tool in tools))

    def test_forbidden_artifacts_are_classified_by_path(self) -> None:
        cases = {
            Path(".DS_Store"),
            Path("scripts/__pycache__/adapter.pyc"),
            Path("screenshots/private.png"),
            Path("debug/Screenshot 2026-08-06.png"),
            Path("backup/reminders.sqlite-wal"),
            Path("backup/reminders.sqlite3"),
            Path("backup/reminders.sqlite-journal"),
            Path("reminders-container-backup-1.tgz"),
            Path("notes.backup"),
            Path("notes.orig"),
            Path("actions.jsonl"),
            Path("diagnostic.log"),
            Path("private.heic"),
            Path("screen-recording.mov"),
            Path("release.zip"),
            Path("release.dmg"),
            Path("assets/logo.png"),
            Path("assets/logo-dark.png"),
        }
        for path in cases:
            with self.subTest(path=path):
                self.assertIsNotNone(audit_source_package.forbidden_path_reason(path))

    def test_name_only_worktree_scan_detects_local_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "shots").mkdir()
            (root / "cache" / "__pycache__").mkdir(parents=True)
            (root / "shots" / "Screenshot private.png").write_bytes(b"")
            (root / "cache" / "__pycache__" / "module.pyc").write_bytes(b"")
            (root / "private.sqlite").write_bytes(b"")
            findings = audit_source_package.scan_worktree_for_forbidden(root)

        self.assertEqual(len(findings), 3)
        self.assertTrue(any("Screenshot" in finding for finding in findings))
        self.assertTrue(any("pyc" in finding for finding in findings))
        self.assertTrue(any("sqlite" in finding for finding in findings))

    def test_forbidden_skill_cache_is_reported_but_not_packaged(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            skill = root / "skills" / "example-skill"
            (skill / "agents").mkdir(parents=True)
            (skill / "evals").mkdir()
            (skill / "scripts" / "__pycache__").mkdir(parents=True)
            (skill / "SKILL.md").write_text("runtime", encoding="utf-8")
            (skill / "agents" / "openai.yaml").write_text("runtime", encoding="utf-8")
            (skill / "evals" / "evals.json").write_text("runtime", encoding="utf-8")
            cache = skill / "scripts" / "__pycache__" / "helper.pyc"
            cache.write_bytes(b"local bytecode")

            files, errors = audit_source_package.package_files(root)
            findings = audit_source_package.scan_worktree_for_forbidden(root)

        self.assertEqual(errors, [])
        self.assertNotIn(cache.relative_to(root), files)
        self.assertTrue(any("helper.pyc" in finding for finding in findings))

    def test_two_source_packages_are_byte_for_byte_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            first = build_source_package.build_package(PLUGIN_ROOT, base / "first")
            second = build_source_package.build_package(PLUGIN_ROOT, base / "second")
            first_bytes = first.read_bytes()
            second_bytes = second.read_bytes()

            self.assertEqual(first_bytes, second_bytes)
            self.assertEqual(
                hashlib.sha256(first_bytes).hexdigest(),
                hashlib.sha256(second_bytes).hexdigest(),
            )
            self.assertEqual(audit_source_package.audit_archive(PLUGIN_ROOT, first), [])

    def test_release_archive_stays_within_size_budget(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            archive = build_source_package.build_package(PLUGIN_ROOT, Path(temp_dir))
            archive_size = archive.stat().st_size

        self.assertLessEqual(
            archive_size,
            RELEASE_ARCHIVE_HARD_CEILING_BYTES,
            "release archive exceeded its 1 MiB hard ceiling; review runtime "
            "contents before raising the ceiling",
        )

    def test_archive_contains_only_runtime_allowlist(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            archive = build_source_package.build_package(PLUGIN_ROOT, Path(temp_dir))
            manifest = json.loads(
                (PLUGIN_ROOT / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8")
            )
            prefix = f"{manifest['name']}/"
            expected, errors = audit_source_package.package_files(PLUGIN_ROOT)
            self.assertEqual(errors, [])
            with zipfile.ZipFile(archive) as handle:
                members = set(handle.namelist())

        self.assertEqual(members, {prefix + path.as_posix() for path in expected})
        self.assertFalse(any("tests/" in member for member in members))
        self.assertFalse(any("minis/" in member for member in members))
        self.assertFalse(any(".github/" in member for member in members))
        self.assertFalse(any("audit_source_package.py" in member for member in members))
        self.assertEqual(
            {member for member in members if member.startswith(prefix + "assets/")},
            {prefix + "assets/icon.png"},
        )

    def test_packaged_server_ignores_all_backend_overrides_even_in_test_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            archive = build_source_package.build_package(PLUGIN_ROOT, base / "build")
            with zipfile.ZipFile(archive) as handle:
                handle.extractall(base / "extracted")
            manifest = json.loads(
                (PLUGIN_ROOT / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8")
            )
            plugin_root = base / "extracted" / manifest["name"]
            server = plugin_root / "mcp" / "server.py"
            overrides = {
                "APPLE_REMINDERS_ADAPTER_PATH": str(base / "outside-adapter.py"),
                "APPLE_REMINDERS_EVENTKIT_BRIDGE_PATH": str(base / "outside-eventkit.py"),
                "APPLE_REMINDERS_DOCTOR_PATH": str(base / "outside-doctor.py"),
                "APPLE_REMINDERS_MCP_TEST_MODE": "1",
                "PYTHONDONTWRITEBYTECODE": "1",
            }
            probe = (
                "import importlib.util,json,sys;"
                "from pathlib import Path;"
                "path=Path(sys.argv[1]);"
                "spec=importlib.util.spec_from_file_location('packaged_server',path);"
                "module=importlib.util.module_from_spec(spec);"
                "sys.modules[spec.name]=module;"
                "spec.loader.exec_module(module);"
                "print(json.dumps([str(module.adapter_path()),"
                "str(module.eventkit_bridge_path()),str(module.doctor_path())]))"
            )
            completed = subprocess.run(
                [sys.executable, "-c", probe, str(server)],
                cwd=plugin_root,
                env={**os.environ, **overrides},
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=10,
                check=False,
            )

        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertEqual(
            json.loads(completed.stdout),
            [
                str((plugin_root / "scripts" / "reminders_adapter.py").resolve()),
                str((plugin_root / "scripts" / "eventkit_bridge.py").resolve()),
                str((plugin_root / "scripts" / "reminders_doctor.py").resolve()),
            ],
        )


if __name__ == "__main__":
    unittest.main()
