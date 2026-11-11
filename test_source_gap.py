import sys,unittest,tempfile
from pathlib import Path
from unittest.mock import Mock
import pandas as pd
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
import data_quality as q
from analytics import resample
from storage import Store
import test_timeframes
class SourceGapTests(unittest.TestCase):
 def test_pre1970_exchange_holidays_are_not_missing_bars(self):
  meta={'venue':'NYQ','timezone':'America/New_York'}
  idx=q.expected_index(meta,'1D','1962-12-24','1962-12-26',pd.Timestamp('2026-10-09',tz='UTC'))
  self.assertNotIn(pd.Timestamp('1962-12-25'),idx);self.assertIn(pd.Timestamp('1962-12-26'),idx)
 def test_complete_aggregates_only_and_warning_survives(self):
  f=test_timeframes.Tests().frame('2025-11-26','2025-11-28 16:00',base='1H')
  f=f.drop(pd.Timestamp('2025-11-28 12:30'));f.attrs['data_quality']={'state':'SOURCE_GAP','issues':['Missing source bar'],'ranges':[['2025-11-28 12:30','2025-11-28 12:30']]}
  for tf in ('2H','3H','4H'):
   out=resample(f,'1H',tf,now=pd.Timestamp('2026-10-04',tz='UTC'))
   self.assertEqual(out.attrs['data_quality']['state'],'SOURCE_GAP');self.assertTrue(out.bar_complete.all())
   self.assertNotIn(pd.Timestamp('2025-11-28 '+('11:30' if tf=='2H' else '12:30' if tf=='3H' else '09:30')),out.index)
   self.assertTrue(out.attrs['data_quality']['source_ranges'])
 def test_pending_gap_excludes_partial_buckets_without_claiming_complete(self):
  now=pd.Timestamp('2025-11-29',tz='UTC')
  for base,targets in [('5m',('10m',)),('15m',('30m',)),('1H',('2H','3H','4H'))]:
   f=test_timeframes.Tests().frame('2025-11-26','2025-11-28 16:00',base=base)
   missing=f.index[3];f=f.drop(missing);f.attrs['data_quality']=q.audit(f,base,now=now)
   self.assertEqual(f.attrs['data_quality']['state'],'GAP')
   for tf in targets:
    with self.subTest(base=base,tf=tf):
     out=resample(f,base,tf,now=now)
     self.assertEqual(out.attrs['data_quality']['state'],'GAP')
     self.assertFalse(out.empty);self.assertTrue(out.bar_complete.all())
     self.assertTrue(out.attrs['data_quality']['excluded_incomplete'])
     self.assertTrue(out.attrs['data_quality']['source_ranges'])
 def test_pending_gap_does_not_mask_impossible_ohlc(self):
  f=test_timeframes.Tests().frame('2025-11-26',base='5m').drop(pd.Timestamp('2025-11-26 09:45'))
  f.attrs['data_quality']={'state':'GAP'};f.iloc[0,f.columns.get_loc('high')]=0
  out=resample(f,'5m','10m',now=pd.Timestamp('2025-11-27',tz='UTC'))
  self.assertTrue(out.empty);self.assertEqual(out.attrs['data_quality']['state'],'INVALID')
 def test_source_gap_never_masks_bad_prices(self):
  f=test_timeframes.Tests().frame('2026-10-02',base='1H');f.iloc[0,f.columns.get_loc('high')]=0
  f.attrs['data_quality']={'state':'SOURCE_GAP'}
  out=resample(f,'1H','2H');self.assertTrue(out.empty);self.assertEqual(out.attrs['data_quality']['state'],'INVALID')
 def test_unavailable_or_recovering_keeps_gap_exclusion_and_blocker(self):
  now=pd.Timestamp('2025-11-29',tz='UTC')
  for state in ('SOURCE_UNAVAILABLE','RECOVERING'):
   f=test_timeframes.Tests().frame('2025-11-26','2025-11-28 16:00',base='15m')
   f=f.drop(f.index[3]);f.attrs['data_quality']={'state':state,'issues':['upstream unavailable']}
   for tf in ('30m','1H','2H','3H','4H'):
    with self.subTest(state=state,tf=tf):
     out=resample(f,'15m',tf,now=now);report=out.attrs['data_quality']
     self.assertFalse(out.empty);self.assertTrue(out.bar_complete.all())
     self.assertEqual(report['state'],state);self.assertEqual(report['observed_state'],'GAP')
     self.assertTrue(report['excluded_incomplete']);self.assertTrue(report['source_ranges'])
     self.assertIn('upstream unavailable',report['issues'])
 def test_availability_never_masks_real_invalid_prices_or_session(self):
  for state in ('SOURCE_UNAVAILABLE','RECOVERING'):
   for defect in ('ohlc','session','identity'):
    with self.subTest(state=state,defect=defect):
     f=test_timeframes.Tests().frame('2025-11-26',base='15m')
     f.attrs['data_quality']={'state':state}
     if defect=='ohlc':f.iloc[0,f.columns.get_loc('high')]=0
     elif defect=='session':f.index=f.index+pd.Timedelta(minutes=1)
     else:f['currency']='EUR'
     out=resample(f,'15m','30m',now=pd.Timestamp('2025-11-27',tz='UTC'))
     self.assertTrue(out.empty);self.assertEqual(out.attrs['data_quality']['state'],'INVALID')
 def test_upstream_invalid_stays_blocked_for_clean_local_rows(self):
  f=test_timeframes.Tests().frame('2025-11-26',base='15m')
  f.attrs['data_quality']={'state':'INVALID','issues':['rejected incoming history']}
  out=resample(f,'15m','30m',now=pd.Timestamp('2025-11-27',tz='UTC'))
  self.assertTrue(out.empty);self.assertEqual(out.attrs['data_quality']['state'],'INVALID')
 def test_failed_empty_repair_preserves_rows_and_reports_source_gap(self):
  f=test_timeframes.Tests().frame('2025-11-28',base='1H').drop(pd.Timestamp('2025-11-28 12:30'))
  f.attrs['intraday_span']=['2025-11-28 09:30','2025-11-28 12:30']
  with tempfile.TemporaryDirectory() as folder:
   store=Store(folder)
   try:
    rows=[('TEST','proxy','NMS','USD','TEST','1H',str(ts),*[float(r[k]) for k in q.FIELDS]) for ts,r in f.iterrows()]
    with store.connect() as db:db.executemany('INSERT INTO series VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',rows)
    report=q.repair({'isin':'TEST'},store,'TEST','1H',f.attrs,Mock(return_value=pd.DataFrame()),now=pd.Timestamp('2026-10-04',tz='UTC'))
    self.assertEqual(report['state'],'SOURCE_GAP');self.assertEqual(len(store.load_series('TEST','1H','proxy','TEST')),len(rows))
   finally:store.read_cache.close()
if __name__=='__main__':unittest.main()
