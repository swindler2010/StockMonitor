"""UI polling uses semantic revisions, not full historical DQ payloads."""
import sys,time,unittest,json
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
from desktop014 import Desktop014
from test_cp_reviewed_risk import Memory

class PollBudgetTests(unittest.TestCase):
 def test_startup_does_not_eagerly_decode_archives_or_lose_feed_buffer(self):
  from unittest.mock import Mock
  for count in (53,200):
   ready=object();app=NS(watchlist=[{'name':str(i)} for i in range(count)],
    data={'0':ready,'removed':object()},data_meta={'0':{'source':'GETTEX'},'removed':{}},store=Mock())
   Desktop014._load_cached(app)
   self.assertEqual(app.data,{'0':ready});self.assertEqual(app.data_meta,{'0':{'source':'GETTEX'}})
   app.store.assert_not_called();self.assertEqual(app.store.mock_calls,[])

 def setUp(self):
  self.store=Memory();self.addCleanup(self.store.read_cache.close)
  self.info={'isin':'TEST'};self.key='data_quality:TEST:SAVED:1H'
  self.store.set_setting('history_identity:TEST',{'symbol':'SAVED'})
  self.report={'state':'UNCONFIRMED','ranges':[['2026-01-01','2026-01-02']]*20000,'checked_at':'before'}
  self.store.set_setting(self.key,self.report)
  self.app=NS(instrument_cfg=lambda:self.info,store=self.store,source_mode=lambda _: 'proxy',timeframe=NS(get=lambda:'1H'),
    _cp_chart_inputs={'isin':'TEST','timeframe':'1H','scopes':['market:TEST:15m']})
 def signature(self):return Desktop014.current_quality_signature(self.app)
 def test_53_200_polls_never_decode_dq_and_keep_relevant_invalidation(self):
  original=self.store.setting;reads=[]
  def setting(key,default=None):
   reads.append(key)
   if key.startswith('data_quality:'):self.fail('full DQ decoded on UI poll')
   return original(key,default)
  with patch.object(self.store,'setting',side_effect=setting),patch('desktop014.time.time',return_value=120):
   before=self.signature()
   for n in (53,200):
    for _ in range(n):self.assertEqual(self.signature(),before)
   self.assertEqual(reads,['history_identity:TEST'])
   q={**self.report,'checked_at':'after','raw_retry_after':99};self.store.set_setting(self.key,q)
   self.assertEqual(before,self.signature())
   self.store.set_setting('unrelated',{'value':1});self.assertEqual(before,self.signature())
   self.store.set_setting(self.key,{**q,'state':'INVALID'});changed=self.signature();self.assertNotEqual(before,changed)
   self.store.db.execute("INSERT INTO candles VALUES ('OTHER','gettex','15m','2026-01-01',1,2,1,2,1)");self.store.db.commit()
   self.assertEqual(changed,self.signature())
   self.store.db.execute("INSERT INTO candles VALUES ('TEST','gettex','15m','2026-01-01',1,2,1,2,1)");self.store.db.commit()
   self.assertNotEqual(changed,self.signature())
   self.store.set_setting('history_identity:TEST',{'symbol':'NEW'})
   self.signature();self.assertEqual(reads,['history_identity:TEST','history_identity:TEST'])
 def test_minute_boundary_and_selection_remain_significant(self):
  with patch('desktop014.time.time',return_value=120):before=self.signature()
  with patch('desktop014.time.time',return_value=180):self.assertNotEqual(before,self.signature())
  with patch('desktop014.time.time',return_value=120):
   self.info={'isin':'OTHER'};self.assertNotEqual(before,self.signature())

class FreshnessBudgetTests(unittest.TestCase):
 def fixture(self):
  from freshness_runtime import FreshnessRuntime
  jobs=[];timers={};clock=[100.];published=[]
  def after(delay,callback):
   key=len(timers)+1;timers[key]=(delay,callback);return key
  root=NS(after=after,after_cancel=lambda key:timers.pop(key,None))
  def job(*args,**kwargs):jobs.append(args);return True
  app=NS(closing=False,watchlist=[{'isin':'TEST'}],root=root,job=job,store=NS(directory=Path('.')),
    _freshness_listener=published.append,bridge=NS())
  return FreshnessRuntime(app),app,jobs,timers,clock,published
 def test_53_200_feed_events_publish_asof_once_and_bound_followup(self):
  r,app,jobs,timers,clock,published=self.fixture()
  with patch('freshness_runtime.perf_counter',side_effect=lambda:clock[0]):
   r.request('FEED_COMPLETED:TEST')
   for n in (53,200):
    for _ in range(n):r.request('FEED_COMPLETED:TEST')
   self.assertEqual(len(jobs),1)
   value={'checked_at':'as-of-original','_automatic_read_seconds':1};jobs[0][1](value)
   self.assertEqual(published,[value]);self.assertTrue(value['_newer_inputs_pending'])
   self.assertEqual(len(timers),1);self.assertEqual(next(iter(timers.values()))[0],60000)
   for _ in range(200):r.request('FEED_COMPLETED:TEST')
   self.assertEqual(len(jobs),1);self.assertEqual(len(timers),1)
   r.request('PANEL_REFRESH');self.assertEqual(len(jobs),2);self.assertFalse(timers)
   app.watchlist.append({'isin':'OTHER'});jobs[1][1]({'checked_at':'obsolete'})
   self.assertEqual(len(published),1);self.assertEqual(next(iter(timers.values()))[0],0)
   app.closing=True;self.assertFalse(r.request());self.assertEqual(len(jobs),2)
 def test_failed_read_is_unavailable_and_does_not_spin_on_feeds(self):
  r,app,jobs,timers,clock,published=self.fixture()
  with patch('freshness_runtime.perf_counter',side_effect=lambda:clock[0]):
   r.request('FEED_COMPLETED:TEST');jobs[0][2]('private error')
   self.assertIsNone(r.latest);self.assertEqual(app.bridge.freshness_activity['state'],'UNAVAILABLE')
   for _ in range(200):r.request('FEED_COMPLETED:TEST')
   self.assertEqual(len(jobs),1);self.assertEqual(len(timers),1);self.assertFalse(published)

class NumericContractBudgetTests(unittest.TestCase):
 def test_large_evidence_not_copied_and_numeric_identity_guards_unchanged(self):
  import pandas as pd,numpy as np
  from gettex_first import bind,bound
  class Evidence:
   def __deepcopy__(self,memo):raise AssertionError('archive metadata copied for numeric validation')
  f=pd.DataFrame(dict(open=[100.]*40,high=[101.]*40,low=[99.]*40,close=[100.]*40,volume=[0.]*40,source=['gettex']*40),index=pd.date_range('2026-11-13 08:00',periods=40,freq='15min'))
  f.attrs=dict(source='gettex',symbol='TEST',venue='GETTEX',currency='EUR',timezone='Europe/Berlin')
  f=bind(f,{'isin':'TEST'});evidence=Evidence();f.attrs['original_evidence']=evidence
  for count in (53,200):
   for _ in range(count):self.assertTrue(bound(f))
  self.assertIs(f.attrs['original_evidence'],evidence);self.assertEqual(float(pd.DataFrame(f,copy=False).volume.sum()),0)
  for column,bad,original in [('volume',-1.,0.),('high',99.,101.),('low',101.,99.),('close',np.nan,100.),('open',0.,100.),('source','other','gettex')]:
   f.iat[0,f.columns.get_loc(column)]=bad;self.assertFalse(bound(f));f.iat[0,f.columns.get_loc(column)]=original
  f.attrs['currency']='USD';self.assertFalse(bound(f));f.attrs['currency']='EUR';self.assertTrue(bound(f))
  f.attrs['gettex_first_contract']['isin']='OTHER';self.assertFalse(bound(f))

class LineageReadBudgetTests(unittest.TestCase):
 def test_one_wal_view_reuses_proof_next_read_rechecks_and_race_rejects(self):
  import tempfile,pandas as pd
  from test_cp_tv_legacy_reference import FlatStore
  from analytical_read import calculate_consistent
  from history_service import attach_parent_provenance,parent_provenance_key
  from performance_cache import InputRevisionChanged
  import revision_watcher as rw
  archive=Path(__file__).parent.parent.parent/'СМ-Архив'
  with tempfile.NamedTemporaryFile(dir=archive,suffix='.sqlite3',delete=False) as file:path=Path(file.name)
  store=None
  try:
   store=FlatStore(path);isin='TEST';key='receipt:TEST';stamp='2026-11-13 10:00:00'
   meta=dict(source='gettex_chart',symbol='TEST.GTX',venue='GETTEX',currency='EUR',timezone='Europe/Berlin')
   receipt=dict(first=stamp,end='2026-11-13 10:15:00',source={'proof':{'identity':meta}})
   store.set_setting(key,receipt);store.set_setting(rw.RETIME_POLICY,{'approved':True})
   store.set_setting('interval_evidence:'+isin,dict(identity=meta,checked_at='2026-11-14T09:00Z'))
   store.set_setting(parent_provenance_key(isin,meta,'15m'),{stamp:{'official_timebase':key}})
   frame=pd.DataFrame([[100,101,99,100,1]],index=pd.to_datetime([stamp]),columns=['open','high','low','close','volume']);frame.attrs=meta
   def calculate(captured,values):
    frames=[attach_parent_provenance(captured,frame.copy(),isin,'15m') for _ in range(values['count'])]
    if values.get('race'):store.set_setting(rw.RETIME_POLICY,{'approved':False})
    return {'invalid':frames[-1].attrs['invalid_parent_provenance']}
   with patch('revision_watcher.retime_current',return_value=True) as check:
    for count in (53,200):
     before=check.call_count
     result=calculate_consistent(store,{'count':count},calculate)
     self.assertEqual(check.call_count,before+1);self.assertEqual(result['invalid'],[])
     self.assertIn('setting:'+rw.RETIME_POLICY,result['_input_revisions'])
     self.assertIn('setting:gettex_chart:TEST:15m',result['_input_revisions'])
    with self.assertRaises(InputRevisionChanged):calculate_consistent(store,{'count':2,'race':True},calculate)
   with patch('revision_watcher.retime_current',return_value=False) as check:
    self.assertEqual(calculate_consistent(store,{'count':2},calculate)['invalid'],[stamp]);self.assertEqual(check.call_count,1)
    # Ordinary writer/read calls never reuse the transaction-only receipt.
    for _ in range(2):attach_parent_provenance(store,frame.copy(),isin,'15m')
    self.assertEqual(check.call_count,3)
  finally:
   if store:store.read_cache.close()
   for suffix in ('','-wal','-shm'):Path(str(path)+suffix).unlink(missing_ok=True)

class PersistentViewTests(unittest.TestCase):
 def test_53_200_reads_reuse_only_current_inputs_and_reject_writer_race(self):
  import tempfile,sqlite3
  from test_cp_tv_legacy_reference import FlatStore
  from analytical_read import calculate_consistent
  from performance_cache import InputRevisionChanged
  archive=Path(__file__).parent.parent.parent/'СМ-Архив'
  with tempfile.NamedTemporaryFile(dir=archive,suffix='.sqlite3',delete=False) as f:path=Path(f.name)
  s=None
  try:
   s=FlatStore(path);s._serial_readonly=True
   with s.connect() as db:db.execute('PRAGMA journal_mode=WAL')
   s.save_instrument({'isin':'TEST','name':'Test'});s.set_setting('policy',1)
   with s.connect() as db:db.execute("INSERT INTO candles VALUES ('TEST','gettex','15m','2026-11-20 08:00',100,101,99,100,1)")
   loads=[]
   def calc(captured,values):
    def load():
     loads.append(1);captured.read_cache.depend('market:TEST:15m')
     return dict(policy=captured.setting('policy'),rows=captured.read_cache.db.execute('SELECT close FROM candles').fetchall())
    result=captured.read_cache.get('verified',load,scoped=True)
    if values.get('race'):s.set_setting('policy',3)
    return result
   original=s.read_cache
   for count in (53,200):
    for _ in range(count):self.assertEqual(calculate_consistent(s,{},calc)['policy'],1)
   self.assertEqual(len(loads),1);self.assertIs(s.read_cache,original);self.assertFalse(original.db.in_transaction)
   s.set_setting('unrelated',2);calculate_consistent(s,{},calc);self.assertEqual(len(loads),1)
   with s.connect() as db:db.execute("INSERT INTO candles VALUES ('TEST','gettex','15m','2026-11-20 08:15',100,101,99,100,1)")
   self.assertEqual(len(calculate_consistent(s,{},calc)['rows']),2);self.assertEqual(len(loads),2)
   with s.connect() as db:db.execute("UPDATE candles SET close=100.5 WHERE ts='2026-11-20 08:00'")
   self.assertEqual(calculate_consistent(s,{},calc)['rows'][0][0],100.5);self.assertEqual(len(loads),3)
   with self.assertRaises(InputRevisionChanged):calculate_consistent(s,{'race':True},calc)
   self.assertFalse(original.db.in_transaction);self.assertEqual(calculate_consistent(s,{},calc)['policy'],3)
   with self.assertRaises(sqlite3.OperationalError):
    calculate_consistent(s,{},lambda c,v:dict(write=c.read_cache.db.execute("DELETE FROM candles")))
   self.assertFalse(original.db.in_transaction);self.assertEqual(len(calculate_consistent(s,{},calc)['rows']),2)
  finally:
   if s:s.read_cache.close()
   for suffix in ('','-wal','-shm'):Path(str(path)+suffix).unlink(missing_ok=True)

class DisplayReuseTests(unittest.TestCase):
 def test_pixel_changes_invalidate_but_telemetry_keeps_existing_artists(self):
  import pandas as pd
  from matplotlib.figure import Figure
  from chart_worker import visual_revision
  from benchmark_ui import BenchmarkMixin
  f=pd.DataFrame(dict(open=[100.,101.],high=[102.,103.],low=[99.,100.],close=[101.,102.],volume=[1.,2.]),index=pd.date_range('2026-11-20',periods=2,freq='h'))
  f.attrs={'source':'gettex','data_quality':{'state':'UNCONFIRMED','checked_at':'before'}}
  result={'data_quality':dict(f.attrs['data_quality']),'support':99.}
  prepared={'frame':f,'analysis':(f,result),'zones':[]};settings={};store=NS(setting=lambda k,d=None:settings.get(k,d));values={'timeframe':'1H'}
  before=visual_revision(prepared,values,store);fig=Figure();axis=fig.subplots();status=['shown'];rendered=[]
  def render():rendered.append(1);axis.plot([0,1],[101,102]);status[0]='shown'
  app=NS(chart_mode=NS(get=lambda:'Candles'),status=NS(set=lambda v:status.__setitem__(0,v),get=lambda:status[0]),root=NS(),timeframe=NS(get=lambda:'1H'),_render_prepared_chart=render)
  prepared['_visual_revision']=before;BenchmarkMixin._render_current_display(app,prepared);artist=axis.lines[0]
  f.attrs['data_quality']['checked_at']='later';result['data_quality']['checked_at']='later'
  self.assertEqual(before,visual_revision(prepared,values,store))
  for count in (53,200):
   for _ in range(count):
    status[0]='loading';BenchmarkMixin._render_current_display(app,prepared)
  self.assertEqual(len(rendered),1);self.assertIs(axis.lines[0],artist);self.assertEqual(status[0],'shown')
  f.iloc[0,f.columns.get_loc('close')]=100.5;changed=visual_revision(prepared,values,store);self.assertNotEqual(before,changed)
  prepared['_visual_revision']=changed;BenchmarkMixin._render_current_display(app,prepared);self.assertEqual(len(rendered),2)
  settings['global_indicators']={'SMA':True};self.assertNotEqual(changed,visual_revision(prepared,values,store))
  f.attrs['data_quality']['state']='INVALID';self.assertNotEqual(changed,visual_revision(prepared,values,store))
 def test_stale_revision_cannot_reach_visual_reuse(self):
  from benchmark_ui import BenchmarkMixin
  from unittest.mock import Mock
  values={'watchlist':[{'isin':'TEST'}],'timeframe':'1H','period':'Max'}
  app=NS(closing=False,store=NS(read_cache=NS(current=lambda _:False)),_retry_current_chart=Mock(),_render_current_display=Mock())
  with patch('chart_worker.capture',return_value=values):BenchmarkMixin._apply_chart(app,(values,{'_input_revisions':{'market:TEST:15m':1},'_visual_revision':'same'}))
  app._render_current_display.assert_not_called();app._retry_current_chart.assert_called_once()
 def test_numeric_indicator_render_preserves_all_plotted_values_without_attrs_copy(self):
  import pandas as pd,numpy as np
  from matplotlib.figure import Figure
  from desktop014 import DEFAULT_IND
  f=pd.DataFrame(dict(open=np.arange(70.)+100,high=np.arange(70.)+102,low=np.arange(70.)+99,close=np.arange(70.)+101,volume=np.arange(70.)),index=pd.date_range('2026-11-20',periods=70,freq='h'))
  class Evidence:
   def __deepcopy__(self,memo):raise AssertionError('Plot copied historical evidence')
  def host():
   fig=Figure();axes=fig.subplots(6);return NS(ax_price=axes[0],panels=list(zip(('Объём','Относительный объём','RSI','MACD','ATR'),axes[1:])),timeframe=NS(get=lambda:'1H'))
  plain=host();Desktop014.draw_indicators(plain,f,f,DEFAULT_IND)
  f.attrs['evidence']=Evidence();rich=host();Desktop014.draw_indicators(rich,f,f,DEFAULT_IND)
  for a,b in zip([plain.ax_price]+[a for _,a in plain.panels],[rich.ax_price]+[a for _,a in rich.panels]):
   self.assertEqual(len(a.lines),len(b.lines))
   for x,y in zip(a.lines,b.lines):np.testing.assert_allclose(x.get_ydata(),y.get_ydata(),equal_nan=True)

class ArchiveYieldTests(unittest.TestCase):
 def test_slow_files_yield_with_cursor_cooldown_and_restart_resume(self):
  import test_cp_gettex_universe as fixture
  from test_gettex_live_evidence import MemoryPath
  import gettex_history as gh,gzip,pandas as pd
  f=fixture.CachedHistory();f.setUp();self.addCleanup(f.doCleanups);s=f.s
  meta=dict(source='gettex',venue='GETTEX',currency='EUR',symbol=fixture.APPLE,timezone='Europe/Berlin')
  s.set_setting('data_quality:'+fixture.APPLE+':'+fixture.APPLE+':15m',dict(state='UNCONFIRMED',identity=meta,ranges=[['2026-11-02 09:00','2026-11-02 09:00']]))
  tick=[0.];calls=[]
  def http(url):
   calls.append(url);tick[0]+=20
   return b'<a href="https://erdk.bayerische-boerse.de/data/posttrade.20261102.08.00.munc.csv.gz">file</a>' if url==gh.PAGE else gzip.compress(b'fixture')
  with patch.object(MemoryPath,'mkdir',create=True),patch.object(gh.time,'monotonic',side_effect=lambda:tick[0]):
   result=gh.acquire_missing_evidence({'isin':fixture.APPLE},s,http,f.now)
   self.assertTrue(result['yielded']);self.assertEqual(result['downloaded'],1);self.assertEqual(len(calls),2)
   cursor=result['file_cursor'];calls.clear()
   self.assertEqual(gh.acquire_missing_evidence({'isin':fixture.APPLE},s,http,f.now+pd.Timedelta(minutes=1))['state'],'COOLDOWN');self.assertFalse(calls)
   # Existing durable store/file checkpoint survives a new facade.
   reopened=NS(connect=s.connect,setting=s.setting,set_setting=s.set_setting,directory=s.directory)
   next_result=gh.acquire_missing_evidence({'isin':fixture.APPLE},reopened,http,f.now+pd.Timedelta(minutes=16))
   self.assertEqual(next_result['downloaded'],1);self.assertNotEqual(next_result['file_cursor'],cursor)
   self.assertFalse(f.rows());self.assertNotEqual(s.setting('data_quality:'+fixture.APPLE+':'+fixture.APPLE+':15m')['state'],'READY')

if __name__=='__main__':unittest.main()
