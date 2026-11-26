"""Exact repair completion; isolated Store, no network, grants or trades."""
import copy,sys,tempfile,unittest
from pathlib import Path
import pandas as pd
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
from storage import Store
import data_quality as dq
class TargetScope(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
  self.s=Store(self.tmp.name);self.addCleanup(self.s.read_cache.close)
  self.info=dict(isin='NL0010273215',name='ASML');self.s.save_instrument(self.info)
  self.meta=dict(source='gettex',symbol=self.info['isin'],venue='GETTEX',currency='EUR',timezone='Europe/Berlin')
  self.now=pd.Timestamp('2026-11-03T11:45Z');self.save('2026-11-03 08:15')
 def save(self,stamp,high=102.):
  f=pd.DataFrame(dict(open=[100.],high=[high],low=[99.],close=[101.],volume=[10.]),index=pd.DatetimeIndex([stamp]))
  f.attrs=self.meta.copy();self.s.save_frame(self.info['isin'],'gettex','15m',f)
 def check(self,first,last=None):return dq.repair_target_complete(self.info,self.s,self.meta['symbol'],'15m',self.meta,(first,last or first),self.now)
 def test_missing_prefix_and_tail_never_complete_from_shorter_native_series(self):
  with self.s.connect() as db:before=db.execute('SELECT * FROM candles').fetchall()
  for size in (53,200):
   for n in range(size):self.assertFalse(self.check('2026-11-03 08:00' if n%2 else '2026-11-03 08:30'))
  with self.s.connect() as db:self.assertEqual(before,db.execute('SELECT * FROM candles').fetchall())
 def test_complete_real_rows_and_bad_geometry_are_independent(self):
  self.save('2026-11-03 08:30');self.assertTrue(self.check('2026-11-03 08:15','2026-11-03 08:30'))
  self.save('2026-11-03 08:30',high=90.);self.assertFalse(self.check('2026-11-03 08:30'))
 def test_empty_interval_requires_exact_independent_source_proof(self):
  proof=dict(isin=self.info['isin'],identity=self.meta,basis='complete_published_trade_files',reference='owned fixture pair',checked_at=self.now.isoformat(),no_trades=[['2026-11-03 08:30','2026-11-03 08:45']],trade_intervals=[])
  self.s.set_setting('interval_evidence:'+self.info['isin'],proof);self.assertTrue(self.check('2026-11-03 08:30'))
  wrong=copy.deepcopy(proof);wrong['identity']['currency']='USD';self.s.set_setting('interval_evidence:'+self.info['isin'],wrong)
  self.assertFalse(self.check('2026-11-03 08:30'))
 def test_future_closed_market_and_changed_listing_are_distinct(self):
  self.assertFalse(self.check('2026-11-03 14:00'));self.assertTrue(self.check('2026-11-01 08:30'))
  self.s.save_instrument({**self.info,'history_venue_lock':'FRA'});self.assertFalse(self.check('2026-11-03 08:15'))
if __name__=='__main__':unittest.main()
