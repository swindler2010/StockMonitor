import sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import pandas as pd
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
from storage import Store
from snapshot_worker import SnapshotContext,calculate
class InterleavedTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.store=Store(self.tmp.name)
        self.addCleanup(self.tmp.cleanup);self.addCleanup(self.store.read_cache.close)
        self.values=dict(watchlist=[{'isin':'A','name':'Alpha'},{'isin':'B','name':'Beta'}],drafts={},feed_errors={},symbol='Alpha',timeframe='1D',period='Max',ai_config={'provider':'none'})
    def test_real_snapshot_interleaves_and_keeps_order(self):
        calls=[]
        def frame(ctx,info,tf):calls.append(('frame',info['isin']));return pd.DataFrame()
        def engine(ctx,info):calls.append(('engine',info['isin']));return {'signal':'WAIT','benchmark_context':{'name':info['isin']}}
        with patch.object(SnapshotContext,'frame_for',frame),patch.object(SnapshotContext,'trading_analysis',engine):
            result=calculate(self.store,self.values,0)
        self.assertEqual(calls,[('frame','A'),('engine','A'),('frame','B'),('engine','B')])
        self.assertEqual([x['isin'] for x in result['stocks']],['A','B'])
        self.assertEqual(result['market_context'],{'name':'A'})
        self.assertGreaterEqual(result['_calculation_timing']['legacy_snapshot_seconds'],0)
    def test_engine_failure_aborts_without_next_instrument(self):
        calls=[]
        def frame(ctx,info,tf):calls.append(info['isin']);return pd.DataFrame()
        with patch.object(SnapshotContext,'frame_for',frame),patch.object(SnapshotContext,'trading_analysis',side_effect=RuntimeError('Stale input')):
            with self.assertRaisesRegex(RuntimeError,'Stale'):calculate(self.store,self.values,0)
        self.assertEqual(calls,['A'])
    def test_only_changed_instrument_repaired_and_parent_token_clean(self):
        calls=[]
        def frame(ctx,info,tf):return pd.DataFrame()
        def engine(ctx,info):
            calls.append(info['isin']);self.store.setting('data_quality:'+info['isin'],{})
            if calls==['A','B']:self.store.set_setting('data_quality:A',{'state':'GAP'})
            return {'signal':'WAIT','benchmark_context':{}}
        with self.store.read_cache.track_inputs() as parent:
            with patch.object(SnapshotContext,'frame_for',frame),patch.object(SnapshotContext,'trading_analysis',engine):
                result=calculate(self.store,self.values,0)
            self.store.read_cache.assert_current(parent)
        self.assertEqual(calls,['A','B','A'])
        self.assertEqual(result['_calculation_timing']['repaired_instruments'],1)
        self.assertIn('setting:data_quality:A',parent)
    def test_shared_dependency_repair_is_bounded(self):
        calls=[];rounds=[];original=self.store.read_cache.token_for
        def frame(ctx,info,tf):return pd.DataFrame()
        def engine(ctx,info):
            calls.append(info['isin']);self.store.setting('benchmark:test',0)
            return {'signal':'WAIT','benchmark_context':{}}
        def revisions(scopes):
            if isinstance(scopes,set):
                rounds.append(1);self.store.set_setting('benchmark:test',len(rounds))
            return original(scopes)
        with patch.object(SnapshotContext,'frame_for',frame),patch.object(SnapshotContext,'trading_analysis',engine),patch.object(self.store.read_cache,'token_for',side_effect=revisions):
            with self.assertRaisesRegex(RuntimeError,'keep changing'):calculate(self.store,self.values,0)
        self.assertEqual(calls,['A','B','A','B','A','B'])
        self.assertEqual(len(rounds),3)
    def test_concurrent_record_read_retries_only_that_record(self):
        calls=[]
        def engine(ctx,info):
            isin=info['isin'];calls.append(isin)
            self.store.setting('data_quality:'+isin,{})
            if calls==['A','B']:
                self.store.set_setting('data_quality:B',{'state':'GAP'})
                self.store.setting('data_quality:B',{})
            return {'signal':'WAIT','benchmark_context':{}}
        with self.store.read_cache.track_inputs() as parent:
            with patch.object(SnapshotContext,'frame_for',return_value=pd.DataFrame()),patch.object(SnapshotContext,'trading_analysis',engine):
                result=calculate(self.store,self.values,0)
            self.store.read_cache.assert_current(parent)
        self.assertEqual(calls,['A','B','B'])
        self.assertEqual(result['_calculation_timing']['instrument_read_retries'],1)
        self.assertEqual([s['isin'] for s in result['stocks']],['A','B'])
    def test_concurrent_record_read_retry_exhaustion_is_bounded(self):
        calls=[]
        def engine(ctx,info):
            calls.append(info['isin'])
            self.store.setting('data_quality:A',{})
            self.store.set_setting('data_quality:A',{'revision':len(calls)})
            self.store.setting('data_quality:A',{})
        with patch.object(SnapshotContext,'frame_for',return_value=pd.DataFrame()),patch.object(SnapshotContext,'trading_analysis',engine):
            with self.assertRaisesRegex(RuntimeError,'instrument_read_retry_exhausted A'):
                calculate(self.store,self.values,0)
        self.assertEqual(calls,['A','A'])
if __name__=='__main__':unittest.main()
