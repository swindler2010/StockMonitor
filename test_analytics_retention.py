import sys,tempfile,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
from storage import Store
class RetentionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.store=Store(self.tmp.name);self.cache=self.store.read_cache;self.calls=0
        self.addCleanup(self.tmp.cleanup);self.addCleanup(self.cache.close)
    def get(self,isin,key=None):
        def load():
            self.calls+=1;self.cache.depend('instrument:'+isin);self.cache.depend('market:'+isin+':1H')
            return {'signal':'WAIT','dq':self.store.setting('data_quality:'+isin,{})}
        return self.cache.get(key or isin,load,scoped=True,category='analytics')
    def test_54_results_survive_frame_churn(self):
        for n in range(54):
            self.get(str(n))
            for tf in range(11):self.cache.get(('frame',n,tf),lambda:0)
        self.store.set_setting('journal_like_unrelated',1)
        for n in range(54):self.get(str(n))
        self.assertEqual(self.calls,54);self.assertLessEqual(len(self.cache.items),64)
    def test_relevant_changes_recompute_other_instruments_reuse(self):
        self.get('A');self.get('B');self.store.set_setting('data_quality:A',{'state':'GAP'})
        self.assertEqual(self.get('A')['dq']['state'],'GAP');self.get('B');self.assertEqual(self.calls,3)
        with self.store.connect() as db:db.execute("insert into input_revisions values('market:A:1H',1)")
        self.get('A');self.assertEqual(self.calls,4)
    def test_hit_replays_revisions_and_return_is_private(self):
        a=self.get('A');a['signal']='BUY'
        with self.cache.track_inputs() as token:self.assertEqual(self.get('A')['signal'],'WAIT')
        with self.store.connect() as db:db.execute("insert into input_revisions values('instrument:A',1)")
        with self.assertRaises(RuntimeError):self.cache.assert_current(token)
    def test_time_settings_keys_and_bound(self):
        self.get('A',('A','closed1'));self.get('A',('A','closed2'));self.assertEqual(self.calls,2)
        for n in range(200):self.get(str(n))
        self.assertEqual(len(self.cache.analytics_items),128)
if __name__=='__main__':unittest.main()
