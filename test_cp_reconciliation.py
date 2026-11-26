"""New assisted final-stage tests; isolated data only, no TV or market network."""
import copy,json,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import pandas as pd
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
from storage import Store
import cp_reconciliation as rv
import cp_controls as ctl
import cp_experience as xp
import data_quality as dq
import analytics
import paper_account

class Reconciliation(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.s=Store(self.temp.name)
  self.i={'isin':'US0378331005','name':'Apple'};self.s.save_instrument(self.i)
  self.m=dict(source='gettex',symbol=self.i['isin'],venue='GETTEX',currency='EUR',timezone='Europe/Berlin')
  self.now=pd.Timestamp('2026-11-02T14:00Z');self.a='2026-10-28 10:15:00'
  with self.s.connect() as db:paper_account.initialize(db)
 def tearDown(self):self.s.read_cache.close();self.temp.cleanup()
 def event(self,at=None,meta=None,code='EMPTY_RESPONSE',method='provider_range'):
  self.event_no=getattr(self,'event_no',0)+1
  with self.s.connect() as db:
   xp.install(db);db.execute('BEGIN IMMEDIATE');xp.record(db,self.i,meta or self.m,'15m',at or self.a,at or self.a,{},self.now-pd.Timedelta(seconds=self.event_no),method,'FAILED',{'version':1,'code':code})
 def proof(self,first=None,last=None,trade=False,zero=False):
  first=first or self.a;last=last or '2026-10-28 10:30:00'
  p=dict(isin=self.i['isin'],identity=self.m,basis='complete_published_trade_files',reference={'GETTEX:20261028':{'fixture':'complete paired files'}},checked_at='2026-11-02T12:00:00Z',
         no_trades=[] if trade or zero else [[first,last]],trade_intervals=[[first,last]] if trade else [],zero_quantity_intervals=[[first,last]] if zero else [])
  self.s.set_setting('interval_evidence:'+self.i['isin'],p);return p
 def jobs(self):
  with self.s.connect() as d:return rv.collect(d,self.i['isin'])
 def one(self):return self.jobs()['rows'][0]
 def check(self):return rv.turn(self.s,self.i,self.now)
 def observation(self,result='ABSENT'):
  j=self.one();return rv.observe(self.s,j['id'],j['isin'],ctl.digest(j),result,'GETTEX:APC',True)
 def snapshot(self):
  with self.s.connect() as d:
   return {t:d.execute('SELECT * FROM '+t+' ORDER BY 1').fetchall() for t in ('instruments','candles','series','paper_accounts','paper_fills')}
 def test_capability_does_not_infer_desktop_access_or_rights(self):
  c=rv.capability();self.assertEqual(c['mode'],'ASSISTED_ONLY');self.assertFalse(c['desktop_controller']);self.assertFalse(c['automated_requests'])
 def test_only_completed_native_data_failures_enter_deduped_queue(self):
  self.event(code='WAITING_PROVIDER');self.event(at='2026-10-28 10:30:00',method='local_children')
  self.event();self.check();self.assertEqual(self.jobs()['total'],1)
  self.check();self.assertEqual(self.jobs()['total'],1);self.assertEqual(self.one()['checks'],1)
 def test_visual_absence_alone_stays_unconfirmed_and_readonly(self):
  before=self.snapshot();self.event();self.check();self.observation();rv.turn(self.s,self.i,self.now+pd.Timedelta(days=1))
  j=self.one();self.assertEqual(j['finding']['state'],'UNCONFIRMED');self.assertEqual(j['finding']['discrepancy'],'MISSING_IN_BOTH')
  self.assertEqual(before,self.snapshot());self.assertIsNone(self.s.setting('cp_manual_risk:'+self.i['isin']))
 def test_same_source_authoritative_no_trades_no_ohlcv_or_grant_write(self):
  self.proof();before=self.snapshot();self.event();self.check();j=self.one()
  self.assertEqual(j['finding']['state'],'NO_TRADES');self.assertEqual(j['finding']['event_count'],0)
  self.assertEqual(j['status'],'RESOLVED_NATIVE');self.assertEqual(before,self.snapshot())
 def test_native_timebase_mismatch_is_not_a_source_certificate(self):
  meta={**self.m,'source':'gettex_chart','symbol':'AAPL.GTX'}
  self.s.set_setting('gettex_chart:'+self.i['isin']+':15m',{'state':'SAVED','identity':meta,'listing_proof':dict(meta,isin=self.i['isin'],status='CONFIRMED')})
  self.proof();self.event(meta=meta);self.check();j=self.one()
  self.assertEqual(j['finding']['market_state'],'NO_TRADES');self.assertEqual(j['finding']['state'],'SOURCE_TIMEBASE_UNCONFIRMED');self.assertFalse(j['finding']['can_affect_dq'])
 def test_positive_trade_and_zero_quantity_never_no_trades(self):
  for zero in (False,True):
   with self.subTest(zero=zero):
    self.proof(trade=not zero,zero=zero);spec=rv._spec(self.i['isin'],self.m,'15m',self.a,self.a)
    with self.s.connect() as d:inputs=rv._input(d,spec)
    result=rv._classify(spec,inputs,self.now)
    self.assertEqual(result['market_state'],'ZERO_QUANTITY_UNCONFIRMED' if zero else 'GAP');self.assertFalse(result['can_affect_dq'])
 def test_wrong_currency_listing_and_review_without_identity_rejected(self):
  with self.assertRaises(ValueError):rv._spec(self.i['isin'],dict(self.m,currency='USD'),'15m',self.a,self.a)
  self.event();self.check();j=self.one()
  for symbol,confirmed in [('NASDAQ:AAPL',True),('GETTEX:APC',False)]:
   with self.assertRaises(ValueError):rv.observe(self.s,j['id'],j['isin'],ctl.digest(j),'ABSENT',symbol,confirmed)
 def test_review_rejects_changed_own_inputs_and_job_revision(self):
  self.event();self.check();j=self.one();self.proof()
  with self.assertRaisesRegex(ValueError,'INPUT_CHANGED'):self.observation()
  with self.assertRaisesRegex(ValueError,'JOB_CHANGED'):rv.observe(self.s,j['id'],j['isin'],'wrong','ABSENT','GETTEX:APC',True)
 def test_new_human_record_retains_prior_observation_and_evidence(self):
  self.event();self.check();self.observation();old=self.one()['observation']
  self.observation('UNCONFIRMED');j=self.one()
  self.assertEqual(j['observation_history'],[old]);self.assertEqual(j['observation']['result'],'UNCONFIRMED')
  self.assertEqual(j['status'],'RECHECK_QUEUED')
 def test_review_metadata_preserves_analysis_inputs_real_price_change_revokes(self):
  self.proof();self.event()
  with self.s.connect() as d:
   d.execute('INSERT INTO candles VALUES (?,?,?,?,?,?,?,?,?)',(self.i['isin'],'gettex','15m','2026-10-28 10:00:00',100.,101.,99.,100.,10.))
   token=dict(d.execute('SELECT scope,revision FROM input_revisions'))
  self.check();self.observation();rv.turn(self.s,self.i,self.now+pd.Timedelta(days=1))
  self.s.read_cache.assert_current(token)
  with self.s.connect() as d:
   d.execute('UPDATE candles SET close=100.5 WHERE isin=?',(self.i['isin'],))
  from performance_cache import InputRevisionChanged
  with self.assertRaises(InputRevisionChanged):self.s.read_cache.assert_current(token)
 def test_recheck_race_does_not_publish_stale_no_trades(self):
  self.event();self.proof();original=rv._classify
  def race(job,inputs,now):
   answer=original(job,inputs,now);self.proof(trade=True);return answer
  with patch.object(rv,'_classify',race):self.check()
  self.assertNotIn('finding',self.one())
 def test_persisted_progress_observation_survives_restart_and_no_retry_loop(self):
  self.event();self.check();self.observation();rv.turn(self.s,self.i,self.now+pd.Timedelta(days=1));j=self.one()
  self.s.read_cache.close();self.s=Store(self.temp.name)
  self.assertEqual(j,self.one());rv.turn(self.s,self.i,self.now+pd.Timedelta(days=1,minutes=1));self.assertEqual(j,self.one())
 def test_bounded_rotation_services_200_jobs(self):
  with self.s.connect() as d:
   for n in range(200):
    at=str(pd.Timestamp('2026-10-28 07:30')+pd.Timedelta(minutes=1)*n)
    spec=rv._spec(self.i['isin'],self.m,'1m',at,at);rv._enqueue(d,spec,{'kind':'fixture'},self.now)
  # Four rotating pages need sixteen visits each (64 / 4 checks).
  # The final eight-job page deliberately does not broaden the work bound.
  for _ in range(64):
   answer=self.check();self.assertLessEqual(answer['checked'],rv.CHECK_LIMIT)
  with self.s.connect() as d:jobs=[json.loads(v) for (v,) in d.execute('SELECT value FROM settings WHERE key LIKE ?',(rv.PREFIX+'%',))]
  self.assertEqual(len(jobs),200);self.assertTrue(all(j.get('checks')==1 for j in jobs));self.assertEqual(self.jobs()['total'],200)
 def test_weekend_and_unclosed_publication_grace_remain_distinct(self):
  for a,now,wanted in [('2026-11-01 10:00','2026-11-02T14:00Z','MARKET_CLOSED'),('2026-11-02 14:00','2026-11-02T13:10Z','PENDING'),('2026-11-02 14:00','2026-11-02T13:20Z','DELAYED')]:
   spec=rv._spec(self.i['isin'],self.m,'15m',a,a)
   with self.s.connect() as d:inputs=rv._input(d,spec)
   self.assertEqual(rv._classify(spec,inputs,pd.Timestamp(now))['state'],wanted)
 def test_daily_no_trades_requires_full_real_session_and_exact_source(self):
  p=self.proof('2026-10-28 07:30:00','2026-10-28 23:00:00')
  frame=pd.DataFrame({'open':[100.,102.],'high':[101.,103.],'low':[99.,101.],'close':[100.,102.],'volume':[10.,20.]},index=pd.to_datetime(['2026-10-27','2026-10-29']))
  frame.attrs=dict(self.m,timeframe='1D',classify_absence=True,absence_evidence=p)
  valid=dq.audit(frame,'1D',self.m,self.now,check_stale=False);self.assertEqual(valid['state'],'READY');self.assertEqual(len(frame),2)
  for proof in [dict(p,no_trades=[['2026-10-28 07:30:00','2026-10-28 12:00:00']]),dict(p,identity=dict(self.m,source='proxy')),dict(p,trade_intervals=[['2026-10-28 10:00:00','2026-10-28 10:05:00']]),dict(p,zero_quantity_intervals=[['2026-10-28 10:00:00','2026-10-28 10:05:00']])]:
   frame.attrs['absence_evidence']=proof;self.assertNotEqual(dq.audit(frame,'1D',self.m,self.now,check_stale=False)['state'],'READY')
 def test_no_trade_metadata_does_not_add_indicator_observations(self):
  idx=pd.date_range('2026-10-28 07:30',periods=45,freq='15min').delete(12)
  close=pd.Series([100+(n%9)*.3+n*.02 for n in range(len(idx))],index=idx)
  frame=pd.DataFrame(dict(open=close,high=close+1,low=close-1,close=close,volume=10.))
  p=self.proof('2026-10-28 10:30:00','2026-10-28 10:45:00');frame.attrs=dict(self.m,timeframe='15m',classify_absence=True,absence_evidence=p)
  self.assertEqual(dq.audit(frame,'15m',self.m,self.now,check_stale=False)['state'],'READY')
  actual=analytics.indicators(frame);plain=frame.copy();plain.attrs={k:v for k,v in frame.attrs.items() if k!='absence_evidence'};expected=analytics.indicators(plain)
  pd.testing.assert_frame_equal(actual[['ema10','ema30','rsi','macd','macd_signal','atr','volume_mean20']],expected[['ema10','ema30','rsi','macd','macd_signal','atr','volume_mean20']])
  self.assertEqual(len(actual),44);self.assertEqual(actual.ema30.iloc[-1],close.ewm(span=30,adjust=False,min_periods=30).mean().iloc[-1])
 def test_health_collection_is_read_only_and_does_not_create_jobs(self):
  with self.s.connect() as d:
   d.execute('PRAGMA query_only=ON');self.assertEqual(rv.collect(d)['total'],0)
  self.event();self.check()
  with self.s.connect() as d:
   d.execute('PRAGMA query_only=ON');all_stocks=rv.collect(d)
  self.assertEqual(all_stocks['rows'],self.jobs()['rows'])
 def test_long_native_range_is_split_and_resumed_without_lost_tail(self):
  with self.s.connect() as d:
   xp.install(d);d.execute('BEGIN IMMEDIATE')
   xp.record(d,self.i,self.m,'1D','2026-07-01','2026-10-29',{},self.now,'provider_range','FAILED',{'version':1,'code':'EMPTY_RESPONSE'})
  a=self.check();self.assertEqual(a['created'],4)
  self.s.read_cache.close();self.s=Store(self.temp.name)
  for _ in range(3):self.assertLessEqual(self.check()['created'],4)
  jobs=sorted(self.jobs()['rows'],key=lambda j:j['first'])
  self.assertEqual(jobs[0]['first'],'2026-07-01 00:00:00');self.assertEqual(jobs[-1]['last'],'2026-10-29 00:00:00')
  self.assertTrue(all(a['end']==b['first'] for a,b in zip(jobs,jobs[1:])))
  self.assertEqual(self.check()['created'],0)
 def test_week_and_month_use_real_calendar_period_not_next_bucket(self):
  w=rv._spec(self.i['isin'],self.m,'1W','2026-10-30','2026-10-30')
  self.assertEqual(w['interval_start'],'2026-10-24 00:00:00');self.assertEqual(w['end'],'2026-10-31 00:00:00')
  m=rv._spec(self.i['isin'],self.m,'1M','2026-10-01','2026-10-01')
  self.assertEqual(m['end'],'2026-11-01 00:00:00')
  with self.assertRaises(ValueError):rv._spec(self.i['isin'],self.m,'1W','2026-10-29','2026-10-29')
 def test_recovered_native_row_closes_review_but_invalid_stays_visible(self):
  spec=rv._spec(self.i['isin'],self.m,'15m',self.a,self.a)
  for high,wanted in [(101.,'NATIVE_ROWS_CONFIRMED'),(99.,'INVALID')]:
   inputs={'rows':[(self.a,100.,high,99.,100.,10.)],'proof':{}}
   self.assertEqual(rv._classify(spec,inputs,self.now)['state'],wanted)
 def test_existing_feed_runs_final_stage_without_refresh_or_new_engine(self):
  from feed_worker import FeedContext
  self.event()
  with patch.object(FeedContext,'_calculate',return_value={'errors':[]}),patch.object(ctl,'automatic_turn',return_value={'state':'IDLE'}):
   result=FeedContext(self.s).calculate(self.i)
  self.assertEqual(result['cp_reconciliation']['checked'],1)
 def test_reconfirmed_listing_time_does_not_invalidate_unchanged_observation(self):
  meta={**self.m,'source':'gettex_chart','symbol':'AAPL.GTX'}
  def listing(at,extra=None):
   proof=dict(meta,isin=self.i['isin'],status='CONFIRMED',verified_at=at,evidence='official listing')
   if extra:proof.update(extra)
   self.s.set_setting('gettex_chart:'+self.i['isin']+':15m',{'state':'SAVED','identity':meta,'listing_proof':proof})
  listing('2026-11-02T10:00Z');self.event(meta=meta);self.check();j=self.one()
  listing('2026-11-02T11:00Z');self.observation();self.assertEqual(self.one()['observation']['result'],'ABSENT')
  listing('2026-11-02T11:30Z',{'evidence':'different identity evidence'})
  with self.assertRaisesRegex(ValueError,'INPUT_CHANGED'):self.observation()
 def test_v1_observation_rebind_requires_unchanged_native_rows_and_proof(self):
  self.event();self.proof();self.check();self.observation();j=self.one()
  with self.s.connect() as d:
   inputs=rv._input(d,j);j.pop('fingerprint_version');j['input_fingerprint']='legacy';j['observation']['input_fingerprint']='legacy'
   ctl.write(d,rv.key(j),j)
  self.assertTrue(rv._observation_current(j,inputs))
  for changed in [dict(inputs,rows=[['2026-10-28 10:15',100,101,99,100,1]]),dict(inputs,proof={**inputs['proof'],'checked_at':'2026-11-02T13:00Z'})]:
   self.assertFalse(rv._observation_current(j,changed))
  self.check();new=self.one();self.assertEqual(new['observation']['previous_input_fingerprint'],'legacy')
  self.assertEqual(new['fingerprint_version'],2);self.assertEqual(new['observation']['input_fingerprint'],inputs['fingerprint'])

if __name__=='__main__':unittest.main()
