"""Fresh data promotion must not retain a mutually exclusive no-feed route."""
import sys,unittest,time,queue
from pathlib import Path
from types import SimpleNamespace
from contextlib import nullcontext
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
from desktop014 import Desktop014

class FreshRouteUpgrade(unittest.TestCase):
 def capture(self,queued,fresh,age=0):
  sent=[]
  ctx=SimpleNamespace(feed_slots=nullcontext(),closing=False,_cp_focus={},
    _cp_fresh_request=fresh,_cp_fresh_requested_at=time.time()-age,
    compute_feed_update=SimpleNamespace(calculate=lambda kind,p:(sent.append(p['info']) or {'errors':[]})),
    events=queue.Queue(),feed_done=object())
  Desktop014._gettex_worker(ctx,queued)
  self.assertEqual(len(sent),1)
  return sent[0]
 def test_fresh_promotion_replaces_calculation_only_and_review_only_routes(self):
  fresh={'isin':'DE0007164600','_gettex_poll_only':True,'_cp_focus':{'isin':'DE0007164600','source':'gettex_chart','timeframe':'1D'}}
  for flag in ['_eligibility_only','_review_only']:
   with self.subTest(flag=flag):
    original={'isin':'DE0007164600',flag:True,'history_symbol':'SAP.DE'}
    out=self.capture(original,fresh)
    self.assertNotIn(flag,out);self.assertTrue(out['_gettex_poll_only'])
    self.assertEqual(out['_cp_focus']['timeframe'],'1D')
    self.assertTrue(original[flag]);self.assertEqual(out['history_symbol'],'SAP.DE')
 def test_expired_or_other_instrument_request_preserves_original_route(self):
  original={'isin':'DE0007164600','_eligibility_only':True}
  for fresh,age in [({'isin':original['isin'],'_gettex_poll_only':True},91),({'isin':'OTHER','_gettex_poll_only':True},0)]:
   with self.subTest(fresh=fresh,age=age):
    self.assertEqual(self.capture(original,fresh,age),original)
 def test_normal_archive_turn_without_promotion_is_unchanged(self):
  original={'isin':'DE0007164600','_review_only':True}
  self.assertEqual(self.capture(original,{}),original)
 def test_promoted_route_reaches_actual_feed_stage_before_any_calculation(self):
  from feed_worker import FeedContext
  queued={'isin':'DE0007164600','_eligibility_only':True}
  routed=self.capture(queued,{'isin':queued['isin'],'_gettex_poll_only':True})
  events=queue.Queue();calls=[]
  def run(info):calls.append(info['isin']);events.put((None,(info,[])))
  ctx=SimpleNamespace(store=object(),events=events,_run_feed=run,_recover_gettex_range=None)
  with patch('cp_controls.service_request',return_value=None),patch('calculation_window.eligibility_turn',side_effect=AssertionError('premature calculation')),patch('cp_controls.historical_review_turn',side_effect=AssertionError('premature review')):
   self.assertEqual(FeedContext._calculate(ctx,routed),{'errors':[]})
  self.assertEqual(calls,[queued['isin']])

if __name__=='__main__':unittest.main()
