from __future__ import annotations

import copy
import hashlib
import json
import plistlib
import stat
import sys
import tempfile
import unittest
import warnings
import zipfile
from pathlib import Path, PurePosixPath
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))
import verify_python_runtime as verifier


class PythonRuntimeVerificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="runtime-verification-test-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.repo = self.root / "repo"
        for name in verifier.SOURCE_INPUTS:
            path = self.repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            actual = REPO_ROOT / name
            path.write_bytes(actual.read_bytes() if actual.exists() else b"fixture workflow\n")
        lock = json.loads((self.repo / "scripts/python-runtime-lock.json").read_text())
        info = plistlib.loads((self.repo / "scripts/python_runtime_info.plist").read_bytes())
        self.contents = {
            verifier.EXECUTABLE: b"\xcf\xfa\xed\xfe" + b"fixture native shim",
            "Contents/Info.plist": plistlib.dumps(info),
            "Contents/Resources/python/bin/python3.13": b"\xcf\xfa\xed\xfe" + b"fixture native Python",
            "Contents/Resources/upstream/PYTHON.json": json.dumps({
                "python_version": "3.13.15", "target_triple": "aarch64-apple-darwin",
                "apple_sdk_deployment_target": "11.0", "license_path": "licenses/LICENSE.cpython.txt",
                "licenses": ["Python-2.0", "CNRI-Python"],
            }).encode(),
            "Contents/Resources/upstream/licenses/LICENSE.cpython.txt": b"Fixture Python license text.\n",
        }
        self.manifest = {
            "schema_version": 1, "architecture": "arm64", "target_triple": "aarch64-apple-darwin",
            "runtime_version": "3.13.15", "runtime_build": "20260901",
            "app_name": verifier.APP_NAME, "bundle_identifier": verifier.BUNDLE_ID,
            "executable_relative_path": verifier.EXECUTABLE,
            "source_commit": None, "workflow_commit": None,
            "source_input_sha256": {name: verifier.sha256(self.repo / name) for name in verifier.SOURCE_INPUTS},
            "upstream": {kind: lock["architectures"]["arm64"][kind] for kind in ("install_only_stripped", "full")},
            "normalization": {"omitted_symlinks": ["python/bin/python", "python/bin/python3"], "dereferenced_symlinks": []},
        }
        self.archive = self.root / "runtime.zip"
        self.manifest_path = self.root / "runtime.json"
        self.rebuild()

    def rebuild(self) -> None:
        files = []
        directories: set[str] = set()
        for name, content in sorted(self.contents.items()):
            macho = content[:4] in verifier.MACHO_MAGICS
            files.append({"path": name, "bytes": len(content), "sha256": hashlib.sha256(content).hexdigest(), "mode": 0o755 if macho else 0o644, "mach_o": macho})
            directories.update(str(parent) for parent in PurePosixPath(name).parents if str(parent) != ".")
        self.manifest.update({
            "files": files, "directories": [{"path": path, "mode": 0o755} for path in sorted(directories)],
            "macho_paths": sorted(item["path"] for item in files if item["mach_o"]),
        })
        entries: list[tuple[str, bytes, int]] = [(verifier.APP_NAME + "/", b"", stat.S_IFDIR | 0o755)]
        entries.extend((verifier.APP_NAME + "/" + item["path"] + "/", b"", stat.S_IFDIR | item["mode"]) for item in self.manifest["directories"])
        entries.extend((verifier.APP_NAME + "/" + item["path"], self.contents[item["path"]], stat.S_IFREG | item["mode"]) for item in files)
        self.write_zip(entries)

    def write_zip(self, entries: list[tuple[str, bytes, int]]) -> None:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            with zipfile.ZipFile(self.archive, "w", compression=zipfile.ZIP_DEFLATED) as handle:
                for name, content, mode in entries:
                    info = zipfile.ZipInfo(name, (1980, 1, 1, 0, 0, 0))
                    info.create_system = 3
                    info.external_attr = mode << 16
                    info.compress_type = zipfile.ZIP_DEFLATED
                    handle.writestr(info, content)
        self.manifest["unsigned_archive_sha256"] = verifier.sha256(self.archive)
        self.manifest["unsigned_archive_bytes"] = self.archive.stat().st_size
        self.save_manifest()

    def entries(self) -> list[tuple[str, bytes, int]]:
        with zipfile.ZipFile(self.archive) as handle:
            return [(item.filename, handle.read(item), item.external_attr >> 16) for item in handle.infolist()]

    def save_manifest(self) -> None:
        self.manifest_path.write_text(json.dumps(self.manifest), encoding="utf-8")

    def validate(self) -> None:
        self.save_manifest()
        manifest = verifier.load_manifest(self.manifest_path, "arm64", repo_root=self.repo)
        verifier.validate_archive(self.archive, manifest)

    def test_valid_unsigned_capsule_requires_no_signature_claims(self) -> None:
        with mock.patch.object(verifier.subprocess, "run", side_effect=AssertionError("unsigned validation must not execute runtime code")):
            self.validate()
        self.assertNotIn("signature", self.manifest)

    def test_upstream_pins_source_identity_and_normalization_drift_are_rejected(self) -> None:
        original = copy.deepcopy(self.manifest)
        changes = (
            ("upstream", lambda: self.manifest["upstream"]["install_only_stripped"].update(sha256="0" * 64)),
            ("launcher", lambda: self.manifest["source_input_sha256"].update({"scripts/python_runtime_launcher.c": "0" * 64})),
            ("source inventory", lambda: self.manifest["source_input_sha256"].pop("scripts/build_python_runtime.py")),
            ("alias normalization", lambda: self.manifest["normalization"].update(omitted_symlinks=[])),
            ("normalized path", lambda: self.manifest["normalization"].update(dereferenced_symlinks=["outside/python"])),
        )
        for name, change in changes:
            with self.subTest(field=name):
                self.manifest = copy.deepcopy(original)
                change()
                with self.assertRaises(verifier.VerificationError):
                    self.validate()

    def test_historical_build_hashes_remain_valid_while_behavior_hashes_stay_current(self) -> None:
        self.manifest["source_input_sha256"]["scripts/build_python_runtime.py"] = "a" * 64
        self.manifest["source_input_sha256"][".github/workflows/prepare-signed-runtime-source.yml"] = "b" * 64
        self.validate()
        self.manifest["source_input_sha256"]["scripts/build_python_runtime.py"] = "not-a-hash"
        with self.assertRaisesRegex(verifier.VerificationError, "digest"):
            self.validate()

    def test_archive_and_member_byte_tampering_are_detected(self) -> None:
        self.archive.write_bytes(self.archive.read_bytes() + b"changed archive")
        with self.assertRaisesRegex(verifier.VerificationError, "hash or size"):
            verifier.validate_archive(self.archive, self.manifest)
        self.rebuild()
        entries = self.entries()
        name, content, mode = entries[-1]
        entries[-1] = (name, b"x" * len(content), mode)
        self.write_zip(entries)
        with self.assertRaisesRegex(verifier.VerificationError, "content or code"):
            self.validate()

    def test_duplicate_manifest_keys_and_duplicate_zip_entries_are_rejected(self) -> None:
        self.manifest_path.write_text('{"schema_version":1,"schema_version":1}', encoding="utf-8")
        with self.assertRaisesRegex(verifier.VerificationError, "duplicate"):
            verifier.load_manifest(self.manifest_path, "arm64", repo_root=self.repo)
        entries = self.entries()
        self.write_zip(entries + [entries[-1]])
        with self.assertRaisesRegex(verifier.VerificationError, "inventory"):
            self.validate()

    def test_missing_extra_and_duplicate_inventory_entries_are_rejected(self) -> None:
        original = copy.deepcopy(self.manifest)
        for change in ("missing", "extra", "duplicate"):
            with self.subTest(change=change):
                self.manifest = copy.deepcopy(original)
                if change == "missing":
                    self.manifest["files"].pop()
                elif change == "extra":
                    item = dict(self.manifest["files"][-1], path="Contents/extra.txt")
                    self.manifest["files"].append(item)
                else:
                    self.manifest["files"].append(dict(self.manifest["files"][-1]))
                with self.assertRaises(verifier.VerificationError):
                    self.validate()

    def test_symlink_zip_entries_and_world_writable_modes_are_rejected(self) -> None:
        original = self.entries()
        for replacement_mode in (stat.S_IFLNK | 0o755, stat.S_IFREG | 0o777):
            with self.subTest(mode=replacement_mode):
                entries = list(original)
                name, content, _ = entries[-1]
                entries[-1] = (name, content, replacement_mode)
                self.write_zip(entries)
                with self.assertRaisesRegex(verifier.VerificationError, "type, mode"):
                    self.validate()

    def test_traversal_and_undeclared_parent_paths_are_rejected(self) -> None:
        original = copy.deepcopy(self.manifest)
        for path in (".", "../outside", "/outside", "Contents/../outside", "Contents\\outside", "Contents/new-parent/item"):
            with self.subTest(path=path):
                self.manifest = copy.deepcopy(original)
                self.manifest["files"][-1]["path"] = path
                with self.assertRaises(verifier.VerificationError):
                    self.validate()
        self.assertFalse((self.root / "outside").exists())

    def test_manifest_file_tree_archive_and_entry_count_bounds(self) -> None:
        original = copy.deepcopy(self.manifest)
        changes = (
            ("file", lambda: self.manifest["files"][-1].update(bytes=verifier.MAX_FILE_BYTES + 1)),
            ("archive", lambda: self.manifest.update(unsigned_archive_bytes=verifier.MAX_ARCHIVE_BYTES + 1)),
            ("count", lambda: self.manifest.update(directories=[{"path": f"Contents/d{index}", "mode": 0o755} for index in range(verifier.MAX_ENTRIES)])),
        )
        for name, change in changes:
            with self.subTest(bound=name):
                self.manifest = copy.deepcopy(original)
                change()
                with self.assertRaises(verifier.VerificationError):
                    self.validate()
        self.manifest = copy.deepcopy(original)
        with mock.patch.object(verifier, "MAX_TREE_BYTES", 1), self.assertRaisesRegex(verifier.VerificationError, "tree size"):
            self.validate()

    def test_symlinked_input_files_are_rejected(self) -> None:
        alias = self.root / "alias.zip"
        alias.symlink_to(self.archive)
        with self.assertRaisesRegex(verifier.VerificationError, "non-symlink"):
            verifier.validate_archive(alias, self.manifest)
        alias = self.root / "alias.json"
        alias.symlink_to(self.manifest_path)
        with self.assertRaisesRegex(verifier.VerificationError, "non-symlink"):
            verifier.load_manifest(alias, "arm64", repo_root=self.repo)

    def test_verified_extraction_uses_a_new_real_directory_and_preserves_modes(self) -> None:
        destination = self.root / "verified"
        app = verifier.extract_verified(self.archive, self.manifest, destination)
        self.assertEqual(app, destination / verifier.APP_NAME)
        self.assertFalse(any(path.is_symlink() for path in app.rglob("*")))
        for item in self.manifest["files"]:
            path = app / item["path"]
            self.assertEqual(verifier.sha256(path), item["sha256"])
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), item["mode"])
        with self.assertRaisesRegex(verifier.VerificationError, "new destination"):
            verifier.extract_verified(self.archive, self.manifest, destination)
        alias = self.root / "directory-alias"
        alias.symlink_to(destination, target_is_directory=True)
        with self.assertRaisesRegex(verifier.VerificationError, "parent is a symlink"):
            verifier.extract_verified(self.archive, self.manifest, alias / "new")
        self.assertFalse((destination / "new").exists())

    def test_required_signed_claims_reject_unsigned_or_incomplete_provenance(self) -> None:
        with self.assertRaisesRegex(verifier.VerificationError, "Developer ID"):
            verifier.load_manifest(self.manifest_path, "arm64", require_signed=True, expected_team_id="ABCDEFGHIJ", repo_root=self.repo)
        self.manifest.update({"signature": "developer-id", "archive_sha256": self.manifest["unsigned_archive_sha256"], "archive_bytes": self.manifest["unsigned_archive_bytes"], "team_id": "ABCDEFGHIJ", "notarized": True, "notarization_checked": True, "source_commit": "a" * 40, "workflow_commit": "b" * 40})
        original = copy.deepcopy(self.manifest)
        for key, value in (("team_id", "WRONGTEAM1"), ("notarized", False), ("notarization_checked", False), ("source_commit", None), ("workflow_commit", "main")):
            with self.subTest(field=key):
                self.manifest = copy.deepcopy(original)
                self.manifest[key] = value
                self.save_manifest()
                with self.assertRaises(verifier.VerificationError):
                    verifier.load_manifest(self.manifest_path, "arm64", require_signed=True, expected_team_id="ABCDEFGHIJ", repo_root=self.repo)

    def test_signed_verification_rejects_a_nested_native_architecture_mismatch(self) -> None:
        def inspect(arguments: list[str], **kwargs: object) -> str:
            return "x86_64\n" if arguments[0] == "/usr/bin/lipo" else ""

        with mock.patch.object(verifier, "run", side_effect=inspect):
            with self.assertRaisesRegex(verifier.VerificationError, "architecture mismatch"):
                verifier.verify_signatures(self.root / verifier.APP_NAME, "arm64", "ABCDEFGHIJ", notarized=False, macho_paths=self.manifest["macho_paths"])

    def test_signed_verification_propagates_nested_signature_and_notarization_failures(self) -> None:
        app = self.root / verifier.APP_NAME
        for failing_tool in ("nested signature", "notarization"):
            with self.subTest(step=failing_tool):
                def inspect(arguments: list[str], **kwargs: object) -> str:
                    if failing_tool == "nested signature" and arguments[0] == "/usr/bin/codesign" and "--deep" not in arguments:
                        raise verifier.VerificationError("nested identity rejected")
                    if failing_tool == "notarization" and arguments[0] == "/usr/sbin/spctl":
                        raise verifier.VerificationError("notarization rejected")
                    return "arm64\n" if arguments[0] == "/usr/bin/lipo" else ""

                with mock.patch.object(verifier, "run", side_effect=inspect):
                    with self.assertRaisesRegex(verifier.VerificationError, "rejected"):
                        verifier.verify_signatures(app, "arm64", "ABCDEFGHIJ", notarized=True, macho_paths=self.manifest["macho_paths"])

    def test_missing_license_and_wrong_app_identity_are_rejected(self) -> None:
        del self.contents["Contents/Resources/upstream/licenses/LICENSE.cpython.txt"]
        self.rebuild()
        with self.assertRaisesRegex(verifier.VerificationError, "licenses missing"):
            self.validate()
        self.contents["Contents/Resources/upstream/licenses/LICENSE.cpython.txt"] = b"Fixture Python license."
        info = plistlib.loads(self.contents["Contents/Info.plist"])
        info["CFBundleIdentifier"] = "invalid.runtime"
        self.contents["Contents/Info.plist"] = plistlib.dumps(info)
        self.rebuild()
        with self.assertRaisesRegex(verifier.VerificationError, "app metadata"):
            self.validate()

    def test_upstream_metadata_must_match_python_architecture_and_supported_macos(self) -> None:
        path = "Contents/Resources/upstream/PYTHON.json"
        original = json.loads(self.contents[path])
        for key, value in (("python_version", "0.0.0"), ("target_triple", "x86_64-apple-darwin"), ("apple_sdk_deployment_target", "15.0"), ("apple_sdk_deployment_target", 11), ("apple_sdk_deployment_target", "unknown")):
            with self.subTest(field=key, value=value):
                metadata = dict(original)
                metadata[key] = value
                self.contents[path] = json.dumps(metadata).encode()
                self.rebuild()
                with self.assertRaisesRegex(verifier.VerificationError, "metadata|deployment"):
                    self.validate()
        self.contents[path] = b"[]"
        self.rebuild()
        with self.assertRaisesRegex(verifier.VerificationError, "metadata"):
            self.validate()

    def test_cpython_license_must_be_named_by_metadata_and_contain_text(self) -> None:
        metadata_path = "Contents/Resources/upstream/PYTHON.json"
        metadata = json.loads(self.contents[metadata_path])
        metadata["license_path"] = "../outside.txt"
        self.contents[metadata_path] = json.dumps(metadata).encode()
        self.rebuild()
        with self.assertRaisesRegex(verifier.VerificationError, "license metadata"):
            self.validate()
        metadata["license_path"] = "licenses/LICENSE.cpython.txt"
        self.contents[metadata_path] = json.dumps(metadata).encode()
        self.contents["Contents/Resources/upstream/licenses/LICENSE.cpython.txt"] = b" \n\t"
        self.rebuild()
        with self.assertRaisesRegex(verifier.VerificationError, "missing or empty"):
            self.validate()
        del self.contents["Contents/Resources/upstream/licenses/LICENSE.cpython.txt"]
        self.contents["Contents/Resources/upstream/licenses/unrelated.txt"] = b"Unrelated license."
        self.rebuild()
        with self.assertRaisesRegex(verifier.VerificationError, "missing or empty"):
            self.validate()

    def test_required_entry_points_must_be_native_and_executable(self) -> None:
        original = dict(self.contents)
        for executable in (verifier.EXECUTABLE, "Contents/Resources/python/bin/python3.13"):
            with self.subTest(executable=executable):
                self.contents = dict(original)
                self.contents[executable] = b"plain text entry point"
                self.rebuild()
                with self.assertRaisesRegex(verifier.VerificationError, "executable Mach-O"):
                    self.validate()
                self.contents = dict(original)
                self.rebuild()
                for item in self.manifest["files"]:
                    if item["path"] == executable:
                        item["mode"] = 0o644
                with self.assertRaisesRegex(verifier.VerificationError, "executable Mach-O"):
                    self.validate()


if __name__ == "__main__":
    unittest.main()
