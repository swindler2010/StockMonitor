import unittest,tempfile,sys,json,sqlite3
from pathlib import Path
import pandas as pd
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
from storage import Store
from candle_timing import record_recovery
from candlepilot_health import collect
import data_quality as dq
class Completion(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.s=Store(self.tmp.name)
  self.meta=dict(source='proxy',venue='GER',currency='EUR',symbol='SAP.DE',timezone='Europe/Berlin')
  self.info={'isin':'DE0007164600','name':'SAP'};self.s.save_instrument(self.info)
  self.f=pd.DataFrame([[10,12,9,10,3]]*4,columns=['open','high','low','close','volume'],index=pd.date_range('2026-10-21 09:00',periods=4,freq='h'));self.f.attrs={**self.meta,'timeframe':'1H'}
 def tearDown(self):self.s.read_cache.close();self.tmp.cleanup()
 def count(self):return collect(self.s.path)['recovered_since_tracking']
 def test_real_recovery_once_and_reopen(self):
  self.s.save_series(self.info,self.f.drop(self.f.index[1]),'1H')
  for _ in range(2):dq.repair(self.info,self.s,'SAP.DE','1H',self.meta,lambda *a,**k:self.f.iloc[1:2].copy(),pd.Timestamp('2026-10-21T11:00Z'))
  self.assertEqual(self.count(),1)
  with self.s.connect() as db:self.assertEqual(db.execute('select reason from candle_recoveries').fetchone()[0],'MISSING_INTERIOR')
  other=Store(self.tmp.name);self.assertEqual(collect(other.path)['recovered_since_tracking'],1);other.read_cache.close()
 def test_no_count_for_tail_or_valid_change(self):
  fresh=self.f.copy();fresh.iloc[0,3]=11
  with self.s.connect() as db:record_recovery(db,self.info['isin'],self.meta,'1H',self.f.iloc[:2],fresh)
  self.assertEqual(self.count(),0)
 def test_invalid_and_rollback(self):
  old=self.f.copy();old.iloc[1,1]=1
  with self.s.connect() as db:record_recovery(db,self.info['isin'],self.meta,'1H',old,self.f)
  self.assertEqual(self.count(),1)
  try:
   with self.s.connect() as db:
    db.execute('BEGIN IMMEDIATE');record_recovery(db,self.info['isin'],self.meta,'15m',old,self.f);raise ValueError()
  except ValueError:pass
  self.assertEqual(self.count(),1)
 def test_failed_provider_not_recovered(self):
  self.s.save_series(self.info,self.f.drop(self.f.index[1]),'1H')
  dq.repair(self.info,self.s,'SAP.DE','1H',self.meta,lambda *a,**k:self.f.iloc[:0].copy(),pd.Timestamp('2026-10-21T11:00Z'))
  self.assertEqual(self.count(),0)
 def test_all_tf_unknown_and_core_revision(self):
  self.s.set_setting('data_quality:DE0007164600:SAP.DE:1H',dict(identity=self.meta,base_tf='1H',state='GAP'))
  with self.s.connect() as db:token=dict(db.execute('select scope,revision from input_revisions'))
  snap={'ready':True,'_input_revisions':token,'stocks':[dict(isin=self.info['isin'],timeframe='1H',trading_engine=dict(status='READY',blockers=[],signal='WAIT'))]}
  r=collect(self.s.path,snap);self.assertEqual(len(r['display_rows']),11);self.assertEqual(sum(x['state']=='UNKNOWN' for x in r['display_rows']),10);self.assertEqual(r['core'][self.info['isin']]['state'],'READY');self.assertEqual(r['states'],{'GAP':1})
  self.s.save_instrument({**self.info,'name':'Changed'})
  self.assertEqual(collect(self.s.path,snap)['core'][self.info['isin']]['state'],'STALE')
if __name__=='__main__':unittest.main()
