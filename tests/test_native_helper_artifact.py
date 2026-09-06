from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import native_helper_artifact as artifact


class NativeHelperArtifactTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def fixture(self, *, notarized=False):
        expected = dict(artifact.BASE_MODES)
        if notarized:
            expected[artifact.TICKET_NAME] = 0o644
        for relative, mode in expected.items():
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"fixture " + relative.encode())
            path.chmod(mode)
        app = self.root / artifact.APP_NAME
        archive = self.root / "candidate.zip"
        artifact.archive_app(app, archive, notarized=notarized)
        return archive, expected

    def test_exact_unsigned_and_stapled_archives_preserve_bytes_and_modes(self):
        for notarized in (False, True):
            with self.subTest(notarized=notarized):
                archive, expected = self.fixture(notarized=notarized)
                destination = self.root / f"extract-{notarized}"
                old_umask = os.umask(0o077)
                try:
                    app = artifact.extract_app(archive, destination, artifact.sha256(archive), notarized=notarized)
                finally:
                    os.umask(old_umask)
                self.assertEqual(stat.S_IMODE(app.stat().st_mode), 0o755)
                for relative, mode in expected.items():
                    self.assertEqual((destination / relative).read_bytes(), (self.root / relative).read_bytes())
                    self.assertEqual(stat.S_IMODE((destination / relative).stat().st_mode), mode)
                archive.unlink()

    def test_digest_mismatch_is_rejected_before_extraction(self):
        archive, _ = self.fixture()
        destination = self.root / "extract"
        with self.assertRaisesRegex(artifact.BuildFailure, "digest mismatch"):
            artifact.extract_app(archive, destination, "0" * 64, notarized=False)
        self.assertFalse(destination.exists())

    def test_extra_member_or_bad_mode_is_rejected_before_any_write(self):
        for bad_mode in (False, True):
            archive = self.root / f"invalid-{bad_mode}.zip"
            with zipfile.ZipFile(archive, "w") as handle:
                for index, (relative, mode) in enumerate(artifact.BASE_MODES.items()):
                    info = zipfile.ZipInfo(relative)
                    info.create_system = 3
                    info.external_attr = ((stat.S_IFLNK | 0o755) if bad_mode and index == 0 else (stat.S_IFREG | mode)) << 16
                    handle.writestr(info, b"fixture")
                if not bad_mode:
                    handle.writestr("unexpected", b"fixture")
            destination = self.root / f"extract-{bad_mode}"
            with self.assertRaises(artifact.BuildFailure):
                artifact.extract_app(archive, destination, artifact.sha256(archive), notarized=False)
            self.assertFalse(destination.exists())

    def test_final_manifest_retains_original_compilation_environment(self):
        archive, _ = self.fixture(notarized=True)
        context = self.root / "context.json"
        original_environment = {key: "original-build-machine" for key in (
            "clang", "linker", "macos_sdk", "macos_sdk_path", "xcode_path")}
        context.write_text(json.dumps({"source_commit": "a" * 40,
            "workflow_commit": "b" * 40, "build_environment": original_environment}))
        output = self.root / "final"
        with (
            patch.object(sys, "argv", ["artifact", "finalize", "--app", str(self.root / artifact.APP_NAME),
                "--source-commit", "a" * 40, "--workflow-commit", "b" * 40,
                "--context", str(context), "--output", str(output)]),
            patch.object(artifact, "verify_app", return_value={}),
            patch.object(artifact, "build_manifest", return_value={}) as builder,
        ):
            artifact.main()
        self.assertEqual(builder.call_args.kwargs["build_environment"], original_environment)
        self.assertEqual({path.name for path in output.iterdir()}, {
            artifact.ARCHIVE_NAME, artifact.MANIFEST_NAME, "SHA256SUMS"})


if __name__ == "__main__":
    unittest.main()
