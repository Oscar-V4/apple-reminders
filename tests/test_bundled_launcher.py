from __future__ import annotations

import hashlib
import json
import os
import platform
import shlex
import stat
import struct
import subprocess
import sys
import tempfile
import unittest
import warnings
import zipfile
from pathlib import Path


LAUNCHER = (
    Path(__file__).resolve().parents[1]
    / "plugins/apple-reminders/scripts/launch_bundled_mcp.sh"
)
APP_NAME = "AppleRemindersPythonRuntime.app"
EXECUTABLE = "Contents/MacOS/apple-reminders-python"


def python_fixture_source(body: str) -> str:
    # A shebang cannot quote an interpreter path containing spaces. The shell
    # executes this preamble; Python sees it as a harmless module docstring.
    # -B also keeps fixture imports from changing a signed bundled interpreter.
    return (
        "#!/bin/sh\n"
        f"'''exec' {shlex.quote(sys.executable)} -B \"$0\" \"$@\"\n"
        "' '''\n"
        + body
    )


@unittest.skipUnless(platform.system() == "Darwin", "uses the supported macOS stock utilities")
class BundledLauncherTests(unittest.TestCase):
    """Synthetic capsules only: never start MCP or access a Reminders store.

    Only private copies of the launcher's fixed tool paths are replaced. No
    production test switches, interpreter overrides, or PATH discovery exist.
    Actual macOS hashing, ZIP inspection/extraction and atomic publication run here;
    these tests are not signing, notarization, or clean-Mac release evidence.
    """

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="reminders bundled launcher ")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.plugin = self.root / "plugin with spaces"
        self.home = self.root / "home with spaces"
        self.home.mkdir()
        (self.plugin / "scripts").mkdir(parents=True)
        (self.plugin / "mcp").mkdir()
        (self.plugin / "mcp/server.py").write_text(
            "raise RuntimeError('The synthetic launcher must not execute MCP')\n",
            encoding="utf-8",
        )
        renderer = self.plugin / "skills/apple-reminders-daily-brief/scripts/render_daily_brief.py"
        renderer.parent.mkdir(parents=True)
        renderer.write_text("raise RuntimeError('The fixture must not execute the real renderer')\n")
        self.runtime = self.plugin / "runtime"
        self.runtime.mkdir()
        self.config_path = self.root / "tool-config.json"
        self.calls_path = self.root / "tool-calls.jsonl"
        self.config = {"architecture": "arm64", "system": "Darwin", "version": "14.0"}
        self.save_config()
        self.fixture_files: dict[str, dict[str, bytes]] = {}
        for architecture in ("arm64", "x86_64"):
            self.write_capsule(architecture)
        self.write_checksums()

        # These stubs are isolated fixtures for platform answers and trust
        # decisions. They execute only data-free fixture code, using the test
        # runner's explicit Python path rather than the launcher's environment.
        tool_source = python_fixture_source(f"""import hashlib, json, os, pathlib, signal, subprocess, sys, time
config = json.loads(pathlib.Path({str(self.config_path)!r}).read_text())
tool = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
with open({str(self.calls_path)!r}, 'a') as log:
    log.write(json.dumps({{'tool': tool, 'args': args}}) + '\\n')
if tool == 'uname':
    print(config['system'] if args == ['-s'] else config['architecture'])
elif tool == 'sw_vers':
    assert args == ['-productVersion'], args
    print(config['version'])
elif tool == 'stat':
    if args == ['-f', '%u', config.get('foreign_owner')]:
        print(os.getuid() + 1)
    else:
        raise SystemExit(subprocess.call(['/usr/bin/stat', *args]))
elif tool == 'codesign':
    if args[:2] == ['--display', '--verbose=4']:
        assert len(args) == 3, args
        app = pathlib.Path(args[-1])
        expected = json.loads(pathlib.Path({str(self.root / 'expected-files.json')!r}).read_text())
        actual = {{str(p.relative_to(app)): hashlib.sha256(p.read_bytes()).hexdigest()
                  for p in app.rglob('*') if p.is_file()}}
        architecture = next(arch for arch, files in expected.items() if files == actual)
        cdhash = config.get('display_cdhash', hashlib.sha256(('synthetic signed ' + architecture).encode()).hexdigest()[:40])
        print('Executable=' + str(app / {EXECUTABLE!r}), file=sys.stderr)
        for value in config.get('display_cdhash_lines', [cdhash]):
            print('CDHash=' + value, file=sys.stderr)
        raise SystemExit(0)
    required = ['--verify', '--deep', '--strict', '--all-architectures', '--test-requirement']
    assert args[:5] == required, args
    assert args[5].startswith('='), 'codesign treats unprefixed requirements as file paths'
    assert 'anchor apple generic' in args[5], args
    assert 'io.github.oscar-v4.apple-reminders.python-runtime' in args[5], args
    assert 'V8347N9346' in args[5], args
    assert 'certificate leaf[field.1.2.840.113635.100.6.1.13] exists' in args[5], args
    if config.get('signature_failure'):
        raise SystemExit(1)
    app = pathlib.Path(args[-1])
    expected = json.loads(pathlib.Path({str(self.root / 'expected-files.json')!r}).read_text())
    actual = {{str(p.relative_to(app)): hashlib.sha256(p.read_bytes()).hexdigest()
              for p in app.rglob('*') if p.is_file()}}
    assert actual in expected.values(), 'runtime contents changed'
elif tool == 'spctl':
    assert args[:3] == ['--assess', '--type', 'execute'], args
    raise SystemExit(config.get('gatekeeper_failure', 0))
elif tool == 'ditto':
    assert len(args) == 5 and args[:3] == ['-x', '-k', '--qtn'], args
    time.sleep(config.get('extraction_delay', 0))
    if config.get('extraction_failure'):
        raise SystemExit(1)
    raise SystemExit(subprocess.call(['/usr/bin/ditto', *args]))
elif tool == 'link':
    assert len(args) == 2, args
    if config.get('publication_signal') == 'before':
        os.kill(os.getppid(), signal.SIGTERM)
        raise SystemExit(1)
    if config.get('publication_failure'):
        raise SystemExit(1)
    status = subprocess.call(['/bin/link', *args], stderr=subprocess.DEVNULL)
    if status == 0 and config.get('publication_signal') == 'after':
        os.kill(os.getppid(), signal.SIGTERM)
    raise SystemExit(status)
else:
    raise AssertionError(tool)
""")
        source = LAUNCHER.read_text(encoding="utf-8")
        tools = self.root / "private fixed tools"
        tools.mkdir()
        for original in (
            "/usr/bin/uname", "/usr/bin/sw_vers", "/usr/bin/stat",
            "/usr/bin/codesign", "/usr/sbin/spctl", "/usr/bin/ditto",
            "/bin/link",
        ):
            path = tools / Path(original).name
            path.write_text(tool_source, encoding="utf-8")
            path.chmod(0o755)
            self.assertIn(original, source)
            source = source.replace(original, shlex.quote(str(path)))
        self.launcher = self.plugin / "scripts/launch_bundled_mcp.sh"
        self.launcher.write_text(source, encoding="utf-8")
        self.launcher.chmod(0o755)

    def save_config(self, **updates: object) -> None:
        self.config.update(updates)
        self.config_path.write_text(json.dumps(self.config), encoding="utf-8")

    def write_capsule(
        self, architecture: str = "arm64", *,
        extra_entries: list[tuple[str, int, bytes]] | None = None,
    ) -> Path:
        shim = python_fixture_source(
            "import json, os, sys\n"
            f"result = {{'architecture': {architecture!r}, 'server': sys.argv[1], "
            "'args': sys.argv[2:], 'no_bytecode': os.environ.get('PYTHONDONTWRITEBYTECODE')}\n"
            "if sys.argv[1].endswith('/render_daily_brief.py'): result['stdin'] = sys.stdin.read()\n"
            "print(json.dumps(result))\n"
        ).encode()
        files = {
            EXECUTABLE: shim,
            "Contents/Info.plist": b"synthetic Info.plist: never a real app",
            "Contents/Resources/python/lib/data.txt": b"nested sealed resource",
        }
        self.fixture_files[architecture] = files
        expected = {
            arch: {name: hashlib.sha256(data).hexdigest() for name, data in entries.items()}
            for arch, entries in self.fixture_files.items()
        }
        (self.root / "expected-files.json").write_text(json.dumps(expected), encoding="utf-8")
        directories = {APP_NAME}
        for name in files:
            directories.update(str(parent) for parent in (Path(APP_NAME) / name).parents if str(parent) != ".")
        entries = [(name + "/", stat.S_IFDIR | 0o755, b"") for name in sorted(directories)]
        entries += [
            (f"{APP_NAME}/{name}", stat.S_IFREG | (0o755 if name == EXECUTABLE else 0o644), content)
            for name, content in sorted(files.items())
        ]
        entries += extra_entries or []
        archive = self.runtime / f"python-runtime-macos-{architecture}.zip"
        with warnings.catch_warnings(), zipfile.ZipFile(archive, "w") as handle:
            warnings.simplefilter("ignore", UserWarning)
            for name, mode, data in entries:
                info = zipfile.ZipInfo(name)
                info.create_system = 3
                info.external_attr = mode << 16
                info.compress_type = zipfile.ZIP_DEFLATED
                handle.writestr(info, data)
        (self.runtime / f"python-runtime-build-{architecture}.json").write_text(
            json.dumps({
                "architecture": architecture, "synthetic_fixture": True,
                "code_directory_hash": hashlib.sha256(f"synthetic signed {architecture}".encode()).hexdigest()[:40],
            }) + "\n",
            encoding="utf-8",
        )
        return archive

    def write_checksums(self) -> None:
        contents = "".join(
            f"{hashlib.sha256(path.read_bytes()).hexdigest()}  {path.name}\n"
            for path in sorted(self.runtime.iterdir())
            if path.name != "SHA256SUMS"
        )
        (self.runtime / "SHA256SUMS").write_text(contents, encoding="utf-8")

    def calls(self, tool: str) -> list[dict[str, object]]:
        if not self.calls_path.exists():
            return []
        return [item for line in self.calls_path.read_text().splitlines()
                if (item := json.loads(line))["tool"] == tool]

    def signature_verifications(self) -> list[dict[str, object]]:
        return [call for call in self.calls("codesign") if call["args"][0] == "--verify"]

    def environment(self, **extra: str) -> dict[str, str]:
        return {"HOME": str(self.home), "PATH": "", **extra}

    def launch(self, *arguments: str, env: dict[str, str] | None = None, input_text: str = "") -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["/bin/sh", str(self.launcher), *arguments], cwd=self.plugin,
            env=env or self.environment(), input=input_text, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=35, check=False,
        )

    def cache(self, architecture: str = "arm64") -> Path:
        archive = self.runtime / f"python-runtime-macos-{architecture}.zip"
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        return self.home / "Library/Caches/apple-reminders-codex/python-runtime" / digest

    def instance(self) -> Path:
        return self.cache() / (self.cache() / "ready").read_text().strip()

    def cached_app(self) -> Path:
        return self.instance() / APP_NAME

    def assert_success(self, result: subprocess.CompletedProcess[str], architecture: str = "arm64", arguments: tuple[str, ...] = (), *, renderer: bool = False, input_text: str = "") -> None:
        self.assertEqual(result.returncode, 0, result.stderr)
        expected = {
            "architecture": architecture,
            "server": str(self.plugin / ("skills/apple-reminders-daily-brief/scripts/render_daily_brief.py" if renderer else "mcp/server.py")),
            "args": list(arguments), "no_bytecode": "1",
        }
        if renderer:
            expected["stdin"] = input_text
        self.assertEqual(json.loads(result.stdout), expected)
        self.assertEqual(result.stderr, "")

    def assert_failure(self, result: subprocess.CompletedProcess[str], message: str) -> None:
        self.assertEqual(result.returncode, 78, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertIn(message, result.stderr)
        self.assertNotIn("/usr/bin/awk:", result.stderr)

    def test_no_python_or_developer_tools_needed_and_arguments_survive_spaces(self) -> None:
        arguments = ("--experimental", "value with spaces", "$literal", "")
        self.assert_success(self.launch(*arguments), arguments=arguments)
        self.assertEqual(len(self.calls("ditto")), 1)
        self.assertEqual(len(self.calls("spctl")), 1)
        self.assertEqual(len(self.signature_verifications()), 2)
        for directory in (self.cache(), self.cache().parent, self.cache().parent.parent):
            self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o700)

    def test_path_python_shims_are_never_probed(self) -> None:
        bin_path = self.root / "untrusted PATH"
        bin_path.mkdir()
        marker = self.root / "forbidden tool invoked"
        for name in ("python", "python3", "xcodebuild", "xcrun", "curl", "wget"):
            path = bin_path / name
            path.write_text(f"#!/bin/sh\nprintf called > {shlex.quote(str(marker))}\nexit 99\n")
            path.chmod(0o755)
        self.assert_success(self.launch(env=self.environment(PATH=str(bin_path))))
        self.assertFalse(marker.exists())

    def test_startup_does_not_require_optional_lock_utilities(self) -> None:
        # macOS 15 CI has no /usr/bin/lockf. Relocate any accidental dependency
        # in the private fixture so a newer developer Mac cannot mask that bug.
        source = self.launcher.read_text()
        for name in ("lockf", "flock", "shlock"):
            source = source.replace(f"/usr/bin/{name}", shlex.quote(str(self.root / "missing" / name)))
        self.launcher.write_text(source)
        self.assert_success(self.launch())

    def test_intel_capsule_is_selected_without_arm_capsule(self) -> None:
        self.save_config(architecture="x86_64")
        (self.runtime / "python-runtime-macos-arm64.zip").unlink()
        self.assert_success(self.launch(), architecture="x86_64")

    def test_unsupported_operating_system_version_and_architecture_fail_before_extraction(self) -> None:
        for config, message in (
            ({"system": "Linux"}, "macOS 14"),
            ({"system": "Darwin", "version": "13.7"}, "macOS 14"),
            ({"version": "unknown"}, "determine the macOS version"),
            ({"version": "14.0", "architecture": "i386"}, "architecture is not supported"),
        ):
            with self.subTest(config=config):
                self.save_config(**config)
                self.assert_failure(self.launch(), message)
                self.assertEqual(self.calls("ditto"), [])

    def test_missing_capsule_gives_reinstall_action(self) -> None:
        (self.runtime / "python-runtime-macos-arm64.zip").unlink()
        self.assert_failure(self.launch(), "Reinstall")
        self.assertEqual(self.calls("ditto"), [])

    def test_selected_files_and_runtime_directory_cannot_be_symlinks(self) -> None:
        paths = [self.runtime / name for name in (
            "python-runtime-macos-arm64.zip", "python-runtime-build-arm64.json", "SHA256SUMS",
        )] + [self.runtime]
        for index, path in enumerate(paths):
            with self.subTest(path=path.name):
                moved = self.root / f"symlink destination {index}"
                path.rename(moved)
                path.symlink_to(moved, target_is_directory=moved.is_dir())
                try:
                    self.assert_failure(self.launch(), "missing or invalid")
                    self.assertEqual(self.calls("ditto"), [])
                finally:
                    path.unlink()
                    moved.rename(path)

    def test_archive_and_manifest_corruption_fail_before_extraction(self) -> None:
        for name in ("python-runtime-macos-arm64.zip", "python-runtime-build-arm64.json"):
            with self.subTest(name=name):
                path = self.runtime / name
                original = path.read_bytes()
                path.write_bytes(original + b"damaged")
                try:
                    self.assert_failure(self.launch(), "checksum does not match")
                    self.assertEqual(self.calls("ditto"), [])
                finally:
                    path.write_bytes(original)

    def test_checksum_inventory_rejects_ambiguous_or_unexpected_lines(self) -> None:
        path = self.runtime / "SHA256SUMS"
        original = path.read_text()
        lines = original.splitlines(keepends=True)
        variants = {
            "duplicate": original + lines[0],
            "missing": "".join(lines[1:]),
            "extra": original + "0" * 64 + "  unrelated.zip\n",
            "unknown": original.replace("python-runtime-build-arm64.json", "unknown.json"),
            "uppercase": original.replace(lines[0].split()[0], lines[0].split()[0].upper()),
            "one space": original.replace("  ", " ", 1),
            "binary mode": original.replace("  ", " *", 1),
            "blank line": original + "\n",
            "prefix": " " + original,
            "same length wrong digest": "g" + original[1:],
        }
        for label, contents in variants.items():
            with self.subTest(label=label):
                path.write_text(contents)
                self.assert_failure(self.launch(), "checksum inventory is invalid")
                self.assertEqual(self.calls("ditto"), [])
        path.write_text(original)

    def test_signature_rejection_cannot_publish_or_execute_runtime(self) -> None:
        self.save_config(signature_failure=True)
        self.assert_failure(self.launch(), "signature is invalid")
        self.assertFalse((self.cache() / "ready").exists())
        self.assertEqual(self.calls("spctl"), [])
        self.assertEqual(list(self.cache().glob("instance.*")), [])

    def test_gatekeeper_rejection_cannot_publish_or_execute_runtime(self) -> None:
        self.save_config(gatekeeper_failure=1)
        self.assert_failure(self.launch(), "macOS did not approve")
        self.assertFalse((self.cache() / "ready").exists())
        self.assertEqual(len(self.signature_verifications()), 1)

    def test_warm_cache_rechecks_signature_without_reextraction(self) -> None:
        self.assert_success(self.launch())
        self.assert_success(self.launch())
        self.assertEqual(len(self.signature_verifications()), 3)
        self.assertEqual(len(self.calls("spctl")), 1)
        self.assertEqual(len(self.calls("ditto")), 1)
        self.save_config(signature_failure=True)
        self.assert_failure(self.launch(), "signature is invalid")

    def test_cached_nested_resource_corruption_is_detected(self) -> None:
        self.assert_success(self.launch())
        (self.cached_app() / "Contents/Resources/python/lib/data.txt").write_text("tampered")
        self.assert_failure(self.launch(), "signature is invalid")
        self.assertEqual(len(self.calls("ditto")), 1)

    def test_a_valid_signature_from_another_capsule_cannot_replace_cached_code(self) -> None:
        self.assert_success(self.launch())
        # Both fixtures pass the same trusted signing requirement. Swapping the
        # sealed content still has to match this capsule's pinned CodeDirectory.
        for name, data in self.fixture_files["x86_64"].items():
            (self.cached_app() / name).write_bytes(data)
        self.assert_failure(self.launch(), "code identity does not match its packaged manifest")
        self.assertEqual(len(self.calls("ditto")), 1)
        self.assertEqual(len(self.calls("spctl")), 1)

    def test_extracted_code_identity_must_match_manifest_before_gatekeeper(self) -> None:
        self.save_config(display_cdhash="0" * 40)
        self.assert_failure(self.launch(), "code identity does not match its packaged manifest")
        self.assertFalse((self.cache() / "ready").exists())
        self.assertEqual(self.calls("spctl"), [])

    def test_code_identity_display_must_be_one_lowercase_digest(self) -> None:
        for values in ([], ["a" * 40, "a" * 40], ["A" * 40], ["a" * 39], ["a" * 40 + " extra"]):
            with self.subTest(values=values):
                self.save_config(display_cdhash_lines=values)
                self.assert_failure(self.launch(), "signed code identity is invalid")
                self.assertFalse((self.cache() / "ready").exists())
                self.assertEqual(self.calls("spctl"), [])

    def test_manifest_requires_code_identity_as_a_lowercase_digest_string(self) -> None:
        path = self.runtime / "python-runtime-build-arm64.json"
        for value in (None, 123, [], {}, "", "A" * 40, "a" * 39, "g" * 40):
            with self.subTest(value=value):
                path.write_text(json.dumps({"code_directory_hash": value}))
                self.write_checksums()
                self.assert_failure(self.launch(), "signed code identity")
                self.assertEqual(self.calls("ditto"), [])

    def test_cached_symlink_is_rejected_before_signature_check(self) -> None:
        self.assert_success(self.launch())
        resource = self.cached_app() / "Contents/Resources/python/lib/data.txt"
        resource.unlink()
        resource.symlink_to(self.root / "outside")
        self.assert_failure(self.launch(), "unexpected owner, link, or special file")
        self.assertEqual(len(self.signature_verifications()), 2)

    def test_plugin_owned_cache_directory_symlinks_are_rejected(self) -> None:
        self.assert_success(self.launch())
        for index, directory in enumerate((self.cache(), self.cache().parent, self.cache().parent.parent)):
            with self.subTest(directory=directory.name):
                moved = self.root / f"cache redirect {index}"
                directory.rename(moved)
                directory.symlink_to(moved, target_is_directory=True)
                try:
                    self.assert_failure(self.launch(), "cache contains a symbolic link")
                finally:
                    directory.unlink()
                    moved.rename(directory)

    def test_foreign_owned_cache_is_rejected_and_private_permissions_repaired(self) -> None:
        self.assert_success(self.launch())
        self.cache().chmod(0o777)
        self.assert_success(self.launch())
        self.assertEqual(stat.S_IMODE(self.cache().stat().st_mode), 0o700)
        self.save_config(foreign_owner=str(self.cache()))
        self.assert_failure(self.launch(), "belongs to another user")

    def test_incomplete_cache_without_ready_receipt_recovers(self) -> None:
        self.cache().mkdir(parents=True)
        self.assert_success(self.launch())
        self.assertEqual(len(self.calls("ditto")), 1)

    def test_orphaned_staging_directory_does_not_block_clean_restart(self) -> None:
        orphan = self.cache() / "instance.00000000"
        orphan.mkdir(parents=True)
        (orphan / "partial-file").write_text("incomplete")
        self.assert_success(self.launch())
        self.assertTrue(orphan.exists())

    def test_failed_extraction_is_cleaned_and_retry_succeeds(self) -> None:
        self.save_config(extraction_failure=True)
        self.assert_failure(self.launch(), "could not be extracted")
        self.assertEqual(list(self.cache().glob("instance.*")), [])
        self.save_config(extraction_failure=False)
        self.assert_success(self.launch())

    def test_bad_archive_paths_types_and_modes_fail_before_extraction(self) -> None:
        for name, mode in (
            ("../escape", stat.S_IFREG | 0o644),
            ("/tmp/escape", stat.S_IFREG | 0o644),
            ("other.app/file", stat.S_IFREG | 0o644),
            (f"{APP_NAME}/../escape", stat.S_IFREG | 0o644),
            (f"{APP_NAME}//escape", stat.S_IFREG | 0o644),
            (f"{APP_NAME}/./escape", stat.S_IFREG | 0o644),
            (f"{APP_NAME}/has space", stat.S_IFREG | 0o644),
            (f"{APP_NAME}/line\nbreak", stat.S_IFREG | 0o644),
            (f"{APP_NAME}/link", stat.S_IFLNK | 0o777),
            (f"{APP_NAME}/fifo", stat.S_IFIFO | 0o644),
            (f"{APP_NAME}/setuid", stat.S_IFREG | 0o4755),
            (f"{APP_NAME}/writable", stat.S_IFREG | 0o666),
            (f"{APP_NAME}/missing-parent/file", stat.S_IFREG | 0o644),
            (f"{APP_NAME}/Contents/Info.plist/file", stat.S_IFREG | 0o644),
            (f"{APP_NAME}/Contents/Info.plist", stat.S_IFREG | 0o644),
            (f"{APP_NAME}/Contents/INFO.plist", stat.S_IFREG | 0o644),
        ):
            with self.subTest(name=name, mode=oct(mode)):
                self.write_capsule(extra_entries=[(name, mode, b"outside")])
                self.write_checksums()
                self.assert_failure(self.launch(), "archive contains invalid")
                self.assertEqual(self.calls("ditto"), [])

    def test_local_header_name_mismatch_fails_before_extraction(self) -> None:
        path = self.runtime / "python-runtime-macos-arm64.zip"
        payload = path.read_bytes()
        # A local header contradicting its central entry must not be handed to
        # ditto after zipinfo inspected the central directory.
        old = f"{APP_NAME}/Contents/Info.plist".encode()
        new = old.replace(b"Info.plist", b"evil.plist")
        path.write_bytes(payload.replace(old, new, 1))
        self.write_checksums()
        self.assert_failure(self.launch(), "ZIP headers or payload are invalid")
        self.assertEqual(self.calls("ditto"), [])

    def test_archive_member_and_total_size_bounds_precede_extraction(self) -> None:
        for declared_size, count in ((52428801, 1), (52428800, 7)):
            with self.subTest(declared_size=declared_size, count=count):
                path = self.write_capsule(extra_entries=[
                    (f"{APP_NAME}/bound-{index}.bin", stat.S_IFREG | 0o644, b"small fixture")
                    for index in range(count)
                ])
                # Change only the declared central-directory sizes. A tiny
                # fixture tests the bounds without generating oversized data.
                payload = bytearray(path.read_bytes())
                with zipfile.ZipFile(path) as archive:
                    offset = archive.start_dir
                    for member in archive.infolist():
                        self.assertEqual(payload[offset:offset + 4], b"PK\x01\x02")
                        name_size, extra_size, comment_size = struct.unpack_from("<HHH", payload, offset + 28)
                        if "/bound-" in member.filename:
                            struct.pack_into("<I", payload, offset + 24, declared_size)
                        offset += 46 + name_size + extra_size + comment_size
                path.write_bytes(payload)
                self.write_checksums()
                self.assert_failure(self.launch(), "archive contains invalid")
                self.assertEqual(self.calls("ditto"), [])

    def test_archive_entry_count_is_bounded_before_extraction(self) -> None:
        self.write_capsule(extra_entries=[
            (f"{APP_NAME}/entry-{index}", stat.S_IFREG | 0o644, b"")
            for index in range(10000)
        ])
        self.write_checksums()
        self.assert_failure(self.launch(), "archive contains invalid")
        self.assertEqual(self.calls("ditto"), [])

    def test_oversized_packaged_files_fail_before_hashing_or_extraction(self) -> None:
        for name, size, message in (
            ("SHA256SUMS", 1025, "checksum inventory is invalid"),
            ("python-runtime-macos-arm64.zip", 104857601, "archive exceeds its size limit"),
            ("python-runtime-build-arm64.json", 8388609, "manifest exceeds its size limit"),
        ):
            with self.subTest(name=name):
                path = self.runtime / name
                original = path.read_bytes()
                with path.open("r+b") as handle:
                    handle.truncate(size)
                try:
                    self.assert_failure(self.launch(), message)
                    self.assertEqual(self.calls("ditto"), [])
                finally:
                    path.write_bytes(original)

    def test_invalid_ready_symlink_and_directory_are_rejected_without_writes(self) -> None:
        self.cache().mkdir(parents=True)
        ready = self.cache() / "ready"
        outside = self.root / "outside-ready"
        outside.mkdir()
        ready.symlink_to(outside, target_is_directory=True)
        self.assert_failure(self.launch(), "ready receipt is invalid")
        self.assertEqual(list(outside.iterdir()), [])
        self.assertEqual(self.calls("ditto"), [])
        ready.unlink()
        ready.mkdir()
        self.assert_failure(self.launch(), "ready receipt is invalid")
        self.assertEqual(list(ready.iterdir()), [])
        self.assertEqual(self.calls("ditto"), [])

    def test_concurrent_startups_publish_one_complete_cache(self) -> None:
        self.save_config(extraction_delay=0.25)
        processes = [subprocess.Popen(
            ["/bin/sh", str(self.launcher)], cwd=self.plugin, env=self.environment(),
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        ) for _ in range(4)]
        try:
            for process in processes:
                stdout, stderr = process.communicate(timeout=35)
                self.assert_success(subprocess.CompletedProcess(process.args, process.returncode, stdout, stderr))
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                process.communicate()
        self.assertEqual(set(self.cache().iterdir()), {self.instance(), self.cache() / "ready"})
        self.assertEqual(set(self.instance().iterdir()), {self.cached_app(), self.instance() / "ready"})
        self.assertTrue(os.path.samefile(self.cache() / "ready", self.instance() / "ready"))
        self.assert_success(self.launch())

    def test_interrupted_publication_recovers_before_and_after_ready_creation(self) -> None:
        for phase in ("before", "after"):
            with self.subTest(phase=phase):
                self.home = self.root / f"home interrupted {phase}"
                self.home.mkdir()
                self.save_config(publication_signal=phase)
                result = self.launch()
                self.assertEqual(result.returncode, 78, result.stderr)
                self.assertEqual(result.stdout, "")
                if phase == "before":
                    self.assertFalse((self.cache() / "ready").exists())
                    self.assertEqual(list(self.cache().glob("instance.*")), [])
                else:
                    self.assertTrue(self.cached_app().is_dir())
                    self.assertTrue(os.path.samefile(self.cache() / "ready", self.instance() / "ready"))
                extractions = len(self.calls("ditto"))
                self.save_config(publication_signal=None)
                self.assert_success(self.launch())
                self.assertEqual(len(self.calls("ditto")), extractions + (phase == "before"))

    def test_daily_brief_dispatch_is_fixed_and_preserves_arguments_and_stdin(self) -> None:
        arguments = ("--date", "2026-09-05", "--timezone", "Asia/Seoul", "--format", "markdown")
        input_text = '{"reminders": [], "note": "literal $value"}\n'
        self.assert_success(
            self.launch("--render-daily-brief", *arguments, input_text=input_text),
            arguments=arguments, renderer=True, input_text=input_text,
        )
        # Only the first exact flag selects the one fixed renderer. Other flags
        # stay with MCP; a caller never gains an arbitrary Python-script mode.
        for forwarded in (("--experimental", "--render-daily-brief"), ("--script", "/tmp/arbitrary.py")):
            with self.subTest(forwarded=forwarded):
                self.assert_success(self.launch(*forwarded), arguments=forwarded)

    def test_extraction_option_environment_cannot_change_behavior(self) -> None:
        self.assert_success(self.launch(env=self.environment(
            UNZIPOPT="-j", ZIPINFOOPT="-1", DITTO_TEST_OPTIONS="1", DITTONORSRC="1",
        )))

    def test_production_launcher_has_no_discovery_or_signature_escape_switches(self) -> None:
        source = LAUNCHER.read_text()
        for forbidden in ("command -v", "/usr/bin/python3", "xcrun", "xcodebuild", "curl ", "wget ", "--noqtn ", "xattr -d", "eval "):
            self.assertNotIn(forbidden, source)
        for unavailable in ("/usr/bin/lockf", "/usr/bin/flock", "/usr/bin/shlock"):
            self.assertNotIn(unavailable, source)
        self.assertIn('/bin/link "$staging/ready" "$ready"', source)
        self.assertIn('exec "$instance/$app_name/Contents/MacOS/apple-reminders-python"', source)

    def test_shell_syntax(self) -> None:
        result = subprocess.run(["/bin/sh", "-n", str(LAUNCHER)], capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
