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

    def run_read(self,mutation,manual=None):
        f=self.f;original=copy.deepcopy(self.original)
        if manual is not None:original['execution_context']['manual_risk']=manual
        def calculate(view,values):
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
