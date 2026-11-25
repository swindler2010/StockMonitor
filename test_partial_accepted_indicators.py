"""Indicators on changed accepted partial projections, no market/database writes."""
import unittest,copy
from unittest.mock import patch
import numpy as np
import pandas as pd
import test_partial_accepted_bands as fx
import partial_chart as pc
import chart_indicators as ci
from analytics import indicators

def reference(part,tf,s):
 """Independent recurrence/rolling oracle on actual rows, no pandas EWM."""
 c=part.close.to_numpy();h=part.high.to_numpy();l=part.low.to_numpy();v=part.volume.to_numpy();n=len(c)
 def ewm(values,alpha,minimum):
  out=np.full(len(values),np.nan);state=None;count=0
  for i,value in enumerate(values):
   if not np.isfinite(value):continue
   state=value if state is None else (1-alpha)*state+alpha*value;count+=1
   if count>=minimum:out[i]=state
  return out
 def ema(p):return ewm(c,2/(p+1),p)
 def rolling(values,p,std=False):
  a=np.full(len(values),np.nan)
  for i in range(p-1,len(values)):
   w=values[i+1-p:i+1];a[i]=np.std(w,ddof=1) if std else np.mean(w)
  return a
 def rsi(p):
  delta=np.r_[np.nan,np.diff(c)];up=ewm(np.maximum(delta,0),1/p,p);down=ewm(np.maximum(-delta,0),1/p,p)
  with np.errstate(divide='ignore',invalid='ignore'):out=100-100/(1+up/down)
  out[(down==0)&(up>0)]=100;out[(down==0)&(up==0)]=50;return out
 tr=np.maximum.reduce([h-l,np.r_[h[0]-l[0],np.abs(h[1:]-c[:-1])],np.r_[h[0]-l[0],np.abs(l[1:]-c[:-1])]])
 macd=ema(12)-ema(26);m=ema(s['macd_fast'])-ema(s['macd_slow']);signal=ewm(m,2/(s['macd_signal']+1),s['macd_signal'])
 vm=rolling(v,s['volume']);mid=rolling(c,s['bb']);std=rolling(c,s['bb'],True)
 vw=np.full(n,np.nan);pv=0.;volume=0.;previous=None
 for i,date in enumerate(part.index.normalize()):
  if tf not in ('1D','1W') and date!=previous:pv=0.;volume=0.
  previous=date;pv+=(h[i]+l[i]+c[i])/3*v[i];volume+=v[i]
  if volume:vw[i]=pv/volume
 with np.errstate(divide='ignore',invalid='ignore'):relative=v/vm
 relative[vm==0]=np.nan
 return dict(ema10=ema(10),ema30=ema(30),rsi=rsi(14),atr=ewm(tr,1/14,14),
  macd=macd,macd_signal=ewm(macd,2/10,9),volume_mean20=rolling(np.r_[np.nan,v[:-1]],20),
  chart_ema_fast=ema(s['ema_fast']),chart_ema_slow=ema(s['ema_slow']),chart_sma=rolling(c,s['sma']),
  chart_bb_high=mid+s['bb_dev']*std,chart_bb_low=mid-s['bb_dev']*std,chart_vwap=vw,
  chart_rsi=rsi(s['rsi']),chart_atr=ewm(tr,1/s['atr'],s['atr']),chart_volume_mean=vm,
  chart_relative_volume=relative,chart_macd=m,chart_macd_signal=signal,chart_macd_hist=m-signal)

class AcceptedIndicators(unittest.TestCase):
 setUp=fx.PartialAcceptedBands.setUp
 tearDown=fx.PartialAcceptedBands.tearDown
 tv=fx.PartialAcceptedBands.tv
 project=fx.PartialAcceptedBands.project
 partial=fx.PartialAcceptedBands.partial
 def test_tv_absence_keeps_real_bar_warmup_without_counting_missing_slot(self):
  self.tv();f=self.project();out=ci.partial_indicators(f);oracle=indicators(pd.DataFrame(f,copy=True))
  self.assertTrue(f.attrs['partial_chart']['break_before']);self.assertFalse(f.attrs['partial_chart']['indicator_break_before'])
  self.assertEqual(out.attrs['partial_indicators']['segments'],1);self.assertNotIn(pd.Timestamp(self.a),out.index)
  for c in ci.FIXED_COLUMNS:np.testing.assert_allclose(out[c],oracle[c],equal_nan=True,rtol=1e-12)
 def test_unknown_resets_but_current_reviewed_real_bar_policy_keeps_visual_warmup(self):
  self.tv();raw=self.raw.drop(pd.Timestamp('2026-10-28 13:45'));out=ci.partial_indicators(self.project(raw=raw))
  self.assertTrue(out.loc['2026-10-28 14:00':'2026-10-28 16:00'].ema10.isna().all())
  self.s.set_setting(fx.cp.TV_POLICY,dict(enabled=False))
  fx.fixture.ReviewedRisk.authorize(self);fx.fixture.ReviewedRisk.turn(self)
  out=ci.partial_indicators(self.project());self.assertEqual(out.attrs['partial_indicators']['segments'],1)
 def test_genuine_gap_before_new_live_tail_resets_indicators(self):
  self.tv();chosen=self.project();now=pd.Timestamp('2026-10-30T12:05Z')
  child=self.raw.tail(1).copy();child.index=pd.DatetimeIndex(['2026-10-30 13:00']);child.attrs['timeframe']='15m'
  with patch('gettex_chart.display_native',side_effect=lambda s,i,b,n:child if b=='15m' else pd.DataFrame()):
   out=pc.live_tail(self.i,'15m',chosen,self.s,now)
  self.assertEqual(out.attrs['live_candle']['timestamp'],'2026-10-30 13:00:00')
  self.assertIn('2026-10-30 13:00:00',out.attrs['partial_chart']['indicator_break_before'])
  enriched=ci.partial_indicators(out);self.assertTrue(enriched.ema10.tail(1).isna().all())
  self.assertFalse(out.bar_complete.iloc[-1]);self.assertFalse(out.attrs['live_candle']['execution_allowed'])
 def test_live_values_update_without_changing_closed_indicators_or_dq(self):
  self.tv();f=self.project();stamp=str(f.index[-1]);f['bar_complete']=True;f.iloc[-1,f.columns.get_loc('bar_complete')]=False
  f.attrs['live_candle']=dict(timestamp=stamp,display_only=True,analysis_mode='PRELIMINARY_ONLY',identity=self.m)
  before=ci.partial_indicators(f);changed=f.copy();changed.loc[stamp,['close','high']]=[103.,104.]
  after=ci.partial_indicators(changed)
  pd.testing.assert_frame_equal(before.iloc[:-1],after.iloc[:-1])
  for c in ('ema10','ema30','macd','rsi','atr'):self.assertNotEqual(before[c].iloc[-1],after[c].iloc[-1])
  self.assertFalse(after.bar_complete.iloc[-1]);self.assertEqual(after.attrs['data_quality']['state'],'INVALID')

if __name__=='__main__':unittest.main()
