"""Issue20: factual calculation and CP opinion are independent."""
import copy,unittest
import test_trading_engine as fixtures
from analytics import _safe_features,resample
from gettex_first import calculation_allowed,bind,bound
from trading_engine import evaluate

class GettexFirstTests(unittest.TestCase):
 def test_paper_runtime_does_not_wait_for_full_history_audit(self):
  from bridge import Bridge
  from pathlib import Path
  from types import SimpleNamespace as NS
  from unittest.mock import patch
  import threading
  b=object.__new__(Bridge);b.directory=Path('.');b.state={};b.lock=threading.Lock()
  b.server=NS(server_port=0);b.instance_id='fixture';b.published_at=None;b.diagnostic_pipeline={}
  with patch('paper_account.status',return_value={'status':'ACTIVE'}) as paper,patch('candlepilot_health.collect',side_effect=AssertionError('unrequested audit')):
   r=b.identity(include_candlepilot=False)
   self.assertEqual(r['paper_account']['status'],'ACTIVE');self.assertEqual(r['candlepilot_health']['status'],'NOT_READ')
   paper.assert_called_once()
 def test_advisory_revision_does_not_revoke_factual_closed_inputs(self):
  import test_paper_hourly as fixture
  from performance_cache import capture_closed_inputs,validate_closed_inputs,InputRevisionChanged
  case=fixture.HourlyTests();case.setUp();self.addCleanup(case.tearDown)
  key='data_quality:'+case.isin+':AAPL.GTX:1H';case.write(key,{'state':'UNCONFIRMED'})
  token=dict(case.db.execute('SELECT scope,revision FROM input_revisions'))
  specs=[copy.deepcopy(s['spec']) for s in case.result['hourly']['proof']['series']]
  for s in specs:s['calculation_policy']='gettex_first_v1'
  old=capture_closed_inputs(case.db,[{k:v for k,v in s.items() if k!='calculation_policy'} for s in specs],token)
  proof=capture_closed_inputs(case.db,specs,token)
  case.write(key,{'state':'INVALID'})
  validate_closed_inputs(case.db,proof)
  with self.assertRaises(InputRevisionChanged):validate_closed_inputs(case.db,old)
  case.db.execute("UPDATE series SET high=high+1 WHERE base_tf='1H'")
  with self.assertRaisesRegex(InputRevisionChanged,'Closed candle changed'):validate_closed_inputs(case.db,proof)
 def test_analytical_read_uses_one_view_and_keeps_actual_revision_guard(self):
  import sqlite3,tempfile,os
  from pathlib import Path
  from contextlib import contextmanager
  from unittest.mock import patch
  from performance_cache import ReadCache,InputRevisionChanged
  from analytical_read import calculate_consistent
  archive=Path(__file__).resolve().parents[2]/'СМ-Архив'
  fd,path=tempfile.mkstemp(prefix='Issue20 read ',suffix='.sqlite3',dir=archive if archive.is_dir() else None);os.close(fd)
  self.addCleanup(lambda:os.unlink(path))
  db=sqlite3.connect(path);self.addCleanup(db.close);db.execute('PRAGMA journal_mode=WAL')
  db.execute('CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT)')
  db.execute('CREATE TABLE input_revisions(scope TEXT PRIMARY KEY,revision INTEGER)')
  for key in ('data_quality:X:15m','actual'):
   db.execute('INSERT INTO settings VALUES (?,?)',(key,'0'))
   db.execute('INSERT INTO input_revisions VALUES (?,0)',('setting:'+key,))
  db.commit()
  class Reader:
   @contextmanager
   def connect(self):
    c=sqlite3.connect(self.path)
    try:yield c
    finally:c.close()
   def setting(self,key):
    self.read_cache.depend('setting:'+key)
    with self.connect() as c:return c.execute('SELECT value FROM settings WHERE key=?',(key,)).fetchone()[0]
  store=Reader();store.path=Path(path);store.read_cache=ReadCache(path);self.addCleanup(store.read_cache.close)
  mode=['data_quality:X:15m']
  def calculate(captured,values):
   first=captured.setting(mode[0]);db.execute('UPDATE settings SET value=value+1 WHERE key=?',(mode[0],))
   db.execute('UPDATE input_revisions SET revision=revision+1 WHERE scope=?',('setting:'+mode[0],));db.commit()
   self.assertEqual(captured.setting(mode[0]),first)
   engine=dict(status='READY',timeframe_context={'1H':dict(facts={'price':1},calculation_availability=dict(policy='gettex_first_v1',source_contract=True,analysis_available=True))})
   return {'analysis_context':{'trading_engine':engine},'warning_as_of':first}
  with patch('analytical_read.calculate',side_effect=calculate):
   self.assertEqual(calculate_consistent(store,{})['warning_as_of'],'0')
   self.assertEqual(calculate_consistent(store,{},calculate)['warning_as_of'],'1')
   mode[0]='actual'
   with self.assertRaises(InputRevisionChanged):calculate_consistent(store,{})
 def test_full_reference_executor_accepts_available_bars_without_faking_dq(self):
  from calculation_window import _full_execution_proof
  from unittest.mock import patch
  pd=fixtures.pd;now=pd.Timestamp('2026-11-04T09:35Z')
  identity={k:self.frame().attrs[k] for k in ('source','symbol','venue','currency','timezone')}
  required=['15m','4H','1D'];isin='US0378331005'
  market={'context':'full_closed_reference_v1','quality':'READY','state':'NEUTRAL','benchmark_id':'INDEX',
   'valid_until':(now+pd.Timedelta(minutes=1)).isoformat(),'_closed_input':{'isin':'INDEX'}}
  rows={tf:dict(context='gettex_first_v1',identity=identity,valid_until=market['valid_until'],
   calculation_availability=dict(policy='gettex_first_v1',source_contract=True,analysis_available=True),
   full_reference=dict(context='actual_gettex_reference_v1',analysis_ready=True,count=40,
    last_close_at=(now-pd.Timedelta(minutes=5)).isoformat(),quality={'state':'INVALID'})) for tf in required}
  candidate=dict(full_execution_version=1,result={'status':'READY'},required_tf=required,signal_benchmark=market,
   closed_inputs={'series':[dict(count=40,spec=dict(isin=isin,timeframe=tf,identity=identity)) for tf in required]+[dict(count=40,spec=market['_closed_input'])]})
  record=dict(isin=isin,timeframes=rows,full_reference_benchmark=market)
  with patch('calculation_window.candidate_state',return_value='CURRENT'):
   self.assertEqual(_full_execution_proof(None,record,candidate,identity,now)[0]['15m']['full_reference']['quality']['state'],'INVALID')
   rows['4H']['calculation_availability']['source_contract']=False
   with self.assertRaisesRegex(ValueError,'REFERENCE_BLOCKED:4H'):_full_execution_proof(None,record,candidate,identity,now)
  with patch('calculation_window.candidate_state',return_value='STALE'):
   with self.assertRaisesRegex(ValueError,'NOT_CURRENT'):_full_execution_proof(None,record,candidate,identity,now)
 def test_busy_trading_universe_cannot_starve_sequential_preparation(self):
  from paper_hourly import Runtime,CONFIG
  from types import SimpleNamespace as NS
  from unittest.mock import Mock,patch
  cfg={'enabled':True,'identities':{str(n):{} for n in range(5)},'automatic_admission':{'enabled':True}}
  store=NS(setting=lambda key,default:cfg if key==CONFIG else {},read_cache=NS(token_for=lambda _:{}))
  jobs=[]
  def submit(work,done,*args,**kwargs):jobs.append(kwargs['key']);done({});return True
  app=NS(store=store,closing=False,user_dirty=False,watchlist=[{'isin':str(n),'name':str(n)} for n in range(5)],
   ai_config={},root=NS(after=Mock()),job=submit)
  runtime=Runtime(app)
  with patch('paper_hourly.time.time',return_value=200):
   for _ in range(4):runtime.tick()
  self.assertEqual(jobs,['paper_auto_hourly','paper_auto_hourly','paper_preparation','paper_auto_hourly'])
  self.assertFalse(runtime.busy)
 def test_candidate_does_not_replace_long_history_with_newer_short_provider(self):
  import gettex_chart
  from unittest.mock import patch
  pd=fixtures.pd;info={'isin':'US0378331005'}
  long=self.frame();long.index=pd.bdate_range('2026-05-01',periods=len(long))+pd.Timedelta(hours=7,minutes=30);long.attrs.update(timeframe='15m')
  long['source']='gettex_chart';long.attrs.update(source='gettex_chart',symbol='AAPL.GTX',
   listing_proof=dict(source='gettex_chart',symbol='AAPL.GTX',venue='GETTEX',currency='EUR',timezone='Europe/Berlin',isin=info['isin'],status='CONFIRMED'))
  short=self.frame().tail(10).copy();short.index=pd.date_range('2026-11-20 07:30',periods=10,freq='15min');short.attrs.update(timeframe='15m')
  with patch('gettex_chart.load',return_value=long):
   out,base=gettex_chart.candidate(None,info,'4H',short,'15m',pd.Timestamp('2026-11-20T12:00Z'))
  self.assertEqual(len(out),len(long));self.assertEqual(out.attrs['source'],'gettex_chart')
 def test_legacy_validation_window_retains_warning_and_own_quote_guards(self):
  from calculation_window import inspect
  pd=fixtures.pd
  f=self.frame();f.index=pd.date_range('2026-11-04 07:30',periods=len(f),freq='15min')
  f.attrs.update(timeframe='15m',data_quality={'state':'INVALID'});f['bar_complete']=False
  f=bind(f,{'isin':'US0378331005'})
  # Use the morning's actual available portion, retaining sufficient older rows.
  f.index=pd.bdate_range('2026-01-01',periods=len(f)-1).append(pd.DatetimeIndex(['2026-11-04 10:15']))
  now=pd.Timestamp('2026-11-04T09:35Z')
  r=inspect({'isin':'US0378331005'},f,'15m',now)
  self.assertEqual(r['status'],'READY',r['blockers']);self.assertEqual(r['archive_quality']['state'],'INVALID')
  self.assertIn('STALE_EXECUTION_BAR',inspect({'isin':'US0378331005'},f,'15m',now+pd.Timedelta(minutes=30))['blockers'])
  f.iloc[0,f.columns.get_loc('high')]=0
  self.assertNotEqual(inspect({'isin':'US0378331005'},f,'15m',now)['status'],'READY')
 def test_quote_warning_is_advisory_but_actual_price_and_age_are_checked(self):
  import test_paper_hourly as fixture
  import paper_account as paper
  from eur_listings import stored_identity
  case=fixture.HourlyTests();case.setUp();self.addCleanup(case.tearDown)
  q=case.result_for('BUY')['execution_quote']
  proof=stored_identity(case.db,case.info,q['identity'],'15m')
  for state in ('INVALID','UNCONFIRMED','STALE','SOURCE_GAP'):
   q['data_quality']={'state':state,'checked_at':'2020-01-01T00:00:00Z'}
   self.assertEqual(paper.quote(case.info,q,case.now,stored_listing=proof)[1]['close'],100)
  bad=copy.deepcopy(q);bad['bars'][0]['close_at']='2026-11-04T08:30:00Z'
  with self.assertRaisesRegex(ValueError,'STALE_QUOTE'):paper.quote(case.info,bad,case.now,stored_listing=proof)
  bad=copy.deepcopy(q);bad['bars'][0]['high']=0
  with self.assertRaisesRegex(ValueError,'BAD_OHLCV'):paper.quote(case.info,bad,case.now,stored_listing=proof)
  bad=copy.deepcopy(q);bad['bars'][0]['open_at']='2026-11-04T10:16:00+01:00'
  with self.assertRaisesRegex(ValueError,'QUOTE_DQ_BLOCKED'):paper.quote(case.info,bad,case.now,stored_listing=proof)
  with self.assertRaisesRegex(ValueError,'IDENTITY_BLOCKED'):paper.quote(case.info,q,case.now)
 def test_aggregation_keeps_actual_bars_without_claiming_complete_coverage(self):
  pd=fixtures.pd
  f=self.frame().iloc[:7].copy()
  f.index=pd.date_range('2026-11-13 07:30',periods=7,freq='15min')
  f.attrs.update(timeframe='15m',data_quality={'state':'UNCONFIRMED'})
  f=f.drop(f.index[2]);f=bind(f,{'isin':'US0378331005'})
  now=pd.Timestamp('2026-11-13T12:00Z')
  for tf in ('30m','1H','2H','3H','4H','1D','1W','1M'):
   with self.subTest(tf=tf):
    out=resample(f,'15m',tf,now)
    self.assertFalse(out.empty)
    self.assertEqual(float(out.volume.sum()),float(f.volume.sum()))
    self.assertEqual(float(out.high.max()),float(f.high.max()))
    self.assertEqual(float(out.low.min()),float(f.low.min()))
    self.assertFalse(out.bar_complete.all())
    self.assertNotEqual(out.attrs['data_quality']['state'],'READY')
 def test_false_or_damaged_source_contract_cannot_enable_aggregation(self):
  f=bind(self.frame(),{'isin':'US0378331005'});self.assertTrue(bound(f))
  f.attrs['currency']='USD';self.assertFalse(bound(f))
  f=bind(self.frame(),{'isin':'US0378331005'});f.iloc[0,f.columns.get_loc('volume')]=-1
  self.assertFalse(bound(f))
 def test_corrected_child_recomputes_parent_and_indicators_without_changing_original(self):
  pd=fixtures.pd
  f=self.frame().copy();f.index=pd.date_range('2026-11-02 07:30',periods=len(f),freq='15min')
  f.attrs.update(timeframe='15m',data_quality={'state':'UNCONFIRMED'})
  f=bind(f,{'isin':'US0378331005'});original=f.copy(deep=True)
  now=pd.Timestamp('2026-12-01T12:00Z');before=resample(f,'15m','1H',now)
  changed=f.copy();changed.iloc[3,changed.columns.get_loc('high')]+=5
  after=resample(changed,'15m','1H',now)
  self.assertTrue(f.equals(original));self.assertNotEqual(before.high.iloc[0],after.high.iloc[0])
  self.assertEqual(float(before.volume.sum()),float(after.volume.sum()))
  self.assertEqual(before.attrs['data_quality']['state'],after.attrs['data_quality']['state'])
 def test_long_gettex_archive_survives_short_clean_alternative(self):
  import gettex_chart
  pd=fixtures.pd
  info={'isin':'US0378331005'}
  long=self.frame();long.index=pd.date_range('2026-11-13 07:30',periods=len(long),freq='5min')
  long.attrs.update(timeframe='5m',data_quality={'state':'INVALID'})
  short=long.iloc[-10:].copy();short.attrs['data_quality']={'state':'READY'}
  view,base=gettex_chart.retain_deeper(info,'1H',long,'5m',short,'5m',pd.Timestamp('2026-11-14T12:00Z'))
  self.assertEqual(base,'5m');self.assertEqual(len(view),len(long))
  self.assertEqual(view.attrs['data_quality']['state'],'INVALID')
 def test_v5_and_v4_use_available_closed_values_without_filling_missing_children(self):
  from paper_v5 import hourly_features,clean_quotes
  from paper_v4 import intraday
  pd=fixtures.pd
  sessions=pd.DataFrame({'open':[pd.Timestamp('2026-11-13T06:30Z')],
      'close':[pd.Timestamp('2026-11-13T10:30Z')]},index=pd.DatetimeIndex(['2026-11-13']))
  rows=[('2026-11-13 07:30:00',100.,101.,99.,100.,0.),('2026-11-13 08:00:00',101.,102.,100.,102.,10.)]
  raw=clean_quotes(rows,'Europe/Berlin',available_bars=True)
  self.assertEqual(len(raw),2)
  v5=hourly_features(sessions,raw,pd.Timestamp('2026-11-13T08:30Z'),available_bars=True)
  v4=intraday(sessions,raw,'1H',pd.Timestamp('2026-11-13T08:30Z'),available_bars=True)
  for frame in (v5,v4):
   self.assertTrue(frame.complete.iloc[0]);self.assertFalse(frame.coverage_complete.iloc[0])
   self.assertEqual(frame.volume.iloc[0],10.)
   self.assertFalse(frame.complete.iloc[1]);self.assertTrue(pd.isna(frame.close.iloc[1]))
  legacy=hourly_features(sessions,raw,pd.Timestamp('2026-11-13T08:30Z'))
  self.assertFalse(legacy.complete.iloc[0])
 def test_decision_evidence_is_stable_for_audit_timestamps_but_tracks_real_prices(self):
  from gettex_first import decision_evidence
  result={'isin':'US0378331005','timeframe':'1H','execution_context':{'trading_engine':{
   'timeframe_context':{'1H':{'facts':{'ema10':10},'data_quality':{'state':'UNCONFIRMED','checked_at':'old'}}}}},
   'execution_quote':{'bars':[{'close':10}],'data_quality':{'checked_at':'old'}}}
  before=decision_evidence(result)
  changed=copy.deepcopy(result);changed['execution_context']['trading_engine']['timeframe_context']['1H']['data_quality']['checked_at']='new'
  changed['execution_quote']['data_quality']['checked_at']='new'
  self.assertEqual(before,decision_evidence(changed))
  changed['execution_quote']['bars'][0]['close']=11
  self.assertNotEqual(before,decision_evidence(changed))
 def test_sequential_admission_does_not_wait_for_another_stocks_quote(self):
  import test_paper_preparation as fixture
  import paper_preparation as prep
  import paper_hourly as hourly
  c=fixture.PreparationTests();c.setUp();self.addCleanup(c.tearDown);f=c.f
  f.db.execute('delete from settings where key=?',(prep.STATE,))
  f.write(hourly.STATUS+c.anchor,dict(core='DATA BLOCKED',quote_blocker='PAPER_STALE_QUOTE',at=f.now))
  self.assertEqual(prep.choose(f.db,f.now+601)['isin'],f.isin)
  # Available closed analysis may be connected even while execution is stale.
  r=f.result;r['quote_blocker']='PAPER_STALE_QUOTE'
  for tf in r['hourly']['required_tf']:
   r['execution_context']['trading_engine']['timeframe_context'][tf]={
    'data_quality':{'state':'UNCONFIRMED'},'facts':{'atr':1},
    'calculation_availability':{'policy':'gettex_first_v1','source_contract':True,'analysis_available':True}}
  # choose() updated the preparation state; retain the existing exact token.
  from performance_cache import capture_closed_inputs
  token={k:(f.db.execute('select revision from input_revisions where scope=?',(k,)).fetchone() or [0])[0]
    for k in r['_input_revisions']}
  r['_input_revisions']=token
  r['hourly']['proof']=capture_closed_inputs(f.db,[s['spec'] for s in r['hourly']['proof']['series']],token)
  out=prep.admit(f.db,f.info,r,f.now)
  self.assertEqual(out['state'],'ADMITTED');self.assertFalse(out['execution_permission'])
  self.assertEqual(out['admission']['basis'],'GETTEX_AVAILABLE_ANALYSIS')
 def test_new_instrument_contract_is_generic_and_keeps_provider_identity(self):
  from gettex_first import source_contract
  info={'isin':'US9999999999'};f=self.frame();f['source']='gettex_chart'
  f.attrs.update(source='gettex_chart',symbol='FIXTURE.GTX')
  self.assertFalse(source_contract(info,f.attrs))
  f.attrs['listing_proof']={k:f.attrs[k] for k in ('source','symbol','venue','currency','timezone')}
  f.attrs['listing_proof'].update(isin=info['isin'],status='CONFIRMED')
  f=bind(f,info);self.assertTrue(bound(f))
  _,r=_safe_features(f,info,'2026-02-01T00:00Z')
  self.assertTrue(calculation_allowed(r))
  self.assertEqual(r['calculation_availability']['source'],'gettex_chart')
 def test_general_assessment_and_display_preserve_advisory_quality(self):
  from calculation_window import bounded_features
  from partial_chart import select_display
  from unittest.mock import patch
  f=bind(self.frame(),{'isin':'US0378331005'})
  f.attrs.update(data_quality={'state':'INVALID','issues':['historical suspicion']},partial_chart={'display_only':True})
  info={'isin':'US0378331005'};now=fixtures.pd.Timestamp('2026-02-01T00:00Z')
  r=bounded_features(info,f,'1H',now)
  self.assertTrue(r['analysis_ready']);self.assertNotEqual(r['window_quality']['state'],'READY')
  with patch('partial_chart.live_tail',side_effect=lambda i,t,v,s,n:v):
   view=select_display(info,'1H',f,None,now)
  self.assertNotIn('partial_chart',view.attrs);self.assertEqual(view.attrs['data_quality']['state'],'INVALID')
  self.assertTrue(f.attrs['partial_chart']['display_only'])
 def test_partial_coverage_closed_bars_are_calculable_and_forming_bar_excluded(self):
  f=bind(self.frame(),{'isin':'US0378331005'});f['bar_complete']=False
  f.attrs.update(data_quality={'state':'UNCONFIRMED'},analysis_scope={'start':'2021-01-01'})
  _,r=_safe_features(f,{'isin':'US0378331005'},'2026-02-01T00:00Z',prove_scope=True)
  self.assertTrue(calculation_allowed(r));self.assertFalse(r['scope_initialization']['checked'])
  f.index=fixtures.pd.date_range('2026-01-30 07:30',periods=len(f),freq='1h')
  _,r=_safe_features(f,{'isin':'US0378331005'},'2026-01-30T09:00Z')
  self.assertFalse(calculation_allowed(r));self.assertLess(r['closed_bar_count'],35)
 def frame(self):
  cls=next(c for c in vars(fixtures).values() if isinstance(c,type) and hasattr(c,'market_frames'))
  f=cls().market_frames()['1H'];f['source']='gettex'
  f.attrs.update(source='gettex',symbol='US0378331005',venue='GETTEX',currency='EUR',timezone='Europe/Berlin')
  return f
 def test_warnings_preserved_and_identical_indicators_for_real_bars(self):
  f=self.frame();info={'isin':'US0378331005'};now='2026-02-01T00:00Z'
  base_x,base=_safe_features(f,info,now)
  for state in ('GAP','UNCONFIRMED','INVALID','STALE','SOURCE_GAP','RECOVERING'):
   with self.subTest(state=state):
    f.attrs['data_quality']={'state':state,'ranges':[['2026-01-01','2026-01-01']]}
    before=copy.deepcopy(f.attrs);x,r=_safe_features(f,info,now)
    self.assertTrue(calculation_allowed(r));self.assertEqual(r['facts'],base['facts'])
    self.assertTrue(x.ema30.equals(base_x.ema30));self.assertEqual(f.attrs,before)
    self.assertEqual(r['data_quality']['state'],state)
    self.assertFalse(r['calculation_availability']['execution_permission'])
 def test_bad_values_missing_bars_and_wrong_listing_remain_unavailable(self):
  for kind in ('bad','empty','short','currency','isin','provider','row_source'):
   f=self.frame();info={'isin':'US0378331005'};f.attrs['data_quality']={'state':'UNCONFIRMED'}
   if kind=='bad':f.iloc[-1,f.columns.get_loc('high')]=0
   if kind=='empty':f=f.iloc[:0]
   if kind=='short':f=f.tail(10)
   if kind=='currency':f.attrs['currency']='USD'
   if kind=='isin':info['isin']='WRONG'
   if kind=='provider':f.attrs['source']='proxy'
   if kind=='row_source':f.iloc[0,f.columns.get_loc('source')]='yahoo'
   _,r=_safe_features(f,info,'2026-02-01T00:00Z')
   self.assertFalse(calculation_allowed(r),kind)
 def test_core_uses_calculation_availability_and_keeps_strategy_rules(self):
  f=self.frame();f.attrs['data_quality']={'state':'UNCONFIRMED'}
  _,r=_safe_features(f,{'isin':'US0378331005'},'2026-02-01T00:00Z')
  contexts={tf:copy.deepcopy(r) for tf in ('1H','4H','1D')}
  self.assertEqual(evaluate(contexts,'1H')['status'],'READY')
  contexts['4H']['ohlcv_valid']=False
  self.assertEqual(evaluate(contexts,'1H')['status'],'DATA BLOCKED')
 def test_paper_dq_warning_does_not_remove_revision_or_quote_guards(self):
  import test_paper_hourly as fixture
  import paper_hourly as hourly
  import paper_account as paper
  case=fixture.HourlyTests();case.setUp();self.addCleanup(case.tearDown)
  result=case.result_for('BUY')
  engine=result['execution_context']['trading_engine']
  for tf in result['hourly']['required_tf']:
   engine['timeframe_context'][tf]={'data_quality':{'state':'UNCONFIRMED'},
    'calculation_availability':{'policy':'gettex_first_v1','source_contract':True,'analysis_available':True}}
  hourly.validate_analysis(case.db,case.info,result,case.now,case.cfg)
  bad=copy.deepcopy(result);bad['hourly']['ledger_token']='changed'
  with self.assertRaisesRegex(ValueError,'POSITION_CHANGED'):
   hourly.validate_analysis(case.db,case.info,bad,case.now,case.cfg)
  bad=copy.deepcopy(result);bad['quote_blocker']='PAPER_STALE_QUOTE'
  with self.assertRaisesRegex(ValueError,'PAPER_STALE_QUOTE'):
   hourly.validate(case.db,case.info,bad,case.args,case.now)
  bad=copy.deepcopy(result);bad['execution_context']['trading_engine']['timeframe_context']['4H']['calculation_availability']['source_contract']=False
  with self.assertRaisesRegex(ValueError,'DQ_BLOCKED'):
   hourly.validate_analysis(case.db,case.info,bad,case.now,case.cfg)

if __name__=='__main__':unittest.main()

