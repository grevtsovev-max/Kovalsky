import io
import json
import os
import re
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse
from urllib.error import HTTPError
from http.server import ThreadingHTTPServer
import urllib.request

from newsroom.billing import fetch_costs, period_range, save_owner_report, spending, sync
from newsroom.db import connect, connect_readonly
from newsroom.runtime import Runtime


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


class BillingTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = str(Path(folder.name)/'test.sqlite3')
        self.db = connect(self.path)
        self.addCleanup(self.db.close)
        self.config = {'newsroom': {'database': self.path}, 'ai': {},
                       'billing': {'admin_api_key_env': 'TEST_BILLING_ADMIN_KEY'}}
        self.now = datetime(2026, 10, 6, 14, tzinfo=timezone.utc)

    def receipt(self, stage, status='SUCCEEDED'):
        self.db.execute('INSERT INTO api_usage(call_id,role,category,model,status,created_at,stage,input_tokens,cached_input_tokens,output_tokens,service_tier) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                        (stage, 'editor', 'fresh', 'gpt-6-luna', status, self.now.isoformat(), stage,
                         1000 if status=='SUCCEEDED' else None, 0 if status=='SUCCEEDED' else None,
                         200 if status=='SUCCEEDED' else None, 'default'))
        self.db.commit()

    def page(self, values, more=False, cursor=None):
        return Response(json.dumps({'data': [{'start_time': int(self.now.timestamp()),
                          'results': [{'amount': {'value': value, 'currency': 'usd'}, 'line_item': item}
                                      for item,value in values]}], 'has_more': more, 'next_page': cursor}).encode())

    def test_counter_period_excludes_old_calls_without_resetting_ledger(self):
        begin = self.now - timedelta(hours=1)
        self.db.execute("UPDATE app_state SET value=? WHERE key='pipeline_counter_epoch_v2'",
                        (json.dumps({'started_at': begin.isoformat(), 'after_item_id': 0}),))
        self.receipt('editorial')
        self.receipt('triage')
        self.db.execute("UPDATE api_usage SET created_at=? WHERE stage='triage'",
                        ((begin-timedelta(seconds=1)).isoformat(),))
        self.db.commit()
        report = spending(self.db, self.config, period='counter', now=self.now)
        self.assertEqual(report['start'], begin.isoformat())
        self.assertEqual(report['local']['calls'], 1)
        self.assertEqual(report['tasks'][0]['name'], 'Разбор ИИ')
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM api_usage').fetchone()[0], 2)
        self.assertIsNone(report['actual'])
        self.assertEqual(sync(self.db, self.config, period='counter', now=self.now)['code'], 'PARTIAL_DAY_PERIOD')

    def test_reported_total_with_unknown_period_is_never_allocated_to_tasks(self):
        save_owner_report(self.db, {'amount_usd': '63'}, self.now)
        self.receipt('editorial')
        self.receipt('triage', 'ERROR')
        report = spending(self.db, self.config, now=self.now)
        self.assertEqual(report['owner_report']['amount_usd'], 63)
        self.assertIsNone(report['actual'])
        self.assertIsNone(report['unexplained_usd'])
        self.assertEqual(sum(t['calls'] for t in report['tasks']), 2)
        self.assertAlmostEqual(sum(t['amount_usd'] for t in report['tasks']), .0002)
        self.assertEqual(sum(t['unpriced_calls'] for t in report['tasks']), 1)

    def test_matching_project_total_reconciles_but_account_total_does_not(self):
        self.receipt('editorial')
        for scope in ('account', 'kovalsky'):
            save_owner_report(self.db, {'amount_usd': 63, 'start': '2026-10-01', 'end': '2026-10-06', 'scope': scope}, self.now)
            report = spending(self.db, self.config, now=self.now)
            self.assertEqual(report['actual']['amount_usd'], 63)
            if scope == 'account':
                self.assertFalse(report['comparable'])
                self.assertIsNone(report['unexplained_usd'])
            else:
                self.assertAlmostEqual(report['unexplained_usd'], 62.9998)

    def test_other_dates_stay_separate_and_month_includes_more_than_seven_days(self):
        self.receipt('editorial')
        self.db.execute('UPDATE api_usage SET created_at=?', ((self.now-timedelta(days=10)).isoformat(),))
        self.db.commit()
        save_owner_report(self.db, {'amount_usd': 63, 'start': '2026-09-01', 'end': '2026-09-30', 'scope': 'kovalsky'}, self.now)
        report = spending(self.db, self.config, now=self.now)
        self.assertIsNone(report['actual'])
        self.assertEqual(report['local']['calls'], 0)
        earlier = spending(self.db, self.config, 'custom', '2026-09-01', '2026-09-30', self.now)
        self.assertEqual(earlier['local']['calls'], 1)
        self.assertEqual(earlier['actual']['amount_usd'], 63)

    def test_pagination_exact_money_groups_and_scoping(self):
        settings = {'project_ids': ['proj_test'], 'dedicated_project': True}
        start,stop = period_range(now=self.now)
        with patch('newsroom.billing.urllib.request.urlopen', side_effect=[
                self.page([('input tokens', .1)], True, 'next'),
                self.page([('input tokens', .2), ('web search', 62.7)])]) as send:
            report = fetch_costs(settings, start, stop, 'private-admin-key')
        self.assertEqual(report['amount_usd'], 63)
        self.assertEqual(report['scope'], 'kovalsky')
        self.assertEqual(sum(r['amount_usd'] for r in report['line_items']), 63)
        query=parse_qs(urlparse(send.call_args_list[1].args[0].full_url).query)
        self.assertEqual(query['group_by'], ['project_id','line_item'])
        self.assertEqual(query['project_ids'], ['proj_test'])
        self.assertEqual(query['page'], ['next'])
        self.assertNotIn('private-admin-key', json.dumps(report))

    def test_failed_pagination_retains_previous_complete_report(self):
        with patch.dict(os.environ, {'TEST_BILLING_ADMIN_KEY': 'private-admin-key'}), \
             patch('newsroom.billing.urllib.request.urlopen', return_value=self.page([('input tokens', 2)])):
            self.assertTrue(sync(self.db,self.config,now=self.now)['ok'])
        first=spending(self.db,self.config,now=self.now)['actual']
        with patch.dict(os.environ, {'TEST_BILLING_ADMIN_KEY': 'private-admin-key'}), \
             patch('newsroom.billing.urllib.request.urlopen', side_effect=[self.page([('input tokens',100)],True,'x'), OSError('private-admin-key')]), \
             patch('newsroom.billing._now', return_value=self.now+timedelta(minutes=10)):
            self.assertFalse(sync(self.db,self.config,now=self.now+timedelta(minutes=10))['ok'])
        report=spending(self.db,self.config,now=self.now+timedelta(minutes=10))
        self.assertEqual(report['actual'],first)
        self.assertNotIn('private-admin-key',json.dumps(report))

    def test_missing_admin_key_makes_no_network_call_and_get_is_read_only(self):
        with patch.dict(os.environ, {}, clear=True), patch('newsroom.billing.urllib.request.urlopen') as send:
            self.assertEqual(sync(self.db,self.config,now=self.now)['code'],'ADMIN_KEY_MISSING')
            readonly=connect_readonly(self.path)
            try:
                report=spending(readonly,self.config,now=self.now)
            finally:
                readonly.close()
            self.assertFalse(report['connection']['configured'])
            send.assert_not_called()

    def test_bad_amount_dates_scope_and_custom_interval_are_rejected(self):
        for payload in ({'amount_usd':'NaN'}, {'amount_usd':-1}, {'amount_usd':'inf'},
                        {'amount_usd':63,'scope':'invented'}, {'amount_usd':63,'start':'2026-10-01'},
                        {'amount_usd':63,'start':'2026-10-08','end':'2026-10-09'}):
            with self.assertRaises(ValueError):save_owner_report(self.db,payload,self.now)
        self.assertIsNone(spending(self.db,self.config,now=self.now)['owner_report'])

    def test_dashboard_report_write_is_authenticated_and_spending_get_is_readonly(self):
        from newsroom import dashboard
        ready, servers = threading.Event(), []
        def make_server(address, handler):
            server = ThreadingHTTPServer(('127.0.0.1',0),handler)
            servers.append(server);ready.set();return server
        with patch('newsroom.dashboard.ThreadingHTTPServer',side_effect=make_server):
            thread=threading.Thread(target=dashboard.serve,args=(self.config,),daemon=True)
            thread.start();self.assertTrue(ready.wait(3));server=servers[0]
            base='http://127.0.0.1:'+str(server.server_port)
            try:
                page=urllib.request.urlopen(base+'/?view=resources',timeout=3).read().decode()
                self.assertIn('Сколько потрачено и на что',page)
                token=re.search(r"const TOKEN='([^']+)'",page).group(1)
                payload=json.dumps({'amount_usd':63}).encode()
                with self.assertRaises(HTTPError) as denied:
                    urllib.request.urlopen(urllib.request.Request(base+'/api/billing/report',data=payload),timeout=3)
                self.assertEqual(denied.exception.code,403);denied.exception.close()
                request=urllib.request.Request(base+'/api/billing/report',data=payload,
                        headers={'Content-Type':'application/json','X-Dashboard-Token':token})
                with urllib.request.urlopen(request,timeout=3) as response:self.assertTrue(json.load(response)['ok'])
                schema=self.db.execute('PRAGMA schema_version').fetchone()[0]
                with urllib.request.urlopen(base+'/api/spending?period=month',timeout=3) as response:report=json.load(response)
                self.assertEqual(report['owner_report']['amount_usd'],63)
                self.assertIsNone(report['actual'])
                self.assertEqual(self.db.execute('PRAGMA schema_version').fetchone()[0],schema)
                with self.assertRaises(HTTPError) as bad:
                    urllib.request.urlopen(base+'/api/spending?period=invalid',timeout=3)
                self.assertEqual(bad.exception.code,400);bad.exception.close()
            finally:
                server.shutdown();server.server_close();thread.join(3)


if __name__=='__main__':
    unittest.main()
