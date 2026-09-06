"""Data-free Native artifact tests: no helper is launched."""
import json
import plistlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import audit_source_package as audit
import build_native_helper_app as build
import verify_native_helper as verify


class NativePackagingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.plugin = Path(self.tmp.name) / 'plugin'
        (self.plugin / '.codex-plugin').mkdir(parents=True)
        (self.plugin / '.codex-plugin/plugin.json').write_text('{"version":"0.6.1"}')
        for relative in build.SOURCES.values():
            target = self.plugin / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(build.DEFAULT_PLUGIN_ROOT / relative, target)

    def fixture(self):
        for relative in audit.PRIVATE_NATIVE_FILES - {audit.PRIVATE_NATIVE_MANIFEST}:
            path = self.plugin / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'\xca\xfe\xba\xbe fixture')
        verification = {
            'schema_version': 1,
            'app_name': build.APP_NAME,
            'app_files': {p.relative_to('native').as_posix(): verify.sha256(self.plugin / p)
                          for p in audit.PRIVATE_NATIVE_FILES - {audit.PRIVATE_NATIVE_MANIFEST}},
            'architectures': list(build.ARCHITECTURES),
            'binary_sha256': {kind: verify.sha256(self.plugin / audit.PRIVATE_NATIVE_APP / 'Contents/MacOS' / name)
                              for kind, name in build.EXECUTABLES.items()},
            'bundle_identifier': build.BUNDLE_IDENTIFIER,
            'executable': build.EXECUTABLE_NAME,
            'executables': build.EXECUTABLES,
            'minimum_macos': '14.0',
            'minimum_macos_by_architecture': {kind: {'arm64':'14.0','x86_64':'14.0'} for kind in build.EXECUTABLES},
            'notarization_checked': True, 'notarized': True,
            'plugin_version': '0.6.1', 'signature': 'developer-id', 'team_id': 'V8347N9346',
        }
        manifest = verify.build_manifest(self.plugin, verification, source_commit='a'*40, workflow_commit='b'*40,
            build_environment={key:'fixture' for key in ('clang','linker','macos_sdk','macos_sdk_path','xcode_path')})
        (self.plugin / audit.PRIVATE_NATIVE_MANIFEST).write_text(json.dumps(manifest))
        return manifest

    def test_absent_native_bundle_is_optional(self):
        self.assertEqual([], audit._validate_private_native_manifest(self.plugin))
        files, _ = audit.package_files(self.plugin)
        self.assertFalse(files & audit.PRIVATE_NATIVE_FILES)

    def test_partial_bundle_requires_manifest_and_complete_inventory(self):
        (self.plugin / audit.PRIVATE_NATIVE_APP).mkdir(parents=True)
        self.assertTrue(audit._validate_private_native_manifest(self.plugin))
        files, _ = audit.package_files(self.plugin)
        self.assertTrue(audit.PRIVATE_NATIVE_FILES <= files)

    def test_manifest_binds_each_binary_source_and_exact_schema(self):
        self.fixture()
        self.assertEqual([], audit._validate_private_native_manifest(self.plugin))
        for kind, name in build.EXECUTABLES.items():
            path = self.plugin / audit.PRIVATE_NATIVE_APP / 'Contents/MacOS' / name
            original = path.read_bytes()
            path.write_bytes(original + b'changed')
            self.assertTrue(audit._validate_private_native_manifest(self.plugin), kind)
            path.write_bytes(original)
        source = self.plugin / build.SOURCES['recovery']
        source.write_bytes(source.read_bytes() + b'changed')
        self.assertTrue(audit._validate_private_native_manifest(self.plugin))

    def test_unknown_manifest_key_rejected(self):
        manifest = self.fixture()
        manifest['extra'] = True
        (self.plugin / audit.PRIVATE_NATIVE_MANIFEST).write_text(json.dumps(manifest))
        self.assertTrue(audit._validate_private_native_manifest(self.plugin))

    def test_all_three_executables_keep_execute_bits(self):
        for binary in audit.PRIVATE_NATIVE_BINARIES:
            self.assertEqual(0o100755, audit.package_member_mode(binary))
        self.assertEqual(0o100644, audit.package_member_mode(audit.PRIVATE_NATIVE_MANIFEST))

    def test_plist_uses_primary_image_executable_and_current_version(self):
        info = plistlib.loads(build.versioned_info_plist(self.plugin))
        self.assertEqual(build.EXECUTABLES['image'], info['CFBundleExecutable'])
        self.assertEqual(build.BUNDLE_IDENTIFIER, info['CFBundleIdentifier'])
        self.assertEqual('14.0', info['LSMinimumSystemVersion'])
        self.assertEqual('0.6.1', info['CFBundleVersion'])

    def test_secondary_binary_identity_is_verified_without_execution(self):
        app = Path(self.tmp.name) / build.APP_NAME
        for relative in ('Contents', 'Contents/MacOS', 'Contents/_CodeSignature'):
            (app / relative).mkdir(parents=True, exist_ok=True)
        (app / 'Contents/Info.plist').write_bytes(build.versioned_info_plist(self.plugin))
        (app / 'Contents/_CodeSignature/CodeResources').write_bytes(b'signature')
        for name in build.EXECUTABLES.values():
            binary = app / 'Contents/MacOS' / name
            binary.write_bytes(b'fixture')
            binary.chmod(0o755)

        def tool(argv, **kwargs):
            self.assertIn(argv[0], (build.XCRUN, build.CODESIGN))
            return subprocess.CompletedProcess(argv, 0, 'arm64 x86_64', '')

        def identity(target):
            identifier = build.BUNDLE_IDENTIFIER
            if target.name == build.EXECUTABLES['sections']:
                identifier = 'unexpected.identity'
            return f'Identifier={identifier}\nTeamIdentifier=V8347N9346\n'

        with (
            patch.object(verify, 'run', side_effect=tool),
            patch.object(verify, '_minimum_version', return_value='14.0'),
            patch.object(verify, '_codesign_details', side_effect=identity),
            patch.object(verify, '_verify_no_entitlements'),
            self.assertRaisesRegex(build.BuildFailure, 'identifier drift'),
        ):
            verify.verify_app(self.plugin, app)

    def test_symlinked_output_rejected_before_build_commands(self):
        output = Path(self.tmp.name) / build.APP_NAME
        output.symlink_to(self.plugin, target_is_directory=True)
        with patch.object(build, 'run') as runner, self.assertRaises(build.BuildFailure):
            build.build_app(self.plugin, output, identity='-')
        runner.assert_not_called()

if __name__ == '__main__':
    unittest.main()
