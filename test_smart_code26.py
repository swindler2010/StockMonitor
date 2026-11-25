import unittest,sys,copy
from pathlib import Path
from types import SimpleNamespace as NS
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
import pandas as pd
from analytics import resample
from timeframes import schedule
from snapshot_worker import inputs,Value
class NewCodeGuards(unittest.TestCase):
 def app(self,p='месяц',tf='15m'):
  return NS(watchlist=[],drafts={},feed_errors={},symbol=Value('A'),timeframe=Value(tf),period=Value(p),ai_config={},store=NS(read_cache=NS(token_for=lambda _:{})))
 def test_equivalent_period_labels_same_inputs(self):self.assertEqual(inputs(self.app('1 месяц')),inputs(self.app('месяц')))
 def test_real_period_and_tf_switch_remain_different(self):
  self.assertNotEqual(inputs(self.app('месяц')),inputs(self.app('год')))
  self.assertNotEqual(inputs(self.app(tf='5m')),inputs(self.app(tf='15m')))
 def daily(self):
  meta=dict(source='proxy',symbol='TEST.DE',venue='GER',currency='EUR',timezone='Europe/Berlin',timeframe='1D')
  idx=pd.DatetimeIndex(schedule(meta,'2026-01-01','2026-01-31').index)
  f=pd.DataFrame(dict(open=10.,high=11.,low=9.,close=10.,volume=100.,source='proxy'),index=idx);f.attrs={**meta,'data_quality':{'state':'READY'}};return f
 def test_complete_month_has_verified_provenance(self):
  f=self.daily();out=resample(f,'1D','1M',pd.Timestamp('2026-01-31T23:00Z'))
  self.assertEqual(out.attrs['aggregation_base'],'1D');self.assertTrue(out.bar_complete.all());self.assertEqual(out.attrs['data_quality']['state'],'READY',out.attrs)
  self.assertEqual(out.volume.sum(),f.volume.sum())
 def test_complete_week_and_partial_retention_not_conflated(self):
  f=self.daily();out=resample(f,'1D','1W',pd.Timestamp('2026-01-31T23:00Z'))
  self.assertFalse(bool(out.bar_complete.iloc[0]));self.assertTrue(out.bar_complete.iloc[1:].all())
  self.assertEqual(out.attrs['data_quality']['state'],'INVALID')
 def test_missing_daily_bar_cannot_gain_ready_month(self):
  f=self.daily().drop(self.daily().index[8]);out=resample(f,'1D','1M',pd.Timestamp('2026-01-31T23:00Z'))
  self.assertFalse(out.bar_complete.all());self.assertNotEqual(out.attrs['data_quality']['state'],'READY')
 def test_damaged_base_not_hidden_by_valid_month_ohlc(self):
  f=self.daily();f.iloc[8,f.columns.get_loc('high')]=1
  out=resample(f,'1D','1M',pd.Timestamp('2026-01-31T23:00Z'));self.assertEqual(out.attrs['data_quality']['state'],'INVALID')
 def test_unconfirmed_source_and_identity_stay_blocked(self):
  f=self.daily();f.attrs['data_quality']={'state':'UNCONFIRMED','issues':['missing proof']}
  out=resample(f,'1D','1M',pd.Timestamp('2026-01-31T23:00Z'));self.assertEqual(out.attrs['data_quality']['state'],'UNCONFIRMED')
  f.attrs['timezone']='America/New_York'
  out=resample(f,'1D','1M',pd.Timestamp('2026-01-31T23:00Z'));self.assertEqual(out.attrs['data_quality']['state'],'INVALID')
 def test_verified_weekly_can_reach_analysis_without_bypassing_warmup(self):
  from calculation_window import bounded_features
  meta=self.daily().attrs.copy();idx=pd.DatetimeIndex(schedule(meta,'2025-05-05','2026-02-13').index)
  f=pd.DataFrame(dict(open=10.,high=11.,low=9.,close=10.,volume=100.,source='proxy'),index=idx);f.attrs=meta
  now=pd.Timestamp('2026-02-14T12:00Z');out=resample(f,'1D','1W',now)
  info={'isin':'TEST','eur_listings':[{**{k:meta[k] for k in ('source','symbol','venue','currency','timezone')},'isin':'TEST','status':'CONFIRMED'}]}
  r=bounded_features(info,out,'1W',now);self.assertTrue(r['analysis_ready'],r)
  short=out.tail(20).copy();r=bounded_features(info,short,'1W',now)
  self.assertFalse(r['analysis_ready']);self.assertIn('INDICATOR_WARMUP_OR_OHLCV',r['blockers'])
 def test_forming_month_is_not_closed_and_future_month_invalid(self):
  import data_quality as dq
  from analytics import bar_ends
  f=self.daily();now=pd.Timestamp('2026-01-15T12:00Z');f=f.loc[:'2026-01-14'].copy()
  out=resample(f,'1D','1M',now);self.assertGreater(bar_ends(out)[0],now)
  future=out.copy();future.index=pd.DatetimeIndex(['2026-02-01']);future.attrs.pop('bar_ends',None)
  self.assertEqual(dq.audit(future,'1M',now=now)['state'],'INVALID')
 def test_month_missing_whole_bucket_is_gap_not_ready(self):
  import data_quality as dq
  f=self.daily();idx=pd.DatetimeIndex(schedule(f.attrs,'2026-01-01','2026-03-31').index)
  f=pd.DataFrame(dict(open=10.,high=11.,low=9.,close=10.,volume=100.,source='proxy'),index=idx);f.attrs=self.daily().attrs.copy()
  out=resample(f,'1D','1M',pd.Timestamp('2026-04-01T06:00Z')).drop(pd.Timestamp('2026-02-01'))
  r=dq.audit(out,'1M',now=pd.Timestamp('2026-04-01T06:00Z'));self.assertEqual(r['state'],'GAP',r)
if __name__=='__main__':unittest.main()
