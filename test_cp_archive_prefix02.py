import copy,json,pathlib,sqlite3,tempfile,unittest
from unittest.mock import patch
import pandas as pd
import gettex_chart as chart
from storage import Store
from test_gettex_chart import ISIN,META,ChartTests

NOW=pd.Timestamp('2026-11-02T10:00Z')
FLOOR='2015-01-19'

class Client:
 def __init__(self,rows=None,meta=None,callback=None):
  self.rows=rows if rows is not None else [dict(_DATE_END='2021-01-04',OPEN_PRC=100,HIGH_1=102,LOW_1=99,CLOSE_PRC=101,ACVOL_1=20)]
  self.meta=meta or META;self.callback=callback;self.calls=[]
 def fetch(self,isin,base,start,end):
  self.calls.append((isin,base,start,end))
  if self.callback:self.callback()
  return dict(status='OK',request=dict(ric='AAPL.GTX',samples='D'),data=copy.deepcopy(self.rows)),self.meta

class Prefix(unittest.TestCase):
 setUp=ChartTests.setUp
 def setup_daily(self,first='2024-01-02',floor='2024-01-01'):
  frame=pd.DataFrame([[100,102,99,101,20],[110,112,109,111,30]],index=pd.to_datetime([first,'2026-10-30']),columns=chart.FIELDS)
  frame.attrs=META;self.store.save_series(self.info,frame,'1D')
  saved=dict(state='SAVED',identity=META,listing_proof=dict(META,isin=ISIN,status='CONFIRMED'),last='2026-10-30 00:00:00',received_at=NOW.isoformat(),quality='UNCONFIRMED',verified_daily_floor=floor)
  self.store.set_setting(chart.state_key(ISIN,'1D'),saved)
  for tf in ('5m','15m'):self.store.set_setting(chart.state_key(ISIN,tf),dict(retry_after=(NOW+pd.Timedelta(days=1)).isoformat()))
  return frame
 def run_turn(self,client,now=NOW):return chart._fetch_turn(self.info,self.store,now,client)
 def rows(self):
  with self.store.connect() as db:return db.execute('select * from series order by ts').fetchall()
 def test_loads_only_missing_prefix_and_preserves_tail_watermark(self):
  self.setup_daily();before=self.rows();c=Client();out=self.run_turn(c)
  self.assertEqual(out['state'],'SAVED',out);self.assertEqual(out['added'],1);self.assertEqual(out['changed'],0)
  self.assertEqual(c.calls[0][2].tz_convert('Europe/Berlin').date(),pd.Timestamp(FLOOR).date())
  self.assertEqual(c.calls[0][3].tz_convert('Europe/Berlin').date(),pd.Timestamp('2024-01-01').date())
  self.assertEqual(out['last'],'2026-10-30 00:00:00');self.assertTrue(all(r in self.rows() for r in before))
  self.assertEqual(out['verified_daily_floor'],FLOOR)
 def test_already_deep_archive_prevents_http_without_ohlcv_changes(self):
  self.setup_daily(FLOOR);before=self.rows();c=Client();r=self.run_turn(c)
  self.assertEqual(c.calls,[]);self.assertEqual(r['archive_prefix']['state'],'ALREADY_PRESENT');self.assertEqual(self.rows(),before)
 def test_restart_does_not_reload_checked_prefix(self):
  self.setup_daily();self.run_turn(Client());self.store.read_cache.close();self.store=Store(self.tmp.name);self.addCleanup(self.store.read_cache.close)
  c=Client(rows=[]);out=self.run_turn(c,NOW+pd.Timedelta(minutes=1))
  self.assertEqual(out['state'],'NOT_DUE');self.assertEqual(c.calls,[])
 def test_empty_older_response_keeps_dq_and_uses_bounded_retry(self):
  self.setup_daily();before=self.rows();out=self.run_turn(Client([]));s=self.store.setting(chart.state_key(ISIN,'1D'))
  self.assertEqual(out['state'],'SOURCE_UNAVAILABLE');self.assertEqual(s['quality'],'UNCONFIRMED');self.assertEqual(s['last'],'2026-10-30 00:00:00')
  self.assertEqual(s['archive_prefix']['state'],'UNCONFIRMED');self.assertEqual(self.rows(),before)
  with patch.object(chart.scheduler,'decision',return_value=dict(due=False,reason='UNCHANGED',key='test')):
   c=Client([]);self.assertEqual(self.run_turn(c,NOW+pd.Timedelta(days=1))['state'],'NOT_DUE');self.assertEqual(c.calls,[])
   c=Client([]);self.run_turn(c,NOW+pd.Timedelta(days=31));self.assertEqual(len(c.calls),1)
 def test_currency_and_venue_mismatch_never_commit_prefix(self):
  self.setup_daily();before=self.rows()
  for field,value in [('currency','USD'),('venue','FRA')]:
   with self.subTest(field=field):
    self.store.set_setting('gettex_chart_next_request',None)
    self.store.set_setting(chart.state_key(ISIN,'1D'),{**self.store.setting(chart.state_key(ISIN,'1D')),'retry_after':None})
    with patch.object(chart.scheduler,'decision',return_value=dict(due=True,key='test')):
     out=self.run_turn(Client(meta={**META,field:value}))
    self.assertEqual(out['state'],'ERROR');self.assertEqual(self.rows(),before)
 def test_revision_race_aborts_prefix_and_keeps_unchecked_floor(self):
  self.setup_daily();before=self.rows()
  def change():
   with self.store.connect() as db:db.execute("update input_revisions set revision=revision+1 where scope=?",('market:'+ISIN+':1D',))
  out=self.run_turn(Client(callback=change));self.assertIn('INPUT_CHANGED',out['reason']);self.assertEqual(self.rows(),before)
  self.assertEqual(self.store.setting(chart.state_key(ISIN,'1D'))['verified_daily_floor'],'2024-01-01')
 def test_invalid_daily_row_retained_as_rejection_not_ready(self):
  self.setup_daily();c=Client();bad=dict(c.rows[0],_DATE_END='2021-01-05',HIGH_1=1);c.rows.append(bad)
  out=self.run_turn(c);self.assertEqual(out['state'],'PARTIAL');self.assertEqual(out['added'],1)
  self.assertEqual(out['quality'],'INVALID');self.assertIn('2021-01-05 00:00:00',out['archive_invalid']['rejected_timestamps'])
 def test_global_retry_after_and_reservation_prevent_prefix_request(self):
  self.setup_daily();c=Client();self.store.set_setting('cp_source_retry:gettex_chart',(NOW+pd.Timedelta(hours=1)).isoformat())
  self.assertEqual(self.run_turn(c)['state'],'SOURCE_COOLDOWN');self.assertEqual(c.calls,[])
  self.store.set_setting('cp_source_retry:gettex_chart',None);self.store.set_setting('gettex_chart_next_request',(NOW+pd.Timedelta(minutes=1)).isoformat())
  self.assertEqual(self.run_turn(c)['state'],'RATE_WAIT');self.assertEqual(c.calls,[])

class SavedDaily(Prefix):
 def test_saved_real_daily_prefix_feeds_weekly_monthly_without_changing_ohlcv(self):
  from analytics import resample as aggregate
  fixture=pathlib.Path(__file__).parent/'history64-evidence/all-history02/before.sqlite3'
  with sqlite3.connect(fixture.as_uri()+'?mode=ro',uri=True) as db:
   rows=db.execute("select ts,open,high,low,close,volume from series where isin=? and source='gettex_chart' and venue='GETTEX' and currency='EUR' and symbol='AAPL.GTX' and base_tf='1D' order by ts",(ISIN,)).fetchall()
  frame=pd.DataFrame([r[1:] for r in rows],index=pd.to_datetime([r[0] for r in rows]),columns=chart.FIELDS)
  frame.attrs=dict(META,timeframe='1D');prefix=frame.loc[frame.index<'2024-01-01'];tail=frame.loc[frame.index>='2024-01-01']
  self.setup_daily()
  with self.store.connect() as db:db.execute('delete from series')
  self.store.save_series(self.info,tail,'1D')
  data=[dict(_DATE_END=str(t.date()),**dict(zip(('OPEN_PRC','HIGH_1','LOW_1','CLOSE_PRC','ACVOL_1'),row.tolist()))) for t,row in prefix.iterrows()]
  out=self.run_turn(Client(data));self.assertEqual(out['added'],len(prefix),out);self.assertEqual(out['changed'],0)
  restored=chart._load_native(self.store,self.info,'1D')
  pd.testing.assert_frame_equal(restored[list(chart.FIELDS)],frame,check_names=False,check_freq=False)
  for tf in ('1W','1M'):
   actual=aggregate(restored,'1D',tf,now=NOW);expected=aggregate(frame,'1D',tf,now=NOW)
   columns=list(chart.FIELDS)+['bar_complete']
   pd.testing.assert_frame_equal(actual[columns],expected[columns],check_names=False,check_freq=False)
   self.assertEqual(actual.attrs['source'],'gettex_chart');self.assertEqual(actual.attrs['currency'],'EUR')
  print('REAL_PREFIX',len(prefix),'TOTAL_DAILY',len(restored),'WEEKLY',len(aggregate(restored,'1D','1W',now=NOW)),'MONTHLY',len(aggregate(restored,'1D','1M',now=NOW)))

if __name__=='__main__':unittest.main()
