"""Fresh feed hands a lightweight durable request to the existing read worker."""
import json,sys,unittest
from pathlib import Path
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import Mock,patch
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
import test_cp_fresh_completion as fx
import cp_assessment as worker
from feed_worker import FeedContext
from paper_hourly import CONFIG


class FreshAssessmentRequest(unittest.TestCase):
 context=fx.FreshCompletion.context
 stages=fx.FreshCompletion.stages
 def feed(self,ctx,isin='A',requested=True):
  with ExitStack() as stack:
   calls=self.stages(stack,requested=requested)
   out=FeedContext.calculate(ctx,{'isin':isin,'_gettex_poll_only':True})
  for name in ('cp_reconciliation.turn','calculation_window.eligibility_turn','cp_reconciliation.complete_analysis_request'):
   calls[name].assert_not_called()
  return out
 def test_requested_fresh_result_enqueues_without_waiting_for_history(self):
  ctx=self.context();ctx.store.set_setting('cp_eligibility:A',{'old':'unchanged'})
  out=self.feed(ctx)
  self.assertIsNotNone(ctx.store.setting(worker.PREFIX+'A'))
  self.assertTrue(out['cp_analysis_deferred'])
  self.assertEqual(ctx.store.setting('cp_eligibility:A'),{'old':'unchanged'})
  self.assertIn('analysis_requested_at',ctx.store.setting('cp_reconciliation_progress:A'))
 def test_duplicate_request_preserves_retry_cooldown_and_original_age(self):
  ctx=self.context();self.feed(ctx);item=ctx.store.setting(worker.PREFIX+'A')
  worker.finish(ctx.store,item,error='INPUT_CHANGED');retry=ctx.store.setting(worker.PREFIX+'A')
  self.feed(ctx)
  self.assertEqual(ctx.store.setting(worker.PREFIX+'A'),retry)
  self.assertIsNone(worker.choose(ctx.store.db,retry['retry_at']-1))
  self.assertEqual(worker.choose(ctx.store.db,retry['retry_at'])['isin'],'A')
 def test_no_new_evidence_does_not_queue_or_clear_quality_error(self):
  ctx=self.context({'errors':['SOURCE_COOLDOWN'],'dq':'INVALID'});ctx.store.set_setting('cp_eligibility:A',{'error':'REAL_DQ'})
  out=self.feed(ctx,requested=False)
  self.assertEqual(out['errors'],['SOURCE_COOLDOWN']);self.assertEqual(out['dq'],'INVALID')
  self.assertIsNone(ctx.store.setting(worker.PREFIX+'A'))
  self.assertEqual(ctx.store.setting('cp_eligibility:A'),{'error':'REAL_DQ'})
 def test_request_survives_restart_without_claiming_completed_analysis(self):
  ctx=self.context();self.feed(ctx)
  other=fx.fixtures.Memory();self.addCleanup(other.db.close);ctx.store.db.backup(other.db)
  self.assertEqual(worker.choose(other.db),ctx.store.setting(worker.PREFIX+'A'))
  self.assertIn('analysis_requested_at',other.setting('cp_reconciliation_progress:A'))
  self.assertIsNone(other.setting('cp_eligibility:A'))
 def test_53_and_200_background_jobs_progress_with_fresh_priority_requests(self):
  for count in (53,200):
   with self.subTest(count=count):
    ctx=self.context();ctx.store.set_setting(CONFIG,dict(enabled=True,approved=True,identities={'APPLE':{},'NVIDIA':{}}))
    for n in range(count):worker.request(ctx.store,str(n))
    seen=set();streak=0;maximum=0
    for _ in range(count*3):
     self.feed(ctx,'APPLE');self.feed(ctx,'NVIDIA')
     item=worker.choose(ctx.store.db)
     if item['isin'] in ('APPLE','NVIDIA'):streak+=1;maximum=max(maximum,streak)
     else:seen.add(item['isin']);streak=0
     worker.finish(ctx.store,item)
    self.assertEqual(len(seen),count);self.assertLessEqual(maximum,2)


if __name__=='__main__':unittest.main()
