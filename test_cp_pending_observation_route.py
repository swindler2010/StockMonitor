"""Saved review on the existing deduplicated feed key must reach history."""
import copy,queue,sys,time,unittest
from pathlib import Path
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
from desktop014 import Desktop014

class PendingObservationRoute(unittest.TestCase):
 def test_live_saved_review_replaces_queued_fresh_payload_but_stale_or_other_review_does_not(self):
  for kind in ('current','stale','other','none'):
   with self.subTest(kind=kind):
    original={'isin':'A','name':'Fixture','_gettex_poll_only':True,'_cp_focus':{'timeframe':'5m'}};before=copy.deepcopy(original)
    review={} if kind=='none' else {'isin':'B' if kind=='other' else 'A','job_id':'exact-saved-review'}
    captured=[]
    compute=SimpleNamespace(calculate=Mock(side_effect=lambda kind,args:(captured.append(args['info']) or {'errors':[]})))
    ctx=SimpleNamespace(feed_slots=nullcontext(),closing=False,_cp_focus={},_cp_fresh_request={'isin':'A','_gettex_poll_only':True,'_cp_focus':{'timeframe':'5m'}},_cp_fresh_requested_at=time.time(),_cp_observation_request=review,_cp_observation_requested_at=time.time()-(91 if kind=='stale' else 0),compute_feed_update=compute,dispatcher=SimpleNamespace(sqlite_busy=Mock()),events=queue.Queue(),feed_done=Mock())
    Desktop014._gettex_worker(ctx,original)
    self.assertEqual(len(captured),1);self.assertEqual(original,before)
    if kind=='current':
     self.assertTrue(captured[0].get('_review_only'));self.assertFalse(captured[0].get('_gettex_poll_only'))
    else:
     self.assertTrue(captured[0].get('_gettex_poll_only'));self.assertFalse(captured[0].get('_review_only'))
    self.assertEqual(ctx._cp_active_feed,{});self.assertEqual(ctx.events.qsize(),1)

 def test_native_batch_uses_same_key_but_yields_to_live_price_and_explicit_review(self):
  for kind in ('native','stale','other','fresh','review'):
   with self.subTest(kind=kind):
    captured=[];original={'isin':'A','name':'Fixture','_gettex_poll_only':True}
    ctx=SimpleNamespace(feed_slots=nullcontext(),closing=False,_cp_focus={},
     _cp_native_empty_requests={'B' if kind=='other' else 'A':time.time()-(91 if kind=='stale' else 0)},
     _cp_fresh_request={'isin':'A','_gettex_poll_only':True} if kind=='fresh' else {},_cp_fresh_requested_at=time.time(),
     _cp_observation_request={'isin':'A','job_id':'owner'} if kind=='review' else {},_cp_observation_requested_at=time.time(),
     compute_feed_update=SimpleNamespace(calculate=Mock(side_effect=lambda k,args:(captured.append(args['info']) or {'errors':[]}))),
     dispatcher=SimpleNamespace(sqlite_busy=Mock()),events=queue.Queue(),feed_done=Mock())
    Desktop014._gettex_worker(ctx,original)
    self.assertEqual(len(captured),1)
    self.assertEqual(bool(captured[0].get('_native_empty_only')),kind=='native')
    self.assertEqual(bool(captured[0].get('_review_only')),kind=='review')
    self.assertEqual(bool(captured[0].get('_gettex_poll_only')),kind in ('stale','other','fresh'))

if __name__=='__main__':unittest.main()
