import json,unittest
from unittest.mock import patch
import pandas as pd
from test_p1_parent_recovery import ParentRecovery
import data_quality as dq
import cp_experience as memory
import history_service as hs

class FailureLearning(unittest.TestCase):
 setUp=ParentRecovery.setUp
 tearDown=ParentRecovery.tearDown
 def repair(self,fetch=None):
  with patch.object(hs,'download',return_value=self.child.iloc[:0].copy()):
   return ParentRecovery.repair(self,fetch)
 rows=ParentRecovery.rows
 def remove_children(self):
  with self.store.connect() as db:db.execute("delete from series where base_tf='5m'")
 def recent(self):
  with self.store.connect() as db:return memory.read(db)['recent']
 def test_empty_response_records_concrete_evidence_without_ready(self):
  self.remove_children();before=self.rows().copy()
  out=self.repair(lambda *a,**k:self.child.iloc[:0].copy())
  event=self.recent()[0]
  self.assertEqual(event['failure_evidence']['code'],'EMPTY_RESPONSE')
  self.assertEqual(event['failure_evidence']['returned_count'],0)
  self.assertEqual(event['failure_evidence']['required_count'],1)
  self.assertEqual(event['failure_evidence']['dq_state'],'SOURCE_UNAVAILABLE')
  self.assertEqual(event['outcome'],'FAILED');self.assertNotEqual(out['state'],'READY')
  pd.testing.assert_frame_equal(before,self.rows())

 def test_partial_response_is_explained_but_not_promoted(self):
  f=self.child.iloc[:3].copy();f.attrs['receipt_sha256']='a'*64
  err=dq.QualityError(dict(state='GAP',issues=['Пропущенные свечи: 2'],missing_count=2))
  evidence=memory.failure_evidence(err,f,pd.date_range('2026-10-26',periods=5,freq='5min'))
  self.assertEqual(evidence['code'],'INCOMPLETE_RESPONSE');self.assertEqual(evidence['returned_count'],3)
  self.assertEqual(evidence['missing_count'],2);self.assertEqual(evidence['required_count'],5)
  self.assertEqual(evidence['response_sha256'],'a'*64);self.assertNotIn('trade_ready',evidence)
  self.assertEqual(evidence['case_id'],'real_children')

 def test_corruption_identity_and_revision_remain_distinct(self):
  examples=[(dq.QualityError(dict(state='INVALID',issues=['Невозможные OHLC/цены/объёмы'])),'INVALID_RESPONSE'),
   (ValueError('HISTORY_RESPONSE_IDENTITY_MISMATCH: currency changed'),'IDENTITY_REJECTED'),
   (ValueError('Recovery input changed; no stale OHLCV write'),'INPUT_CHANGED'),
   (ValueError('PARENT_CHILD_CONFLICT: valid published parent differs'),'DATA_CONFLICT'),
   (ValueError('SOURCE_LIMITED: source archive exhausted'),'SOURCE_LIMITED')]
  for error,code in examples:
   self.assertEqual(memory.failure_evidence(error)['code'],code)

 def test_wait_race_does_not_learn_a_failed_request_or_create_ready(self):
  self.remove_children();before=self.rows().copy()
  def wait(*a,**k):raise ValueError('GETTEX_RECOVERY_RATE_WAIT')
  out=self.repair(wait)
  self.assertEqual(self.recent(),[]);self.assertNotEqual(out['state'],'READY')
  self.assertIn('next_retry',out);pd.testing.assert_frame_equal(before,self.rows())

 def test_unknown_error_keeps_no_secret_or_invented_cause(self):
  self.remove_children()
  def failed(*a,**k):raise ValueError('https://provider.invalid/?token=PRIVATE&password=SECRET changed')
  self.repair(failed);event=self.recent()[0]
  self.assertEqual(event['failure_evidence']['code'],'UNCLASSIFIED_FAILURE')
  self.assertEqual(event['failure_evidence']['dq_state'],'UNKNOWN')
  self.assertNotIn('PRIVATE',json.dumps(event));self.assertNotIn('SECRET',json.dumps(event))
  self.assertIsNone(event['failure_evidence']['returned_count'])

 def test_legacy_failure_is_not_rewritten_or_guessed(self):
  with self.store.connect() as db:
   db.execute('BEGIN IMMEDIATE')
   memory.record(db,self.info,self.meta,'1H','2026-10-26 10:00','2026-10-26 10:00',{},self.now,'provider_range','FAILED','DQ_REJECTED')
  event=self.recent()[0];self.assertEqual(event['reason'],'DQ_REJECTED');self.assertIsNone(event['failure_evidence'])
  with self.store.connect() as db:
   db.execute('pragma query_only=ON');before=db.total_changes;memory.read(db);self.assertEqual(db.total_changes,before)

 def test_new_evidence_survives_restart_without_changing_child_preference(self):
  from storage import Store
  self.remove_children();self.repair(lambda *a,**k:self.child.iloc[:0].copy())
  event=self.recent()[0];self.store.read_cache.close();self.store=Store(self.tmp.name)
  self.assertEqual(self.recent()[0],event)
  self.assertEqual(memory.child_order(self.store,self.info,self.meta,'1H',('15m','5m')),('15m','5m'))

 def test_non_dataframe_error_response_does_not_break_diagnostics(self):
  evidence=memory.failure_evidence(TypeError('bad provider response'),object())
  self.assertIsNone(evidence['returned_count']);self.assertEqual(evidence['code'],'UNCLASSIFIED_FAILURE')

 def test_file_scope_http_and_dq_causes_are_specific_without_secret_text(self):
  from urllib.error import HTTPError
  error=HTTPError('https://provider.invalid/?token=SECRET',429,'PRIVATE',{'Retry-After':'120'},None)
  d=memory.failure_evidence(error);self.assertEqual(d['code'],'RATE_LIMIT_RESPONSE');self.assertEqual(d['http_status'],429)
  self.assertNotIn('SECRET',json.dumps(d));self.assertNotIn('PRIVATE',json.dumps(d))
  d=memory.failure_evidence(ValueError('В доступных файлах GETTEX нет сделок EUR для TEST'))
  self.assertEqual(d['code'],'AVAILABLE_FILES_NO_MATCH');self.assertIn('не доказана',d['explanation_ru'])
  d=memory.failure_evidence(dq.QualityError(dict(state='INVALID',issues=['Невозможные OHLC/цены/объёмы','Свечи вне сессии/неправильное выравнивание'])))
  self.assertEqual(d['issue_codes'],['OHLCV','SESSION_ALIGNMENT'])
  self.assertLess(len(json.dumps(d,ensure_ascii=False)),2000)

if __name__=='__main__':unittest.main()
