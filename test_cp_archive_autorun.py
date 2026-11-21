"""Archive repair without a button; same writer and real saved GETTEX replay."""
import copy,sys,unittest
from pathlib import Path
from unittest.mock import Mock,patch
from contextlib import ExitStack
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
from feed_worker import FeedContext
import cp_controls as cp
import test_cp_automatic_repair as fixture
import pandas as pd

class ArchiveAutorun(unittest.TestCase):
 def setUp(self):
  self.f=fixture.AutomaticRepair();self.f.setUp();self.addCleanup(self.f.doCleanups)
 def test_without_assessment_repairs_saved_real_row_and_persists_receipt(self):
  f=self.f;rows,reply=f.real_fixture();f.s.set_setting('cp_eligibility:'+f.isin,{})
  fetch=Mock(return_value=reply);host=f.host();host._recover_gettex_range=fetch
  host._calculate=lambda info:FeedContext._calculate(host,info)
  with ExitStack() as stack:
   f.stages(stack)
   stack.enter_context(patch('cp_controls.stamp',return_value=f.now.isoformat()))
   stack.enter_context(patch('requests.get',side_effect=AssertionError('saved replay only')))
   assess=stack.enter_context(patch('calculation_window.eligibility_turn',return_value={'timeframes':{}}))
   stack.enter_context(patch('cp_reconciliation.complete_analysis_request'))
   result=FeedContext.calculate(host,f.info)
  self.assertEqual(result['cp_automatic_repair']['state'],'QUEUED')
  self.assertEqual(result['cp_automatic_repair']['dependency_candidates'],0)
  receipt=result['cp_priority'];assess.assert_called_once()
  self.assertEqual(receipt['state'],'COMPLETED');fetch.assert_called_once()
  self.assertEqual(f.s.db.execute('SELECT ts,open,high,low,close,volume FROM candles ORDER BY ts').fetchall(),[tuple(r) for r in rows])
  self.assertEqual(f.s.db.execute('SELECT count(*) FROM candle_recoveries').fetchone()[0],1)
  self.assertEqual(f.jobs()[cp.repair_id(receipt['problem'])],receipt)
  self.assertEqual(f.plan()['candidates'],0);self.assertIsNone(f.s.setting('cp_manual_risk:'+f.isin))
 def test_ready_analysis_does_not_disable_archive_and_empty_provider_keeps_dq(self):
  f=self.f;rows,reply=f.real_fixture();f.s.set_setting('cp_eligibility:'+f.isin,{'isin':f.isin,'timeframes':{'4H':{'analysis_ready':True}}})
  self.assertEqual(f.plan()['state'],'QUEUED')
  with patch('requests.get',side_effect=AssertionError('saved replay only')):
   receipt=cp.service_request(f.info,f.s,Mock(return_value=reply.iloc[:0].copy()),f.now)
  self.assertIn(receipt['state'],('BLOCKED','WAITING'));self.assertTrue(receipt['reason'])
  self.assertEqual(f.s.db.execute('SELECT count(*) FROM candles').fetchone()[0],2)
  self.assertNotEqual(f.s.setting(f.qkey)['state'],'READY')
  before=copy.deepcopy(f.jobs());f.plan();self.assertEqual(before,f.jobs())
 def test_regular_yield_does_not_defer_repair_to_next_universe_rotation(self):
  f=self.f;rows,reply=f.real_fixture();f.s.set_setting('cp_eligibility:'+f.isin,{})
  f.s.set_setting('cp_repair_plan:'+f.isin,{'yield_regular':True})
  fetch=Mock(return_value=reply);host=f.host();host._recover_gettex_range=fetch
  host._calculate=lambda info:FeedContext._calculate(host,info)
  with ExitStack() as stack:
   f.stages(stack)
   stack.enter_context(patch('cp_controls.stamp',return_value=f.now.isoformat()))
   regular=stack.enter_context(patch('cp_controls.historical_review_turn',return_value={}))
   stack.enter_context(patch('requests.get',side_effect=AssertionError('saved replay only')))
   stack.enter_context(patch('calculation_window.eligibility_turn',return_value={'timeframes':{}}))
   stack.enter_context(patch('cp_reconciliation.complete_analysis_request'))
   result=FeedContext.calculate(host,{**f.info,'_review_only':True})
  regular.assert_called_once();fetch.assert_called_once()
  self.assertEqual(result['cp_priority']['state'],'COMPLETED')
  self.assertEqual(f.s.db.execute('SELECT count(*) FROM candle_recoveries').fetchone()[0],1)
 def test_owner_click_precedes_automatic_in_actual_writer_selection(self):
  f=self.f;f.plan();automatic=f.task()
  q={**f.q,'ranges':[['2026-11-08 10:00:00','2026-11-08 10:00:00'],f.span]};f.s.set_setting(f.qkey,q)
  p=next(p for p in cp.problems(f.s.db,f.isin) if p['range'][0].startswith('2026-11-08'))
  cp.request_repair(f.s,p,priority=True)
  with patch('data_quality.repair',return_value={'state':'READY','ranges':[]}) as writer:
   receipt=cp.service_request(f.info,f.s,Mock(),f.now)
  self.assertEqual(receipt['problem']['id'],p['id']);self.assertEqual(writer.call_args.kwargs['target_range'],p['range'])
  self.assertEqual(f.jobs()[cp.repair_id(automatic['problem'])]['state'],'QUEUED')
 def test_foreign_future_and_changed_source_never_write(self):
  f=self.f
  for field,value in [('venue','XETRA'),('currency','USD'),('source','proxy'),('timezone','UTC')]:
   with self.subTest(field=field):
    f.s.set_setting(f.qkey,{**f.q,'identity':{**f.meta,field:value}})
    self.assertEqual(f.plan()['candidates'],0)
  f.s.set_setting(f.qkey,{**f.q,'ranges':[['2026-11-11 10:00:00']*2]})
  self.assertEqual(f.plan()['candidates'],0)
  f.s.set_setting(f.qkey,f.q);rows,reply=f.real_fixture();f.s.set_setting('cp_eligibility:'+f.isin,{})
  f.plan()
  def changed(*args):
   import json
   f.s.db.execute('UPDATE instruments SET payload=? WHERE isin=?',(json.dumps({**f.info,'name':'changed'}),f.isin));f.s.db.commit();return reply
  with patch('requests.get',side_effect=AssertionError('saved replay only')):
   receipt=cp.service_request(f.info,f.s,changed,f.now)
  self.assertNotEqual(receipt['state'],'COMPLETED');self.assertEqual(f.s.db.execute('SELECT count(*) FROM candles').fetchone()[0],2)

if __name__=='__main__':unittest.main()

