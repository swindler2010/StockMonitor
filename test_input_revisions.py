"""Only targeted dependency-generation tests; no feeds, no working data."""
import sys,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
from storage import Store
from snapshot_worker import SnapshotContext,Value,inputs
from features import Features
from compute_process import Client
import pandas as pd

class InputRevisionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.store=Store(self.tmp.name)
        self.info={'isin':'US0378331005','name':'Apple','source_mode':'proxy','benchmark_id':None,'position':{'quantity':0}}
        self.store.save_instrument(self.info)
        self.ui=SimpleNamespace(store=self.store,watchlist=[self.info],drafts={},feed_errors={},symbol=Value('Apple'),timeframe=Value('1D'),period=Value('год'),ai_config={'provider':'local'},closing=False,bridge=SimpleNamespace(publish=Mock()),update_ai_view=Mock())
    def tearDown(self):
        self.store.read_cache.close();self.tmp.cleanup()
    def calculate(self,mutation):
        c=SnapshotContext();c.store=self.store
        for k,v in inputs(self.ui).items():setattr(c,k,Value(v) if k in ('symbol','timeframe','period') else v)
        def frame(info,tf):
            self.store.load_series(info['isin'],tf,'proxy','APC.DE')
            self.store.setting('history_identity:'+info['isin'],{})
            self.store.setting('data_quality:'+info['isin']+':'+tf,{})
            return pd.DataFrame()
        c.frame_for=frame
        def core(*a,**kw):mutation();return {'signal':'WAIT','benchmark_context':{}}
        with patch('analytics.analyse_multiframe',side_effect=core):return c.trading_analysis(self.info)
    def test_journal_and_unrelated_settings_do_not_cancel_engine(self):
        def write():self.store.event('test',{});self.store.set_setting('unrelated',1)
        self.assertEqual(self.calculate(write)['signal'],'WAIT')
    def test_other_instrument_does_not_cancel_engine(self):
        self.assertEqual(self.calculate(lambda:self.store.save_instrument({'isin':'OTHER','name':'Other'}))['signal'],'WAIT')
    def test_other_instrument_candles_and_unused_tf_do_not_cancel(self):
        def write():
            with self.store.connect() as db:
                for isin,tf in [('OTHER','1D'),(self.info['isin'],'5m')]:
                    db.execute('INSERT INTO series VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',(isin,'proxy','GER','EUR','X',''+tf,'2026-10-01',1,2,1,2,3))
        self.assertEqual(self.calculate(write)['signal'],'WAIT')
    def test_used_ohlcv_change_rejects(self):
        def write():
            with self.store.connect() as db:db.execute("INSERT INTO series VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",(self.info['isin'],'proxy','GER','EUR','APC.DE','1D','2026-10-01',1,2,1,2,3))
        with self.assertRaisesRegex(RuntimeError,'input dependency'):self.calculate(write)
    def test_used_identity_and_dq_change_rejects(self):
        for key in ('history_identity:'+self.info['isin'],'data_quality:'+self.info['isin']+':1D'):
            with self.subTest(key=key),self.assertRaisesRegex(RuntimeError,'input dependency'):
                self.calculate(lambda:self.store.set_setting(key,{'venue':'FRA','currency':'EUR','state':'INVALID'}))
    def test_instrument_position_change_rejects(self):
        with self.assertRaisesRegex(RuntimeError,'input dependency'):
            self.calculate(lambda:self.store.save_instrument({**self.info,'position':{'quantity':3}}))
    def test_real_worker_publication_ignores_journal_but_checks_dependencies(self):
        client=Client(self.store.path)
        try:
            values=inputs(self.ui);result=client.calculate('snapshot',values,0)
            self.assertEqual(result['stocks'][0]['trading_engine']['status'],'DATA BLOCKED')
            self.store.event('unrelated',{})
            self.assertTrue(Features.accept_background_snapshot(self.ui,result,values))
            self.store.set_setting('history_identity:'+self.info['isin'],{'symbol':'APC.F','venue':'FRA','currency':'EUR'})
            self.assertFalse(Features.accept_background_snapshot(self.ui,result,values))
        finally:
            client.close()
            if client.process:client.process.wait(timeout=10);client.process.stdin.close();client.process.stdout.close()
    def test_cached_frame_replays_its_read_dependencies(self):
        def loader():return self.store.setting('used',1)
        self.store.read_cache.get('probe',loader)
        with self.store.read_cache.track_inputs() as token:self.store.read_cache.get('probe',loader)
        self.store.set_setting('used',2)
        self.assertFalse(self.store.read_cache.current(token))
    def test_benchmark_and_settings_reads_are_dependencies(self):
        with self.store.read_cache.track_inputs() as token:
            self.store.load_series('INDEX:NDX','1D','proxy','^NDX')
            self.store.setting('benchmark_history:INDEX:NDX',{})
            self.store.setting('global_indicators',{})
        self.assertIn('market:INDEX:NDX:1D',token)
        self.store.set_setting('global_indicators',{'EMA':False})
        self.assertFalse(self.store.read_cache.current(token))
    def test_same_value_and_rollback_do_not_invalidate(self):
        self.store.set_setting('used',1)
        with self.store.read_cache.track_inputs() as token:self.store.setting('used')
        self.store.set_setting('used',1)
        self.assertTrue(self.store.read_cache.current(token))
        try:
            with self.store.connect() as db:
                db.execute("UPDATE settings SET value='2' WHERE key='used'")
                raise ValueError('rollback')
        except ValueError:pass
        self.assertTrue(self.store.read_cache.current(token))

if __name__=='__main__':unittest.main()
