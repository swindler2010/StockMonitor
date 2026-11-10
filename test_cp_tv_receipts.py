"""TV request completion through existing CP eligibility queue, isolated DB."""
import sys,pathlib,tempfile,unittest
from unittest.mock import patch
import pandas as pd
sys.path.insert(0,str(pathlib.Path(__file__).parent/'stock_monitor'))
from storage import Store
import calculation_window as cw
from cp_reconciliation import PROGRESS
from paper_account import UNIVERSE_VERSION

class Receipts(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
  self.s=Store(self.tmp.name);self.addCleanup(self.s.read_cache.close)
  self.i={'isin':'US02079K1079','name':'Alphabet','kind':'stock'};self.s.save_instrument(self.i)
  self.request='2026-11-02T19:16:27+00:00';self.s.set_setting(PROGRESS+self.i['isin'],{'analysis_requested_at':self.request})
 def assessment(self,ctx,info,now):
  return dict(isin=info['isin'],checked_at=now.isoformat(),expires_at=(now+pd.Timedelta(minutes=10)).isoformat(),
    input_binding_version=2,closed_input_semantics=2,timeframes={},candidates=[],
    analysis_scope={'policy':cw.SCOPE_POLICY,'start':(now.tz_convert('Europe/Berlin').normalize()-pd.DateOffset(years=5)).date().isoformat(),'timezone':'Europe/Berlin'})
 def turn(self,fn=None):
  with patch.object(cw,'assess_all',side_effect=fn or self.assessment),patch.object(cw,'attach_execution'):
   return cw.eligibility_turn(self.s,self.i)
 def test_normal_queue_turn_completes_saved_tv_request(self):
  result=self.turn();p=self.s.setting(PROGRESS+self.i['isin'])
  self.assertNotIn('analysis_requested_at',p);self.assertEqual(p['analysis_receipt']['assessment_checked_at'],result['checked_at'])
  self.assertIsNone(p['analysis_receipt']['error']);self.assertEqual(result['paper_universe_version'],UNIVERSE_VERSION)
 def test_unchanged_proof_reuse_completes_without_recalculation(self):
  previous=self.turn();self.s.set_setting(PROGRESS+self.i['isin'],{'analysis_requested_at':'later'})
  result=self.turn(lambda *a: self.fail('duplicate computation'))
  self.assertEqual(result,previous);self.assertEqual(self.s.setting(PROGRESS+self.i['isin'])['analysis_receipt']['requested_at'],'later')
 def test_newer_request_during_calculation_is_not_acknowledged(self):
  def race(*args):
   self.s.set_setting(PROGRESS+self.i['isin'],{'analysis_requested_at':'new-request'})
   return self.assessment(*args)
  self.turn(race);self.assertEqual(self.s.setting(PROGRESS+self.i['isin']),{'analysis_requested_at':'new-request'})
 def test_changed_input_cannot_publish_ready_and_receipt_preserves_error(self):
  def race(*args):
   self.s.save_instrument({**self.i,'name':'changed'});return self.assessment(*args)
  result=self.turn(race);self.assertIn('error',result)
  self.assertEqual(self.s.setting(PROGRESS+self.i['isin'])['analysis_receipt']['error'],result['error'])
  self.assertEqual(self.s.setting('cp_eligibility:'+self.i['isin']),result)
  self.assertEqual(cw.eligibility_due(self.s,[self.i],pd.Timestamp.now(tz='UTC')),[])
 def test_universe_upgrade_reassesses_old_current_then_returns_to_reuse(self):
  r=self.turn();r.pop('paper_universe_version');self.s.set_setting('cp_eligibility:'+self.i['isin'],r)
  self.assertEqual(cw.eligibility_due(self.s,[self.i],pd.Timestamp.now(tz='UTC')),[self.i])
  self.assertEqual(self.turn()['paper_universe_version'],UNIVERSE_VERSION)
  self.assertEqual(cw.eligibility_due(self.s,[self.i],pd.Timestamp.now(tz='UTC')),[])

if __name__=='__main__':unittest.main()
