"""Synthetic scheduling/publication tests; no providers or working DB writes."""
import copy,json,unittest,time
from types import SimpleNamespace
from unittest.mock import patch
import test_cp_automatic_repair as fixture
import cp_assessment as worker
import calculation_window as windows
from cp_reconciliation import complete_analysis_request
from paper_hourly import CONFIG


class AssessmentWorker(unittest.TestCase):
 def setUp(self):
  self.s=fixture.Memory();self.addCleanup(self.s.db.close)
 def test_feed_defers_without_calculation_or_readiness_publication(self):
  self.s.defer_eligibility=True
  self.s.set_setting('cp_eligibility:A',{'unchanged':True})
  with patch.object(windows,'calculate_eligibility',side_effect=AssertionError('heavy feed calculation')):
   first=windows.eligibility_turn(self.s,{'isin':'A'})
   second=windows.eligibility_turn(self.s,{'isin':'A'})
  self.assertTrue(first['deferred']);self.assertEqual(first,second)
  self.assertEqual(self.s.setting('cp_eligibility:A'),{'unchanged':True})
 def test_deferred_review_remains_pending_until_actual_assessment(self):
  self.s.set_setting('cp_reconciliation_progress:A',{'analysis_requested_at':'test'})
  complete_analysis_request(self.s,'A','test',{'deferred':True})
  self.assertEqual(self.s.setting('cp_reconciliation_progress:A'),{'analysis_requested_at':'test'})
 def test_queue_survives_restart_and_failure_retries_without_ready(self):
  item=worker.request(self.s,'A');saved=self.s.setting(worker.PREFIX+'A')
  other=fixture.Memory();self.addCleanup(other.db.close);self.s.db.backup(other.db)
  self.assertEqual(worker.choose(other.db),saved)
  worker.finish(other,saved,error='INPUT_CHANGED')
  self.assertIsNone(worker.choose(other.db))
  self.assertIsNotNone(worker.choose(other.db,time.time()+61))
  self.assertIsNone(other.setting('cp_eligibility:A'))
 def test_53_and_200_background_requests_progress_under_continuous_priority_load(self):
  for count in (53,200):
   with self.subTest(count=count):
    s=fixture.Memory();self.addCleanup(s.db.close)
    s.set_setting(CONFIG,dict(enabled=True,approved=True,identities={'APPLE':{},'NVIDIA':{}}))
    for i in range(count):worker.request(s,str(i))
    seen=set();max_streak=0;streak=0
    for _ in range(count*3):
     worker.request(s,'APPLE');worker.request(s,'NVIDIA')
     item=worker.choose(s.db)
     if item['isin'] in ('APPLE','NVIDIA'):streak+=1;max_streak=max(max_streak,streak)
     else:seen.add(item['isin']);streak=0
     worker.finish(s,item)
    self.assertEqual(len(seen),count);self.assertLessEqual(max_streak,2)
 def test_changed_revision_rejects_publication_and_keeps_request(self):
  worker.request(self.s,'A');self.s.set_setting('cp_eligibility:A',{'old':True})
  item=worker.choose(self.s.db)
  with patch.object(windows,'eligibility_state',return_value='STALE'):
   with self.assertRaisesRegex(ValueError,'ELIGIBILITY_INPUT_CHANGED'):
    worker.finish(self.s,item,{'isin':'A','ready':True})
  self.assertEqual(self.s.setting('cp_eligibility:A'),{'old':True})
  self.assertIsNotNone(worker.choose(self.s.db))
 def test_current_and_expired_publish_without_extending_deadlines(self):
  for state in ('CURRENT','EXPIRED'):
   worker.request(self.s,'A');item=worker.choose(self.s.db)
   result={'isin':'A','expires_at':'2026-11-10T21:00Z','_input_revisions':{'instrument:A':3}}
   with patch.object(windows,'eligibility_state',return_value=state):worker.finish(self.s,item,result)
   saved=self.s.setting('cp_eligibility:A')
   self.assertEqual(saved['expires_at'],result['expires_at'])
   self.assertEqual(saved['_input_revisions'],result['_input_revisions'])
   self.assertEqual(saved['publication_state'],state)
   self.assertIsNone(worker.choose(self.s.db))
 def test_new_review_request_is_not_acknowledged_by_older_calculation(self):
  worker.request(self.s,'A');item=worker.choose(self.s.db);item['analysis_requested_at']='old'
  self.s.set_setting('cp_reconciliation_progress:A',{'analysis_requested_at':'new'})
  with patch.object(windows,'eligibility_state',return_value='CURRENT'):
   worker.finish(self.s,item,{'isin':'A','checked_at':'now'})
  self.assertEqual(self.s.setting('cp_reconciliation_progress:A')['analysis_requested_at'],'new')
 def test_expired_publication_retains_request_and_review_until_current_retry(self):
  self.s.set_setting('cp_reconciliation_progress:A',{'analysis_requested_at':'review'})
  item=worker.request(self.s,'A');item['analysis_requested_at']='review'
  result=dict(isin='A',checked_at='2026-11-13T07:54:55.699158Z',expires_at='2026-11-13T07:55:00Z',
   _input_revisions={'instrument:A':3},candidates=[dict(timeframe='1H',expires_at='2026-11-13T08:30:00Z',
    execution_revisions={'market:A:15m':7},execution_expires_at='2026-11-13T08:00:00Z',trade_ready=False)])
  with patch.object(windows,'eligibility_state',return_value='EXPIRED'),patch.object(worker.time,'time',return_value=1000):
   worker.finish(self.s,item,result)
  pending=self.s.setting(worker.PREFIX+'A');self.assertIsNotNone(pending)
  self.assertEqual(pending['requested_at'],item['requested_at'])
  self.assertEqual(pending['retry_at'],1060);self.assertEqual(pending['retry_reason'],'EXPIRED_AT_PUBLICATION')
  self.assertEqual(self.s.setting('cp_reconciliation_progress:A')['analysis_requested_at'],'review')
  saved=self.s.setting('cp_eligibility:A')
  self.assertEqual(saved,{**result,'publication_state':'EXPIRED'})
  other=fixture.Memory();self.addCleanup(other.db.close);self.s.db.backup(other.db)
  self.assertIsNone(worker.choose(other.db,1059));due=worker.choose(other.db,1060)
  self.assertEqual(due['isin'],'A');due['analysis_requested_at']='review'
  with patch.object(windows,'eligibility_state',return_value='CURRENT'):
   worker.finish(other,due,result)
  self.assertIsNone(other.setting(worker.PREFIX+'A'))
  self.assertNotIn('analysis_requested_at',other.setting('cp_reconciliation_progress:A'))
 def test_expired_retry_does_not_starve_53_200_ordinary_requests(self):
  for count in (53,200):
   s=fixture.Memory();self.addCleanup(s.db.close)
   s.set_setting(CONFIG,dict(enabled=True,approved=True,identities={'A':{}}))
   item=worker.request(s,'A')
   for n in range(count):worker.request(s,'B'+str(n))
   with patch.object(windows,'eligibility_state',return_value='EXPIRED'),patch.object(worker.time,'time',return_value=1000):
    worker.finish(s,item,dict(isin='A',expires_at='old'))
   pending=s.setting(worker.PREFIX+'A');self.assertIsNotNone(pending)
   seen=[]
   for n in range(count):
    job=worker.choose(s.db,1059);seen.append(job['isin']);worker.finish(s,job)
   self.assertEqual(len(set(seen)),count);self.assertNotIn('A',seen)
   self.assertEqual(worker.choose(s.db,1060)['isin'],'A')

if __name__=='__main__':unittest.main()
