"""Automatic cross-source diagnostics use the normal history worker for any stock."""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent/'stock_monitor'))

import pandas as pd

import cp_reconciliation as reconciliation
import cp_time_sales as time_sales
import gettex_chart
from cp_controls import read, write
from test_cp_automatic_repair import Memory


class TimeSales(unittest.TestCase):
    def setUp(self):
        self.store = Memory()
        self.addCleanup(self.store.db.close)
        self.store.db.execute('CREATE TABLE trades(isin,ts,price,volume)')
        self.store.set_setting('cp_gettex_source_policy', dict(version=1, approved=True,
            scope='registered_stocks', exclusive_venue='GETTEX'))
        self.now = pd.Timestamp('2026-11-13T19:00Z')

    def job(self, isin, symbol, base, stamp):
        identity = gettex_chart.identity(isin, symbol)
        job = reconciliation._spec(isin, identity, base, stamp, stamp)
        job['finding'] = dict(state='SOURCE_TIMEBASE_UNCONFIRMED', market_state='NO_TRADES')
        self.store.set_setting(reconciliation.key(job), job)
        return job

    def inputs(self, db, job):
        first = pd.Timestamp(job['first'])
        slots = pd.date_range(first, job['end'], freq='5min', inclusive='left')
        five_minutes = pd.Timedelta(minutes=5)
        return dict(rows=[], fingerprint='test', proof=dict(basis='complete_published_trade_files',
            isin=job['isin'], identity={**job['identity'], 'source':'gettex', 'symbol':job['isin']},
            reference={'GETTEX:20261113':{'fixture':'hash'}},
            no_trades=[(str(at), str(at+five_minutes)) for at in slots]))

    def reply(self, job):
        def fetch(isin, base, start, end):
            self.assertEqual((isin, base), (job['isin'], job['tf']))
            request = dict(ric=job['identity']['symbol'], filter='TRADES_ONLY', count=100,
                fromDate=int(start.timestamp()*1000), toDate=int(end.timestamp()*1000))
            return dict(status='OK', request=request, data=[]), job['identity']
        return fetch

    def test_two_stocks_two_base_intervals_and_no_rewrite_of_risk_input(self):
        jobs = [
            self.job('US30303M1027', 'FB2A.GTX', '15m', '2026-11-13 12:45'),
            self.job('US0378331005', 'AAPL.GTX', '5m', '2026-11-13 14:00'),
        ]
        with patch('candle_schedule.clock', return_value=self.now), \
             patch.object(reconciliation, '_input', side_effect=self.inputs), \
             patch.object(gettex_chart, 'reserve_request', return_value=True) as reserve, \
             patch.object(time_sales.TimeSales, 'fetch') as fetch:
            for job in jobs:
                fetch.side_effect = self.reply(job)
                info = {'isin':job['isin']}
                self.assertEqual(time_sales.turn(self.store, info)['state'], 'COMPARED')
                self.assertIsNone(time_sales.turn(self.store, info))
                key = reconciliation.key(job)
                self.assertEqual(read(self.store.db, key), job)
                receipt = read(self.store.db, time_sales.receipt_key(key))
                self.assertEqual(receipt['vendor_completeness'], 'UNCONFIRMED')
                self.assertEqual(reconciliation.collect(self.store.db, job['isin'])['rows'][0]['time_sales'], receipt)
                self.assertIsNone(read(self.store.db, 'cp_manual_risk:'+job['isin']))
            self.assertEqual(fetch.call_count, 2)
            self.assertEqual(reserve.call_count, 2)

    def test_invalid_newest_job_yields_and_revoked_policy_discards_reply(self):
        i = 'US30303M1027'
        valid = self.job(i, 'FB2A.GTX', '15m', '2026-11-13 12:45')
        invalid = self.job(i, 'FB2A.GTX', '15m', '2026-11-13 13:45')
        def inputs(db, job):
            result = self.inputs(db, job)
            if job == invalid:
                result['proof']['no_trades'] = []
            return result
        def fetch(*args):
            self.store.set_setting('cp_gettex_source_policy', {})
            return self.reply(valid)(*args)
        with patch('candle_schedule.clock', return_value=self.now), \
             patch.object(reconciliation, '_input', side_effect=inputs), \
             patch.object(gettex_chart, 'reserve_request', return_value=True), \
             patch.object(time_sales.TimeSales, 'fetch', side_effect=fetch) as call:
            self.assertIsNone(time_sales.turn(self.store, {'isin':i}))
            call.assert_called_once()
        invalid_receipt = time_sales.receipt_key(reconciliation.key(invalid))
        self.assertEqual(read(self.store.db, invalid_receipt)['state'], 'WAITING_EVIDENCE')
        receipt = read(self.store.db, time_sales.receipt_key(reconciliation.key(valid)))
        self.assertEqual(receipt['state'], 'LOCAL_COMPARED')
        self.assertNotIn('response_digest', receipt)

    def test_saved_comparison_does_not_wait_for_a_provider_slot(self):
        job = self.job('US30303M1027', 'FB2A.GTX', '15m', '2026-11-13 12:45')
        with patch('candle_schedule.clock', return_value=self.now), \
             patch.object(reconciliation, '_input', side_effect=self.inputs), \
             patch.object(gettex_chart, 'reserve_request', return_value=False), \
             patch.object(time_sales.TimeSales, 'fetch') as fetch:
            self.assertIsNone(time_sales.turn(self.store, {'isin':job['isin']}))
            fetch.assert_not_called()
        receipt = read(self.store.db, time_sales.receipt_key(reconciliation.key(job)))
        self.assertEqual(receipt['state'], 'LOCAL_COMPARED')
        self.assertTrue(receipt['local']['official_gap_covered'])

    def test_assistants_prepare_each_stock_once_without_network_and_hand_off(self):
        jobs = [
            self.job('US30303M1027', 'FB2A.GTX', '15m', '2026-11-13 12:45'),
            self.job('US0378331005', 'AAPL.GTX', '5m', '2026-11-13 14:00'),
            self.job('US67066G1040', 'NVDA.GTX', '15m', '2026-11-13 13:45'),
        ]
        infos = [{'isin':job['isin']} for job in jobs]
        with patch('candle_schedule.clock', return_value=self.now), \
             patch.object(reconciliation, '_input', side_effect=self.inputs) as inputs, \
             patch.object(gettex_chart, 'reserve_request', return_value=True) as reserve, \
             patch.object(time_sales.TimeSales, 'fetch', side_effect=self.reply(jobs[0])) as fetch:
            time_sales.prepare_assistants(self.store, infos)
            time_sales.prepare_assistants(self.store, infos)
            self.assertEqual(inputs.call_count, 6)
            reserve.assert_not_called()
            fetch.assert_not_called()
            for job in jobs:
                key = reconciliation.key(job)
                receipt = read(self.store.db, time_sales.receipt_key(key))
                self.assertEqual(receipt['state'], 'LOCAL_COMPARED')
                self.assertEqual(receipt['escalation']['owner'], 'CandlePilot')
                self.assertEqual(read(self.store.db, key), job)
            self.assertEqual(time_sales.turn(self.store, infos[0])['state'], 'COMPARED')
            fetch.assert_called_once()

    def test_assistant_drops_stale_snapshot_before_publication(self):
        job = self.job('US30303M1027', 'FB2A.GTX', '15m', '2026-11-13 12:45')
        before = self.inputs(self.store.db, job)
        after = {**before, 'fingerprint':'changed'}
        with patch('candle_schedule.clock', return_value=self.now), \
             patch.object(reconciliation, '_input', side_effect=[before, after]):
            time_sales.prepare_assistants(self.store, [{'isin':job['isin']}])
        self.assertIsNone(read(self.store.db, time_sales.receipt_key(reconciliation.key(job))))

    def test_assistants_rotate_for_53_and_200_stocks_with_bounded_work(self):
        for count in (53,200):
            self.store.set_setting(time_sales.ASSISTANT_CURSOR,'')
            jobs = [self.job(f'US{1000000000+n}',f'T{n}.GTX','15m','2026-11-13 12:45')
                    for n in range(count)]
            # Only the last five stocks have eligible jobs. Empty scans must advance.
            for job in jobs[:-5]:
                self.store.set_setting(reconciliation.key(job),{**job,'finding':{}})
            infos = [{'isin':job['isin']} for job in reversed(jobs)]
            with patch('candle_schedule.clock',return_value=self.now), \
                 patch.object(reconciliation,'_input',side_effect=self.inputs) as inputs, \
                 patch.object(time_sales,'_due_jobs',wraps=time_sales._due_jobs) as scans:
                for _ in range(count//time_sales.ASSISTANT_SCAN_LIMIT+3):
                    inputs.reset_mock()
                    scans.reset_mock()
                    time_sales.prepare_assistants(self.store,infos)
                    self.assertLessEqual(inputs.call_count,2*time_sales.ASSISTANT_JOB_LIMIT)
                    self.assertLessEqual(scans.call_count,time_sales.ASSISTANT_SCAN_LIMIT)
            for job in jobs[-5:]:
                receipt = time_sales.receipt_key(reconciliation.key(job))
                self.assertEqual(read(self.store.db,receipt)['worker'],'stock_assistant')
            for job in jobs[-5:]:
                self.store.db.execute('DELETE FROM settings WHERE key=?',
                    (time_sales.receipt_key(reconciliation.key(job)),))
            self.store.db.commit()

    def test_latest_closed_core_bars_outrank_archive_then_yield(self):
        i = 'US30303M1027'
        now = pd.Timestamp('2026-11-13T18:50Z')  # 19:50 in Berlin.
        cases = [
            ('15m', '2026-11-13 19:30', 0),  # Forming CORE bar.
            ('15m', '2026-11-13 18:45', 1),  # Latest closed 1H.
            ('15m', '2026-11-13 16:15', 1),  # Latest closed 4H.
            ('5m', '2026-11-13 18:45', 0),   # Separate source series.
            ('15m', '2026-11-10 12:00', 0),
        ]
        jobs = [(reconciliation.key(self.job(i,'FB2A.GTX',base,stamp)), core)
                for base,stamp,core in cases]
        with self.store.connect() as db:
            for lane in ('local','provider'):
                local = lane=='local'
                self.assertEqual(time_sales._due_jobs(db,i,now,local)[:2],jobs[1:3])
                time_sales._advance_rotation(db,i,lane,True)
                time_sales._advance_rotation(db,i,lane,True)
                ordered = time_sales._due_jobs(db,i,now,local)
                self.assertEqual(ordered[:2],jobs[3:])
                self.assertNotIn(jobs[0],ordered)
                time_sales._advance_rotation(db,i,lane,False)
                self.assertEqual(time_sales._due_jobs(db,i,now,local)[:2],jobs[1:3])
