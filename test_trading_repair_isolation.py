"""Repair fairness and live acquisition isolation; no working market writes."""
import json,unittest,copy,time
from unittest.mock import patch,Mock
import test_cp_automatic_repair as fixture
from test_cp_automatic_repair import cp,pd
import candle_schedule as schedule
import gettex_chart as chart

class TradeRepairProgress(unittest.TestCase):
 def setUp(self):
  self.f=fixture.AutomaticRepair();self.f.setUp();self.addCleanup(self.f.doCleanups)
  self.s=self.f.s;self.info=self.f.info;self.now=self.f.now;self.s.directory='UNUSED'
 def test_historical_empty_reply_preserves_live_schedule(self):
  meta=dict(self.f.meta,source='gettex_chart',symbol='AAPL.GTX')
  self.s.db.execute('CREATE TABLE input_revisions(scope TEXT PRIMARY KEY,revision INTEGER)');self.s.db.commit()
  key=schedule.unit_key(self.info['isin'],meta,'15m');prior={'reason':'NEW_CLOSED_INTERVAL','outcome':'CHECKED','close_at':'2026-11-10T18:30Z','finished_at':'2026-11-10T18:31Z'}
  self.s.set_setting(key,prior);self.s.directory='UNUSED'
  client=Mock();client.fetch.side_effect=ValueError('Источник не вернул пригодные закрытые свечи')
  with patch('eur_listings.stored_identity',return_value=meta),patch('delivery_observations.outcome'),self.assertRaises(ValueError):
   chart.recovery_download(self.s,self.info,meta,'15m',pd.Timestamp('2026-10-21T11:30Z'),pd.Timestamp('2026-10-21T11:45Z'),self.now,client)
  self.assertEqual(self.s.setting(key),prior)
 def test_legacy_113_attempt_receipt_cannot_make_another_provider_request(self):
  self.f.plan();jobs=self.f.jobs();task=next(iter(jobs.values()));task.update(attempts=113,origin={'kind':'CURRENT_TV_PRESENCE'});self.s.set_setting(self.f.key,jobs)
  report=dict(state='UNCONFIRMED',ranges=[self.f.span],next_retry=(self.now+pd.Timedelta(minutes=15)).isoformat())
  with patch('data_quality.repair',return_value=report) as repair:
   result=cp.service_request(self.info,self.s,Mock(),self.now)
  self.assertTrue(repair.call_args.kwargs.get('local_only'))
  self.assertEqual(result['state'],'BLOCKED');self.assertEqual(result['attempts'],113)
 def test_source_cooldown_does_not_prevent_validation_of_present_real_candle(self):
  self.f.plan()
  retry=(self.now+pd.Timedelta(hours=2)).isoformat();self.s.set_setting('cp_source_retry:gettex',retry);fetch=Mock(side_effect=AssertionError('network during cooldown'))
  with patch('data_quality.repair',return_value={'state':'READY','ranges':[]}) as repair:
   result=cp.service_request(self.info,self.s,fetch,self.now)
  self.assertTrue(repair.call_args.kwargs['local_only'])
  self.assertEqual(repair.call_args.kwargs['network_not_before'],retry)
  self.assertEqual(result['state'],'COMPLETED');fetch.assert_not_called();self.assertEqual(self.s.setting('cp_source_retry:gettex'),retry)
  self.assertEqual(result.get('attempts',0),0)
 def synthetic_store(self):
  import data_quality as dq
  from performance_cache import install_input_revisions
  from candle_timing import install
  from types import SimpleNamespace
  from history_service import attach_parent_provenance
  s=self.s;isin=self.info['isin'];meta=self.f.meta
  rows=[['2026-11-09 11:00:00',100,101,99,100,10],['2026-11-09 11:15:00',100,102,99,101,12],['2026-11-09 11:30:00',101,102,100,101,9]]
  s.db.executemany('INSERT INTO candles VALUES (?,?,?,?,?,?,?,?,?)',[(isin,'gettex','15m',*r) for r in (rows[0],rows[2])])
  install_input_revisions(s.db);install(s.db);s.db.commit()
  s.read_cache=SimpleNamespace(token_for=lambda scopes:{k:(s.db.execute('SELECT revision FROM input_revisions WHERE scope=?',(k,)).fetchone() or [0])[0] for k in scopes},depend=lambda *a:None)
  def load(isin,base='15m'):
   data=s.db.execute("SELECT ts,open,high,low,close,volume FROM candles WHERE isin=? AND base_tf=? ORDER BY ts",(isin,base)).fetchall()
   frame=pd.DataFrame(data,columns=['ts',*dq.FIELDS]).set_index('ts');frame.index=pd.to_datetime(frame.index);frame.attrs=meta.copy()
   return attach_parent_provenance(s,dq.attach_interval_evidence(s,frame,isin),isin,base)
  s.load_official=load;s.load_series=lambda *a,**k:pd.DataFrame()
  return rows
 def test_actual_local_reaudit_completes_without_http_or_new_recovery_count(self):
  rows=self.synthetic_store();self.f.plan()
  self.s.db.execute('INSERT INTO candles VALUES (?,?,?,?,?,?,?,?,?)',(self.info['isin'],'gettex','15m',*rows[1]));self.s.db.commit()
  retry=(self.now+pd.Timedelta(hours=2)).isoformat();self.s.set_setting('cp_source_retry:gettex',retry)
  fetch=Mock(side_effect=AssertionError('HTTP blocked'))
  result=cp.service_request(self.info,self.s,fetch,self.now)
  self.assertEqual(result['state'],'COMPLETED');fetch.assert_not_called()
  if self.s.db.execute("SELECT 1 FROM sqlite_master WHERE name='candle_recoveries'").fetchone():
   self.assertEqual(self.s.db.execute('SELECT count(*) FROM candle_recoveries').fetchone()[0],0)
  self.assertEqual(result['attempts'],0)
 def test_partial_local_data_cannot_bypass_cooldown_via_auxiliary_endpoint(self):
  import data_quality as dq
  self.synthetic_store()
  partial=self.s.load_official(self.info['isin']).iloc[:1].copy()
  retry=(self.now+pd.Timedelta(hours=2)).isoformat()
  for local_only in (True,False):
   with self.subTest(local_only=local_only),patch('cp_experience.local_recovery',return_value=(partial,False)),patch('history_service.recover_from_children',side_effect=AssertionError('Auxiliary HTTP forbidden')) as auxiliary:
    result=dq.repair(self.info,self.s,self.f.meta['symbol'],'15m',self.f.meta,Mock(side_effect=AssertionError('HTTP forbidden')),self.now,max_ranges=1,target_range=self.f.span,network_not_before=retry,local_only=local_only)
   auxiliary.assert_not_called();self.assertNotEqual(result['state'],'READY')
   self.assertEqual(result['next_retry'],retry);self.assertFalse(result['recovery_provider_attempted'])
 def test_historical_429_still_blocks_all_provider_requests(self):
  from types import SimpleNamespace
  meta=dict(self.f.meta,source='gettex_chart',symbol='AAPL.GTX')
  self.s.db.execute('CREATE TABLE input_revisions(scope TEXT PRIMARY KEY,revision INTEGER)');self.s.db.commit()
  error=ValueError('429');error.response=SimpleNamespace(status_code=429,headers={'Retry-After':'7200'})
  client=Mock();client.fetch.side_effect=error
  with patch('eur_listings.stored_identity',return_value=meta),patch('delivery_observations.outcome'),self.assertRaises(ValueError):
   chart.recovery_download(self.s,self.info,meta,'15m',pd.Timestamp('2026-10-21T11:30Z'),pd.Timestamp('2026-10-21T11:45Z'),self.now,client)
  self.assertEqual(pd.Timestamp(self.s.setting('cp_source_retry:gettex_chart')),self.now+pd.Timedelta(hours=2))
  self.assertIsNone(self.s.setting(schedule.unit_key(self.info['isin'],meta,'15m')))

if __name__=='__main__':unittest.main()
