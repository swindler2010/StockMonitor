"""Source quarantines must reach ordinary CP recovery without false completion."""
import copy,sys,unittest
from pathlib import Path
from unittest.mock import patch,Mock
import pandas as pd
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
import cp_controls as cp
import data_quality as dq
import gettex_chart as chart
import cp_experience
import test_cp_recovered_quarantine as fixture

class SourceQuarantineRoute(unittest.TestCase):
 def setUp(self):
  self.f=fixture.RecoveredQuarantine('test_real_source_repair_removes_only_resolved_rejection_and_preserves_raw_receipt');self.f.setUp();self.addCleanup(self.f.doCleanups)
 def pending(self):
  with self.f.store.connect() as db:return [p for p in cp.problems(db,self.f.info['isin'],include_source_quarantines=True) if p['report_key']==self.f.key]
 def test_source_diagnostics_do_not_expand_existing_risk_or_trade_problem_universe(self):
  with self.f.store.connect() as db:
   self.assertEqual(cp.problems(db,self.f.info['isin']),[])
   self.assertEqual(len(cp.problems(db,self.f.info['isin'],include_source_quarantines=True)),1)
 def test_saved_source_quarantine_is_addressable_without_duplicate_dq_problem(self):
  rows=self.pending();self.assertEqual(len(rows),1);p=rows[0]
  self.assertEqual(p['range'],[self.f.at,self.f.at]);self.assertEqual(p['identity'],self.f.meta)
  report={'state':'INVALID','identity':self.f.meta,'base_tf':'5m','ranges':[[self.f.at,self.f.at]],'issues':['same rejection']}
  self.f.store.set_setting(dq.key(self.f.info,self.f.meta['symbol'],'5m'),report)
  with self.f.store.connect() as db:rows=cp.problems(db,self.f.info['isin'],include_source_quarantines=True)
  same=[p for p in rows if p['identity']==self.f.meta and p['tf']=='5m' and p['range']==[self.f.at,self.f.at]]
  self.assertEqual(len(same),1)
 def test_existing_valid_row_still_requires_fresh_source_recheck_before_retirement(self):
  self.f.store.save_series(self.f.info,self.f.full,'5m');download=Mock(return_value=self.f.full.copy())
  with patch.object(cp_experience,'local_recovery',return_value=(None,False)):
   result=dq.repair(self.f.info,self.f.store,self.f.meta['symbol'],'5m',self.f.meta,download,now=self.f.now,max_ranges=1,target_range=[self.f.at,self.f.at])
  self.assertEqual(download.call_count,1);self.assertNotIn('archive_invalid',self.f.store.setting(self.f.key));self.assertEqual(result['recovery_completed_ranges'],[[self.f.at,self.f.at]])
 def test_bad_or_foreign_recheck_preserves_original_quarantine(self):
  self.f.store.save_series(self.f.info,self.f.full,'5m');bad=self.f.full.copy();bad.attrs={**bad.attrs,'currency':'USD'};download=Mock(return_value=bad)
  with patch.object(cp_experience,'local_recovery',return_value=(None,False)):
   dq.repair(self.f.info,self.f.store,self.f.meta['symbol'],'5m',self.f.meta,download,now=self.f.now,max_ranges=1,target_range=[self.f.at,self.f.at])
  self.assertEqual(download.call_count,1);self.assertEqual(self.f.store.setting(self.f.key),self.f.saved)
 def test_outside_session_source_row_stays_unconfirmed_without_http_or_market_closed_completion(self):
  f=self.f;f.at='2026-11-13 23:00:00';saved=copy.deepcopy(f.saved);saved['archive_invalid'].update(rejected_timestamps=[f.at],ranges=[[f.at,f.at]]);f.store.set_setting(f.key,saved)
  now=pd.Timestamp('2026-11-13T22:05Z');download=Mock()
  result=dq.repair(f.info,f.store,f.meta['symbol'],'5m',f.meta,download,now=now,max_ranges=1,target_range=[f.at,f.at])
  download.assert_not_called();self.assertEqual(result.get('recovery_constraints',{}).get('status'),'SOURCE_TIMEBASE_UNCONFIRMED');self.assertIn('archive_invalid',f.store.setting(f.key));self.assertFalse(dq.repair_target_complete(f.info,f.store,f.meta['symbol'],'5m',f.meta,[f.at,f.at],now))
 def test_service_cannot_complete_while_exact_source_quarantine_remains(self):
  f=self.f;rows=self.pending();self.assertEqual(len(rows),1)
  cp.request_repair(f.store,rows[0],priority=True)
  with patch.object(dq,'repair',return_value={'state':'READY','ranges':[]}),patch.object(dq,'repair_target_complete',return_value=True),patch.object(chart,'recovery_wait',return_value=None):
   result=cp.service_request(f.info,f.store,Mock(),now=pd.Timestamp.now(tz='UTC'))
  self.assertNotEqual(result['state'],'COMPLETED');self.assertIn('SOURCE_QUARANTINE',result['reason']);self.assertIn('archive_invalid',f.store.setting(f.key))
 def test_automatic_turn_reaches_source_and_clears_only_confirmed_date_without_click(self):
  f=self.f;f.store.set_setting('cp_gettex_source_policy',{'approved':True,'version':1,'scope':'registered_stocks','exclusive_venue':'GETTEX'})
  queued=cp.automatic_repair_turn(f.store,f.info,now=f.now)
  self.assertEqual(queued['state'],'QUEUED')
  with patch.object(cp_experience,'local_recovery',return_value=(None,False)),patch.object(chart,'recovery_download',return_value=f.full.copy()) as source,patch.object(chart,'recovery_wait',return_value=None):
   result=cp.service_request(f.info,f.store,Mock(),now=f.now)
  self.assertEqual(source.call_count,1);self.assertEqual(result['state'],'COMPLETED');self.assertNotIn('archive_invalid',f.store.setting(f.key))
  self.assertEqual(f.store.setting(f.key)['invalid_receipts'],f.saved['invalid_receipts'])
 def test_wrong_source_fault_timeframe_is_not_a_valid_recovery_target(self):
  f=self.f;saved=copy.deepcopy(f.saved);saved['archive_invalid']['base_tf']='15m';f.store.set_setting(f.key,saved)
  self.assertEqual(self.pending(),[])
 def test_changed_source_identity_supersedes_queued_request_without_http(self):
  f=self.f;rows=self.pending();cp.request_repair(f.store,rows[0],priority=True)
  changed=copy.deepcopy(f.saved);changed['archive_invalid']['identity']={**f.meta,'currency':'USD'};f.store.set_setting(f.key,changed)
  with patch.object(chart,'recovery_download') as source:result=cp.service_request(f.info,f.store,Mock(),now=pd.Timestamp.now(tz='UTC'))
  source.assert_not_called();self.assertEqual(result['state'],'SUPERSEDED');self.assertEqual(f.store.setting(f.key),changed)
 def test_53_and_200_dates_have_stable_exact_ids_and_keep_foreign_identity_out(self):
  f=self.f
  for count in (53,200):
   saved=copy.deepcopy(f.saved);times=list(map(str,pd.date_range('2026-11-13 07:30',periods=count,freq='1min')));saved['archive_invalid'].update(rejected_timestamps=times,ranges=[]);f.store.set_setting(f.key,saved)
   rows=self.pending();self.assertEqual(len(rows),count);self.assertEqual(len({p['id'] for p in rows}),count)
   self.assertEqual([p['id'] for p in rows],[p['id'] for p in self.pending()])
   saved['archive_invalid']['identity']={**f.meta,'currency':'USD'};f.store.set_setting(f.key,saved);self.assertEqual(self.pending(),[])
if __name__=='__main__':unittest.main()
