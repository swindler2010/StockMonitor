import unittest,sys,copy
from pathlib import Path
from unittest.mock import patch,Mock
import pandas as pd
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
from analytics import resample
from timeframes import schedule
from partial_chart import cached_periods,bind
class ChartRecovery(unittest.TestCase):
 def daily(self):
  meta=dict(source='proxy',symbol='TEST.DE',venue='GER',currency='EUR',timezone='Europe/Berlin',timeframe='1D')
  idx=pd.DatetimeIndex(schedule(meta,'2026-01-01','2026-03-31').index)
  f=pd.DataFrame(dict(open=10.,high=11.,low=9.,close=10.,volume=100.,source='proxy'),index=idx);f.attrs={**meta,'data_quality':{'state':'READY'}};return f
 def test_boundary_keeps_complete_weeks_without_weakening_dq(self):
  f=resample(self.daily(),'1D','1W',pd.Timestamp('2026-04-01T00:00Z'));before=f.copy(deep=True)
  p=cached_periods(f,'1W',pd.Timestamp('2026-04-01T00:00Z'))
  self.assertGreater(len(p),5);self.assertLess(len(p),len(f));self.assertTrue(p.bar_complete.all());self.assertEqual(p.attrs['data_quality']['state'],'INVALID');pd.testing.assert_frame_equal(f,before)
 def test_missing_daily_excludes_entire_month_and_breaks_line(self):
  d=self.daily().drop(pd.Timestamp('2026-02-10'));d.attrs['data_quality']={'state':'GAP'}
  f=resample(d,'1D','1M',pd.Timestamp('2026-04-01T00:00Z'));p=cached_periods(f,'1M',pd.Timestamp('2026-04-01T00:00Z'))
  self.assertEqual(list(p.index.month),[1,3]);self.assertEqual(p.attrs['partial_chart']['break_before'],['2026-03-01 00:00:00'])
  pd.testing.assert_frame_equal(p, f.loc[p.index]);self.assertEqual(bind({},p)['trading_engine']['status'],'DATA BLOCKED')
 def test_invalid_raw_or_unknown_proof_never_display(self):
  d=self.daily();d.iloc[8,d.columns.get_loc('high')]=1
  f=resample(d,'1D','1M',pd.Timestamp('2026-04-01T00:00Z'));self.assertTrue(cached_periods(f,'1M').empty)
  f.attrs.pop('daily_provenance');self.assertTrue(cached_periods(f,'1M').empty)
 def test_forming_period_not_promoted(self):
  f=resample(self.daily(),'1D','1M',pd.Timestamp('2026-03-16T00:00Z'));p=cached_periods(f,'1M',pd.Timestamp('2026-03-16T00:00Z'));self.assertNotIn(3,p.index.month)
 def test_complete_source_selected_despite_derived_boundary(self):
  from eur_listings import cached_daily_history
  d=self.daily();listing={**{k:d.attrs[k] for k in ('source','symbol','venue','currency','timezone')},'isin':'TEST','status':'CONFIRMED'}
  store=Mock();store.load_series.return_value=d;store.setting.return_value={}
  result=cached_daily_history(store,{'isin':'TEST','eur_listings':[listing]},'1W',listing)
  self.assertIsNotNone(result);self.assertEqual(len(result),len(d))
  d.iloc[8,d.columns.get_loc('high')]=1;self.assertIsNone(cached_daily_history(store,{'isin':'TEST','eur_listings':[listing]},'1W',listing))
 def test_partial_prepare_does_not_call_engine(self):
  import chart_worker
  frame=resample(self.daily(),'1D','1W',pd.Timestamp('2026-04-01T00:00Z'))
  context=Mock();context.instrument_cfg.return_value={'isin':'TEST'};context.user_state.return_value={};context.frame_for.return_value=frame;context.trading_analysis.side_effect=AssertionError('Engine must not run')
  with patch.object(chart_worker,'SnapshotContext',return_value=context):out=chart_worker.prepare(Mock(),{'timeframe':'1W'})
  self.assertGreater(len(out['frame']),0);context.trading_analysis.assert_not_called();self.assertEqual(out['trading_engine']['signal'],'WAIT')
 def test_gettex_week_requires_whole_session_coverage(self):
  meta=dict(source='gettex',symbol='TEST',venue='GETTEX',currency='EUR',timezone='Europe/Berlin',timeframe='15m')
  sched=schedule(meta,'2026-10-12','2026-10-16');idx=pd.DatetimeIndex([])
  for _,row in sched.iterrows():idx=idx.append(pd.date_range(row.open,row.close,freq='15min',inclusive='left').tz_convert('Europe/Berlin').tz_localize(None))
  d=pd.DataFrame(dict(open=10.,high=11.,low=9.,close=10.,volume=100.,source='gettex'),index=idx);d.attrs={**meta,'data_quality':{'state':'READY'}}
  now=pd.Timestamp('2026-10-17T23:00Z');f=resample(d,'15m','1W',now)
  self.assertTrue(f.bar_complete.all());self.assertEqual(f.attrs['aggregation_base'],'15m')
  day=resample(d,'15m','1D',now);self.assertTrue(day.bar_complete.all());self.assertEqual(day.attrs['aggregation_base'],'15m')
  broken_day=resample(d.drop(idx[10]),'15m','1D',now);shown=cached_periods(broken_day,'1D',now);self.assertEqual(len(shown),4);self.assertNotIn(idx[0].normalize(),shown.index)
  broken=resample(d.drop(idx[10]),'15m','1W',now)
  self.assertFalse(broken.bar_complete.any());self.assertTrue(cached_periods(broken,'1W',now).empty)
 def test_unknown_currency_not_promoted(self):
  d=self.daily();d.attrs['currency']='';f=resample(d,'1D','1M',pd.Timestamp('2026-04-01T00:00Z'))
  self.assertTrue(cached_periods(f,'1M').empty)
if __name__=='__main__':unittest.main()
