import sys,unittest,json,copy,sqlite3
from pathlib import Path
from unittest.mock import patch,Mock
import pandas as pd
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
import history_backfill as h
import data_quality as q
class Settings:
 def __init__(self,dump=None):
  self.db=sqlite3.connect(':memory:')
  if dump:self.db.executescript(dump)
  else:self.db.execute('create table settings(key primary key,value)')
 def setting(self,k,default=None):
  row=self.db.execute('select value from settings where key=?',(k,)).fetchone();return json.loads(row[0]) if row else default
 def set_setting(self,k,v):self.db.execute('insert or replace into settings values(?,?)',(k,json.dumps(v)));self.db.commit()
class Tests(unittest.TestCase):
 def setUp(self):
  self.s=Settings();self.addCleanup(self.s.db.close);self.i={'isin':'TEST'};self.now=pd.Timestamp('2026-10-18T12:00Z')
  self.r={'state':'INVALID','identity':{'symbol':'TEST.F','venue':'FRA','currency':'EUR','source':'proxy','timezone':'Europe/Berlin'},'base_tf':'1D','issues':['Impossible OHLC'],'ranges':[['2012-03-12','2012-03-12']]}
 def fail(self,report,at,base='1D'):
  with patch.object(h,'_sync',side_effect=q.QualityError(copy.deepcopy(report))) as run:
   with self.assertRaises(q.QualityError):h.sync(self.i,self.s,'TEST.F',base,Mock(),at)
  return run.call_count,self.s.setting(q.key(self.i,'TEST.F',base))
 def test_two_identical_rejections_then_no_download_after_restart(self):
  calls,first=self.fail(self.r,self.now);self.assertEqual(calls,1)
  self.assertEqual(pd.Timestamp(first['raw_retry_after'])-self.now,pd.Timedelta(minutes=5))
  at=self.now+pd.Timedelta(minutes=6);calls,second=self.fail(self.r,at)
  self.assertEqual(pd.Timestamp(second['raw_retry_after'])-at,pd.Timedelta(days=1))
  restored=Settings('\n'.join(self.s.db.iterdump()));self.addCleanup(restored.db.close);self.s=restored
  calls,third=self.fail(self.r,at+pd.Timedelta(hours=1));self.assertEqual(calls,0);self.assertEqual(third,second)
  for k in self.r:self.assertEqual(second[k],self.r[k])
 def test_changed_significant_diagnosis_gets_short_retry(self):
  self.fail(self.r,self.now);changed=copy.deepcopy(self.r);changed['ranges']=[['2020-01-02','2020-01-02']]
  at=self.now+pd.Timedelta(minutes=6);_,r=self.fail(changed,at)
  self.assertEqual(pd.Timestamp(r['raw_retry_after'])-at,pd.Timedelta(minutes=5))
 def test_other_timeframe_not_suppressed(self):
  self.fail(self.r,self.now);self.fail(self.r,self.now+pd.Timedelta(minutes=6))
  calls,r=self.fail({**self.r,'base_tf':'1H'},self.now+pd.Timedelta(minutes=7),'1H');self.assertEqual(calls,1)
 def test_transient_outage_not_daily_blocked(self):
  r={'state':'SOURCE_UNAVAILABLE','issues':['outage'],'ranges':[]}
  self.fail(r,self.now);at=self.now+pd.Timedelta(minutes=6);_,r=self.fail(r,at)
  self.assertEqual(pd.Timestamp(r['raw_retry_after'])-at,pd.Timedelta(minutes=5))
if __name__=='__main__':unittest.main()
