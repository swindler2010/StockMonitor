"""Exact imported-source reads and CP hand-off; no website or working DB writes."""
import csv
import copy
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / 'stock_monitor'))
from storage import Store
from performance_cache import ReadCache, install_input_revisions
from snapshot_worker import SnapshotContext, Value
from analytical_read import calculate, calculate_consistent
import data_quality as dq
import onvista_archive as ov
from eur_listings import stored_identity

NOW = pd.Timestamp('2026-11-24T21:30:00Z')
INFO = dict(isin='FR001400AJ45', name='Michelin', source_mode='proxy', benchmark_id=None)
META = dict(source='onvista', symbol='120477648', venue='GETTEX', currency='EUR', timezone='Europe/Berlin')


class OnvistaCalculationBinding(unittest.TestCase):
    def setUp(self):
        # Addressed temporary files in the owner's existing archive; no folder.
        archive = ROOT.parent.parent / 'СМ-Архив'
        self.files = []
        for suffix in ('.sqlite3', '.csv'):
            with tempfile.NamedTemporaryFile(prefix='SM-Onvista-test-', suffix=suffix, dir=archive, delete=False) as f:
                self.files.append(Path(f.name))
        self.addCleanup(self.clean)
        self.store = Store.__new__(Store)
        self.store.path = self.files[0]
        self.store.directory = archive
        with closing(sqlite3.connect(self.store.path)) as db, db:
            db.executescript('''
                CREATE TABLE instruments(isin TEXT PRIMARY KEY,payload TEXT NOT NULL);
                CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE candles(isin TEXT,source TEXT,base_tf TEXT,ts TEXT,open REAL,high REAL,low REAL,close REAL,volume REAL,PRIMARY KEY(isin,source,base_tf,ts));
                CREATE TABLE series(isin TEXT,source TEXT,venue TEXT,currency TEXT,symbol TEXT,base_tf TEXT,ts TEXT,open REAL,high REAL,low REAL,close REAL,volume REAL,PRIMARY KEY(isin,source,venue,currency,symbol,base_tf,ts));
                CREATE TABLE journal(id INTEGER PRIMARY KEY,at TEXT,kind TEXT,payload TEXT);
            ''')
            install_input_revisions(db)
        self.store.read_cache = ReadCache(self.store.path)
        self.store.save_instrument(INFO)
        self.grid = dq.expected_index(META, '1D', '2021-11-24', '2026-11-23', NOW)
        with self.files[1].open('w', encoding='utf-8-sig', newline='') as f:
            writer = csv.writer(f, delimiter=';')
            writer.writerow(ov.HEADER)
            for stamp in self.grid:
                writer.writerow([stamp.strftime('%d.%m.%Y'), '10,00 EUR', '12,00 EUR', '9,00 EUR', '11,00 EUR', '0 Stk.'])
        self.manifest = dict(**META, status='CONFIRMED', isin=INFO['isin'], base_tf='1D',
            source_url='https://www.onvista.de/aktien/historische-kurse/Michelin-Aktie-'+INFO['isin']+'?notation=120477648',
            market_label='gettex (EUR, Echtzeit)', sha256=hashlib.sha256(self.files[1].read_bytes()).hexdigest(),
            observed_rows=len(self.grid), observed_first=str(self.grid[0].date()), observed_last=str(self.grid[-1].date()),
            requested_start='2021-11-24', requested_end='2026-11-24', export_method='ISOLATED_TEST_FIXTURE')

    def clean(self):
        if hasattr(self, 'store') and hasattr(self.store, 'read_cache'):
            self.store.read_cache.close()
        for p in self.files:
            if p.exists():
                p.unlink()

    def install(self):
        plan = ov.prepare(self.store, INFO, self.files[1], self.manifest, NOW)
        ov.commit(self.store, plan)
        return plan

    def values(self, **args):
        return dict(watchlist=[INFO], drafts={}, symbol=INFO['name'], timeframe=args.get('timeframe', '1D'),
            period='Max', ai_config={}, feed_errors={}, request={**dict(isin=INFO['isin'], timeframe='1D',
                source='onvista', symbol=META['symbol'], venue='GETTEX', currency='EUR', closed_only=True, limit=2), **args})

    def read(self, **args):
        with patch('analytical_read.read_time', return_value=NOW):
            return calculate_consistent(self.store, self.values(**args))

    def test_import_enqueues_existing_cp_assessment_transactionally(self):
        self.store.set_setting('cp_assessment_request:'+INFO['isin'], dict(isin=INFO['isin'],requested_at=123,explicit_recheck_at=456))
        plan = self.install()
        request = self.store.setting('cp_assessment_request:'+INFO['isin'])
        self.assertEqual(request['requested_at'], 123)
        self.assertEqual(request['explicit_recheck_at'], 456)
        self.assertGreater(request['market_revisions']['market:'+INFO['isin']+':1D'], 0)
        with self.store.connect() as db:
            before = db.execute('SELECT * FROM settings ORDER BY key').fetchall()
        self.assertEqual(ov.commit(self.store, ov.prepare(self.store,INFO,self.files[1],self.manifest,NOW))['inserted'], 0)
        with self.store.connect() as db:
            self.assertEqual(before, db.execute('SELECT * FROM settings ORDER BY key').fetchall())

    def test_failed_queue_handoff_rolls_back_whole_import(self):
        plan = ov.prepare(self.store,INFO,self.files[1],self.manifest,NOW)
        with patch('cp_assessment.request_from_db', side_effect=RuntimeError('isolated queue failure')):
            with self.assertRaisesRegex(RuntimeError,'queue failure'):
                ov.commit(self.store,plan)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM series').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT count(*) FROM settings').fetchone()[0],0)

    def test_stored_proof_is_exact_daily_only_and_revocable(self):
        self.install()
        with self.store.connect() as db:
            self.assertEqual(stored_identity(db,INFO,META,'1D')['listing_proof']['status'],'CONFIRMED')
            self.assertIsNone(stored_identity(db,INFO,META,'5m'))
            self.assertIsNone(stored_identity(db,INFO,{**META,'currency':'USD'},'1D'))
        key='onvista_archive:'+INFO['isin']+':'+META['symbol']
        receipt=self.store.setting(key);receipt['listing_proof']['status']='UNCONFIRMED'
        self.store.set_setting(key,receipt)
        with self.store.connect() as db:self.assertIsNone(stored_identity(db,INFO,META,'1D'))

    def test_full_source_read_avoids_display_source_reselection(self):
        self.install()
        with patch('partial_chart.select_display', side_effect=AssertionError('Source must stay pinned')):
            result=self.read()
        self.assertEqual(result['identity'],META)
        self.assertEqual(result['bars'][0]['close'],11)
        self.assertEqual(result['data_quality']['state'],'READY')
        self.assertIsNone(result['execution_context'])
        self.assertFalse(result['analysis_context']['execution_permission'])
        for row in result['analysis_context']['trading_engine'].get('timeframe_context',{}).values():
            self.assertNotIn('IDENTITY_MISMATCH', str(row))

    def test_cp_secondary_timeframes_never_fall_back_to_native(self):
        self.install()
        ctx=SnapshotContext();ctx.store=self.store;ctx.drafts={};ctx.set_analysis_scope(NOW)
        entry=ctx.pinned_frame(INFO,'1D',META)
        with patch.object(ctx,'frame_for',side_effect=AssertionError('Cross-source fallback')):
            for tf in ('1D','1W','1M'):
                f=ctx.frame_for_entry(INFO,tf,entry)
                self.assertFalse(f.empty)
                self.assertEqual({k:f.attrs.get(k) for k in META},META)
        self.assertTrue(any(s['identity']==META for s in ctx.closed_specs.values()))

    def test_intraday_and_incomplete_or_wrong_identity_rejected(self):
        self.install()
        with self.assertRaisesRegex(ValueError,'DAILY_ONLY'):self.read(timeframe='5m')
        values=self.values();values['request'].pop('symbol')
        with self.assertRaisesRegex(ValueError,'EXACT_STORED_IDENTITY_REQUIRED'):calculate(self.store,values)
        with self.assertRaisesRegex(ValueError,'Unconfirmed'):self.read(symbol='999')
        with self.assertRaisesRegex(ValueError,'Unconfirmed'):self.read(currency='USD')

    def test_missing_secondary_intraday_is_source_unavailable_not_bad_prices(self):
        self.install();ctx=SnapshotContext();ctx.store=self.store;ctx.drafts={};ctx.set_analysis_scope(NOW)
        entry=ctx.pinned_frame(INFO,'1D',META)
        missing=ctx.frame_for_entry(INFO,'4H',entry)
        self.assertTrue(missing.empty)
        self.assertEqual(missing.attrs['data_quality']['state'],'SOURCE_UNAVAILABLE')
        self.assertEqual(missing.attrs['source'],'onvista')
        self.assertIn('ONVISTA_DAILY_ONLY',str(missing.attrs['data_quality']))
        from analytics import _safe_features
        _,blocked=_safe_features(missing,INFO,NOW)
        self.assertEqual(blocked['data_quality']['state'],'SOURCE_UNAVAILABLE')
        self.assertFalse(blocked['ohlcv_valid'])

    def test_sparse_reference_is_not_promoted_or_silently_replaced(self):
        self.install()
        with self.store.connect() as db:
            db.execute("DELETE FROM series WHERE ts=?",(str(self.grid[-10]),))
        result=self.read()
        self.assertEqual(result['identity'],META)
        self.assertEqual(result['data_quality']['state'],'GAP')
        self.assertFalse(result['analysis_context']['analysis_ready'])
        self.assertFalse(result['analysis_context']['execution_permission'])

    def test_changed_used_rows_reject_old_page_revision(self):
        self.install();first=self.read()
        with self.store.connect() as db:
            db.execute('UPDATE series SET close=10.5 WHERE ts=?',(str(self.grid[-1]),))
        with self.assertRaisesRegex(ValueError,'STALE_PAGE'):
            self.read(offset=2,expected_revision=first['revision'])

    def test_receipt_revocation_invalidates_pinned_cache(self):
        self.install();ctx=SnapshotContext();ctx.store=self.store;ctx.drafts={};ctx.set_analysis_scope(NOW)
        self.assertFalse(ctx.pinned_frame(INFO,'1D',META).empty)
        key='onvista_archive:'+INFO['isin']+':'+META['symbol']
        saved=self.store.setting(key);saved['listing_proof']['isin']='US0378331005'
        self.store.set_setting(key,saved)
        with self.assertRaisesRegex(ValueError,'Unconfirmed'):ctx.pinned_frame(INFO,'1D',META)

    def test_corrupt_prices_remain_blocking(self):
        self.install()
        with self.store.connect() as db:
            db.execute('UPDATE series SET high=1 WHERE ts=?',(str(self.grid[-1]),))
        result=self.read()
        self.assertEqual(result['data_quality']['state'],'INVALID')
        self.assertFalse(result['analysis_context']['analysis_ready'])

    def test_stale_daily_source_is_not_promoted(self):
        self.install()
        with patch('analytical_read.read_time',return_value=NOW+pd.Timedelta(days=3)):
            result=calculate_consistent(self.store,self.values())
        self.assertEqual(result['data_quality']['state'],'STALE')
        self.assertFalse(result['analysis_context']['analysis_ready'])

    def test_weekly_and_monthly_reads_keep_exact_imported_source(self):
        self.install()
        for tf in ('1W','1M'):
            result=self.read(timeframe=tf)
            self.assertEqual(result['identity'],META)
            self.assertGreater(result['total_count'],0)
            self.assertFalse(result['analysis_context']['execution_permission'])
            self.assertEqual(result['provenance'].get('aggregation_base'),'1D')
            self.assertNotIn('DAILY_PROVENANCE',str(result['data_quality']))
            self.assertTrue(result['analysis_context']['analysis_ready'],
                result['analysis_context']['trading_engine'].get('blockers'))

    def test_daily_calendar_can_prove_historical_whole_weeks_and_months(self):
        self.install()
        from analytics import resample
        raw=ov.available_daily(self.store,INFO,NOW)[0]
        self.assertEqual(raw.attrs['timeframe'],'1D')
        for tf in ('1W','1M'):
            derived=resample(raw,'1D',tf,now=NOW)
            self.assertEqual(derived.attrs['aggregation_base'],'1D')
            self.assertTrue(derived['bar_complete'].iloc[1:-1].all())
            self.assertNotIn('DAILY_PROVENANCE',str(derived.attrs['data_quality']))
            self.assertEqual(derived.attrs['source'],'onvista')


class GettexDeadlineBoundary(unittest.TestCase):
    def test_november_padding_does_not_turn_all_eleven_tf_into_one_minute(self):
        from calculation_window import context_deadline
        now=pd.Timestamp('2026-11-24T21:45:46Z')
        for tf in ('5m','10m','15m','30m','1H','2H','3H','4H','1D','1W','1M'):
            deadline=context_deadline(META,tf,now)
            self.assertGreater(deadline,now+pd.Timedelta(minutes=1),tf)
        self.assertEqual(context_deadline(META,'1D',now),pd.Timestamp('2026-11-24T22:00:00Z'))
        self.assertEqual(context_deadline(META,'1W',now),pd.Timestamp('2026-11-27T22:00:00Z'))
        self.assertEqual(context_deadline(META,'1M',now),pd.Timestamp('2026-11-30T22:00:00Z'))

    def test_unverified_next_year_and_bad_identity_keep_conservative_fallback(self):
        from calculation_window import context_deadline
        now=pd.Timestamp('2027-01-04T12:00:00Z')
        self.assertEqual(context_deadline(META,'1D',now),now+pd.Timedelta(minutes=1))
        self.assertEqual(context_deadline({**META,'timezone':'UTC'},'1D',NOW),NOW+pd.Timedelta(minutes=1))

    def test_year_boundary_does_not_invent_a_complete_week_in_2027(self):
        from calculation_window import context_deadline
        now=pd.Timestamp('2026-12-29T12:00:00Z')
        self.assertEqual(context_deadline(META,'1W',now),now+pd.Timedelta(minutes=1))
        self.assertEqual(context_deadline(META,'1M',now),pd.Timestamp('2026-12-30T22:00:00Z'))


if __name__=='__main__':
    unittest.main()
