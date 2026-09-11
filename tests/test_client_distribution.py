from __future__ import annotations

import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import Mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
sys.path.insert(0, str(ROOT / 'tests'))
sys.path.insert(0, str(ROOT / 'plugins/apple-reminders'))

from audit_source_package import audit_archive
from build_source_package import build_package, sha256
from smoke_installed_package import client_command, extract_audited_archive
from test_package_source import copy_worktree_plugin_snapshot
from validate_plugin import validate_clients
from verify_release_assets import verify_release_payload, VerificationError
from mcp import server


class ClientSchemaTests(unittest.TestCase):
    def test_all_tools_are_visible_to_clients_that_reject_root_composition(self):
        runtime = server.McpRuntime()
        runtime.handle({'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {
            'protocolVersion': '2025-11-25', 'capabilities': {},
            'clientInfo': {'name': 'client-compatibility-test', 'version': '1'},
        }})
        tools = runtime.handle({'jsonrpc': '2.0', 'id': 2, 'method': 'tools/list'})['result']['tools']
        self.assertEqual({tool['name'] for tool in tools}, set(server.TOOLS_BY_NAME))
        self.assertEqual(len(tools), 15)
        for tool in tools:
            schema = tool['inputSchema']
            self.assertEqual(schema['type'], 'object')
            self.assertFalse(schema['additionalProperties'])
            self.assertFalse({'oneOf', 'anyOf', 'allOf'} & schema.keys())
            self.assertEqual(schema['properties'], server.TOOLS_BY_NAME[tool['name']]['inputSchema']['properties'])
        # Discovery must not mutate the authoritative validation contract.
        self.assertIn('oneOf', server.TOOLS_BY_NAME['fetch_reminders']['inputSchema'])

    def test_branch_constraints_still_reject_before_backend_dispatch(self):
        for name, arguments in (
            ('fetch_reminders', {'list_ids': ['list'], 'status': 'completed'}),
            ('inspect_recently_deleted', {'kind': 'list', 'reminder_id': 'item'}),
            ('inspect_reminder_native', {'kind': 'sections', 'list_id': 'list', 'query': 'label'}),
        ):
            with self.subTest(tool=name):
                dispatch = Mock(side_effect=AssertionError('invalid branch must not dispatch'))
                result = server._call_tool(name, arguments, dispatch=dispatch, rate_limit_allows_call=lambda: True)
                dispatch.assert_not_called()
                self.assertTrue(result['isError'])
                self.assertEqual(result['structuredContent']['status'], 'failed_no_mutation')


class ClientDistributionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workspace = tempfile.TemporaryDirectory(prefix='client distribution ')
        cls.base = Path(cls.workspace.name)
        cls.plugin = copy_worktree_plugin_snapshot(cls.base / 'source')
        cls.manifest = json.loads((cls.plugin / '.codex-plugin/plugin.json').read_text())

    @classmethod
    def tearDownClass(cls):
        cls.workspace.cleanup()

    def test_marketplaces_resolve_to_the_same_complete_plugin(self):
        codex = json.loads((ROOT / '.agents/plugins/marketplace.json').read_text())
        claude = json.loads((ROOT / '.claude-plugin/marketplace.json').read_text())
        self.assertEqual(codex['name'], claude['name'])
        self.assertEqual(codex['plugins'][0]['source']['path'], claude['plugins'][0]['source'])
        self.assertTrue((ROOT / claude['plugins'][0]['source'] / '.claude-plugin/plugin.json').is_file())
        errors = []
        validate_clients(self.plugin, self.manifest, errors)
        self.assertEqual(errors, [])

    def test_client_versions_and_launch_paths_cannot_drift(self):
        with tempfile.TemporaryDirectory() as temporary:
            plugin = Path(temporary)
            for name in ('.claude-plugin/plugin.json', 'manifest.json', '.mcp.json'):
                target = plugin / name
                target.parent.mkdir(exist_ok=True)
                target.write_bytes((self.plugin / name).read_bytes())
            for name, change in (
                ('.claude-plugin/plugin.json', lambda p: p.update(version='99.0.0')),
                ('.claude-plugin/plugin.json', lambda p: p['mcpServers']['apple-reminders-local'].update(args=['./scripts/launch_bundled_mcp.sh'])),
                ('manifest.json', lambda p: p['server']['mcp_config'].update(command='python3')),
                ('manifest.json', lambda p: p.update(compatibility={'platforms': ['darwin', 'linux']})),
            ):
                with self.subTest(name=name):
                    payload = json.loads((self.plugin / name).read_text())
                    change(payload)
                    (plugin / name).write_text(json.dumps(payload))
                    errors = []
                    validate_clients(plugin, self.manifest, errors)
                    self.assertTrue(errors)
                    (plugin / name).write_bytes((self.plugin / name).read_bytes())

    def test_mcpb_is_deterministic_root_layout_and_preserves_the_signed_helpers(self):
        with tempfile.TemporaryDirectory(prefix='Desktop package ') as temporary:
            base = Path(temporary)
            first = build_package(self.plugin, base / 'a', format='mcpb')
            second = build_package(self.plugin, base / 'b', format='mcpb')
            self.assertEqual(sha256(first), sha256(second))
            self.assertEqual(audit_archive(self.plugin, first), [])
            installed = base / 'installed extension with spaces'
            extract_audited_archive(first, installed)
            with zipfile.ZipFile(first) as archive:
                self.assertIn('manifest.json', archive.namelist())
                self.assertNotIn('apple-reminders/manifest.json', archive.namelist())
                self.assertFalse(any(name.startswith(('tests/', 'docs/', '.github/')) for name in archive.namelist()))
            for path in (self.plugin / 'native').rglob('*'):
                if path.is_file():
                    copied = installed / path.relative_to(self.plugin)
                    self.assertEqual(path.read_bytes(), copied.read_bytes())
                    self.assertEqual(path.stat().st_mode & 0o777, copied.stat().st_mode & 0o777)
            for client in ('claude-code', 'claude-desktop'):
                command = client_command(installed, client)
                self.assertEqual(command, ['/bin/sh', str(installed / 'scripts/launch_bundled_mcp.sh')])
            with zipfile.ZipFile(first, 'a') as archive:
                archive.writestr('unexpected.txt', 'not in the source allowlist')
            self.assertTrue(any('allowlist mismatch' in error for error in audit_archive(self.plugin, first)))

    def test_release_requires_the_extension_bytes_and_both_checksum_lines(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = 'apple-reminders-0.8.0.zip'
            extension = 'apple-reminders-0.8.0.mcpb'
            (root / package).write_bytes(b'zip')
            (root / extension).write_bytes(b'extension')
            expected = {package: sha256(root / package), extension: sha256(root / extension)}
            (root / 'SHA256SUMS').write_text(''.join(f'{digest}  {name}\n' for name, digest in expected.items()))
            args = dict(expected_package_sha256=expected[package], expected_extension_sha256=expected[extension],
                        expected_checksums_sha256=sha256(root / 'SHA256SUMS'))
            result = verify_release_payload(root, package, **args)
            self.assertEqual(set(result), {package, extension, 'SHA256SUMS'})
            (root / extension).write_bytes(b'tampered')
            with self.assertRaisesRegex(VerificationError, 'extension digest drift'):
                verify_release_payload(root, package, **args)
            (root / extension).unlink()
            with self.assertRaisesRegex(VerificationError, 'inventory drift'):
                verify_release_payload(root, package, **args)


if __name__ == '__main__':
    unittest.main()
