import unittest,tempfile,sys,json,hashlib
from pathlib import Path
from unittest.mock import patch
import pandas as pd
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
from storage import Store
from calculation_window import eligibility_turn,eligibility_due,eligibility_state
from candlepilot_health import collect

class QueueEligibility(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.s=Store(self.tmp.name);self.i={'isin':'TEST','name':'Test'};self.s.save_instrument(self.i)
 def tearDown(self):self.s.read_cache.close();self.tmp.cleanup()
 def assessment(self,ctx,info,now):
  return dict(isin=info['isin'],checked_at=now.isoformat(),expires_at=(now+pd.Timedelta(minutes=1)).isoformat(),timeframes={'5m':{'analysis_ready':True,'chart_ready':True}},candidates=[{'timeframe':'5m','analysis_ready':True,'signal_ready':False,'trade_ready':False}])
 def run_turn(self):
  with patch('calculation_window.assess_all',side_effect=self.assessment):return eligibility_turn(self.s,self.i)
 def test_persist_and_resume_no_duplicate(self):
  r=self.run_turn()
  with patch('calculation_window.assess_all',side_effect=AssertionError('duplicate')):self.assertEqual(eligibility_turn(self.s,self.i),r)
  other=Store(self.tmp.name)
  self.assertEqual(eligibility_due(other,[self.i],pd.Timestamp.now(tz='UTC')),[]);other.read_cache.close()
 def test_revision_revoke_before_ui_and_mcp(self):
  self.run_turn();self.assertEqual(collect(self.s.path)['eligibility_summary']['analysis_ready'],1)
  self.s.save_instrument({**self.i,'name':'Changed'})
  r=collect(self.s.path);self.assertEqual(r['eligibility']['TEST']['state'],'STALE');self.assertEqual(r['eligibility_summary']['analysis_ready'],0)
 def test_unrelated_write_does_not_revoke(self):
  self.run_turn();self.s.set_setting('unrelated','new')
  self.assertEqual(collect(self.s.path)['eligibility']['TEST']['state'],'CURRENT')
 def test_expiry_revokes(self):
  r=self.run_turn();r['expires_at']='2000-01-01T00:00:00+00:00';self.s.set_setting('cp_eligibility:TEST',r)
  data=collect(self.s.path);self.assertEqual(data['eligibility']['TEST']['state'],'EXPIRED');self.assertFalse(any(x['analysis_ready'] for x in data['eligibility_rows']))
 def test_race_cannot_publish_ready(self):
  def racing(ctx,info,now):
   r=self.assessment(ctx,info,now);self.s.save_instrument({**self.i,'name':'Changed'});return r
  with patch('calculation_window.assess_all',side_effect=racing):r=eligibility_turn(self.s,self.i)
  self.assertIn('error',r);self.assertEqual(collect(self.s.path)['eligibility_summary']['analysis_ready'],0)
 def test_all_49_and_11_unknown_not_ready(self):
  for n in range(48):self.s.save_instrument({'isin':str(n),'name':str(n)})
  for n in range(6):self.s.save_instrument({'isin':'INDEX:'+str(n),'name':'Index','kind':'benchmark'})
  r=collect(self.s.path);self.assertEqual(r['eligibility_summary']['stocks'],49);self.assertEqual(len(r['eligibility_rows']),539)
  self.assertEqual(r['eligibility_summary']['trade_ready'],0)
 def test_unvisited_before_retry_and_cooldown(self):
  r=self.run_turn();r['expires_at']='2000-01-01T00:00:00+00:00';self.s.set_setting('cp_eligibility:TEST',r)
  other={'isin':'NEW','name':'New'};self.s.save_instrument(other)
  self.assertEqual(eligibility_due(self.s,[self.i,other],pd.Timestamp.now(tz='UTC')),[other])
  self.s.set_setting('cp_eligibility:NEW',{'error':'provider','checked_at':pd.Timestamp.now(tz='UTC').isoformat(),'expires_at':'2099-01-01T00:00:00Z'})
  self.assertEqual(eligibility_due(self.s,[other],pd.Timestamp.now(tz='UTC')),[])
 def test_read_only_and_no_market_data_mutation(self):
  self.run_turn();before=hashlib.sha256(self.s.path.read_bytes()).hexdigest();collect(self.s.path)
  self.assertEqual(hashlib.sha256(self.s.path.read_bytes()).hexdigest(),before)
 def test_eligibility_only_skips_network_pipeline(self):
  from feed_worker import FeedContext
  ctx=FeedContext(self.s)
  with patch.object(ctx,'_run_feed',side_effect=AssertionError('network')),patch('calculation_window.eligibility_turn',return_value={}):
   self.assertEqual(ctx.calculate({**self.i,'_eligibility_only':True}),{'errors':[]})

if __name__=='__main__':unittest.main()
