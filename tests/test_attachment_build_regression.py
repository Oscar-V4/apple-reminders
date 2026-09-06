"""Data-free regression coverage for the v0.6.1 attachment preflight failures."""
from pathlib import Path
import json
import sqlite3
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'plugins/apple-reminders/scripts'))
import experimental_capabilities as capabilities
import reminders_adapter as adapter
import reminders_doctor as doctor


class AttachmentBuildRegressionTests(unittest.TestCase):
    def setUp(self):
        # These existing cases explicitly exercise the contributor source-build path.
        env = mock.patch.dict("os.environ", {"APPLE_REMINDERS_NATIVE_ALLOW_SOURCE_BUILD": "1"})
        env.start()
        self.addCleanup(env.stop)
        bundled = mock.patch.object(doctor, "resolve_native_helper", side_effect=doctor.NativeHelperUnavailable("test fixture"))
        bundled.start()
        self.addCleanup(bundled.stop)

    def test_doctor_passes_sdk_to_selected_compiler(self):
        selected = capabilities.resolve_selected_clang(
            runner=lambda *args: SimpleNamespace(returncode=0, stdout='/Selected/Developer\n'),
            selection_tool_usable=lambda path: True,
            compiler_usable=lambda path: 'Toolchains' in str(path),
            sdk_usable=lambda path: True,
            environment={},
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / 'helper.m'
            source.write_text('#import <AppKit/AppKit.h>\n')
            runner = mock.Mock(return_value=subprocess.CompletedProcess([], 0, '', ''))
            result = doctor.inspect_helper_toolchain(
                {'home': root, 'helper_source': source, 'public_frameworks': {}},
                runner=runner, toolchain_resolver=lambda: selected,
            )
        self.assertEqual(result['code'], 'helper_statically_buildable')
        self.assertEqual(runner.call_args.args[0][:3], [
            str(selected.compiler_path), '-isysroot',
            '/Selected/Developer/Platforms/MacOSX.platform/Developer/SDKs/MacOSX.sdk',
        ])

    def test_recorded_whole_schema_is_projected_to_attachment_contract(self):
        schema = json.loads((ROOT / 'tests/fixtures/attachment_schema_25F84.json').read_text())
        schema = {table: set(columns) for table, columns in schema.items()}
        self.assertEqual(doctor.schema_fingerprint(schema),
                         '82761d59e465cf4c90ca8c98bb51eab498c6976e81d608023535f3bf0ec63d62')
        identity = capabilities.RuntimeIdentity('26.5.2', '25F84', '7.0', '3976')
        with sqlite3.connect(':memory:') as con:
            con.row_factory = sqlite3.Row
            for table, columns in schema.items():
                con.execute(f'CREATE TABLE "{table}" (' + ','.join(f'"{c}"' for c in sorted(columns)) + ')')
            runtime = adapter.command_capability(con, 'attachment_mutation_db')
            diagnostic = doctor.runtime_command_schema_capabilities(schema)['attachment_mutation_db']
            self.assertEqual(runtime['schema_fingerprint'], diagnostic['schema_fingerprint'])
            self.assertNotEqual(runtime['schema_fingerprint'], doctor.schema_fingerprint(schema))
            for name in ('image_attachment_mutation', 'url_attachment_mutation', 'attachment_delete_mutation'):
                with self.subTest(capability=name):
                    self.assertTrue(capabilities.evaluate_capability(
                        name, identity, schema_fingerprint=runtime['schema_fingerprint'],
                        compiler_available=True,
                    ).allowed)
            con.execute('ALTER TABLE ZREMCDOBJECT ADD COLUMN ZUNREVIEWEDFIELD')
            changed = adapter.command_capability(con, 'attachment_mutation_db')
            decision = capabilities.evaluate_capability(
                'image_attachment_mutation', identity,
                schema_fingerprint=changed['schema_fingerprint'], compiler_available=True,
            )
            self.assertFalse(decision.allowed)
            self.assertEqual(decision.reason_code, 'schema_fingerprint_mismatch')


if __name__ == '__main__':
    unittest.main()
