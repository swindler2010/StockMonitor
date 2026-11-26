import unittest
from types import SimpleNamespace
from unittest.mock import patch,Mock
from feed_worker import FeedContext
from test_cp_fresh_fair_share import Memory
import candle_schedule as schedule

class HistorySlotRelease(unittest.TestCase):
 def host(self,calculate=None):
  store=Memory();store.set_setting(schedule.FRESH_ROTATION,{'consecutive':3,'isin':'OTHER'})
  return SimpleNamespace(store=store,_calculate=calculate or Mock(return_value={'errors':[]}))
 def run_host(self,host,info):
  with patch('cp_controls.automatic_turn',return_value={}),patch('cp_controls.tv_risk_turn',return_value={}),patch('cp_controls.reviewed_risk_turn',return_value={}),patch('cp_reconciliation.turn',return_value={}):
   return FeedContext.calculate(host,info)
 def test_completed_nonfetch_history_routes_release_live_slot(self):
  for extra in ({},{'_review_only':True},{'_eligibility_only':True}):
   host=self.host();self.run_host(host,{'isin':'NV',**extra})
   self.assertEqual(host.store.setting(schedule.FRESH_ROTATION)['consecutive'],0)
 def test_fresh_route_never_releases_history_requirement(self):
  host=self.host();self.run_host(host,{'isin':'NV','_gettex_poll_only':True})
  self.assertEqual(host.store.setting(schedule.FRESH_ROTATION)['consecutive'],3)
 def test_failed_work_does_not_claim_finished_history(self):
  host=self.host(Mock(side_effect=RuntimeError('failed')))
  with self.assertRaises(RuntimeError):self.run_host(host,{'isin':'NV'})
  self.assertEqual(host.store.setting(schedule.FRESH_ROTATION)['consecutive'],3)
 def test_already_released_work_does_not_rewrite_progress(self):
  host=self.host();host.store.set_setting(schedule.FRESH_ROTATION,{'consecutive':0,'history_finished_at':'original'})
  self.run_host(host,{'isin':'NV'})
  self.assertEqual(host.store.setting(schedule.FRESH_ROTATION)['history_finished_at'],'original')

if __name__=='__main__':unittest.main()
