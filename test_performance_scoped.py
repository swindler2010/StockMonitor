import sys,tempfile,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
from storage import Store

class ScopedCacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.store=Store(self.tmp.name);self.cache=self.store.read_cache
        self.addCleanup(self.tmp.cleanup);self.addCleanup(self.cache.close)
        self.calls=0
    def load(self):
        self.calls+=1
        return {'dq':self.store.setting('data_quality:A',{}),'identity':self.store.setting('history_identity:A',{})}
    def get(self):return self.cache.get('A',self.load,scoped=True)
    def test_unrelated_commit_reuses_and_replays_dependencies(self):
        a=self.get();self.store.set_setting('unrelated',1)
        with self.cache.track_inputs() as token:self.assertEqual(a,self.get())
        self.assertEqual(self.calls,1);self.assertIn('setting:data_quality:A',token)
        self.assertTrue(self.cache.current(token))
    def test_dq_identity_and_source_changes_invalidate(self):
        self.get();self.store.set_setting('data_quality:A',{'state':'INVALID'});self.assertEqual(self.get()['dq']['state'],'INVALID')
        self.store.set_setting('history_identity:A',{'venue':'FRA','currency':'EUR','symbol':'APC.F'})
        self.assertEqual(self.get()['identity']['venue'],'FRA');self.assertEqual(self.calls,3)
    def test_real_candle_revision_invalidates(self):
        def load():self.cache.depend('market:A:1H');self.calls+=1;return self.calls
        self.assertEqual(self.cache.get('bars',load,scoped=True),1)
        with self.store.connect() as db:db.execute("insert into input_revisions values('market:A:1H',1)")
        self.assertEqual(self.cache.get('bars',load,scoped=True),2)
    def test_cached_result_still_rejected_after_real_change(self):
        self.get()
        with self.cache.track_inputs() as token:self.get()
        self.store.set_setting('data_quality:A',{'state':'GAP'})
        with self.assertRaisesRegex(RuntimeError,'Stale'):self.cache.assert_current(token)
    def test_local_scope_does_not_include_other_instruments(self):
        with self.cache.track_inputs():
            self.cache.depend('instrument:B')
            with self.cache.track_inputs():
                self.cache.depend('instrument:A')
                self.assertEqual(set(self.cache.dependencies(local=True)),{'instrument:A'})
                self.assertEqual(set(self.cache.dependencies()),{'instrument:A','instrument:B'})
    def test_changed_during_loader_not_cached_and_token_rejected(self):
        def loader():
            result=self.load();self.store.set_setting('data_quality:A',{'state':'GAP'});return result
        with self.cache.track_inputs() as token:self.cache.get('race',loader,scoped=True)
        self.assertNotIn('race',self.cache.items)
        with self.assertRaises(RuntimeError):self.cache.assert_current(token)
    def test_empty_dependencies_keep_conservative_global_guard(self):
        def loader():self.calls+=1;return self.calls
        self.cache.get('pure',loader,scoped=True);self.store.set_setting('any',1)
        self.assertEqual(self.cache.get('pure',loader,scoped=True),2)

    def test_frame_cache_separates_draft_listing_and_source(self):
        from snapshot_worker import SnapshotContext,Value
        from unittest.mock import Mock
        c=SnapshotContext();c.store=self.store;c.drafts={}
        info={'isin':'A','name':'A','source_mode':'proxy','history_symbol':'APC.DE','history_venue':'GER','history_currency':'EUR'}
        c._frame_uncached=Mock(return_value={'test':'frame'})
        c.frame_for(info,'1H');c.frame_for(info,'1H')
        self.assertEqual(c._frame_uncached.call_count,1)
        c.drafts['A']={**info,'history_symbol':'APC.F','history_venue':'FRA'}
        c.frame_for(info,'1H');self.assertEqual(c._frame_uncached.call_count,2)
        c.drafts['A']={**info,'source_mode':'gettex'}
        c.frame_for(info,'1H');self.assertEqual(c._frame_uncached.call_count,3)

if __name__=='__main__':unittest.main()
