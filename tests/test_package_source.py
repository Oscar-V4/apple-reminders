from __future__ import annotations

import hashlib
import json
import os
import plistlib
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_RELATIVE_ROOT = Path("plugins/apple-reminders")
LIVE_PLUGIN_ROOT = REPO_ROOT / PLUGIN_RELATIVE_ROOT
SCRIPTS = REPO_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import audit_source_package  # noqa: E402
import build_source_package  # noqa: E402


# Exact runtime inventories and upstream/signature provenance bound binary
# content. Keep a separate source-overhead budget; signed interpreter capsules
# are accounted for by their validated actual sizes, not an ever-changing
# few-hundred-byte headroom calculation.
RELEASE_SOURCE_OVERHEAD_BYTES = 3 * 1024 * 1024
DEFAULT_MCP_TOOL_NAMES = [
    "request_reminders_access",
    "list_reminder_lists",
    "fetch_reminders",
    "read_reminder",
    "create_reminder",
    "change_reminder",
    "delete_reminder",
    "ensure_reminder_list",
    "diagnose_reminders",
]


def installed_mcp_command(plugin_root: Path) -> tuple[str, ...]:
    payload = json.loads((plugin_root / ".mcp.json").read_text(encoding="utf-8"))
    registered = payload["mcpServers"]["apple-reminders-local"]
    return (registered["command"], *registered["args"])


def copy_worktree_plugin_snapshot(
    destination: Path,
    *,
    repo_root: Path = REPO_ROOT,
    plugin_relative_root: Path = PLUGIN_RELATIVE_ROOT,
) -> Path:
    """Copy current, nonignored plugin paths into an isolated test input.

    Package tests exercise the real audit and builder against this isolated
    input. Ignored caches created by concurrent Python processes cannot race
    the tests. Tracked edits/deletions and new nonignored files remain visible
    before commit, so the snapshot still represents the current worktree.
    """

    completed = subprocess.run(
        [
            "git",
            "ls-files",
            "-z",
            "--cached",
            "--others",
            "--exclude-standard",
            "--",
            plugin_relative_root.as_posix(),
        ],
        cwd=repo_root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"could not enumerate worktree plugin files: {detail}")

    relative_paths = [
        Path(os.fsdecode(raw_path))
        for raw_path in completed.stdout.split(b"\0")
        if raw_path
    ]
    if not relative_paths:
        raise RuntimeError("worktree plugin snapshot is empty")

    for relative in relative_paths:
        source = repo_root / relative
        target = destination / relative
        if not source.exists() and not source.is_symlink():
            # git ls-files still reports an indexed path deleted in the
            # worktree. Omitting it makes the isolated input reflect deletion.
            continue
        if not source.is_file() and not source.is_symlink():
            raise RuntimeError(f"worktree plugin path is not a file: {relative}")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target, follow_symlinks=False)

    return destination / plugin_relative_root


class SourcePackagePolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        super().setUpClass()
        snapshot = tempfile.TemporaryDirectory(prefix="apple-reminders-package-tests-")
        try:
            cls.plugin_root = copy_worktree_plugin_snapshot(Path(snapshot.name))
        except BaseException:
            snapshot.cleanup()
            raise
        cls._source_snapshot = snapshot

    @classmethod
    def tearDownClass(cls) -> None:
        cls._source_snapshot.cleanup()
        super().tearDownClass()

    def test_extracted_manifest_does_not_probe_any_host_python(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            archive = build_source_package.build_package(self.plugin_root, base / "build")
            with zipfile.ZipFile(archive) as handle:
                handle.extractall(base / "extracted")
            manifest = json.loads(
                (self.plugin_root / ".codex-plugin" / "plugin.json").read_text(
                    encoding="utf-8"
                )
            )
            plugin_root = base / "extracted" / manifest["name"]

            old_bin = base / "old" / "bin"
            supported_bin = base / "supported" / "bin"
            old_bin.mkdir(parents=True)
            supported_bin.mkdir(parents=True)
            old_selected = base / "old-selected"
            old_python = old_bin / "python3"
            old_python.write_text(
                "#!/bin/sh\n"
                f"printf selected > {shlex.quote(str(old_selected))}\n"
                "if [ \"${1-}\" = \"-c\" ]; then exit 1; fi\n"
                "exit 97\n",
                encoding="utf-8",
            )
            old_python.chmod(0o755)
            selected = base / "selected"
            supported_python = supported_bin / "python3"
            supported_python.write_text(
                "#!/bin/sh\n"
                f"printf selected > {shlex.quote(str(selected))}\n"
                f"exec {shlex.quote(sys.executable)} \"$@\"\n",
                encoding="utf-8",
            )
            supported_python.chmod(0o755)
            requests = [
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-11-25",
                        "capabilities": {},
                        "clientInfo": {"name": "launcher-path-test", "version": "1"},
                    },
                },
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            ]
            completed = subprocess.run(
                installed_mcp_command(plugin_root),
                cwd=plugin_root,
                input="".join(json.dumps(request) + "\n" for request in requests),
                env={
                    **os.environ,
                    "PATH": f"{old_bin}:{supported_bin}:/usr/bin:/bin",
                    "PYTHONDONTWRITEBYTECODE": "1",
                },
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=10,
                check=False,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertFalse(old_selected.exists(), "launcher probed the old host Python")
            self.assertFalse(
                selected.is_file(),
                "launcher selected an external supported Python instead of its bundled runtime",
            )
            responses = [json.loads(line) for line in completed.stdout.splitlines()]
            self.assertEqual(len(responses[1]["result"]["tools"]), 9)

    def test_worktree_source_snapshot_allowlist_passes(self) -> None:
        result = audit_source_package.audit_source(self.plugin_root)
        self.assertEqual(result.errors, ())
        self.assertGreater(len(result.files), 20)

    def test_snapshot_preserves_current_nonignored_worktree_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            repo = base / "repo"
            relative_root = Path("plugins/example")
            plugin = repo / relative_root
            plugin.mkdir(parents=True)
            (plugin / ".gitignore").write_text("*.pyc\n", encoding="utf-8")
            tracked = plugin / "tracked.txt"
            deleted = plugin / "deleted.txt"
            tracked.write_text("indexed", encoding="utf-8")
            deleted.write_text("indexed", encoding="utf-8")
            initialized = subprocess.run(
                ["git", "init", "--quiet"],
                cwd=repo,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            added = subprocess.run(
                ["git", "add", relative_root.as_posix()],
                cwd=repo,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(initialized.returncode, 0, initialized.stderr)
            self.assertEqual(added.returncode, 0, added.stderr)

            tracked.write_text("current worktree", encoding="utf-8")
            deleted.unlink()
            (plugin / "new-runtime.txt").write_text("new", encoding="utf-8")
            (plugin / "ignored.pyc").write_bytes(b"cache")
            snapshot = copy_worktree_plugin_snapshot(
                base / "snapshot",
                repo_root=repo,
                plugin_relative_root=relative_root,
            )

            self.assertEqual(
                (snapshot / "tracked.txt").read_text(encoding="utf-8"),
                "current worktree",
            )
            self.assertFalse((snapshot / "deleted.txt").exists())
            self.assertEqual(
                (snapshot / "new-runtime.txt").read_text(encoding="utf-8"),
                "new",
            )
            self.assertFalse((snapshot / "ignored.pyc").exists())

    def test_marketplace_runtime_subtree_is_closed_and_dev_free(self) -> None:
        files, errors = audit_source_package.package_files(self.plugin_root)

        self.assertEqual(errors, [])
        self.assertEqual(
            audit_source_package.unallowlisted_runtime_files(self.plugin_root, files),
            [],
        )
        self.assertEqual(
            audit_source_package.scan_worktree_for_forbidden(self.plugin_root),
            [],
        )
        self.assertNotIn(Path("scripts/reminders_recovery.py"), files)
        for excluded in ("tests", "docs", ".github", "screenshots", "dist"):
            self.assertFalse((self.plugin_root / excluded).exists(), excluded)

    def test_install_local_public_docs_match_canonical_github_docs(self) -> None:
        self.assertEqual(
            audit_source_package.validate_document_mirrors(REPO_ROOT, self.plugin_root),
            [],
        )

    def test_public_release_documents_are_packaged_and_manifest_links_match(self) -> None:
        required = {
            Path("CHANGELOG.md"),
            Path("SECURITY.md"),
            Path("SUPPORT.md"),
            Path("TERMS.md"),
        }
        files, errors = audit_source_package.package_files(self.plugin_root)
        self.assertEqual(errors, [])
        self.assertTrue(required.issubset(files))
        for relative in required:
            text = (self.plugin_root / relative).read_text(encoding="utf-8")
            self.assertTrue(text.strip(), relative)
            self.assertNotIn("TODO", text)

        manifest = json.loads(
            (self.plugin_root / ".codex-plugin" / "plugin.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(
            manifest["interface"]["termsOfServiceURL"],
            "https://github.com/Oscar-V4/apple-reminders/blob/main/TERMS.md",
        )

    def test_public_runtime_modules_are_in_the_package(self) -> None:
        files, errors = audit_source_package.package_files(self.plugin_root)

        self.assertEqual(errors, [])
        self.assertTrue(
            {
                Path("mcp/v2_contract.py"),
                Path("mcp/v2_core.py"),
                Path("mcp/v2_core_backend.py"),
                Path("mcp/v2_diagnostics.py"),
                Path("mcp/v2_native.py"),
                Path("mcp/v2_native_backend.py"),
                Path("mcp/v2_recovery.py"),
                Path("mcp/v2_recovery_backend.py"),
                Path("mcp/v2_transport.py"),
                Path("scripts/bounded_process.py"),
                Path("scripts/durable_idempotency.py"),
                Path("scripts/eventkit_protocol.py"),
                Path("scripts/experimental_capabilities.py"),
                Path("scripts/reminders_image_input.py"),
                Path("scripts/remkit_recover.m"),
                Path("scripts/reminders_service.py"),
            }.issubset(files)
        )

    def test_extracted_package_initializes_and_lists_exact_public_tools(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            archive = build_source_package.build_package(self.plugin_root, base / "build")
            with zipfile.ZipFile(archive) as handle:
                handle.extractall(base / "extracted")
            manifest = json.loads(
                (self.plugin_root / ".codex-plugin" / "plugin.json").read_text(
                    encoding="utf-8"
                )
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
                installed_mcp_command(plugin_root),
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
        self.assertEqual(responses[0]["result"]["serverInfo"]["version"], manifest["version"])
        tools = responses[1]["result"]["tools"]
        self.assertEqual([tool["name"] for tool in tools], DEFAULT_MCP_TOOL_NAMES)
        self.assertTrue(all("outputSchema" not in tool for tool in tools))

    def test_extracted_package_constructs_core_without_giant_runtime_imports(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            archive = build_source_package.build_package(self.plugin_root, base / "build")
            with zipfile.ZipFile(archive) as handle:
                handle.extractall(base / "extracted")
            manifest = json.loads(
                (self.plugin_root / ".codex-plugin" / "plugin.json").read_text(
                    encoding="utf-8"
                )
            )
            plugin_root = base / "extracted" / manifest["name"]
            server = plugin_root / "mcp" / "server.py"
            probe = (
                "import importlib.util,json,sys;"
                "from pathlib import Path;"
                "path=Path(sys.argv[1]);"
                "spec=importlib.util.spec_from_file_location('packaged_core_probe',path);"
                "module=importlib.util.module_from_spec(spec);"
                "sys.modules[spec.name]=module;"
                "spec.loader.exec_module(module);"
                "facade=module._LocalToolDispatch(module.DEFAULT_BACKEND_PATHS).core_facade();"
                "adapter_loaded=any(Path(str(getattr(item,'__file__',''))).name=="
                "'reminders_adapter.py' for item in sys.modules.values());"
                "bridge_loaded=any(Path(str(getattr(item,'__file__',''))).name=="
                "'eventkit_bridge.py' for item in sys.modules.values());"
                "protocol_loaded=any(Path(str(getattr(item,'__file__',''))).name=="
                "'eventkit_protocol.py' for item in sys.modules.values());"
                "print(json.dumps({'facade':type(facade).__name__,"
                "'adapter_loaded':adapter_loaded,'bridge_loaded':bridge_loaded,"
                "'protocol_loaded':protocol_loaded}))"
            )
            core_probe = subprocess.run(
                [sys.executable, "-c", probe, str(server)],
                cwd=plugin_root,
                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=10,
                check=False,
            )
            adapter_help = subprocess.run(
                [
                    sys.executable,
                    str(plugin_root / "scripts" / "reminders_adapter.py"),
                    "--help",
                ],
                cwd=plugin_root,
                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=10,
                check=False,
            )

        self.assertEqual(core_probe.returncode, 0, core_probe.stderr)
        self.assertEqual(
            json.loads(core_probe.stdout),
            {
                "facade": "V2CoreFacade",
                "adapter_loaded": False,
                "bridge_loaded": False,
                "protocol_loaded": True,
            },
        )
        self.assertEqual(adapter_help.returncode, 0, adapter_help.stderr)
        self.assertIn("recover_deleted_reminder", adapter_help.stdout)
        self.assertIn("replace_attachment", adapter_help.stdout)

    def test_recursive_marketplace_source_copy_initializes_with_exact_public_tools(self) -> None:
        marketplace = json.loads(
            (REPO_ROOT / ".agents/plugins/marketplace.json").read_text(encoding="utf-8")
        )
        source_path = marketplace["plugins"][0]["source"]["path"]
        source = (REPO_ROOT / source_path).resolve()
        self.assertEqual(source, LIVE_PLUGIN_ROOT.resolve())

        with tempfile.TemporaryDirectory() as temp_dir:
            installed = Path(temp_dir) / "apple-reminders"
            shutil.copytree(self.plugin_root, installed)
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
                installed_mcp_command(installed),
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
        self.assertEqual([tool["name"] for tool in tools], DEFAULT_MCP_TOOL_NAMES)
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

    def test_private_home_path_error_never_echoes_detected_value(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            relative = Path("README.md")
            private_path = "/Users/fixture-user/private.txt"
            (root / relative).write_text(
                f"private source: {private_path}\n",
                encoding="utf-8",
            )
            errors: list[str] = []
            audit_source_package._validate_file(root, relative, errors)

        self.assertEqual(errors, [f"absolute user-home path found in {relative}"])
        self.assertNotIn(private_path, "\n".join(errors))

    def test_local_environment_files_are_ignored_but_examples_are_reviewable(self) -> None:
        for path in (".env", ".env.local", ".env.development"):
            with self.subTest(path=path):
                completed = subprocess.run(
                    ["git", "check-ignore", "--no-index", "--quiet", path],
                    cwd=REPO_ROOT,
                    check=False,
                )
                self.assertEqual(completed.returncode, 0)
        for path in (".env.example", ".env.sample"):
            with self.subTest(path=path):
                completed = subprocess.run(
                    ["git", "check-ignore", "--no-index", "--quiet", path],
                    cwd=REPO_ROOT,
                    check=False,
                )
                self.assertEqual(completed.returncode, 1)

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

    def test_release_audit_and_builder_still_reject_ignored_runtime_cache(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            polluted = base / "apple-reminders"
            shutil.copytree(self.plugin_root, polluted)
            cache = polluted / "scripts" / "__pycache__" / "module.pyc"
            cache.parent.mkdir()
            cache.write_bytes(b"local bytecode")

            normal = audit_source_package.audit_source(polluted)
            strict = audit_source_package.audit_source(
                polluted,
                strict_worktree=True,
            )
            with self.assertRaises(RuntimeError) as raised:
                build_source_package.build_package(polluted, base / "build")

        relative = cache.relative_to(polluted).as_posix()
        self.assertTrue(
            any(relative in error for error in normal.errors),
            normal.errors,
        )
        self.assertTrue(
            any(relative in error for error in strict.errors),
            strict.errors,
        )
        self.assertIn(relative, str(raised.exception))

    def test_two_source_packages_are_byte_for_byte_deterministic(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            first = build_source_package.build_package(self.plugin_root, base / "first")
            second = build_source_package.build_package(self.plugin_root, base / "second")
            first_bytes = first.read_bytes()
            second_bytes = second.read_bytes()

            self.assertEqual(first_bytes, second_bytes)
            self.assertEqual(
                hashlib.sha256(first_bytes).hexdigest(),
                hashlib.sha256(second_bytes).hexdigest(),
            )
            self.assertEqual(
                audit_source_package.audit_archive(self.plugin_root, first),
                [],
            )

    def test_reviewed_native_helper_is_bound_and_keeps_its_execute_bit(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            base = Path(temp_dir)
            plugin = base / "apple-reminders"
            shutil.copytree(self.plugin_root, plugin)
            shutil.rmtree(plugin / "native")
            app = plugin / audit_source_package.NATIVE_HELPER_APP
            executable = plugin / audit_source_package.NATIVE_HELPER_EXECUTABLE
            info = app / "Contents" / "Info.plist"
            signature = app / "Contents" / "_CodeSignature" / "CodeResources"
            ticket = app / "Contents" / "CodeResources"
            executable.parent.mkdir(parents=True)
            signature.parent.mkdir(parents=True)

            info_payload = plistlib.loads(
                (REPO_ROOT / "scripts" / "eventkit_helper_app_info.plist").read_bytes()
            )
            fixture_version = json.loads((plugin / ".codex-plugin/plugin.json").read_text())["version"]
            info_payload["CFBundleShortVersionString"] = fixture_version
            info_payload["CFBundleVersion"] = fixture_version
            info.write_bytes(plistlib.dumps(info_payload, sort_keys=True))
            executable.write_bytes(b"\xca\xfe\xba\xbe" + b"universal-helper")
            executable.chmod(0o755)
            signature.write_bytes(b"reviewed-signature")
            ticket.write_bytes(b"reviewed-notary-ticket")

            app_members = audit_source_package.NATIVE_HELPER_FILES - {
                audit_source_package.NATIVE_HELPER_MANIFEST
            }
            source_files = {
                relative: hashlib.sha256((plugin / relative).read_bytes()).hexdigest()
                for relative in sorted(audit_source_package.NATIVE_HELPER_SOURCE_FILES)
            }
            build_inputs = {
                relative: hashlib.sha256((REPO_ROOT / relative).read_bytes()).hexdigest()
                for relative in sorted(
                    audit_source_package.NATIVE_HELPER_BUILD_INPUT_FILES
                )
            }
            self.assertIn(
                ".github/workflows/prepare-signed-helper-source.yml",
                build_inputs,
            )
            self.assertNotIn(
                ".github/workflows/prepare-signed-helper.yml",
                build_inputs,
            )
            app_files = {
                relative.relative_to("native").as_posix(): hashlib.sha256(
                    (plugin / relative).read_bytes()
                ).hexdigest()
                for relative in sorted(app_members, key=lambda item: item.as_posix())
            }
            manifest = {
                "schema_version": 1,
                "source_commit": "a" * 40,
                "workflow_commit": "c" * 40,
                "source_files": source_files,
                "build_inputs": build_inputs,
                "build_environment": {
                    "clang": "fixture clang",
                    "linker": "fixture linker",
                    "macos_sdk": "14.0",
                    "xcode_path": "/Applications/Xcode.app/Contents/Developer",
                },
                "app_name": "AppleRemindersEventKitHelper.app",
                "app_files": app_files,
                "architectures": ["arm64", "x86_64"],
                "binary_sha256": hashlib.sha256(executable.read_bytes()).hexdigest(),
                "bundle_identifier": "io.github.oscar-v4.apple-reminders.eventkit-bridge",
                "executable": "apple-reminders-eventkit-helper",
                "minimum_macos": "14.0",
                "minimum_macos_by_architecture": {
                    "arm64": "14.0",
                    "x86_64": "14.0",
                },
                "notarization_checked": True,
                "notarized": True,
                "plugin_version": fixture_version,
                "signature": "developer-id",
                "team_id": "V8347N9346",
            }
            manifest_path = plugin / audit_source_package.NATIVE_HELPER_MANIFEST
            manifest_path.write_text(
                json.dumps(manifest, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )

            archive = build_source_package.build_package(plugin, base / "build")
            package_name = json.loads(
                (plugin / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8")
            )["name"]
            executable_member = (
                f"{package_name}/{audit_source_package.NATIVE_HELPER_EXECUTABLE.as_posix()}"
            )
            with zipfile.ZipFile(archive) as handle:
                info_by_name = {item.filename: item for item in handle.infolist()}

            manifest["unexpected"] = True
            manifest_path.write_text(
                json.dumps(manifest, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            extra_key_errors = audit_source_package.audit_source(plugin).errors
            manifest.pop("unexpected")
            manifest["minimum_macos_by_architecture"]["x86_64"] = "13.0"
            manifest_path.write_text(
                json.dumps(manifest, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            minos_errors = audit_source_package.audit_source(plugin).errors
            manifest["minimum_macos_by_architecture"]["x86_64"] = "14.0"
            manifest.pop("workflow_commit")
            manifest_path.write_text(
                json.dumps(manifest, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            missing_workflow_commit_errors = audit_source_package.audit_source(
                plugin
            ).errors

        self.assertEqual(
            info_by_name[executable_member].external_attr >> 16,
            0o100755,
        )
        self.assertIn(
            "native helper manifest top-level key inventory drift",
            extra_key_errors,
        )
        self.assertIn(
            "native helper manifest minimum_macos_by_architecture drift",
            minos_errors,
        )
        self.assertIn(
            "native helper manifest workflow_commit is not a full commit hash",
            missing_workflow_commit_errors,
        )

    def test_incomplete_native_helper_tree_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            plugin = Path(temp_dir) / "apple-reminders"
            shutil.copytree(self.plugin_root, plugin)
            shutil.rmtree(plugin / "native")
            (plugin / "native").mkdir()

            result = audit_source_package.audit_source(plugin)

        self.assertFalse(result.ok)
        self.assertTrue(
            any("native helper manifest" in error for error in result.errors),
            result.errors,
        )

    def test_release_archive_stays_within_size_budget(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            archive = build_source_package.build_package(self.plugin_root, Path(temp_dir))
            archive_size = archive.stat().st_size

        self.assertLessEqual(
            archive_size,
            RELEASE_SOURCE_OVERHEAD_BYTES + sum(
                path.stat().st_size for path in (self.plugin_root / "runtime").glob("*")
                if path.is_file()
            ),
            "release archive exceeded its 1.769 MiB hard ceiling; review runtime "
            "contents before raising the ceiling",
        )

    def test_archive_contains_only_runtime_allowlist(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            archive = build_source_package.build_package(self.plugin_root, Path(temp_dir))
            manifest = json.loads(
                (self.plugin_root / ".codex-plugin" / "plugin.json").read_text(
                    encoding="utf-8"
                )
            )
            prefix = f"{manifest['name']}/"
            expected, errors = audit_source_package.package_files(self.plugin_root)
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
            archive = build_source_package.build_package(self.plugin_root, base / "build")
            with zipfile.ZipFile(archive) as handle:
                handle.extractall(base / "extracted")
            manifest = json.loads(
                (self.plugin_root / ".codex-plugin" / "plugin.json").read_text(
                    encoding="utf-8"
                )
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
                "paths=module.DEFAULT_BACKEND_PATHS;"
                "print(json.dumps([str(paths.adapter),str(paths.eventkit_bridge),"
                "str(paths.doctor)]))"
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
