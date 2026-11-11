"""Owner-authorized whole-series Proxy EUR/session selection; no network/orders."""
import copy,sys,tempfile,unittest,queue
from pathlib import Path
from types import SimpleNamespace as NS
from contextlib import nullcontext
from unittest.mock import patch
import pandas as pd
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
from storage import Store
from desktop014 import Desktop014,prefer_recent_official
from snapshot_worker import SnapshotContext,Value
import data_quality as dq
import candle_schedule as cs
import calculation_window as cw

class ProxySession(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.s=Store(self.tmp.name)
  self.now=pd.Timestamp('2026-10-27T22:16Z');self.isin='DE0007164600'
  self.g=cs.gettex_meta(self.isin)
  self.p=dict(source='proxy',symbol='SAP.DE',venue='GER',currency='EUR',timezone='Europe/Berlin')
  self.info=dict(isin=self.isin,name='SAP',source_mode='proxy',history_symbol='SAP.DE',history_currency='EUR',history_venue='GER',
    eur_listings=[dict(self.p,status='CONFIRMED',isin=self.isin)])
  self.s.save_watchlist([self.info]);self.s.set_setting('history_identity:'+self.isin,self.p)
  for b in ('5m','15m'):
   f=self.make(self.g,b,100);self.s.save_frame(self.isin,'gettex',b,f)
  for b in ('5m','15m','1H','1D'):
   f=self.make(self.p,b,900);self.s.save_series(self.info,f,b)
  self.ctx=NS(store=self.s,source_mode=lambda i:'proxy',analysis_at=self.now)
 def tearDown(self):self.s.read_cache.close();self.tmp.cleanup()
 def make(self,meta,tf,price):
  idx=dq.expected_index(meta,tf,'2026-10-26','2026-10-27 23:00',self.now)
  f=pd.DataFrame(dict(open=float(price),high=float(price+2),low=float(price-1),close=float(price+1),volume=10.,source=meta['source']),index=idx)
  f.attrs=dict(meta);return f
 def read(self,tf):return Desktop014._frame_uncached(self.ctx,self.info,tf)
 def test_all_intraday_use_whole_gettex_eur_series_without_proxy_prices(self):
  for tf in ('5m','10m','15m','30m','1H','2H','3H','4H'):
   with self.subTest(tf=tf):
    out=self.read(tf);self.assertGreater(len(out),0)
    self.assertEqual((out.attrs['source'],out.attrs['venue'],out.attrs['currency']),('gettex','GETTEX','EUR'))
    self.assertEqual(out.attrs['source_selection']['selected'],'GETTEX_EUR')
    self.assertTrue(out['close'].eq(101).all());self.assertEqual(str(out.index[-1].time()),'22:55:00' if tf=='5m' else '22:50:00' if tf=='10m' else '22:45:00' if tf=='15m' else '22:30:00' if tf in ('30m','1H','3H') else '21:30:00' if tf=='2H' else '19:30:00')
    self.assertEqual(str(out.index[0].time()),'07:30:00')
  self.assertEqual(self.s.instruments(),[self.info]);self.assertEqual(self.s.setting('history_identity:'+self.isin),self.p)
 def test_gettex_primary_choice_is_independent_of_reserve_day(self):
  g=self.s.load_official(self.isin,'5m');p=self.s.load_series(self.isin,'5m','proxy')
  g=g.loc[g.index<'2026-10-27 09:00'].copy()
  self.assertTrue(prefer_recent_official(self.info,'5m',g,p,self.now,True))
  g=g.loc[g.index<'2026-10-27'].copy()
  self.assertTrue(prefer_recent_official(self.info,'5m',g,p,self.now,True))
  self.assertNotEqual(dq.audit(g,'5m',now=self.now)['state'],'READY')
 def test_wrong_identity_and_forming_only_official_not_selected(self):
  g=self.s.load_official(self.isin,'5m');p=self.s.load_series(self.isin,'5m','proxy')
  for k,v in [('symbol','OTHER'),('venue','GER'),('currency','USD'),('timezone','UTC'),('source','other')]:
   bad=g.copy();bad.attrs[k]=v
   self.assertFalse(prefer_recent_official(self.info,'5m',bad,p,self.now,True),k)
  forming=g.tail(1).copy();forming.index=pd.DatetimeIndex(['2026-10-27 23:30'])
  self.assertFalse(prefer_recent_official(self.info,'5m',forming,p,pd.Timestamp('2026-10-27T21:00Z'),True))
 def test_no_gettex_keeps_actual_proxy_hours_with_limitation(self):
  with patch.object(self.s,'load_official',return_value=pd.DataFrame()):out=self.read('5m')
  self.assertEqual(out.attrs['source_selection']['selected'],'PROXY_EUR_LIMITED')
  self.assertEqual(out.attrs['venue'],'GER');self.assertEqual(str(out.index[-1].time()),'17:25:00')
  self.assertNotIn('session_close',out.attrs)
 def test_no_usd_substitution_when_eur_missing(self):
  usd=self.make(dict(self.p,currency='USD'), '5m',900)
  self.s.set_setting('history_identity:'+self.isin,dict(self.p,currency='USD'))
  with patch.object(self.s,'load_official',return_value=pd.DataFrame()),patch.object(self.s,'load_series',return_value=usd):out=self.read('5m')
  self.assertTrue(out.empty);self.assertEqual(out.attrs['data_quality']['state'],'SOURCE_UNAVAILABLE')
 def test_gettex_replaces_usd_without_conversion(self):
  usd=self.make(dict(self.p,currency='USD'),'5m',900)
  self.s.set_setting('history_identity:'+self.isin,dict(self.p,currency='USD'))
  with patch.object(self.s,'load_series',return_value=usd):out=self.read('5m')
  self.assertEqual(out.attrs['currency'],'EUR');self.assertTrue(out.close.eq(101).all())
 def test_daily_history_and_required_pin_not_switched(self):
  out=self.read('1D');self.assertEqual(out.attrs['venue'],'GETTEX');self.assertEqual(out.attrs['source_selection']['selected'],'GETTEX_EUR')
  out=Desktop014._frame_uncached(self.ctx,self.info,'1D',required_identity=self.p)
  self.assertEqual(out.attrs['symbol'],'SAP.DE')
 def test_real_gap_invalid_and_stale_not_waived(self):
  self.s.set_setting(dq.key(self.info,self.isin,'5m'),dict(state='SOURCE_GAP',issues=['real missing trades'],ranges=[]))
  out=self.read('5m');self.assertEqual(out.attrs['data_quality']['state'],'SOURCE_GAP')
  self.s.set_setting(dq.key(self.info,self.isin,'5m'),dict(state='INVALID',issues=['damaged archive'],ranges=[]))
  self.assertEqual(self.read('5m').attrs['data_quality']['state'],'INVALID')
  self.ctx.analysis_at=pd.Timestamp('2026-10-28T12:00Z')
  self.s.set_setting(dq.key(self.info,self.isin,'5m'),{})
  self.assertGreater(self.read('5m').attrs['data_quality'].get('stale_count',0),0)
 def test_actual_official_revision_revokes_read_token(self):
  with self.s.read_cache.track_inputs() as token:out=self.read('5m')
  self.assertTrue(self.s.read_cache.current(token))
  f=self.s.load_official(self.isin,'5m').tail(1).copy();f.iloc[0,f.columns.get_loc('close')]=102
  self.s.save_frame(self.isin,'gettex','5m',f)
  self.assertFalse(self.s.read_cache.current(token));self.assertEqual(self.read('5m').close.iloc[-1],102)
 def test_core_expected_identity_and_daily_dependency_follow_auto_row(self):
  self.info.update(levels={'stop':800.,'target':950.},levels_source='proxy',levels_symbol='SAP.DE',levels_venue='GER',levels_currency='EUR')
  ctx=SnapshotContext();ctx.store=self.s;ctx.watchlist=[self.info];ctx.drafts={};ctx.timeframe=Value('5m');ctx.period=Value('год');ctx.set_analysis_scope(self.now)
  seen={}
  def core(frames,tf,state,market,now):
   seen.update(frames=frames,state=copy.deepcopy(state));return {'status':'DATA BLOCKED'}
  with patch('analytics.analyse_multiframe',side_effect=core),patch('benchmarks.load',return_value=pd.DataFrame()),patch('benchmarks.relative_performance',return_value={'state':'UNAVAILABLE'}):ctx._trading_analysis(self.info)
  self.assertEqual(seen['state']['expected_identity']['source'],'gettex')
  self.assertEqual(seen['state']['levels'],{})
  self.assertTrue(all(f.attrs.get('venue')=='GETTEX' for f in seen['frames'].values()))
  self.assertEqual(self.info['levels'],{'stop':800.,'target':950.})
 def test_cp_assessment_uses_selected_identity_not_old_proxy_pin(self):
  seen=[]
  def inspect(state,frame,tf,now):
   seen.append((tf,state.get('expected_identity'),frame.attrs.get('source_selection')))
   return dict(identity={k:frame.attrs.get(k) for k in dq.IDENTITY},blockers=['TEST_MISSING_WARMUP'])
  ctx=NS(user_state=lambda i:dict(self.info),source_mode=lambda i:'proxy',frame_for=lambda i,tf:self.read(tf))
  with patch.object(cw,'bounded_features',side_effect=inspect):cw.assess_all(ctx,self.info,self.now)
  intraday=[r for r in seen if r[2]];self.assertEqual(len(intraday),8)
  self.assertTrue(all(r[1]['source']=='gettex' and r[1]['symbol']==self.isin for r in intraday))
 def test_cp_evening_feed_saves_new_bars_resumes_without_duplicate_request(self):
  now=pd.Timestamp('2026-10-28T21:45Z');info=dict(self.info,_gettex_poll_only=True)
  trades=pd.DataFrame(dict(price=[105.,106.,107.],volume=[2.,3.,4.]),index=pd.to_datetime(['2026-10-28 22:15','2026-10-28 22:20','2026-10-28 22:25']))
  obj=NS(store=self.s,closing=False,feed_slots=nullcontext(),events=queue.Queue(),feed_done=lambda *x:None,_http_bytes=lambda x:None,_parse_gettex_csv=lambda *a:None)
  obj._download_gettex_trades=lambda isin:Desktop014._download_gettex_trades(obj,isin)
  real_clock=cs.clock
  with patch.object(cs,'clock',side_effect=lambda n=None:real_clock(now if n is None else n)),patch('gettex_history.download',return_value=trades) as fetch:
   Desktop014._run_feed(obj,info)
   self.assertEqual(fetch.call_count,1)
   self.assertFalse(cs.decision(self.s,self.isin,self.g,'5m',now)['due'])
   self.s.read_cache.close();self.s=Store(self.tmp.name);obj.store=self.s
   Desktop014._run_feed(obj,info);self.assertEqual(fetch.call_count,1)
  self.ctx.store=self.s;self.ctx.analysis_at=now
  for tf in ('5m','15m'):
   out=self.read(tf);self.assertEqual(str(out.index[-1].date()),'2026-10-28');self.assertEqual(out.attrs['source'],'gettex')
  saved=self.s.load_official(self.isin,'15m');self.assertEqual(saved.close.iloc[-1],107.);self.assertEqual(saved.volume.iloc[-1],9.)

if __name__=='__main__':unittest.main()
