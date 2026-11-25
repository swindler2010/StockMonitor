"""Apple normal-archive fallback, exact partial reviews and closed visual levels."""
import unittest,copy
from unittest.mock import patch
import numpy as np
import pandas as pd
import test_partial_accepted_bands as fx
import partial_chart as pc
import chart_indicators as ci
from test_partial_accepted_indicators import reference

class DisplayLevels(unittest.TestCase):
 setUp=fx.PartialAcceptedBands.setUp
 tearDown=fx.PartialAcceptedBands.tearDown
 tv=fx.PartialAcceptedBands.tv
 partial=fx.PartialAcceptedBands.partial
 project=fx.PartialAcceptedBands.project
 def normal_projection(self,tf='4H'):
  raw=self.raw.drop(pd.Timestamp('2026-10-29 10:00'));raw.attrs['data_quality']=fx.dq.audit(raw,'15m',now=self.now)
  # Exercise select_display's non-INVALID branch, never project_saved.
  with patch.object(pc,'load',return_value=None),patch.object(pc,'live_tail',side_effect=lambda i,t,f,s,n:f),patch('gettex_chart._load_native',side_effect=lambda s,i,b:raw if b=='15m' else pd.DataFrame()),patch.object(self.s,'load_official',return_value=pd.DataFrame()),patch.object(pc,'project_saved',side_effect=AssertionError('wrong branch')):
   return pc.select_display(self.i,tf,raw.iloc[:0],self.s,self.now)
 def test_normal_partial_retains_reviewed_boundary_when_new_gap_exists(self):
  fx.fixture.ReviewedRisk.authorize(self);fx.fixture.ReviewedRisk.turn(self)
  out=self.normal_projection();p=out.attrs['partial_chart']
  self.assertIn('2026-10-28 15:30:00',p['accepted_break_before'])
  self.assertNotIn('2026-10-28 15:30:00',p['indicator_break_before'])
  self.assertIn('2026-10-29 11:30:00',p['indicator_break_before'])
  self.assertNotIn('accepted_risk',out.attrs)
 def test_normal_projection_revocation_restores_boundary(self):
  fx.fixture.ReviewedRisk.authorize(self);fx.fixture.ReviewedRisk.turn(self)
  out=self.normal_projection();self.assertTrue(out.attrs['partial_chart']['accepted_break_before'])
  policy=self.s.setting(fx.cp.REVIEWED_PREFIX+self.i['isin']);policy['approved']=False;self.s.set_setting(fx.cp.REVIEWED_PREFIX+self.i['isin'],policy)
  out=self.normal_projection();self.assertFalse(out.attrs['partial_chart']['accepted_break_before'])
  self.assertIn('2026-10-28 15:30:00',out.attrs['partial_chart']['indicator_break_before'])
 def test_levels_real_closed_extrema_without_database_write_or_engine_permission(self):
  self.tv();f=self.project();before=list(self.s.db.iterdump());en=ci.partial_indicators(f)
  result,zones=pc.display_levels(en,self.now)
  self.assertTrue(result['display_levels_available']);self.assertEqual(result['support'],en.low.iloc[:-1].tail(20).min())
  self.assertEqual(result['resistance'],en.high.iloc[:-1].tail(20).max());self.assertFalse(result['display_levels_historical'])
  bound=pc.bind({'trading_engine':{'status':'READY','signal':'BUY'}},f)
  self.assertFalse(bound['analysis'][1]['ohlcv_valid']);self.assertEqual(bound['trading_engine']['status'],'DATA BLOCKED')
  self.assertEqual(bound['trading_engine']['signal'],'WAIT');self.assertIsNone(bound['analysis'][1]['stop'])
  self.assertEqual(before,list(self.s.db.iterdump()))
 def test_live_price_does_not_move_closed_levels(self):
  self.tv();f=self.project();stamp=str(f.index[-1]);f['bar_complete']=True;f.iloc[-1,f.columns.get_loc('bar_complete')]=False
  f.attrs['live_candle']=dict(timestamp=stamp,display_only=True,analysis_mode='PRELIMINARY_ONLY',identity=self.m)
  a=pc.display_levels(ci.partial_indicators(f),self.now)[0];g=f.copy();g.loc[stamp,['high','close']]=[150.,149.]
  b=pc.display_levels(ci.partial_indicators(g),self.now)[0];self.assertEqual(a,b)
 def test_fresh_unknown_gap_keeps_older_levels_dated_and_new_indicators_unwarmed(self):
  self.tv();f=self.project();edge=str(f.index[-2]);f.attrs['partial_chart']['indicator_break_before']=[edge]
  en=ci.partial_indicators(f);result,zones=pc.display_levels(en,self.now)
  self.assertTrue(result['display_levels_historical']);self.assertLessEqual(pd.Timestamp(result['display_levels_at']).tz_localize(None),pd.Timestamp(edge))
  self.assertTrue(en.ema10.tail(2).isna().all())
 def test_insufficient_or_invalid_identity_cannot_produce_levels(self):
  self.tv();f=self.project().tail(10);self.assertFalse(pc.display_levels(ci.partial_indicators(f),self.now)[0]['display_levels_available'])
  f.attrs['currency']='USD';self.assertFalse(pc.display_levels(ci.partial_indicators(f),self.now)[0].get('display_levels_available',False))
 def test_all_twenty_indicator_columns_match_independent_reference_after_review(self):
  fx.fixture.ReviewedRisk.authorize(self);fx.fixture.ReviewedRisk.turn(self)
  f=self.project();n=len(f);wave=np.sin(np.arange(n)/5);f['close']=100+wave;f['open']=100+wave*.8;f['high']=102+wave;f['low']=98+wave;f['volume']=100+np.arange(n)%23
  en=ci.partial_indicators(f);expected=reference(f,'15m',dict(ema_fast=10,ema_slow=30,sma=20,bb=20,bb_dev=2,rsi=14,atr=14,volume=20,macd_fast=12,macd_slow=26,macd_signal=9))
  for name,values in expected.items():
   with self.subTest(column=name):np.testing.assert_allclose(en[name],values,rtol=1e-12,atol=1e-11,equal_nan=True)

if __name__=='__main__':unittest.main()
