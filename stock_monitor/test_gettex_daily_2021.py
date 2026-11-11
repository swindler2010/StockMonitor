import importlib.util
import json
from pathlib import Path
import unittest
import pandas as pd
import data_quality as q
import gettex_chart as g
from analytics import resample,bar_ends,_safe_features
from calculation_window import scope_raw,exclude_verified_leading_partial
from timeframes import schedule,endpoints
R=Path(__file__).resolve().parents[1];E=R/'history64-evidence/apple-periods30'
M=g.identity('US0378331005','AAPL.GTX');NOW=pd.Timestamp('2026-10-30T13:00:00Z')

class Daily2021Tests(unittest.TestCase):
 def test_official_holiday_changes_and_open_holidays(self):
  s=q.calendar('GETTEX',2021,2023,daily=True).schedule
  for t in ('2021-01-01','2021-04-02','2021-04-05','2021-05-24','2021-12-24','2021-12-31','2022-04-15','2022-04-18','2022-12-26','2023-04-07','2023-04-10','2023-05-01','2023-12-25','2023-12-26'):
   self.assertNotIn(pd.Timestamp(t),s.index,t)
  for t in ('2021-01-06','2021-11-01','2022-06-06','2022-10-03','2023-05-29','2023-11-01'):
   self.assertIn(pd.Timestamp(t),s.index,t)

 def test_earlier_daily_and_historical_intraday_stay_rejected(self):
  for tf,first,last in (('1D','2014-12-30','2015-01-04'),('15m','2023-01-02','2023-01-03')):
   with self.subTest(tf=tf),self.assertRaises(ValueError):schedule(dict(M,timeframe=tf),first,last)
  f=pd.DataFrame([[100,101,99,100,1]],index=pd.to_datetime(['2015-01-16']),columns=q.FIELDS);f.attrs=dict(M,timeframe='1D')
  self.assertEqual(q.audit(f,'1D',M,NOW)['state'],'INVALID')

 def test_existing_2024_2026_schedule_and_dst_unchanged(self):
  spec=importlib.util.spec_from_file_location('calendar_before',E/'before-code/data_quality.py');before=importlib.util.module_from_spec(spec);spec.loader.exec_module(before)
  for daily in (True,False):
   a=q.calendar('GETTEX',2024,2026,daily=daily).schedule.loc['2024':]
   b=before.calendar('GETTEX',2024,2026,daily=daily).schedule
   pd.testing.assert_frame_equal(a,b)
  s=q.calendar('GETTEX',2021,2023,daily=True).schedule
  self.assertEqual(s.loc['2021-10-29','close'],pd.Timestamp('2021-10-29T22:00:00Z'))
  self.assertEqual(s.loc['2021-11-01','close'],pd.Timestamp('2021-11-01T23:00:00Z'))

 def test_real_archive_aggregates_and_retains_forming_period(self):
  f=pd.read_pickle(E/'expanded-daily.pkl');self.assertEqual(q.audit(f,'1D',M,NOW)['state'],'READY')
  f=scope_raw(f,'1D',NOW)
  for tf,n in (('1W',260),('1M',59)):
   a=exclude_verified_leading_partial(resample(f,'1D',tf,NOW),tf,NOW)
   complete=(endpoints(a)<=NOW)&a.bar_complete
   self.assertEqual(int(complete.sum()),n)
   self.assertFalse(bool(a.bar_complete.iloc[-1]))
   self.assertEqual(a.attrs['data_quality']['state'],'READY')
   period=a.index[-2];start=period-pd.Timedelta(days=6) if tf=='1W' else period
   end=period if tf=='1W' else period+pd.offsets.MonthEnd(1)
   raw=f.loc[start:end]
   self.assertEqual(list(a.loc[period,list(q.FIELDS)]),[raw.open.iloc[0],raw.high.max(),raw.low.min(),raw.close.iloc[-1],raw.volume.sum()])

 def test_missing_day_corrupt_row_and_foreign_currency_stay_blocked(self):
  f=pd.read_pickle(E/'expanded-daily.pkl');f=f.loc['2023-01-01':'2023-02-28'].copy();f.attrs=dict(M,timeframe='1D',classify_absence=True)
  gap=f.drop(pd.Timestamp('2023-01-10'));a=resample(gap,'1D','1M',NOW)
  self.assertFalse(bool(a.loc[pd.Timestamp('2023-01-01'),'bar_complete']))
  broken=f.copy();broken.iloc[0,broken.columns.get_loc('high')]=0
  self.assertEqual(q.audit(broken,'1D',M,NOW)['state'],'INVALID')
  wrong=f.copy();wrong.attrs=dict(M,currency='USD',timeframe='1D')
  self.assertEqual(q.audit(wrong,'1D',M,NOW)['state'],'INVALID')

 def test_recursive_guard_not_bypassed_by_more_months(self):
  f=scope_raw(pd.read_pickle(E/'expanded-daily.pkl'),'1D',NOW)
  for tf in ('1W','1M'):
   a=exclude_verified_leading_partial(resample(f,'1D',tf,NOW),tf,NOW)
   _,result=_safe_features(a,{},NOW,prove_scope=True)
   self.assertFalse(result['sufficient_history']);self.assertEqual(result['reason'],'INSUFFICIENT_RECURSIVE_WARMUP_5Y')

 def test_forming_week_and_month_do_not_activate_invalid_chart(self):
  from partial_chart import cached_periods
  spec=importlib.util.spec_from_file_location('partial_before',E/'before-code/partial_chart.py');before=importlib.util.module_from_spec(spec);spec.loader.exec_module(before)
  f=pd.read_pickle(E/'expanded-daily.pkl')
  for tf in ('1W','1M'):
   a=resample(f,'1D',tf,NOW)
   self.assertEqual(a.attrs['data_quality']['state'],'READY')
   self.assertFalse(bool(a.bar_complete.iloc[-1]))
   self.assertIsNotNone(before.cached_periods(a,tf,NOW))
   self.assertIsNone(cached_periods(a,tf,NOW))

 def test_closed_incomplete_period_still_uses_safe_projection(self):
  from partial_chart import cached_periods
  f=pd.read_pickle(E/'expanded-daily.pkl').drop(pd.Timestamp('2023-01-10'))
  for tf in ('1W','1M'):
   a=resample(f,'1D',tf,NOW);projected=cached_periods(a,tf,NOW)
   self.assertIsNotNone(projected)
   self.assertTrue(projected.attrs['partial_chart']['display_only'])
   self.assertTrue(projected.bar_complete.all())
   self.assertTrue((endpoints(projected)<=NOW).all())
   self.assertEqual(projected.attrs['data_quality'],a.attrs['data_quality'])

 def test_invalid_upstream_is_not_promoted_by_forming_exception(self):
  from partial_chart import cached_periods
  a=resample(pd.read_pickle(E/'expanded-daily.pkl'),'1D','1W',NOW)
  a.attrs['daily_provenance']['raw_state']='INVALID'
  a.attrs['data_quality']['state']='INVALID'
  projected=cached_periods(a,'1W',NOW)
  self.assertIsNotNone(projected);self.assertTrue(projected.empty)
  self.assertEqual(projected.attrs['data_quality']['state'],'INVALID')

if __name__=='__main__':unittest.main()
