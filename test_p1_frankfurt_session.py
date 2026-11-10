import copy,sqlite3,sys,unittest
from pathlib import Path
import pandas as pd
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
import data_quality as dq
import calculation_window as cw
from timeframes import schedule,endpoints,aggregate
from candle_schedule import boundary

class FrankfurtSession(unittest.TestCase):
 def setUp(self):
  self.meta=dict(source='proxy',symbol='TEST.F',venue='FRA',currency='EUR',timezone='Europe/Berlin')
 def frame(self,index,tf):
  f=pd.DataFrame(dict(open=10.,high=11.,low=9.,close=10.5,volume=100.),index=pd.DatetimeIndex(index))
  f.attrs={**self.meta,'timeframe':tf};return f
 def test_distinct_venues_dst_holidays_and_special_close(self):
  for day,opened,closed in [('2026-10-23','06:00','20:00'),('2026-10-26','07:00','21:00'),('2026-05-14','06:00','18:00')]:
   row=schedule(self.meta,day,day).iloc[0]
   self.assertEqual(row.open.strftime('%H:%M'),opened);self.assertEqual(row.close.strftime('%H:%M'),closed)
  self.assertTrue(schedule(self.meta,'2026-10-25','2026-10-25').empty)
  self.assertTrue(schedule(self.meta,'2026-04-03','2026-04-03').empty)
  self.assertEqual(schedule({**self.meta,'venue':'GER'},'2026-10-26','2026-10-26').iloc[0].close.hour,16)
  # Retain the pre-existing special-year-end rule rather than inventing 22h.
  self.assertEqual(schedule(self.meta,'2025-12-30','2025-12-30').iloc[0].close.hour,13)
 def test_daily_bar_not_closed_before_actual_frankfurt_close(self):
  f=self.frame(['2026-10-23','2026-10-26'],'1D')
  self.assertEqual(endpoints(f)[-1],pd.Timestamp('2026-10-26T21:00Z'))
  self.assertEqual(int((endpoints(f)<=pd.Timestamp('2026-10-26T18:00Z')).sum()),1)
  b=boundary(self.meta,'1D','2026-10-26T18:00Z')
  self.assertTrue(b['session_open']);self.assertEqual(b['close_at'],'2026-10-23T20:00:00+00:00')
 def test_stale_only_after_real_close_and_existing_grace(self):
  f=self.frame(['2026-10-22','2026-10-23'],'1D');before=f.copy();attrs=copy.deepcopy(f.attrs)
  for now,state in [('2026-10-26T18:00Z','READY'),('2026-10-26T21:29Z','READY'),('2026-10-26T21:30Z','STALE')]:
   self.assertEqual(dq.audit(f,'1D',now=pd.Timestamp(now))['state'],state)
  pd.testing.assert_frame_equal(f,before);self.assertEqual(f.attrs,attrs)
 def test_evening_real_candles_valid_but_missing_or_bad_remain_blocked(self):
  f=self.frame(pd.date_range('2026-10-26 20:00','2026-10-26 21:45',freq='15min'),'15m');now=pd.Timestamp('2026-10-26T21:30Z')
  self.assertEqual(dq.audit(f,'15m',now=now)['state'],'READY')
  self.assertEqual(dq.audit(f.drop(f.index[3]),'15m',now=now)['state'],'GAP')
  f.loc[f.index[3],'high']=1
  self.assertEqual(dq.audit(f,'15m',now=now)['state'],'INVALID')
 def test_aggregates_keep_frankfurt_boundaries_without_synthetic_slots(self):
  f=self.frame(pd.date_range('2026-10-26 08:00','2026-10-26 21:45',freq='15min'),'15m');now=pd.Timestamp('2026-10-26T21:30Z')
  for tf,count,last in [('1H',14,'21:00'),('4H',4,'20:00')]:
   out=aggregate(f,'15m',tf,now)
   self.assertEqual(len(out),count);self.assertEqual(out.index[-1].strftime('%H:%M'),last)
   self.assertEqual(endpoints(out)[-1],pd.Timestamp('2026-10-26T21:00Z'));self.assertTrue(out.bar_complete.all())
   self.assertEqual(out.volume.sum(),f.volume.sum());self.assertEqual(out.attrs['venue'],'FRA')
  damaged=aggregate(f.drop(f.index[5]),'15m','4H',now)
  self.assertNotEqual(damaged.attrs['data_quality']['state'],'READY')
 def test_saved_old_calendar_permission_revoked_and_revision_guard_retained(self):
  db=sqlite3.connect(':memory:');db.execute('create table input_revisions(scope text,revision integer)');db.execute("insert into input_revisions values('instrument:TEST',1)")
  rec=dict(isin='TEST',execution_version=1,timeframes={'1D':{'identity':self.meta}},_input_revisions={'instrument:TEST':1},expires_at='2026-10-26T21:00Z')
  now=pd.Timestamp('2026-10-26T18:00Z')
  self.assertEqual(cw.eligibility_state(db,rec,now),'STALE')
  rec['calendar_revision']=dq.XFRA_SESSION_REVISION
  self.assertEqual(cw.eligibility_state(db,rec,now),'CURRENT')
  self.assertEqual(cw.eligibility_state(db,rec,pd.Timestamp(rec['expires_at'])),'EXPIRED')
  db.execute('update input_revisions set revision=2');self.assertEqual(cw.eligibility_state(db,rec,now),'STALE');db.close()
 def test_deadline_and_historical_real_missing_sessions_not_waived(self):
  self.assertEqual(cw.context_deadline(self.meta,'1D',pd.Timestamp('2026-10-26T18:00Z')),pd.Timestamp('2026-10-26T21:00Z'))
  self.assertEqual(cw.context_deadline(self.meta,'1D',pd.Timestamp('2026-10-26T21:10Z')),pd.Timestamp('2026-10-26T21:30Z'))
  for year in (2011,2012,2013):
   day=f'{year}-10-03';self.assertIn(pd.Timestamp(day),schedule(self.meta,day,day).index)
  f=self.frame(pd.bdate_range('2012-10-01','2012-10-05').difference(pd.DatetimeIndex(['2012-10-03'])),'1D')
  r=dq.audit(f,'1D',now=pd.Timestamp('2012-10-05T23:00Z'));self.assertEqual(r['state'],'GAP');self.assertEqual(r['missing_count'],1)

if __name__=='__main__':unittest.main()
