"""New single Proxy UI policy: legacy preferences, deep EUR daily, strict pins."""
import unittest,copy
from types import SimpleNamespace as NS
from unittest.mock import patch
import test_proxy_session28 as fixture
from desktop014 import Desktop014
from snapshot_worker import SnapshotContext,Value
import pandas as pd
import data_quality as dq

class AutomaticProxy(unittest.TestCase):
 def setUp(self):
  self.f=fixture.ProxySession('runTest');self.f.setUp()
  self.s,self.info,self.ctx=self.f.s,self.f.info,self.f.ctx
  self.ctx.source_mode=lambda i:i.get('source_mode','gettex')
 def tearDown(self):self.f.tearDown()
 def read(self,tf,**kw):return Desktop014._frame_uncached(self.ctx,self.info,tf,**kw)
 def test_legacy_gettex_and_default_now_use_same_automatic_policy(self):
  for mode in ('gettex','proxy',None):
   if mode:self.info['source_mode']=mode
   else:self.info.pop('source_mode',None)
   for tf in ('5m','15m','4H'):
    with self.subTest(mode=mode,tf=tf):
     out=self.read(tf)
     self.assertEqual(out.attrs['source_selection']['requested_mode'],'proxy')
     self.assertEqual(out.attrs['source'],'gettex');self.assertTrue(out.close.eq(101).all())
 def test_legacy_gettex_falls_back_honestly_to_separate_eur_proxy(self):
  self.info['source_mode']='gettex'
  with patch.object(self.s,'load_official',return_value=pd.DataFrame()):out=self.read('5m')
  self.assertEqual(out.attrs['venue'],'GER');self.assertEqual(out.attrs['currency'],'EUR')
  self.assertEqual(out.attrs['source_selection']['selected'],'PROXY_EUR_LIMITED')
  self.assertEqual(str(out.index[-1].time()),'17:25:00');self.assertTrue(out.close.eq(901).all())
 def deep_daily(self):
  idx=dq.expected_index(self.f.p,'1D','2024-01-01','2026-10-27',self.f.now)
  frame=pd.DataFrame(dict(open=900.,high=902.,low=899.,close=901.,volume=10.),index=idx);frame.attrs=dict(self.f.p)
  self.s.save_series(self.info,frame,'1D');return frame
 def test_deep_daily_week_month_separate_from_evening_gettex(self):
  self.info.update(source_mode='gettex',eur_history_auto=False);self.deep_daily()
  for tf in ('1D','1W','1M'):
   with self.subTest(tf=tf):
    out=self.read(tf);self.assertGreater(len(out),0)
    self.assertEqual(out.attrs['source_selection']['selected'],'GETTEX_EUR')
    self.assertEqual(out.attrs['venue'],'GETTEX');self.assertTrue(out.close.eq(101).all())
    self.assertGreater(len(self.s.load_series(self.info['isin'],'1D','proxy')),10)
  self.assertEqual(self.read('15m').attrs['venue'],'GETTEX')
 def test_no_eur_daily_no_usd_price_or_conversion(self):
  self.info['eur_listings']=[]
  usd=self.f.make(dict(self.f.p,currency='USD'),'1D',900)
  self.s.set_setting('history_identity:'+self.info['isin'],dict(self.f.p,currency='USD'))
  with patch.object(self.s,'load_official',return_value=pd.DataFrame()),patch.object(self.s,'load_series',return_value=usd):
   for tf in ('1D','1W','1M'):
    out=self.read(tf);self.assertTrue(out.empty);self.assertEqual(out.attrs['data_quality']['state'],'SOURCE_UNAVAILABLE')
 def test_daily_without_proxy_uses_real_gettex_only(self):
  with patch.object(self.s,'load_series',return_value=pd.DataFrame()):out=self.read('1D')
  self.assertFalse(out.empty);self.assertEqual(out.attrs['venue'],'GETTEX')
  self.assertTrue(out.close.eq(101).all());self.assertEqual(len(out),2) # No invented deep daily history.
 def test_selection_does_not_rewrite_preferences_levels_or_ohlcv(self):
  self.info.update(source_mode='gettex',levels={'stop':95},levels_source='gettex');before=copy.deepcopy(self.info)
  def rows():
   with self.s.connect() as db:return {t:sorted(db.execute('SELECT * FROM '+t).fetchall()) for t in ('instruments','candles','series')}
  saved=rows()
  for tf in ('5m','1D','1M'):self.read(tf)
  self.assertEqual(rows(),saved);self.assertEqual(self.info,before)
  ui=NS(user_state=lambda _:self.info)
  self.assertEqual(Desktop014.source_mode(ui,self.info),'gettex') # Old untagged levels keep their provenance.
 def test_explicit_calculation_pin_cannot_switch_to_auto(self):
  for meta in (self.f.g,self.f.p):
   out=self.read('1D',required_identity=meta)
   self.assertEqual(out.attrs['source'],meta['source']);self.assertEqual(out.attrs['venue'],meta['venue'])
   self.assertNotIn('source_selection',out.attrs)
  with self.assertRaises(ValueError):self.read('1D',required_identity=dict(self.f.p,currency='USD'))
 def test_core_auto_daily_identity_and_foreign_levels(self):
  self.deep_daily();self.info.update(source_mode='gettex',levels={'stop':90},levels_source='proxy',levels_symbol='SAP.DE',levels_venue='GER',levels_currency='EUR')
  ctx=SnapshotContext();ctx.store=self.s;ctx.watchlist=[self.info];ctx.drafts={};ctx.timeframe=Value('1D');ctx.period=Value('год');ctx.set_analysis_scope(self.f.now)
  seen={}
  def core(frames,tf,state,market,now):seen.update(state=copy.deepcopy(state));return {'status':'DATA BLOCKED'}
  with patch('analytics.analyse_multiframe',side_effect=core),patch('benchmarks.load',return_value=pd.DataFrame()),patch('benchmarks.relative_performance',return_value={'state':'UNAVAILABLE'}):ctx._trading_analysis(self.info)
  self.assertEqual(seen['state']['expected_identity'],dict(source='gettex',symbol=self.info['isin'],venue='GETTEX',currency='EUR'))
  self.assertEqual(seen['state']['levels'],{});self.assertEqual(self.info['levels'],{'stop':90})
 def test_automatic_daily_keeps_invalid_and_revision_gate(self):
  self.deep_daily();self.info['source_mode']='gettex'
  self.s.set_setting(dq.key(self.info,'SAP.DE','1D'),dict(state='INVALID',issues=['provider damage'],ranges=[]))
  with self.s.read_cache.track_inputs() as token:out=self.read('1D',required_identity=self.f.p)
  self.assertEqual(out.attrs['data_quality']['state'],'INVALID')
  frame=self.s.load_series(self.info['isin'],'1D','proxy','SAP.DE','GER','EUR').tail(1).copy();frame['close']=902.
  self.s.save_series(self.info,frame,'1D');self.assertFalse(self.s.read_cache.current(token))

if __name__=='__main__':unittest.main()
