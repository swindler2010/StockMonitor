import json
import sqlite3
import unittest
from pathlib import Path
from unittest.mock import patch
import pandas as pd
from test_p1_parent_recovery import ParentRecovery
import cp_experience as memory
import data_quality as dq
import history_service as hs
from storage import Store
from timeframes import aggregate


class Experience(unittest.TestCase):
    setUp = ParentRecovery.setUp
    tearDown = ParentRecovery.tearDown
    repair = ParentRecovery.repair
    rows = ParentRecovery.rows

    def history(self):
        with self.store.connect() as db:
            return memory.read(db)

    def test_catalogue_references_and_read_do_not_create_table_or_permission(self):
        with self.store.connect() as db:
            db.execute('PRAGMA query_only=ON')
            out = memory.read(db)
            self.assertFalse(memory.exists(db))
        self.assertEqual(out['attempts'], [])
        self.assertIsNone(out['probability'])
        self.assertEqual(out['historical_attempt_total'], 'UNKNOWN')
        root = Path(__file__).parent
        for case in out['cases']:
            for ref in case['evidence']:
                self.assertTrue((root/ref).is_file(), ref)
        self.assertGreaterEqual(len(out['evidence_index']), 30)
        request = next(s for s in out['source_notes'] if s.get('contact'))
        self.assertFalse(request['automatic_send'])
        self.assertNotIn('trade_ready', out)

    def test_committed_repair_learns_method_once_and_survives_store_restart(self):
        self.repair()
        history = self.history()
        self.assertEqual(history['attempts'], [dict(method='children:5m', outcome='SUCCESS', attempts=1)])
        self.assertEqual(len(self.rows()), 3)
        self.assertIn('experience_plan', self.store.setting(dq.key(self.info,self.meta['symbol'],'1H')))
        self.store.read_cache.close()
        self.store = Store(self.tmp.name)
        self.repair()
        self.assertEqual(len(self.history()['recent']), 1)
        self.assertEqual(memory.child_order(self.store,self.info,self.meta,'1H',('15m','5m','1m')), ('5m','15m','1m'))

    def test_success_is_exact_identity_parent_tf_and_isin_scoped(self):
        self.repair()
        default = ('15m','5m','1m')
        for field, value in [('currency','USD'),('source','gettex_chart'),('venue','FRA'),('symbol','OTHER'),('timezone','UTC')]:
            self.assertEqual(memory.child_order(self.store,self.info,{**self.meta,field:value},'1H',default),default)
        self.assertEqual(memory.child_order(self.store,{**self.info,'isin':'OTHER'},self.meta,'1H',default),default)
        self.assertEqual(memory.child_order(self.store,self.info,self.meta,'15m',default),default)

    def test_republished_event_is_idempotent_and_failed_event_not_success(self):
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            args=(db,self.info,self.meta,'1H','2026-10-26 10:00','2026-10-26 10:00',{'market:TEST:5m':1},self.now,'children:5m','FAILED','DQ_REJECTED')
            one=memory.record(*args);two=memory.record(*args)
            self.assertEqual(one,two)
        self.assertEqual(len(self.history()['recent']),1)
        self.assertEqual(memory.child_order(self.store,self.info,self.meta,'1H',('15m','5m')),('15m','5m'))

    def test_revision_race_rolls_back_success_and_records_failed_attempt(self):
        original=hs.recover_from_children
        def race(*a,**kw):
            result=original(*a,**kw)
            self.store.save_instrument({**self.info,'changed':True})
            return result
        with patch.object(hs,'recover_from_children',side_effect=race):report=self.repair()
        self.assertIn('input changed',report['recovery_error'])
        self.assertEqual(len(self.rows()),2)
        self.assertEqual(self.history()['recent'][0]['outcome'],'FAILED')
        self.assertEqual(self.history()['recent'][0]['reason'],'INPUT_CHANGED')
        self.assertFalse(any(x['outcome']=='SUCCESS' for x in self.history()['attempts']))

    def test_preferred_child_revalidated_and_alternative_still_available(self):
        self.repair()
        mid=aggregate(self.child,'5m','15m',self.now)
        self.store.save_series(self.info,mid,'15m')
        with self.store.connect() as db:
            db.execute("DELETE FROM series WHERE base_tf='1H' AND ts='2026-10-26 10:00:00'")
        bad=self.child.loc[['2026-10-26 10:05']].copy();bad['high']=1
        self.store.save_series(self.info,bad,'5m')
        frame=hs.recover_from_children(self.info,self.store,self.meta,'1H','2026-10-26 10:00','2026-10-26 10:00',self.now)
        self.assertIsNotNone(frame)
        self.assertEqual(frame.attrs['recovery_method'],'children:15m')
        self.assertEqual(frame.iloc[0]['high'],102)

    def test_preferred_bad_child_and_empty_source_do_not_create_ready(self):
        self.repair()
        with self.store.connect() as db:
            db.execute("DELETE FROM series WHERE base_tf='1H' AND ts='2026-10-26 10:00:00'")
            db.execute("DELETE FROM series WHERE base_tf='5m' AND ts='2026-10-26 10:05:00'")
        empty=self.child.iloc[:0].copy();empty.attrs=self.meta.copy()
        report=self.repair(lambda *a,**k:empty)
        self.assertNotEqual(report['state'],'READY')
        self.assertEqual(len(self.rows()),2)
        self.assertTrue(any(x['outcome']=='FAILED' for x in self.history()['recent']))

    def test_success_ledger_rolls_back_with_parent_transaction(self):
        with self.assertRaisesRegex(ValueError,'rollback'):
            with self.store.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                memory.record(db,self.info,self.meta,'1H','a','b',{},self.now,'children:5m','SUCCESS')
                raise ValueError('rollback')
        self.assertEqual(self.history()['attempts'],[])

    def test_health_projection_is_readonly_and_distinguishes_cases_from_attempts(self):
        self.repair()
        from candlepilot_health import collect
        with self.store.connect() as db:
            before=db.execute('SELECT count(*) FROM cp_recovery_experience').fetchone()[0]
        result=collect(self.store.path)
        self.assertEqual(result['experience']['attempts'],self.history()['attempts'])
        rows=memory.display_rows(result['experience'])
        self.assertEqual(len(rows),len(memory.catalogue()['cases'])+1)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM cp_recovery_experience').fetchone()[0],before)

    def test_stored_success_does_not_bypass_provider_wait_or_unknown_new_children(self):
        self.repair()
        with self.store.connect() as db:
            db.execute("DELETE FROM series WHERE base_tf='1H' AND ts='2026-10-26 10:00:00'")
            db.execute("DELETE FROM series WHERE base_tf='5m' AND ts='2026-10-26 10:05:00'")
        wait=(self.now+pd.Timedelta(hours=1)).isoformat()
        report=dq.repair(self.info,self.store,self.meta['symbol'],'1H',self.meta,
                         lambda *a,**kw:self.fail('HTTP during provider cooldown'),self.now,network_not_before=wait)
        self.assertEqual(report['next_retry'],wait)
        self.assertEqual(len(self.rows()),2)
        self.assertEqual(len(self.history()['recent']),1)

    def test_old_success_never_allows_forming_parent(self):
        self.repair()
        self.assertIsNone(hs.recover_from_children(self.info,self.store,self.meta,'1H','2026-10-26 10:00','2026-10-26 10:00',pd.Timestamp('2026-10-26T09:30Z')))

    def test_failed_provider_does_not_store_secret_url_as_experience(self):
        with self.store.connect() as db:db.execute("DELETE FROM series WHERE base_tf='5m'")
        def fail(*a,**kw):raise ValueError('https://provider.invalid/?token=PRIVATE')
        self.repair(fail)
        self.assertNotIn('PRIVATE',json.dumps(self.history()['recent']))
        self.assertEqual(self.history()['recent'][0]['outcome'],'FAILED')


from test_local_recovery_cooldown30 import LocalRecoveryCooldown


class RealExperience(unittest.TestCase):
    setUp=LocalRecoveryCooldown.setUp
    rows=LocalRecoveryCooldown.rows
    turn=LocalRecoveryCooldown.turn

    def test_real_gettex_queue_records_only_committed_recovery_and_reuses_experience(self):
        before=self.rows().copy()
        self.turn()
        with self.store.connect() as db:history=memory.read(db)
        self.assertEqual(history['recent'][0]['method'],'children:5m')
        self.assertEqual(history['recent'][0]['outcome'],'SUCCESS')
        self.assertEqual(history['recent'][0]['identity']['currency'],'EUR')
        self.assertEqual(history['recent'][0]['identity']['venue'],'GETTEX')
        pd.testing.assert_frame_equal(self.rows().loc[before.index,list(dq.FIELDS)],before[list(dq.FIELDS)])
        pd.testing.assert_series_equal(self.rows().loc[self.stamp,list(dq.FIELDS)],self.expected.loc[self.stamp,list(dq.FIELDS)],check_dtype=False)
        self.assertEqual(self.store.setting('cp_source_retry:gettex_chart'),self.wait)
        self.assertEqual(memory.child_order(self.store,self.info,self.meta,'15m',('1m','5m')),('5m','1m'))

    def test_real_gettex_waiting_is_not_a_failed_attempt_or_success(self):
        with self.store.connect() as db:db.execute("DELETE FROM series WHERE base_tf='5m' AND ts=?",(str(self.stamp+pd.Timedelta(minutes=5)),))
        self.turn()
        with self.store.connect() as db:history=memory.read(db)
        self.assertEqual(history['attempts'],[])
        self.assertNotIn(self.stamp,self.rows().index)
        self.assertEqual(self.store.setting('cp_source_retry:gettex_chart'),self.wait)


if __name__=='__main__':unittest.main()
