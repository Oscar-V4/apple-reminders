from __future__ import annotations

import io
import json
import os
import plistlib
import stat
import subprocess
import sys
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))
import build_python_runtime as runtime


class PythonRuntimeBuildTests(unittest.TestCase):
    def archive(self, root: Path, entries: list[tuple[str, bytes | str]]) -> Path:
        archive = root / "input.tar.gz"
        with tarfile.open(archive, "w:gz") as handle:
            for name, content in entries:
                item = tarfile.TarInfo(name)
                if isinstance(content, str):
                    item.type = tarfile.SYMTYPE
                    item.linkname = content
                    handle.addfile(item)
                else:
                    item.size = len(content)
                    item.mode = 0o755 if "/bin/" in name else 0o644
                    handle.addfile(item, io.BytesIO(content))
        return archive

    def test_lock_has_fixed_assets_for_both_architectures(self) -> None:
        lock = runtime.load_lock()
        self.assertEqual(set(lock["architectures"]), {"arm64", "x86_64"})
        self.assertEqual(lock["python_version"], "3.13.15")
        self.assertEqual(lock["architectures"]["arm64"]["install_only_stripped"]["sha256"], "d3904bd6a072246e07aa0bdadee9a14e80521e42a943c0848059feb16a2816dc")
        self.assertEqual(lock["architectures"]["x86_64"]["install_only_stripped"]["sha256"], "f712a9143c8a5d248438ec7921a0b48d548bca4f1337d33c690d28c2d0504137")

    def test_runtime_identity_is_independent_of_plugin_version(self) -> None:
        info = plistlib.loads((REPO_ROOT / "scripts/python_runtime_info.plist").read_bytes())
        self.assertEqual(info["CFBundleIdentifier"], runtime.BUNDLE_IDENTIFIER)
        self.assertEqual(info["CFBundleShortVersionString"], "3.13.15")
        self.assertEqual(info["CFBundleVersion"], "20260901")
        self.assertEqual(info["LSMinimumSystemVersion"], "14.0")
        self.assertNotIn("NSRemindersFullAccessUsageDescription", info)

    def test_archive_hash_is_checked_before_use(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "download"
            path.write_bytes(b"unexpected upstream bytes")
            pin = {"bytes": path.stat().st_size, "sha256": "0" * 64}
            with self.assertRaisesRegex(runtime.BuildFailure, "checksum"):
                runtime.verified_archive(pin, path, root)

    def test_safe_in_tree_file_symlinks_become_regular_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = self.archive(root, [("python/bin/python3.13", b"interpreter"), ("python/bin/alias1", "python3.13"), ("python/bin/alias2", "alias1"), ("python/lib/config", b"configuration")])
            output = root / "runtime"
            runtime.extract_install(archive, output)
            for name in ("alias1", "alias2", "python3.13"):
                path = output / "bin" / name
                self.assertFalse(path.is_symlink())
                self.assertEqual(path.read_bytes(), b"interpreter")
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o755)
            self.assertEqual(stat.S_IMODE((output / "lib/config").stat().st_mode), 0o644)

    def test_only_redundant_interpreter_aliases_are_omitted(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = self.archive(root, [("python/bin/python3.13", b"interpreter"), ("python/bin/python3", "python3.13"), ("python/bin/python", "python3"), ("python/bin/pydoc3.13", b"pydoc"), ("python/bin/pydoc3", "pydoc3.13")])
            output = root / "runtime"
            normalization = runtime.extract_install(archive, output)
            self.assertEqual(normalization, {"omitted_symlinks": ["python/bin/python", "python/bin/python3"], "dereferenced_symlinks": ["python/bin/pydoc3"]})
            self.assertFalse((output / "bin/python").exists())
            self.assertFalse((output / "bin/python3").exists())
            self.assertEqual((output / "bin/python3.13").read_bytes(), b"interpreter")
            self.assertEqual((output / "bin/pydoc3").read_bytes(), b"pydoc")

    def test_unsafe_archive_paths_and_links_are_rejected_before_extraction(self) -> None:
        examples = [
            [("../victim", b"bad")],
            [("/python/victim", b"bad")],
            [("python/../victim", b"bad")],
            [("python/bin/python", "../../../victim")],
            [("python/bin/python", "/usr/bin/python3")],
            [("python/a", "b"), ("python/b", "a")],
            [("python/a", "missing")],
            [("python/a", b"first"), ("python/a", b"second")],
            [("python/a", "b"), ("python/a/child", b"bad"), ("python/b", b"regular")],
        ]
        for entries in examples:
            with self.subTest(entries=entries), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                archive = self.archive(root, entries)
                output = root / "runtime"
                with self.assertRaises(runtime.BuildFailure):
                    runtime.extract_install(archive, output)
                self.assertFalse(output.exists())
                self.assertFalse((root / "victim").exists())

    def test_symlinked_inputs_and_parent_directories_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            real = root / "real"
            real.mkdir()
            data = real / "data"
            data.write_text("keep", encoding="utf-8")
            alias = root / "alias"
            alias.symlink_to(real, target_is_directory=True)
            with self.assertRaises(runtime.BuildFailure):
                runtime.safe_path(alias / "data", regular=True)
            symlink = root / "input"
            symlink.symlink_to(data)
            with self.assertRaises(runtime.BuildFailure):
                runtime.safe_path(symlink, regular=True)

    def test_zip_is_deterministic_and_inventory_tracks_native_files_and_modes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            app = root / runtime.APP_NAME
            native = app / runtime.EXECUTABLE_RELATIVE_PATH
            native.parent.mkdir(parents=True)
            native.write_bytes(b"\xcf\xfa\xed\xfe" + b"native executable")
            text = app / "Contents/Info.plist"
            text.write_bytes(b"configuration")
            files, directories = runtime.app_inventory(app)
            native_entry = next(item for item in files if item["path"] == runtime.EXECUTABLE_RELATIVE_PATH)
            self.assertTrue(native_entry["mach_o"])
            self.assertEqual(native_entry["mode"], 0o755)
            self.assertNotIn("", [item["path"] for item in directories])
            first, second = root / "one.zip", root / "two.zip"
            runtime.write_archive(app, first, files, directories)
            runtime.write_archive(app, second, files, directories)
            self.assertEqual(first.read_bytes(), second.read_bytes())
            with zipfile.ZipFile(first) as archive:
                self.assertEqual(archive.namelist()[0], runtime.APP_NAME + "/")
                self.assertTrue(all(item.date_time == runtime.ZIP_TIME for item in archive.infolist()))
                self.assertTrue(all(not stat.S_ISLNK(item.external_attr >> 16) for item in archive.infolist()))


@unittest.skipUnless(os.environ.get("APPLE_REMINDERS_TEST_RUNTIME_APP"), "set APPLE_REMINDERS_TEST_RUNTIME_APP to run the assembled runtime probe")
class PythonRuntimeExecutionTests(unittest.TestCase):
    def test_standard_library_and_child_scripts_run_without_host_python_configuration(self) -> None:
        app = Path(os.environ["APPLE_REMINDERS_TEST_RUNTIME_APP"]).resolve()
        # PBS ships three precompiled encoding modules; execution must leave
        # them unchanged and must not create additional bytecode files.
        initial_bytecode = {str(path.relative_to(app)): runtime.sha256(path) for path in app.rglob("*.pyc")}
        with tempfile.TemporaryDirectory(prefix="python-runtime-probe-") as temporary:
            root = Path(temporary)
            (root / "sibling.py").write_text('VALUE = "sibling-import-ok"\n', encoding="utf-8")
            child = root / "child.py"
            child.write_text('import json, sibling, sys\nprint(json.dumps({"value": sibling.VALUE, "executable": sys.executable}))\n', encoding="utf-8")
            env = {"PATH": "/usr/bin:/bin", "HOME": temporary, "PYTHONHOME": "/nonexistent/python-home", "PYTHONPATH": "/nonexistent/python-path", "PYTHONWARNINGS": "invalid-warning", "PYTHONBREAKPOINT": "injected.breakpoint", "__PYVENV_LAUNCHER__": "/nonexistent/injected-python"}
            program = '''import os, sys, json, subprocess, ssl, sqlite3, ctypes, zoneinfo, bz2, lzma, tomllib, hashlib, tkinter, dbm.ndbm
child = subprocess.run([sys.executable, sys.argv[1]], check=True, capture_output=True, text=True)
print(json.dumps({"version": sys.version.split()[0], "executable": sys.executable, "environment": {k:v for k,v in os.environ.items() if k.startswith("PYTHON")}, "child": json.loads(child.stdout), "timezone": str(zoneinfo.ZoneInfo("Asia/Seoul"))}))
'''
            result = subprocess.run([str(app / runtime.EXECUTABLE_RELATIVE_PATH), "-c", program, str(child)], env=env, capture_output=True, text=True, timeout=30, check=False)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stderr, "")
            payload = json.loads(result.stdout)
            expected = str(app / "Contents/Resources/python/bin/python3.13")
            self.assertEqual(payload["version"], "3.13.15")
            self.assertEqual(payload["executable"], expected)
            self.assertEqual(payload["child"], {"value": "sibling-import-ok", "executable": expected})
            self.assertEqual(payload["environment"], {"PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUTF8": "1"})
            self.assertEqual(payload["timezone"], "Asia/Seoul")
            self.assertEqual({str(path.relative_to(app)): runtime.sha256(path) for path in app.rglob("*.pyc")}, initial_bytecode)
            self.assertFalse((root / "__pycache__").exists())


if __name__ == "__main__":
    unittest.main()
