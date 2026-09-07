from __future__ import annotations
import json
import sys
import unittest
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'plugins/apple-reminders'))
from mcp.v2_native import NativeFacade, FacadeError
from mcp.v2_native_backend import NativeBackend

class EarlyReminderContractTests(unittest.TestCase):
    def test_calendar_month_is_not_elapsed_seconds(self):
        self.assertEqual(NativeFacade._native_action('organize_reminder', {
            'kind':'set_early_reminder','early_reminder':{'unit':'month','value':1}
        }), ('set_early_reminder', {'early_reminder':{'unit':'month','value':1}}))

    def test_clear_is_distinct_from_alarm_replacement(self):
        self.assertEqual(NativeFacade._native_action('organize_reminder', {
            'kind':'set_early_reminder','early_reminder':None
        }), ('set_early_reminder', {'early_reminder':None}))

    def test_reject_ambiguous_or_lossy_input(self):
        for value in [False, {}, {'unit':'month','value':True}, {'unit':'month','value':1.5},
                      {'unit':'month','value':0}, {'unit':'month','value':201},
                      {'unit':'second','value':30}, {'offset_seconds':-2592000},
                      {'unit':'month','value':1,'offset_seconds':-2592000}]:
            with self.subTest(value=value), self.assertRaises(FacadeError):
                NativeFacade._native_action('organize_reminder', {'kind':'set_early_reminder','early_reminder':value})

    def test_final_read_checks_unit_count_and_presence(self):
        args={'early_reminder': {'unit':'month','value':1}}
        for actual, match in [({'unit':'month','value':1},True),({'unit':'day','value':30},False),
                              ({'unit':'month','value':2},False),(None,False)]:
            state={'early_reminder':actual,'early_reminder_count':1}
            self.assertEqual(NativeBackend._final_state_matches('set_early_reminder',args,{},state),match)
        self.assertFalse(NativeBackend._final_state_matches('set_early_reminder',{'early_reminder':None},{},{}))

    def test_catalog_preserves_existing_relative_alarm(self):
        tools={t['name']:t for t in json.loads((ROOT/'plugins/apple-reminders/schemas/mcp-tools.json').read_text())['tools']}
        self.assertEqual(len(tools),15)
        actions=tools['organize_reminder']['inputSchema']['properties']['action']['oneOf']
        self.assertIn('set_early_reminder',[a['properties']['kind']['const'] for a in actions])
        self.assertIn('offset_seconds', json.dumps(tools['create_reminder']))


from unittest import mock
from test_mcp_v2_native import Backend, References, mutation_payload
from test_mcp_v2_native_backend import GUARD, REMINDER_ID, transport, verified_public_reminder
from reminders_service import MutationOutcome
from mcp.v2_contract import validate_public_result
import reminders_adapter as adapter
from experimental_capabilities import evaluate_capability, RuntimeIdentity

class EarlyReminderSafetyTests(unittest.TestCase):
    def facade(self, backend, references):
        return NativeFacade(adapter_call=backend.adapter, references=references,
            native_read=backend.native_read, native_mutation=backend.native_mutation,
            section_mutation=backend.section_mutation)

    def test_stale_reference_stops_before_dispatch(self):
        backend, refs=Backend(),References();refs.reject=True
        result,state=self.facade(backend,refs).call_with_state('organize_reminder',{
            'reference':'rev1.'+'x'*32,'action':{'kind':'set_early_reminder','early_reminder':{'unit':'month','value':1}}})
        self.assertEqual(state,'not_mutated');self.assertEqual(backend.native_mutations,[])

    def test_lost_final_read_or_wrong_unit_never_issues_reference(self):
        for fault in ('final_read','wrong_unit','missing','duplicate'):
            backend,refs=Backend(),References();refs.fail_fresh_read=fault=='final_read'
            after={'early_reminder':{'unit':'month','value':1},'early_reminder_count':1}
            if fault=='wrong_unit': after['early_reminder']['unit']='day'
            if fault=='missing': after.pop('early_reminder')
            if fault=='duplicate': after['early_reminder_count']=2
            payload=mutation_payload('set_early_reminder',after=after)
            backend.native_mutation_payloads['set_early_reminder']=MutationOutcome(payload,'committed')
            result,state=self.facade(backend,refs).call_with_state('organize_reminder',{
                'reference':'rev1.'+'x'*32,'action':{'kind':'set_early_reminder','early_reminder':{'unit':'month','value':1}}})
            validate_public_result('organize_reminder',result,state)
            self.assertEqual(result['status'],'committed_verification_pending',fault)
            self.assertIsNone(result['after']);self.assertEqual(state,'committed')

    def test_unknown_build_and_schema_fail_closed(self):
        observed=RuntimeIdentity('26.5.2','25F84','7.0','3976')
        for capability in ('early_reminder_inspection','early_reminder_mutation'):
            for identity,fingerprint in [(RuntimeIdentity('26.5.3','unknown','7.0','3976'),'644afc465f44355207ce5dc511f85e44328c1fd709bdd037bb56d8223c3133c7'),(observed,'0'*64),(observed,None)]:
                self.assertFalse(evaluate_capability(capability,identity,schema_fingerprint=fingerprint,
                    compiler_available=True,native_helper_available=True).allowed)

    def test_native_backend_preserves_complete_core_state(self):
        for field in ('alarms','due','recurrence_rules','notes','start','list_id'):
            public=verified_public_reminder();before=public.payload['data']['reminder']
            before.update(alarms=[{'kind':'relative','offset_seconds':-3600}]*2,
                due={'kind':'all_day','date':'2028-03-31'},recurrence_rules=[{'frequency':'yearly','interval':1}],
                notes='Keep this note',start={'kind':'all_day','date':'2028-03-31'},list_id='LIST-1')
            import copy
            final=copy.deepcopy(public);final.payload['data']['reminder'][field]=None
            early={'reminder_id':REMINDER_ID,'early_reminder':{'unit':'month','value':1},'early_reminder_count':1}
            receipt=mutation_payload('set_early_reminder',after=early)
            backend=NativeBackend(bridge_call=mock.Mock(side_effect=[public,final]),
                adapter_call=mock.Mock(side_effect=[transport({'reminder_id':REMINDER_ID,'reminder_version':5}),transport(receipt),transport({'reminder':early})]),
                build_adapter_argv=lambda name,args:[name],receipt_validator=lambda *a,**k:None)
            result=backend.mutate(GUARD,'set_early_reminder',{'early_reminder':{'unit':'month','value':1}})
            self.assertEqual(result.receipt['status'],'committed_verification_pending',field)
            self.assertEqual(result.mutation_state,'committed')

    def test_adapter_preserves_known_commit_when_final_read_fails(self):
        import argparse
        snapshot={'reminder_id':'00000000-0000-4000-8000-000000000001','native_guard':'a'*64,
                  'stable_digest':'b'*64,'early_reminder':None,'early_reminder_count':0,'early_reminders':[]}
        args=argparse.Namespace(id=snapshot['reminder_id'],if_version=1,early_reminder_json='{"unit":"month","value":1}')
        with mock.patch.object(adapter,'resolve_database',return_value=Path('/tmp/synthetic')), \
             mock.patch.object(adapter,'connect_read_only',return_value=mock.Mock()), \
             mock.patch.object(adapter,'require_command_capability',return_value={}), \
             mock.patch.object(adapter,'find_reminder',return_value={}), \
             mock.patch.object(adapter,'require_reminder_version'), \
             mock.patch.object(adapter,'invoke_early_reminder',side_effect=[{'snapshot':snapshot},{'saved':True},RuntimeError('read lost')]), \
             mock.patch.object(adapter,'json_out') as output:
            adapter.cmd_set_early_reminder(args)
        receipt=output.call_args.args[0]
        self.assertEqual(receipt['status'],'committed_verification_pending')
        self.assertIs(receipt['verification']['write_performed'],True)
        self.assertEqual(receipt['after'],{})

if __name__ == '__main__': unittest.main()
