"""Data-free Native bundle and default compiler-free route regression tests."""
import hashlib
import json
import os
from pathlib import Path
import plistlib
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'plugins/apple-reminders/scripts'))
import native_helper as native
import reminders_adapter as adapter
import reminders_doctor as doctor
import experimental_capabilities as capabilities


class NativeTrustTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.app = self.root / 'native' / native.APP_NAME
        for relative in ('Contents/MacOS', 'Contents/_CodeSignature'):
            (self.app / relative).mkdir(parents=True, exist_ok=True)
        (self.root / '.codex-plugin').mkdir()
        (self.root / '.codex-plugin/plugin.json').write_text('{"version":"0.6.1"}')
        (self.root / 'scripts').mkdir()
        for p in native.SOURCES:
            (self.root / p).write_text('// synthetic source')
        info = dict(CFBundleIdentifier=native.BUNDLE_ID, CFBundleExecutable=native.EXECUTABLES['image'], CFBundlePackageType='APPL', CFBundleShortVersionString='0.6.1', CFBundleVersion='0.6.1', LSMinimumSystemVersion='14.0')
        (self.app / 'Contents/Info.plist').write_bytes(plistlib.dumps(info))
        (self.app / 'Contents/_CodeSignature/CodeResources').write_bytes(b'synthetic signature')
        data = native.trust.BUNDLED_HELPER_PATH.read_bytes()
        for name in native.EXECUTABLES.values():
            path = self.app / 'Contents/MacOS' / name
            path.write_bytes(data)
            path.chmod(0o755)
        inventory = native._inventory(self.app)
        self.manifest = dict(schema_version=1, app_name=native.APP_NAME, architectures=['arm64','x86_64'], bundle_identifier=native.BUNDLE_ID, team_id=native.TEAM_ID, signature='developer-id', executable=native.EXECUTABLES['image'], executables=native.EXECUTABLES, minimum_macos='14.0', minimum_macos_by_architecture={k:{'arm64':'14.0','x86_64':'14.0'} for k in native.EXECUTABLES}, plugin_version='0.6.1', notarized=True, notarization_checked=True, app_files=inventory, source_files={p:hashlib.sha256((self.root/p).read_bytes()).hexdigest() for p in native.SOURCES}, binary_sha256={k:hashlib.sha256(data).hexdigest() for k in native.EXECUTABLES}, source_commit='a'*40, workflow_commit='b'*40, build_inputs={p:'c'*64 for p in native.BUILD_INPUTS}, build_environment={k:'synthetic' for k in ('clang','linker','macos_sdk','xcode_path','macos_sdk_path')})
        self.save()
        for patch in (mock.patch.object(native, 'PLUGIN_ROOT', self.root), mock.patch.object(native.sys, 'platform', 'darwin'), mock.patch.object(native.platform, 'machine', return_value='arm64')):
            patch.start(); self.addCleanup(patch.stop)
        self.check = mock.patch.object(native.trust, '_run_bundled_helper_check', return_value=('', f'Identifier={native.BUNDLE_ID}\nTeamIdentifier={native.TEAM_ID}\nCodeDirectory flags=runtime\nTimestamp=test'))
        self.runner = self.check.start(); self.addCleanup(self.check.stop)

    def save(self):
        (self.app.parent/'native-helper-build.json').write_text(json.dumps(self.manifest))

    def test_verified_universal_bundle_resolves_all_kinds_without_toolchain(self):
        with mock.patch.object(adapter, 'resolve_selected_clang', side_effect=AssertionError('compiler invoked')):
            for kind, name in native.EXECUTABLES.items():
                self.assertEqual(native.resolve_helper(kind), self.app/'Contents/MacOS'/name)
        self.assertTrue(all(call.args[0][0] == '/usr/bin/codesign' for call in self.runner.call_args_list))

    def test_tampered_source_or_binary_is_rejected_before_signature(self):
        for path in (self.root/native.SOURCES[0], self.app/'Contents/MacOS'/native.EXECUTABLES['sections']):
            old = path.read_bytes(); path.write_bytes(b'tampered')
            with self.assertRaises(native.NativeHelperUnavailable): native.resolve_helper()
            path.write_bytes(old)
        self.runner.assert_not_called()

    def test_provenance_cannot_admit_unsigned_wrong_team_or_unknown_keys(self):
        for key, value in [('notarized',False),('team_id','OTHER'),('schema_version',True),('unexpected',True)]:
            old = dict(self.manifest); self.manifest[key]=value; self.save()
            with self.assertRaises(native.NativeHelperUnavailable): native.resolve_helper()
            self.manifest=old
        self.runner.assert_not_called()

    def test_symlink_or_extra_member_is_rejected(self):
        path=self.app/'Contents/MacOS'/native.EXECUTABLES['image']
        path.unlink(); path.symlink_to(native.trust.BUNDLED_HELPER_PATH)
        with self.assertRaises(native.NativeHelperUnavailable): native.resolve_helper()
        self.runner.assert_not_called()

    def test_signature_failure_and_unknown_architecture_fail_closed(self):
        self.runner.side_effect=native.NativeHelperUnavailable('signature')
        with self.assertRaises(native.NativeHelperUnavailable): native.resolve_helper()
        self.runner.reset_mock()
        with mock.patch.object(native.platform,'machine',return_value='unknown'):
            with self.assertRaises(native.NativeHelperUnavailable): native.resolve_helper()
        self.runner.assert_not_called()


class CompilerFreeRoutesTests(unittest.TestCase):
    def setUp(self):
        patch=mock.patch.dict(os.environ, {}, clear=True); patch.start(); self.addCleanup(patch.stop)

    def test_production_missing_bundle_never_compiles(self):
        with mock.patch.object(adapter,'resolve_native_helper',side_effect=native.NativeHelperUnavailable('missing')), mock.patch.object(adapter,'resolve_selected_clang',side_effect=AssertionError('clang')):
            for builder in (adapter.reminderkit_attach_helper,adapter.reminderkit_sections_helper,adapter.reminderkit_recover_helper,adapter.require_native_helper_runtime):
                with self.assertRaises(adapter.MutationNotStartedError) as raised: builder()
                self.assertEqual(raised.exception.details['reason_code'],'native_helper_unavailable')

    def test_doctor_verifies_bundle_without_source_sdk_or_compiler(self):
        with mock.patch.object(doctor,'resolve_native_helper',return_value=Path('/verified')), mock.patch.object(doctor,'resolve_selected_clang',side_effect=AssertionError('clang')):
            result=doctor.inspect_helper_toolchain({})
        self.assertEqual(result['code'],'native_helper_verified')
        self.assertEqual(result['details']['compiler_requirement'],'not_required')

    def test_doctor_missing_bundle_does_not_probe_compiler(self):
        with mock.patch.object(doctor,'resolve_native_helper',side_effect=native.NativeHelperUnavailable('missing')), mock.patch.object(doctor,'resolve_selected_clang',side_effect=AssertionError('clang')):
            result=doctor.inspect_helper_toolchain({})
        self.assertEqual(result['code'],'native_helper_unavailable')

    def test_bundled_provider_does_not_bypass_identity_or_schema(self):
        identity=capabilities.RuntimeIdentity('26.5.2','25F84','7.0','3976')
        decision=capabilities.evaluate_capability('image_attachment_mutation',identity,schema_fingerprint='unknown',compiler_available=False,native_helper_available=True)
        self.assertEqual(decision.reason_code,'schema_fingerprint_mismatch')
        self.assertEqual(decision.compiler_requirement,'not_required')
        unknown=capabilities.RuntimeIdentity('99','unknown','99','unknown')
        decision=capabilities.evaluate_capability('image_attachment_mutation',unknown,schema_fingerprint=None,compiler_available=False,native_helper_available=True)
        self.assertEqual(decision.reason_code,'unsupported_build')

    def test_admitted_schema_reports_bundled_provider_without_clang(self):
        from argparse import Namespace
        identity=capabilities.RuntimeIdentity('26.5.2','25F84','7.0','3976')
        fingerprint=next(iter(capabilities.COMPATIBILITY_ALLOWLIST['image_attachment_mutation'][0].schema_fingerprints))
        connection=mock.Mock()
        with mock.patch.object(adapter,'detect_runtime_identity',return_value=identity), mock.patch.object(adapter,'resolve_native_helper',return_value=Path('/verified')), mock.patch.object(adapter,'resolve_selected_clang',side_effect=AssertionError('clang')), mock.patch.object(adapter,'resolve_database',return_value=Path('/synthetic')), mock.patch.object(adapter,'connect_read_only',return_value=connection), mock.patch.object(adapter,'command_capability',return_value={'supported':True,'schema_fingerprint':fingerprint}):
            public=adapter.preflight_experimental_command(Namespace(command='attach_image',image='x',backend=None,url=None))
        self.assertEqual(public['runtime_provider'],'bundled_signed')
        self.assertEqual(public['compiler_requirement'],'not_required')
        self.assertEqual(public['schema_gate'],'minimum_fields_and_exact_fingerprint')
        connection.close.assert_called_once()

    def test_unknown_build_stops_before_provider_and_store(self):
        from argparse import Namespace
        with mock.patch.object(adapter,'detect_runtime_identity',return_value=capabilities.RuntimeIdentity('99','unknown','99','unknown')), mock.patch.object(adapter,'resolve_native_helper',side_effect=AssertionError('provider')), mock.patch.object(adapter,'resolve_database',side_effect=AssertionError('store')):
            with self.assertRaises(adapter.MutationNotStartedError): adapter.preflight_experimental_command(Namespace(command='attach_image',image='x',backend=None,url=None))

if __name__ == '__main__': unittest.main()
