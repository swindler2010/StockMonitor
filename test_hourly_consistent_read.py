"""Concurrent writer regression: exact read snapshot, no relaxed market guard."""
import copy
import sqlite3
import tempfile
import unittest
from pathlib import Path
from contextlib import contextmanager,closing
from types import SimpleNamespace
from unittest.mock import patch
import test_paper_hourly as fixture
from performance_cache import ReadCache,InputRevisionChanged


class ConsistentReadTests(unittest.TestCase):
    def setUp(self):
        self.f=fixture.HourlyTests();self.f.setUp();f=self.f
        self.used='data_quality:'+f.isin+':AAPL.GTX:1H'
        self.other='data_quality:'+f.isin+':'+f.isin+':15m'
        for key in (self.used,self.other):f.write(key,{'state':'READY','identity':f.identity,'checked_at':'old'})
        self.original=f.result_for('WAIT');self.tmp=tempfile.TemporaryDirectory(prefix='sm-hourly-view-')
        self.path=Path(self.tmp.name)/'data.sqlite3'
        with closing(sqlite3.connect(self.path)) as c:
            f.db.commit();f.db.backup(c);c.execute('PRAGMA journal_mode=WAL')
        @contextmanager
        def connect():
            db=sqlite3.connect(self.path.resolve().as_uri()+'?mode=ro',uri=True)
            try:yield db
            finally:db.close()
        self.store=SimpleNamespace(path=self.path,connect=connect)

    def tearDown(self):self.f.db.close();self.tmp.cleanup()

    def run_read(self,mutation,manual=None,prepare=None):
        original=copy.deepcopy(self.original)
        if manual is not None:original['execution_context']['manual_risk']=manual
        def calculate(view,values,**kwargs):
            if prepare is not None:
                prepare(view.read_cache.db,original)
            for key in original['_input_revisions']:view.read_cache.depend(key)
            before=view.read_cache.db.execute('SELECT close FROM series LIMIT 1').fetchone()
            with closing(sqlite3.connect(self.path)) as writer:
                mutation(writer);writer.commit()
            self.assertEqual(view.read_cache.db.execute('SELECT close FROM series LIMIT 1').fetchone(),before)
            # Repeat a read after the writer commits: old code read a new
            # revision midway through one context and failed inconsistently.
            view.read_cache.depend('setting:'+self.other)
            return original
        with patch.object(fixture.hourly,'calculate',calculate):
            return fixture.hourly.calculate_snapshot(self.store,{'instrument_revisions':{}})

    def test_other_provider_dq_change_does_not_interrupt_closed_context(self):
        def change(db):db.execute('UPDATE settings SET value=? WHERE key=?',('{"state":"GAP"}',self.other))
        result=self.run_read(change)
        self.assertNotIn('setting:'+self.other,result['_input_revisions'])
        self.assertEqual(result['execution_context']['trading_engine']['signal'],'WAIT')

    def test_changed_used_dq_or_closed_candle_still_reject(self):
        with self.assertRaises(InputRevisionChanged):
            self.run_read(lambda db:db.execute('UPDATE series SET close=100 WHERE base_tf=?',('1H',)))

    def test_changed_used_dq_still_rejects(self):
        with self.assertRaises(InputRevisionChanged):
            self.run_read(lambda db:db.execute('UPDATE settings SET value=? WHERE key=?',('{"state":"INVALID"}',self.used)))

    def test_recheck_timestamp_only_uses_existing_semantic_guard(self):
        result=self.run_read(lambda db:db.execute("UPDATE settings SET value=json_set(value,'$.checked_at','new') WHERE key=?",(self.used,)))
        with self.store.connect() as db:
            revision=db.execute('SELECT revision FROM input_revisions WHERE scope=?',('setting:'+self.used,)).fetchone()[0]
        self.assertEqual(result['_input_revisions']['setting:'+self.used],revision)

    def test_newer_quote_cannot_rebase_over_still_valid_hourly_bars(self):
        f=self.f
        def insert(db):db.execute('INSERT INTO series VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
            (f.isin,'gettex_chart','GETTEX','EUR','AAPL.GTX','15m','2026-11-04 10:15:00',100,102,99,100,0))
        with self.assertRaisesRegex(InputRevisionChanged,'quote changed'):self.run_read(insert)

    def test_quote_only_listing_transport_receipt_is_rebased_but_identity_is_not(self):
        f=self.f
        key=f.proof_setting
        def receipt(db):
            db.execute("UPDATE settings SET value=json_set(value,'$.received_at','new') WHERE key=?",(key,))
        result=self.run_read(receipt)
        with self.store.connect() as db:
            revision=db.execute('SELECT revision FROM input_revisions WHERE scope=?',('setting:'+key,)).fetchone()[0]
        self.assertEqual(result['_input_revisions']['setting:'+key],revision)
        def identity(db):
            db.execute("UPDATE settings SET value=json_set(value,'$.identity.venue','FRA') WHERE key=?",(key,))
        with self.assertRaisesRegex(InputRevisionChanged,'input changed: setting:gettex_chart'):
            self.run_read(identity)

    def test_v5_supplemental_15m_range_is_checked_when_core_shares_its_revision(self):
        from paper_v5 import peer_rows
        from performance_cache import capture_closed_inputs

        f=self.f
        row_prefix=(f.isin, f.identity['source'], f.identity['venue'], 'EUR', f.identity['symbol'], '15m')
        stamps=('08:45', '09:00', '09:15', '09:30', '09:45', '10:00', '10:15')
        with closing(sqlite3.connect(self.path)) as db, db:
            db.executemany('INSERT INTO series VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                ((*row_prefix,'2026-11-04 '+stamp+':00',100.,102.,99.,100.,20.) for stamp in stamps))

        def calculate_ranges(db,result):
            inputs={}
            peer_rows(db,f.isin,f.identity,'2026-11-04 09:00:00','2026-11-04 10:15:00',inputs)
            token=result['_input_revisions']
            scope=next(iter(inputs))
            token[scope]=db.execute('SELECT revision FROM input_revisions WHERE scope=?',(scope,)).fetchone()[0]
            spec=dict(isin=f.isin,identity=f.identity,base_tf='15m',timeframe='4H',
                      through='2026-11-04T09:30:00Z',**{'from':'2026-11-04 09:30:00'})
            result['timeframe']='4H'
            result['_peer_inputs']=inputs
            result['hourly']['proof']=capture_closed_inputs(db,[spec],token)

        def edit_at(stamp):
            return lambda db:db.execute("UPDATE series SET close=101 WHERE base_tf='15m' AND ts=?",
                ('2026-11-04 '+stamp+':00',))

        self.run_read(edit_at('08:45'),prepare=calculate_ranges)
        with self.assertRaisesRegex(InputRevisionChanged,'supplemental input changed'):
            self.run_read(edit_at('09:00'),prepare=calculate_ranges)

    def test_v5_entry_screen_does_not_publish_after_quote_input_changes(self):
        f=self.f
        scope='market:'+f.isin+':15m'

        def screen(db,result):
            result.setdefault('_input_revisions',{})[scope]=0
            self.assertIsNone(db.execute("SELECT revision FROM input_revisions WHERE scope=?",(scope,)).fetchone())
            result.update(_v5_entry_screen=True,timeframe='4H')
            result.pop('hourly')

        def insert_quote(db):
            db.execute('INSERT INTO series VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                (f.isin,'gettex_chart','GETTEX','EUR','AAPL.GTX','15m','2026-11-04 10:15:00',100,102,99,100,20))

        self.assertTrue(self.run_read(lambda db:None,prepare=screen)['_v5_entry_screen'])
        with self.assertRaisesRegex(InputRevisionChanged,'V5 entry screen input changed'):
            self.run_read(insert_quote,prepare=screen)

    def test_owner_config_and_permission_changes_remain_blocking(self):
        with self.assertRaises(InputRevisionChanged):
            self.run_read(lambda db:db.execute("UPDATE settings SET value=json_set(value,'$.enabled',0) WHERE key=?",(fixture.hourly.CONFIG,)))

    def test_new_unknown_problem_is_rechecked_by_current_manual_gate(self):
        with patch('cp_controls.manual_gate',side_effect=ValueError('PAPER_MANUAL_NEW_OR_UNACCEPTED_PROBLEM')) as gate:
            with self.assertRaisesRegex(ValueError,'NEW_OR_UNACCEPTED'):
                self.run_read(lambda db:None,manual={'grant_digest':'old'})
            self.assertEqual(gate.call_count,1)

    def test_candidate_proof_joins_existing_consistent_read_transaction(self):
        from calculation_window import _candidate_proof
        with self.store.connect() as db:
            db.execute('BEGIN')
            @contextmanager
            def connect():yield db
            proof=_candidate_proof(SimpleNamespace(connect=connect),
                [x['spec'] for x in self.original['hourly']['proof']['series']],self.original['_input_revisions'])
            self.assertTrue(db.in_transaction)
            self.assertEqual(proof,self.original['hourly']['proof'])


if __name__=='__main__':unittest.main()
