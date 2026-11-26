import json,pathlib,unittest
import pandas as pd
import data_quality as dq
import gettex_chart as chart
from analytics import resample
from timeframes import endpoints
from test_gettex_chart import ISIN,META

NOW=pd.Timestamp('2026-11-02T10:00Z')
class DailyCalendar(unittest.TestCase):
 def test_official_gettex_holidays_are_year_specific(self):
  cal=dq.calendar('GETTEX',2015,2026,daily=True).schedule.index
  opened=['2015-05-25','2016-05-16','2016-10-03','2017-05-25','2018-05-10','2019-05-30','2020-05-21','2020-06-11']
  closed=['2015-01-16','2015-04-03','2015-12-24','2016-03-25','2016-12-26','2017-06-05','2017-10-03','2017-10-31','2018-05-21','2018-10-03','2019-06-10','2019-10-03','2020-06-01','2020-12-24']
  self.assertEqual(cal[0],pd.Timestamp('2015-01-19'))
  for t in opened:self.assertIn(pd.Timestamp(t),cal,t)
  for t in closed:self.assertNotIn(pd.Timestamp(t),cal,t)
 def test_daily_extension_does_not_invent_old_intraday_hours(self):
  with self.assertRaises(ValueError):dq.expected_index(META,'15m','2020-07-01','2020-07-02',NOW)
  rows=dq.expected_index(META,'1D','2020-03-27','2020-03-30',NOW)
  f=pd.DataFrame(index=rows);f.attrs=dict(META,timeframe='1D')
  self.assertEqual(list(endpoints(f)),[pd.Timestamp('2020-03-27T23:00Z'),pd.Timestamp('2020-03-30T22:00Z')])
 def test_missing_real_day_and_prelaunch_bar_still_fail_quality(self):
  idx=dq.expected_index(META,'1D','2018-05-02','2018-06-01',NOW)
  f=pd.DataFrame([[100,102,99,101,20]]*len(idx),index=idx,columns=chart.FIELDS);f.attrs=dict(META,timeframe='1D')
  self.assertEqual(dq.audit(f,'1D',META,NOW,check_stale=False)['state'],'READY')
  g=f.drop(idx[3]);g.attrs=f.attrs;self.assertNotEqual(dq.audit(g,'1D',META,NOW,check_stale=False)['state'],'READY')
  x=f.iloc[:1].copy();x.index=pd.to_datetime(['2015-01-16']);self.assertEqual(dq.audit(x,'1D',META,NOW,check_stale=False)['state'],'INVALID')
 def test_real_saved_deep_archive_keeps_bad_rows_out_and_provenance(self):
  p=pathlib.Path(__file__).parent/'history64-evidence/proxy-auto28/source-probes/gettex-apple-daily-depth.json'
  payload=json.loads(p.read_text());original=json.dumps(payload,sort_keys=True)
  f=chart.decode(payload,META,ISIN,'1D',pd.Timestamp(dq.GETTEX_DAILY_START,tz='Europe/Berlin'),pd.Timestamp('2026-10-28T00:00Z'),NOW)
  self.assertEqual(json.dumps(payload,sort_keys=True),original)
  self.assertLess(f.index.min(),pd.Timestamp('2016-01-01'))
  self.assertEqual(f.attrs['archive_invalid']['state'],'INVALID');self.assertGreater(len(f),2800)
  for tf in ('1W','1M'):
   out=resample(f,'1D',tf,now=NOW)
   self.assertGreater(len(out),100);self.assertEqual(out.attrs['source'],'gettex_chart')
   self.assertEqual(out.attrs['currency'],'EUR');self.assertNotEqual(out.attrs['data_quality']['state'],'READY')
  print('DEEP_REAL',len(f),'rejected',len(f.attrs['archive_invalid']['rejected_timestamps']),'first',f.index.min())
if __name__=='__main__':unittest.main()
