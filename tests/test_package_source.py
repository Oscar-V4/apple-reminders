from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import audit_source_package  # noqa: E402
import build_source_package  # noqa: E402


# The slim package is about 701 KB. This leaves roughly 14% headroom, but not
# enough to silently reintroduce one of the removed, roughly 100 KB icon copies.
RELEASE_ARCHIVE_SIZE_BUDGET_BYTES = 800_000


class SourcePackagePolicyTests(unittest.TestCase):
    def test_real_source_package_allowlist_passes(self) -> None:
        result = audit_source_package.audit_source(ROOT)
        self.assertEqual(result.errors, ())
        self.assertGreater(len(result.files), 20)

    def test_public_release_documents_are_packaged_and_manifest_links_match(self) -> None:
        required = {
            Path("CHANGELOG.md"),
            Path("SECURITY.md"),
            Path("SUPPORT.md"),
            Path("TERMS.md"),
        }
        files, errors = audit_source_package.package_files(ROOT)
        self.assertEqual(errors, [])
        self.assertTrue(required.issubset(files))
        for relative in required:
            text = (ROOT / relative).read_text(encoding="utf-8")
            self.assertTrue(text.strip(), relative)
            self.assertNotIn("TODO", text)

        manifest = json.loads(
            (ROOT / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8")
        )
        self.assertEqual(
            manifest["interface"]["termsOfServiceURL"],
            "https://github.com/Oscar-V4/apple-reminders/blob/main/TERMS.md",
        )

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
            first = build_source_package.build_package(ROOT, base / "first")
            second = build_source_package.build_package(ROOT, base / "second")
            first_bytes = first.read_bytes()
            second_bytes = second.read_bytes()

            self.assertEqual(first_bytes, second_bytes)
            self.assertEqual(
                hashlib.sha256(first_bytes).hexdigest(),
                hashlib.sha256(second_bytes).hexdigest(),
            )
            self.assertEqual(audit_source_package.audit_archive(ROOT, first), [])

    def test_release_archive_stays_within_size_budget(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            archive = build_source_package.build_package(ROOT, Path(temp_dir))
            archive_size = archive.stat().st_size

        self.assertLessEqual(
            archive_size,
            RELEASE_ARCHIVE_SIZE_BUDGET_BYTES,
            "release archive exceeded its 800 KB budget; review package growth "
            "before raising the ceiling",
        )

    def test_archive_contains_only_runtime_allowlist(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            archive = build_source_package.build_package(ROOT, Path(temp_dir))
            manifest = json.loads(
                (ROOT / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8")
            )
            prefix = f"{manifest['name']}/"
            expected, errors = audit_source_package.package_files(ROOT)
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
            archive = build_source_package.build_package(ROOT, base / "build")
            with zipfile.ZipFile(archive) as handle:
                handle.extractall(base / "extracted")
            manifest = json.loads(
                (ROOT / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8")
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
