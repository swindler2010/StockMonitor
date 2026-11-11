import unittest,sys,tempfile,json
from pathlib import Path
from unittest.mock import patch
import pandas as pd
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
from storage import Store
from feed_worker import FeedContext
from calculation_window import eligibility_state

class RefreshBoundary(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.store=Store(self.tmp.name)
  self.sap={'isin':'DE0007164600','name':'SAP'};self.other={'isin':'OTHER','name':'Other'}
  for i in (self.sap,self.other):self.store.save_instrument(i)
  self.ctx=FeedContext(self.store);self.calls=[]
 def tearDown(self):self.store.read_cache.close();self.tmp.cleanup()
 def feed(self,i):self.ctx.events.put((None,(i,[])))
 def assessment(self,c,i,n):
  self.calls.append(i['isin'])
  return dict(isin=i['isin'],checked_at=n.isoformat(),expires_at=(n+pd.Timedelta(seconds=60)).isoformat(),timeframes={},candidates=[])
 def execute(self,info):
  with patch.object(self.ctx,'_run_feed',side_effect=self.feed),patch('calculation_window.assess_all',side_effect=self.assessment):return self.ctx.calculate(info)
 def test_expired_sap_serviced_during_other_queued_feed(self):
  self.store.set_setting('cp_eligibility:'+self.sap['isin'],{'isin':self.sap['isin'],'checked_at':'2000-01-01T00:00:00Z','expires_at':'2000-01-01T00:15:00Z','execution_version':1})
  # Ensure both have prior records: existing five-stock priority applies.
  self.store.set_setting('cp_eligibility:OTHER',{'isin':'OTHER','checked_at':'2000-01-01T00:00:00Z','expires_at':'2000-01-01T00:15:00Z','execution_version':1})
  self.assertEqual(self.execute(self.other),{'errors':[]});self.assertEqual(self.calls,[self.sap['isin'],'OTHER'])
  r=self.store.setting('cp_eligibility:'+self.sap['isin'],{})
  with self.store.connect() as db:self.assertEqual(eligibility_state(db,r,pd.Timestamp.now(tz='UTC')),'CURRENT')
  self.assertEqual((pd.Timestamp(r['expires_at'])-pd.Timestamp(r['checked_at'])).total_seconds(),900)
 def test_same_due_instrument_not_assessed_twice(self):
  self.execute(self.sap);self.assertEqual(self.calls,[self.sap['isin']])
 def test_fast_poll_does_not_add_history_or_eligibility_work(self):
  self.execute({**self.other,'_gettex_poll_only':True});self.assertEqual(self.calls,[])
 def test_only_one_urgent_job_before_own_assessment(self):
  for n in range(12):self.store.save_instrument({'isin':str(n),'name':str(n)})
  self.execute(self.other);self.assertEqual(len(self.calls),2)
 def test_urgent_revision_race_still_rejects_and_feed_completes(self):
  normal=self.assessment
  def changing(c,i,n):
   r=normal(c,i,n)
   if i['isin']==self.sap['isin']:self.store.save_instrument({**i,'name':'Changed'})
   return r
  self.assessment=changing
  self.assertEqual(self.execute(self.other),{'errors':[]})
  r=self.store.setting('cp_eligibility:'+self.sap['isin'],{})
  self.assertIn('error',r);self.assertNotIn('candidates',r)

if __name__=='__main__':unittest.main()
