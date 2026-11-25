import sys,unittest,tempfile,copy
from pathlib import Path
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
from storage import Store
class WatchlistSave(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory(prefix='p1-save-');self.store=Store(self.temp.name);self.items=[{'isin':'A','name':'Alpha','currency':'EUR','position':{}},{'isin':'B','name':'Beta','currency':'EUR','position':{}}];self.store.save_watchlist(self.items)
 def tearDown(self):self.store.read_cache.close();self.temp.cleanup()
 def revisions(self):
  with self.store.connect() as db:return dict(db.execute('SELECT scope,revision FROM input_revisions'))
 def test_unchanged_save_keeps_revisions_and_receipt_current(self):
  token=self.revisions();self.store.save_watchlist(copy.deepcopy(self.items));self.assertEqual(token,self.revisions());self.assertTrue(self.store.read_cache.current(token))
 def test_real_identity_or_position_change_revokes_only_affected(self):
  for field,value in [('currency','USD'),('position',{'quantity':1})]:
   token=self.revisions();changed=copy.deepcopy(self.items);changed[0][field]=value;self.store.save_watchlist(changed)
   self.assertGreater(self.revisions()['instrument:A'],token['instrument:A']);self.assertEqual(self.revisions()['instrument:B'],token['instrument:B']);self.assertFalse(self.store.read_cache.current(token))
 def test_order_change_is_preserved_without_false_revisions(self):
  token=self.revisions();self.store.save_watchlist(self.items[::-1]);self.assertEqual(self.store.instruments(),self.items[::-1]);self.assertEqual(token,self.revisions())
 def test_remove_add_preserve_other_revisions(self):
  token=self.revisions();self.store.save_watchlist([self.items[1],{'isin':'C','name':'Gamma'}]);self.assertGreater(self.revisions()['instrument:A'],token['instrument:A']);self.assertEqual(self.revisions()['instrument:B'],token['instrument:B']);self.assertEqual([i['isin']for i in self.store.instruments()],['B','C'])
 def test_invalid_payload_does_not_partially_save(self):
  token=self.revisions()
  with self.assertRaises(ValueError):self.store.save_watchlist([self.items[0],{'isin':'B','price':float('nan')}])
  self.assertEqual(self.store.instruments(),self.items);self.assertEqual(token,self.revisions())
 def test_duplicate_isin_rolls_back(self):
  token=self.revisions()
  with self.assertRaises(ValueError):self.store.save_watchlist([self.items[0],self.items[0]])
  self.assertEqual(self.store.instruments(),self.items);self.assertEqual(token,self.revisions())
 def test_actual_save_exit_path_keeps_unchanged_instrument_revisions(self):
  self.store.enable_user_drafts();token=self.revisions()
  self.store.set_setting('selected',{'symbol':'Alpha','timeframe':'1D'})
  self.store.save_user_state(copy.deepcopy(self.items))
  self.assertEqual(token,{k:v for k,v in self.revisions().items() if k.startswith('instrument:')});self.assertFalse(self.store.pending_user_settings)
  self.assertEqual(self.store.setting('selected')['timeframe'],'1D')
 def test_actual_save_exit_real_change_and_order_remain_atomic(self):
  self.store.enable_user_drafts();token=self.revisions();items=copy.deepcopy(self.items[::-1]);items[0]['position']={'quantity':2}
  self.store.set_setting('selected',{'symbol':'Beta'});self.store.save_user_state(items)
  self.assertEqual(self.store.instruments(),items);self.assertEqual(token['instrument:A'],self.revisions()['instrument:A']);self.assertGreater(self.revisions()['instrument:B'],token['instrument:B'])
 def test_settings_write_failure_rolls_back_instruments_and_retains_drafts(self):
  import sqlite3
  self.store.enable_user_drafts();token=self.revisions();items=copy.deepcopy(self.items);items[0]['currency']='USD'
  self.store.set_setting('selected',{'symbol':'Beta'})
  with self.store.connect() as db:db.execute("CREATE TRIGGER fail_selection BEFORE INSERT ON settings WHEN NEW.key='selected' BEGIN SELECT RAISE(ABORT,'test failure'); END")
  with self.assertRaises(sqlite3.IntegrityError):self.store.save_user_state(items)
  self.assertEqual(self.store.instruments(),self.items);self.assertEqual(token,self.revisions());self.assertIn('selected',self.store.pending_user_settings)
if __name__=='__main__':unittest.main()
