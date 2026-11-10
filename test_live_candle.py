"""New live-display contract. In-memory fixtures, no production data/network."""
import copy,json,sys,sqlite3,contextlib,unittest,hashlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch,Mock
import pandas as pd
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
import partial_chart as pc
import gettex_chart as gc
import candle_schedule as cs
from analytics import TIMEFRAMES,resample,bar_ends
from chart_worker import LatestRequest

INFO=dict(isin='US0378331005',name='Apple',kind='stock')
META=gc.identity(INFO['isin'],'AAPL.GTX')
PROOF={**META,'isin':INFO['isin'],'status':'CONFIRMED'}
NOW=pd.Timestamp('2026-11-03T18:34:00Z')
def frame(index,base='1m'):
 f=pd.DataFrame(dict(open=100.,high=102.,low=99.,close=101.,volume=2.),index=pd.DatetimeIndex(index))
 f.attrs={**META,'listing_proof':PROOF,'timeframe':base,'classify_absence':True,'received_at':NOW.isoformat()}
 return f
def empty(tf):return frame([],tf)

class LiveTail(unittest.TestCase):
 def setUp(self):
  self.minute=frame(pd.date_range('2026-11-03 07:30','2026-11-03 19:33',freq='min'))
  self.daily=frame(pd.to_datetime(['2026-11-02']),'1D')
  self.raw={'1m':self.minute,'1D':self.daily}
  self.store=SimpleNamespace()
 def display(self,tf,chosen=None,now=NOW):
  with patch.object(gc,'display_native',side_effect=lambda s,i,b,n:self.raw.get(b,empty(b))):
   return pc.live_tail(INFO,tf,empty(tf) if chosen is None else chosen,self.store,now)
 def test_all_eleven_tf_from_actual_minute_children(self):
  for tf in TIMEFRAMES:
   with self.subTest(tf=tf):
    out=self.display(tf);self.assertEqual(len(out),1)
    live=out.attrs['live_candle'];self.assertFalse(out.bar_complete.iloc[-1]);self.assertFalse(live['execution_allowed'])
    self.assertEqual(live['identity'],META);self.assertEqual(live['name'],'Живая свеча')
    self.assertEqual(out.close.iloc[-1],101.);self.assertEqual(live['status'],'FORMING')
    if tf=='4H':self.assertEqual(live['child_count'],4);self.assertEqual(live['child_first'],'2026-11-03 19:30:00')
 def test_first_parent_does_not_wait_for_closed_parent_history(self):
  self.minute=self.minute.tail(2).copy();self.raw={'1m':self.minute}
  self.store.setting=lambda key,default=None:default;self.store.load_official=lambda *a:pd.DataFrame()
  with patch.object(gc,'_load_native',side_effect=lambda s,i,b:self.raw.get(b,empty(b))),patch.object(gc,'display_native',side_effect=lambda s,i,b,n:self.raw.get(b,empty(b))):
   out=pc.select_display(INFO,'4H',empty('4H'),self.store,NOW)
  self.assertEqual(len(out),1);self.assertEqual(out.attrs['live_candle']['child_count'],2);self.assertFalse(out.bar_complete.iloc[0])
 def test_live_update_keeps_same_slot_and_all_older_rows(self):
  old=frame(pd.to_datetime(['2026-11-02 19:30']),'4H');old['bar_complete']=True
  a=self.display('4H',old);self.minute.loc[pd.Timestamp('2026-11-03 19:34')]=[101,103,100,102,3]
  b=self.display('4H',old,NOW+pd.Timedelta(minutes=1))
  self.assertEqual(a.index[-1],b.index[-1]);self.assertEqual(b.close.iloc[-1],102.)
  self.assertEqual(b.volume.iloc[-1],a.volume.iloc[-1]+3)
  pd.testing.assert_frame_equal(a.iloc[:-1],b.iloc[:-1]);self.assertEqual(len(b),2)
 def test_delay_keeps_last_values_and_never_promotes_by_clock(self):
  a=self.display('4H');b=self.display('4H',now=NOW+pd.Timedelta(hours=7))
  pd.testing.assert_frame_equal(a,b);self.assertEqual(b.attrs['live_candle']['status'],'WAITING_DATA')
  self.assertFalse(b.bar_complete.iloc[-1]);self.assertLess(pd.Timestamp(b.attrs['live_candle']['observed_through']),NOW+pd.Timedelta(hours=7))
 def test_no_real_data_no_invented_candle(self):
  self.raw={}
  for tf in TIMEFRAMES:self.assertTrue(self.display(tf).empty)
 def test_wrong_identity_or_invalid_ohlcv_is_rejected(self):
  self.raw.pop('1D')
  for change in ({'currency':'USD'},{'venue':'FRA'},{'source':'gettex'},{'symbol':'MSFT.GTX'}):
   self.minute.attrs.update(change);self.assertTrue(self.display('4H').empty);self.minute.attrs.update(META)
  self.minute.loc[self.minute.index[-1],'high']=1.;self.assertTrue(self.display('4H').empty)
 def test_completed_native_bar_wins_over_lower_tf_preview(self):
  native=frame(pd.to_datetime(['2026-11-03 19:30']),'4H');native['bar_complete']=True;native['close']=100.5
  out=self.display('4H',native,NOW+pd.Timedelta(hours=7));pd.testing.assert_frame_equal(native,out)
  self.assertNotIn('live_candle',out.attrs)
 def test_week_and_month_preserve_previous_daily_volume(self):
  for tf in ('1W','1M'):
   out=self.display(tf);self.assertEqual(out.volume.iloc[0],self.minute.volume.sum()+self.daily.volume.sum())
   self.assertEqual(out.open.iloc[0],self.daily.open.iloc[0]);self.assertFalse(out.bar_complete.iloc[0])
 def test_no_provider_join_for_onvista_daily(self):
  f=empty('1D');f.attrs.update(source='onvista',listing_proof={**PROOF,'source':'onvista'})
  self.store.load_series=Mock(return_value=empty('1m'))
  self.assertTrue(self.display('1D',f).empty)
 def test_interior_gap_remains_blocked_display_tail_only(self):
  raw=self.minute.drop(pd.Timestamp('2026-11-03 08:00'));raw.attrs['display_live_tail']=True
  shown=resample(raw,'1m','4H',NOW)
  self.assertNotIn(pd.Timestamp('2026-11-03 07:30'),shown.index)
  self.assertIn(pd.Timestamp('2026-11-03 19:30'),shown.index)
  raw.attrs.pop('display_live_tail');strict=resample(raw,'1m','4H',NOW)
  self.assertNotIn(pd.Timestamp('2026-11-03 19:30'),strict.index)

class Receipts(unittest.TestCase):
 def test_retained_forming_receipt_survives_later_response_without_tail(self):
  d=Path(__file__).resolve().parents[1].parent/'СМ-Архив';old=d/'chart_cache'/INFO['isin']/'old-live.json';new=old.with_name('new-closed.json')
  pending=json.dumps(self.payload()).encode();p=self.payload();p['data']=p['data'][:1];closed=json.dumps(p).encode()
  common=dict(identity=META,received_at=NOW.isoformat(),requested_from=(NOW-pd.Timedelta(days=1)).isoformat(),requested_to=NOW.isoformat())
  prior={**common,'receipt':str(old),'sha256':hashlib.sha256(pending).hexdigest(),'provisional_timestamps':['2026-11-03 19:30:00']}
  saved={**common,'state':'SAVED','listing_proof':PROOF,'receipt':str(new),'sha256':hashlib.sha256(closed).hexdigest(),'live_receipt':prior}
  native=self.decode(p);s=SimpleNamespace(directory=d,setting=lambda *a:saved,read_cache=SimpleNamespace(get=lambda key,fn,**kw:fn()))
  with patch.object(gc,'_load_native',return_value=native),patch.object(Path,'stat',return_value=SimpleNamespace(st_size=1000,st_mtime_ns=1)),patch.object(Path,'read_bytes',lambda path:pending if path==old else closed):
   out=gc.display_native(s,INFO,'5m',NOW+pd.Timedelta(hours=4))
  self.assertEqual(len(out),2);self.assertEqual(out.close.iloc[-1],101.)
  self.assertIn('2026-11-03 19:30:00',out.attrs['provisional_children'])
  self.assertEqual(gc.pending_receipt(saved,saved,native),prior)
  complete=self.decode(self.payload(),True);complete.attrs['provisional_timestamps']=[]
  self.assertIsNone(gc.pending_receipt(saved,saved,complete))
 def test_first_forming_only_fetch_persists_evidence_not_ohlcv(self):
  s=Memory();self.addCleanup(s.db.close);s.directory=Path(__file__).resolve().parents[1].parent/'СМ-Архив'
  s.db.executescript('create table instruments(isin text,payload text);create table input_revisions(scope text primary key,revision integer);create table series(dummy integer);')
  s.db.execute('insert into instruments values (?,?)',(INFO['isin'],json.dumps(INFO)));s.db.commit()
  p=self.payload();p['data']=p['data'][-1:]
  client=SimpleNamespace(fetch=lambda *args:(p,META))
  exists=Path.exists
  with (patch.object(cs,'decision',side_effect=lambda store,isin,m,b,now:dict(due=b=='5m',key='fixture')),
       patch.object(cs,'finish'),patch('delivery_observations.observe'),patch('delivery_observations.outcome'),
       patch.object(Path,'mkdir'),patch.object(Path,'write_bytes'),patch.object(Path,'exists',lambda p:False if p.suffix=='.json' and p.parent.name==INFO['isin'] else exists(p))):
   result=gc._fetch_turn(INFO,s,NOW,client,preferred_tf='5m')
  self.assertEqual(result['state'],'UNCHANGED',result);self.assertTrue(result['provisional_only']);self.assertEqual(result['added'],0)
  saved=s.setting(gc.state_key(INFO['isin'],'5m'));self.assertTrue(gc.confirmed_state(INFO,saved))
  self.assertEqual(saved['live_receipt']['provisional_timestamps'],['2026-11-03 19:30:00'])
  self.assertEqual(saved['quality'],'PENDING');self.assertEqual(s.db.execute('select count(*) from series').fetchone()[0],0)
 def payload(self):
  return dict(status='OK',request=dict(ric='AAPL.GTX',interval=5,intervalUnit='m'),data=[
   dict(TIME=int(pd.Timestamp(t,tz='Europe/Berlin').timestamp()),OPEN=100,HIGH=102,LOW=99,CLOSE=101,VOLUME=2)
   for t in ('2026-11-03 19:25','2026-11-03 19:30')])
 def decode(self,p,forming=False):
  return gc.decode(p,META,INFO['isin'],'5m',NOW-pd.Timedelta(days=1),NOW,NOW,include_forming=forming)
 def test_forming_kept_only_in_explicit_display_decode(self):
  self.assertEqual(len(self.decode(self.payload())),1);self.assertEqual(len(self.decode(self.payload(),True)),2)
 def test_bad_envelope_not_accepted_for_live(self):
  for k,v in [('ric','MSFT.GTX'),('interval',15)]:
   p=self.payload();p['request'][k]=v
   with self.assertRaises(ValueError):self.decode(p,True)
 def test_receipt_pinned_time_hash_and_no_sqlite_write(self):
  directory=Path(__file__).resolve().parents[1].parent/'СМ-Архив'
  path=directory/'chart_cache'/INFO['isin']/'fixture-live.json';payload=json.dumps(self.payload()).encode()
  saved=dict(state='SAVED',identity=META,listing_proof=PROOF,receipt=str(path),sha256=hashlib.sha256(payload).hexdigest(),
   received_at=NOW.isoformat(),requested_from=(NOW-pd.Timedelta(days=1)).isoformat(),requested_to=NOW.isoformat())
  cache=SimpleNamespace(get=lambda key,fn,**kw:fn())
  store=SimpleNamespace(directory=directory,setting=lambda *a:saved,read_cache=cache)
  native=self.decode(self.payload());before=native.copy(deep=True)
  with patch.object(gc,'_load_native',return_value=native),patch.object(Path,'stat',return_value=SimpleNamespace(st_size=len(payload),st_mtime_ns=1)),patch.object(Path,'read_bytes',return_value=payload):
   out=gc.display_native(store,INFO,'5m',NOW+pd.Timedelta(hours=7))
   self.assertEqual(len(out),2);self.assertIn('2026-11-03 19:30:00',out.attrs['provisional_children'])
   saved['sha256']='wrong';self.assertEqual(len(gc.display_native(store,INFO,'5m',NOW)),1)
  pd.testing.assert_frame_equal(native,before)

class Memory:
 def __init__(self):
  self.db=sqlite3.connect(':memory:');self.db.execute('create table settings(key text primary key,value text)')
 @contextlib.contextmanager
 def connect(self):
  with self.db:yield self.db
 def setting(self,k,d=None):
  r=self.db.execute('select value from settings where key=?',(k,)).fetchone();return json.loads(r[0]) if r else d
 def set_setting(self,k,v):self.db.execute('insert or replace into settings values (?,?)',(k,json.dumps(v)));self.db.commit()

class MinuteScheduler(unittest.TestCase):
 def setup_universe(self,n):
  self.s=Memory();self.addCleanup(self.s.db.close);self.now=NOW
  self.infos=[dict(isin=f'I{i:03}',name=f'Fixture{i}',kind='stock') for i in range(n)]
  for i in self.infos:self.s.set_setting('gettex_chart:'+i['isin']+':15m',dict(identity=META,received_at='2026-11-02T12:00Z'))
  self.focus={'isin':'I001','timeframe':'4H'};self.pos=dict(isin='I000',quantity=1,timeframe='15m',identity=META)
 def plan(self):
  with patch('paper_account.ledger',return_value={'positions':[self.pos]}),patch.object(gc,'confirmed_state',side_effect=lambda i,v:bool(v.get('identity'))),patch.object(cs,'decision',side_effect=lambda store,isin,m,tf,now:dict(due=m['source']=='gettex_chart',session_open=True,key='unused')):
   return cs.fresh_candidate(self.s,self.infos,self.now,self.focus)[0]
 def finish(self,item):
  cs.fresh_finished(self.s,item,'gettex_chart','SAVED',self.now)
  self.s.set_setting('gettex_chart:'+item['isin']+':'+item['_cp_focus']['timeframe'],dict(identity=META,received_at=self.now.isoformat()));self.now+=pd.Timedelta(seconds=1)
 def test_53_200_jobs_keep_minutes_positions_and_history_fair(self):
  for n in (53,200):
   self.setup_universe(n);seen=set();bases=set();history=0
   for _ in range(4*(n-2)):
    item=self.plan()
    if item is None:cs.history_finished(self.s,self.now);history+=1
    else:seen.add(item['isin']);bases.add(item['_cp_focus']['timeframe']);self.finish(item)
   self.assertEqual(len(seen),n);self.assertEqual(history,n-2);self.assertIn('1m',bases);self.assertIn('15m',bases)
 def test_minute_bootstrap_respects_retry_and_global_rate_reservation(self):
  self.setup_universe(3);self.finish(self.plan())
  self.assertEqual(self.plan()['_cp_focus']['timeframe'],'1m')
  self.s.set_setting('gettex_chart:I001:1m',dict(retry_after=(NOW+pd.Timedelta(hours=2)).isoformat()))
  self.assertEqual(self.plan()['_cp_focus']['timeframe'],'15m')
  self.s.set_setting('gettex_chart_next_request',(NOW+pd.Timedelta(minutes=1)).isoformat());self.assertIsNone(self.plan())
 def test_restart_preserves_fairness_position(self):
  self.setup_universe(5);self.finish(self.plan());self.finish(self.plan())
  other=Memory();self.addCleanup(other.db.close);self.s.db.backup(other.db);self.s=other
  self.assertEqual(self.plan()['isin'],'I002')

class ChartGuards(unittest.TestCase):
 def test_provisional_receipt_does_not_invalidate_closed_core_but_quality_does(self):
  from performance_cache import _setting_value
  s=Memory();self.addCleanup(s.db.close);key=gc.state_key(INFO['isin'],'5m')
  value=dict(identity=META,state='UNCHANGED',quality='READY',live_receipt={'sha256':'old'},provisional_only=True)
  s.set_setting(key,value);before=_setting_value(s.db,key)
  value['live_receipt']={'sha256':'new'};value['provisional_only']=False;s.set_setting(key,value)
  self.assertEqual(before,_setting_value(s.db,key))
  value['quality']='INVALID';s.set_setting(key,value);self.assertNotEqual(before,_setting_value(s.db,key))
 def test_native_receipt_revision_invalidates_cached_live_view(self):
  from performance_cache import ReadCache
  cache=ReadCache(':memory:');self.addCleanup(cache.close)
  scope='setting:gettex_chart:'+INFO['isin']+':1m'
  cache.db.execute('create table input_revisions(scope text primary key,revision integer)')
  cache.db.execute('insert into input_revisions values (?,1)',(scope,));cache.db.commit()
  minute=frame(pd.date_range('2026-11-03 19:30',periods=4,freq='min'))
  store=SimpleNamespace(read_cache=cache,setting=lambda k,d=None:d,load_official=lambda *a:pd.DataFrame())
  normal=frame(pd.to_datetime(['2026-11-02 19:30']),'4H')
  def native(s,i,b,n):cache.depend(scope);return minute if b=='1m' else empty(b)
  with patch.object(gc,'display_native',side_effect=native),patch.object(gc,'_load_native',return_value=pd.DataFrame()):
   a=pc.select_display(INFO,'4H',normal,store,NOW)
   minute.loc[minute.index[-1],'close']=102.
   cache.db.execute('update input_revisions set revision=2 where scope=?',(scope,));cache.db.commit()
   b=pc.select_display(INFO,'4H',normal,store,NOW)
  self.assertEqual(a.close.iloc[-1],101.);self.assertEqual(b.close.iloc[-1],102.)
  self.assertFalse(cache.current({scope:1}))
 def test_feed_refreshes_only_selected_chart_and_never_closing_window(self):
  from desktop014 import Desktop014
  for selected,closing,wanted in ((True,False,1),(False,False,0),(True,True,0)):
   app=SimpleNamespace(closing=closing,feed_active={INFO['isin']},instrument_cfg=lambda:INFO if selected else {'isin':'OTHER'},refresh_chart=Mock(),_write_log=Mock())
   with patch('freshness_runtime.feed_completed'):Desktop014.feed_done(app,{**INFO,'_gettex_poll_only':True},[])
   self.assertEqual(app.refresh_chart.call_count,wanted);self.assertFalse(app.feed_active)
 def test_chart_marks_live_and_delayed_without_new_gap_band(self):
  from matplotlib.figure import Figure
  from matplotlib.backends.backend_agg import FigureCanvasAgg
  from chart_modes import draw_prices
  f=frame(pd.to_datetime(['2026-11-03 19:30']),'4H');f['bar_complete']=False
  for status in ('FORMING','WAITING_DATA'):
   f.attrs['live_candle']=dict(timestamp=str(f.index[-1]),status=status,observed_through=NOW.isoformat())
   fig=Figure();ax=fig.subplots();draw_prices(ax,f,'Candles');FigureCanvasAgg(fig).draw()
   self.assertTrue(any('Живая свеча' in t.get_text() for t in ax.texts));self.assertEqual(len(ax.collections),0)
 def test_53_200_updates_apply_only_current_request_and_reject_closed_window(self):
  for count in (53,200):
   jobs=[];applied=[];q=LatestRequest(lambda work,ok,bad:jobs.append((work,ok,bad)),applied.append,lambda e:None)
   for i in range(count):q.request(i)
   self.assertEqual(len(jobs),1);jobs[0][1]('stale revision');self.assertEqual(applied,[])
   self.assertEqual(jobs[1][0],count-1);jobs[1][1]('current revision');self.assertEqual(applied,['current revision'])
   q.request(count);q.close();jobs[2][1]('after close');self.assertEqual(applied,['current revision'])
 def test_analysis_receives_closed_rows_only(self):
  import chart_worker
  f=frame(pd.to_datetime(['2026-11-03 19:15','2026-11-03 19:30']),'15m');f['bar_complete']=[True,False]
  f.attrs['live_candle']={'timestamp':str(f.index[-1])};received=[]
  class Context:
   def instrument_cfg(self):return INFO
   def user_state(self,i):return {}
   def source_mode(self,i):return 'gettex_chart'
   def frame_for(self,*a):return f
   def analyse_cached(self,i,fr,s):received.append(fr);return fr.copy(),{'ohlcv_valid':False}
   def trading_analysis(self,*a,**kw):return {'benchmark_context':{'state':'BLOCKED'}}
  with patch.object(chart_worker,'SnapshotContext',Context),patch.object(pc,'select_display',return_value=f):
   result=chart_worker.prepare(None,dict(timeframe='15m'))
  self.assertEqual(len(received[0]),1);self.assertEqual(len(result['frame']),2)
  self.assertEqual(result['trading_engine']['benchmark_context']['state'],'BLOCKED')
 def test_preliminary_indicators_move_without_changing_confirmed_decision(self):
  import chart_worker
  f=frame(pd.date_range('2026-11-03 07:30',periods=40,freq='15min'),'15m');f['bar_complete']=True;f.loc[f.index[-1],'bar_complete']=False
  f.loc[f.index[-1],['high','close']]=[110.,110.];f.attrs['live_candle']={'timestamp':str(f.index[-1])}
  from analytics import indicators
  class Context:
   def instrument_cfg(self):return INFO
   def user_state(self,i):return {}
   def source_mode(self,i):return 'gettex_chart'
   def frame_for(self,*a):return f
   def analyse_cached(self,i,fr,s):return indicators(fr),{'ohlcv_valid':False,'decision':'WAIT','confirmed_close':fr.close.iloc[-1]}
   def trading_analysis(self,*a,**kw):return {'benchmark_context':{'state':'BLOCKED'}}
  with patch.object(chart_worker,'SnapshotContext',Context),patch.object(pc,'select_display',return_value=f):result=chart_worker.prepare(None,dict(timeframe='15m'))
  enriched,decision=result['analysis'];self.assertGreater(enriched.ema10.iloc[-1],enriched.ema10.iloc[-2])
  self.assertEqual(decision['confirmed_close'],101.);self.assertEqual(decision['decision'],'WAIT');self.assertFalse(enriched.bar_complete.iloc[-1])

if __name__=='__main__':unittest.main(verbosity=2)
