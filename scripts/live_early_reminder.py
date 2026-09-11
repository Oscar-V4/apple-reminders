#!/usr/bin/env python3
"""Opt-in composed Early Reminder acceptance through the exact bundled MCP."""
from __future__ import annotations
import argparse
import json
import os
from pathlib import Path
import uuid

from live_smoke import McpStdioClient, PLUGIN_ROOT, SmokeFailure


def run(plugin: Path, list_id: str, record_path: Path, *, cleanup: bool = False, timed: bool = False) -> None:
    if os.environ.get('APPLE_REMINDERS_NATIVE_ALLOW_SOURCE_BUILD') == '1':
        raise SmokeFailure('Final acceptance requires the signed bundled Native helper')
    if record_path.is_symlink() or (record_path.exists() and not cleanup):
        raise SmokeFailure('Use a new private record path, or explicit cleanup of an existing record')
    if cleanup:
        record = json.loads(record_path.read_text())
        if record['list_id'] != list_id:
            raise SmokeFailure('Cleanup list differs from owned fixture record')
    else:
        token = str(uuid.uuid4())
        record = {'scenario': 'calendar_early_reminder', 'list_id': list_id,
            'title': f'Codex calendar renewal {token}', 'idempotency_key': token,
            'reminder_id': None, 'timed': timed, 'steps': [], 'outcome': 'incomplete', 'cleanup': 'not_created'}
    def save():
        record_path.parent.mkdir(parents=True, exist_ok=True)
        record_path.write_text(json.dumps(record, ensure_ascii=False, indent=2)+'\n')
        record_path.chmod(0o600)
    with McpStdioClient(plugin_root=plugin, server_path=plugin/'mcp/server.py', bundled_runtime=True,
                        timeout_seconds=120) as client:
        def call(name, args, *, statuses=('verified','unchanged')):
            receipt = dict(client.call_tool(name,args))
            record['steps'].append({'tool':name, 'receipt':receipt});save()
            print(name,receipt['status'],flush=True)
            if name == 'create_reminder':
                record['reminder_id']=receipt.get('target',{}).get('reminder_id');
                record['cleanup']='retained' if record['reminder_id'] else 'unknown';save()
            if receipt['status'] not in statuses:
                raise SmokeFailure(f'{name}: {receipt["status"]}; inspect the private record before retrying')
            return receipt
        def read():
            return call('read_reminder',{'reminder_id':record['reminder_id']})['data']['reminder']
        def inspect():
            return call('inspect_reminder_native',{'kind':'reminder','reference':read()['reference'],
                        'include':['early_reminder']})['data']
        def set_early(value):
            state=inspect()
            return call('organize_reminder',{'reference':state['reference'],
                'action':{'kind':'set_early_reminder','early_reminder':value}})
        if cleanup:
            if record['cleanup']=='verified': return
            current=read()
            if current['title']!=record['title'] or current['list_id']!=list_id:
                raise SmokeFailure('Owned fixture identity changed; cleanup refused')
            call('delete_reminder',{'reference':current['reference']})
            gone=client.call_tool('read_reminder',{'reminder_id':record['reminder_id']})
            if gone.get('error',{}).get('code')!='not_found':
                raise SmokeFailure('Exact absence was not verified')
            record['cleanup']='verified';save();print('cleanup verified',flush=True);return
        diagnosis=call('diagnose_reminders',{'scope':'early_reminder'})
        if not all(c['available'] for c in diagnosis['data']['capabilities']):
            raise SmokeFailure('Early Reminder capability is unavailable')
        lists=call('list_reminder_lists',{'writable_only':True,'limit':200})['data']['items']
        selected=[item for item in lists if item['id']==list_id]
        if len(selected)!=1:
            raise SmokeFailure('Exact destination list was not resolved')
        original_due = ({'kind':'timed','floating':True,'local_date_time':'2028-03-31T09:30:00'}
                        if timed else {'kind':'all_day','date':'2028-03-31'})
        call('create_reminder',{'list_id':list_id,'title':record['title'],
            'notes':'Preserve this renewal note.','priority':5,'due':original_due,
            'recurrence_rules':[{'frequency':'yearly','interval':1}],
            'alarms':[{'kind':'relative','offset_seconds':-2678400}],
            'idempotency_key':record['idempotency_key']})
        month={'unit':'month','value':1}
        result=set_early(month)
        assert result['after']['early_reminder']==month
        # This calendar delta survives changes to the annual due occurrence.
        dates = ['2029-03-31', '2032-03-31']
        for day in dates:
            due = ({'kind':'timed','floating':True,'local_date_time':day+'T09:30:00'}
                   if timed else {'kind':'all_day','date':day})
            current=read()
            call('change_reminder',{'reference':current['reference'],'action':{'kind':'patch','patch':{'due':due}}})
            assert inspect()['early_reminder']==month
        current=read()
        call('change_reminder',{'reference':current['reference'],'action':{'kind':'patch',
            'patch':{'due':original_due,'alarms':[]}}})
        assert inspect()['early_reminder']==month
        assert read()['alarms']==[]
        state=inspect();stale=state['reference']
        call('organize_reminder',{'reference':stale,'action':{'kind':'set_early_reminder','early_reminder':None}})
        rejection=call('organize_reminder',{'reference':stale,'action':{'kind':'set_early_reminder','early_reminder':month}},statuses=('failed_no_mutation',))
        assert rejection['error']['code'] in ('concurrent_modification','invalid_input')
        assert inspect()['early_reminder_count']==0
        set_early(month)
        assert set_early(month)['status']=='unchanged'
        final=read()
        assert final['notes']=='Preserve this renewal note.' and final['priority']==5
        assert final['recurrence_rules']==[{'frequency':'yearly','interval':1}]
        record['outcome']='passed';record['cleanup']='retained_for_ui';save()
        print('Acceptance passed; observe the exact fixture UI, then use --cleanup-record.',flush=True)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--confirm-live-reminders',action='store_true',required=True)
    parser.add_argument('--plugin-root',type=Path,default=PLUGIN_ROOT)
    parser.add_argument('--list-id',required=True)
    parser.add_argument('--record',type=Path,required=True,help='Private JSON path outside the source repository')
    parser.add_argument('--cleanup-record',action='store_true')
    parser.add_argument('--timed',action='store_true',help='Use a separate floating wall-clock fixture; preserve its temporal mode')
    args=parser.parse_args()
    if Path(__file__).resolve().parents[1] in args.record.resolve().parents:
        parser.error('--record must stay outside the source repository')
    run(args.plugin_root.resolve(),args.list_id,args.record.resolve(),cleanup=args.cleanup_record,timed=args.timed)

if __name__=='__main__': main()
