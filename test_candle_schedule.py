import sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from types import SimpleNamespace
import pandas as pd
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
from storage import Store
import candle_schedule as cs

class Schedule(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.s=Store(self.tmp.name);self.isin='DE0007164600'
  self.g=cs.gettex_meta(self.isin);self.x=dict(source='proxy',venue='GER',symbol='SAP.DE',currency='EUR',timezone='Europe/Berlin')
 def tearDown(self):self.s.read_cache.close();self.tmp.cleanup()
 def test_gettex_slice_and_delay(self):
  self.assertLess(cs.boundary(self.g,'5m','2026-10-23T06:00:00Z')['close_at'],'2026-10-23T06:00:00Z')
  b=cs.boundary(self.g,'5m','2026-10-23T06:00:00Z');self.assertEqual(b['close_at'],'2026-10-23T05:45:00+00:00');self.assertIn('UNKNOWN',b['delay_basis'])
 def test_xetra_not_gettex_evening(self):
  b=cs.boundary(self.x,'5m','2026-10-23T20:00Z');self.assertFalse(b['session_open']);self.assertEqual(b['close_at'],'2026-10-23T15:30:00+00:00')
  self.assertTrue(cs.boundary(self.g,'5m','2026-10-23T20:00Z')['session_open'])
 def test_weekend_and_holiday(self):
  for day in ['2026-10-24','2026-12-25']:
   for m in [self.g,self.x]:
    b=cs.boundary(m,'15m',day+'T12:00Z');self.assertFalse(b['session_open']);self.assertLess(b['close_at'][:10],day)
 def test_dst_spring_and_autumn(self):
  for date,close in [('2026-03-27','22:00'),('2026-03-30','21:00'),('2026-10-23','21:00'),('2026-10-26','22:00')]:
   self.assertEqual(cs.boundary(self.g,'1D',date+'T23:30Z')['close_at'],date+'T'+close+':00+00:00')
 def test_partial_last_bar_and_before_publish(self):
  self.assertEqual(cs.boundary(self.x,'1H','2026-10-23T15:30:59Z')['close_at'],'2026-10-23T15:00:00+00:00')
  self.assertEqual(cs.boundary(self.x,'1H','2026-10-23T15:31:00Z')['close_at'],'2026-10-23T15:30:00+00:00')
 def test_identical_closed_interval_suppressed_until_recheck(self):
  now=pd.Timestamp('2026-10-23T10:01Z');p=cs.decision(self.s,self.isin,self.x,'1H',now);cs.finish(self.s,self.isin,self.x,'1H',p,now=now)
  self.assertFalse(cs.decision(self.s,self.isin,self.x,'1H',now+pd.Timedelta(minutes=1))['due'])
  self.assertEqual(cs.decision(self.s,self.isin,self.x,'1H',now+pd.Timedelta(minutes=15))['reason'],'PUBLICATION_RECHECK_EXPIRED')
 def test_revision_change_is_valid_reason(self):
  now=pd.Timestamp('2026-10-23T10:01Z');p=cs.decision(self.s,self.isin,self.x,'1H',now);cs.finish(self.s,self.isin,self.x,'1H',p,now=now)
  with self.s.connect() as db:db.execute('INSERT OR REPLACE INTO input_revisions VALUES (?,?)',('market:'+self.isin+':1H',999))
  self.assertEqual(cs.decision(self.s,self.isin,self.x,'1H',now)['reason'],'INPUT_REVISION')
 def test_restart_suppression_and_failed_work_retry(self):
  now=pd.Timestamp('2026-10-23T10:01Z');p=cs.decision(self.s,self.isin,self.x,'5m',now);cs.finish(self.s,self.isin,self.x,'5m',p,RuntimeError('failed'),now)
  other=Store(self.tmp.name)
  try:
   self.assertFalse(cs.decision(other,self.isin,self.x,'5m',now+pd.Timedelta(minutes=5))['due'])
   self.assertTrue(cs.decision(other,self.isin,self.x,'5m',now+pd.Timedelta(minutes=16))['due'])
  finally:other.read_cache.close()
 def test_retry_after_shared_all_tf(self):
  now=pd.Timestamp('2026-10-23T10:01Z');p=cs.decision(self.s,self.isin,self.x,'5m',now)
  cs.finish(self.s,self.isin,self.x,'5m',p,HTTPError('https://example',429,'limited',{'Retry-After':'7200'},None),now)
  for b in ['5m','15m','1H','1D']:self.assertEqual(cs.decision(self.s,self.isin,self.x,b,now+pd.Timedelta(hours=1))['reason'],'SOURCE_COOLDOWN')
 def test_gettex_429_no_inner_retries(self):
  import gettex_history as gh
  calls=[]
  def http(url):calls.append(url);raise HTTPError(url,429,'limited',{'Retry-After':'7200'},None)
  with self.assertRaises(HTTPError):gh.download(self.isin,http,lambda *a:None,self.s)
  with self.assertRaisesRegex(ValueError,'cooldown'):gh.download(self.isin,http,lambda *a:None,self.s)
  self.assertEqual(len(calls),1)
 def test_gettex_dns_failure_stops_inner_retries_and_pauses_shared_source(self):
  import gettex_history as gh
  import socket
  from urllib.error import URLError
  calls=[]
  error=URLError(socket.gaierror(11001,'getaddrinfo failed'))
  def http(url):calls.append(url);raise error
  start=pd.Timestamp.now(tz='UTC')
  with self.assertRaises(URLError):gh.download(self.isin,http,lambda *a:None,self.s)
  with self.assertRaisesRegex(ValueError,'cooldown'):gh.download('US0378331005',http,lambda *a:None,self.s)
  self.assertEqual(calls,[gh.PAGE])
  due=pd.Timestamp(self.s.setting('cp_source_retry:gettex'))
  self.assertGreaterEqual(due,start+pd.Timedelta(minutes=1))
  plan=cs.decision(self.s,'US0378331005',cs.gettex_meta('US0378331005'),'5m',due-pd.Timedelta(seconds=1))
  self.assertEqual(plan['reason'],'SOURCE_COOLDOWN')
  self.assertNotEqual(cs.decision(self.s,self.isin,self.x,'5m',start)['reason'],'SOURCE_COOLDOWN')
  plan={'key':cs.unit_key(self.isin,self.g,'5m')}
  cs.finish(self.s,self.isin,self.g,'5m',plan,error,start)
  state=self.s.setting(plan['key'])
  self.assertEqual(state['error'],'DNS_RESOLUTION_FAILED')
  self.assertEqual(state['retry_after'],due.isoformat())
  self.assertTrue(cs.decision(self.s,self.isin,self.g,'5m',due)['due'])
  import sqlite3,json
  from contextlib import closing
  with closing(sqlite3.connect(self.s.directory/'delivery_observations.sqlite3')) as db:
   receipt=json.loads(db.execute('SELECT payload FROM receipts ORDER BY id LIMIT 1').fetchone()[0])
  self.assertEqual(receipt['reason'],'DNS_RESOLUTION_FAILED')
  self.assertIsNone(receipt['http_status'])
 def test_gettex_file_dns_failure_aborts_package_and_next_run_recovers(self):
  import gzip,socket
  import gettex_history as gh
  from urllib.error import URLError
  root='https://erdk.bayerische-boerse.de/'
  urls=[root+'posttrade.20261124.14.30.'+mic+'.csv.gz' for mic in ('munc','mund')]
  calls=[]
  offline=True
  def http(url):
   calls.append(url)
   if url==gh.PAGE:return ''.join(f'<a href="{file}">file</a>' for file in urls).encode()
   if offline:raise URLError(socket.gaierror(11001,'file host DNS failed'))
   return gzip.compress(b'fixture')
  def parse(raw,isin,file_time):
   self.assertEqual(raw,b'fixture')
   return pd.DataFrame({'price':[10.0],'volume':[1.0]},index=[pd.Timestamp('2026-11-24 15:23')])
  with self.assertRaises(URLError):gh.download(self.isin,http,parse,self.s)
  self.assertEqual(calls,[gh.PAGE,urls[0]])
  self.s.set_setting('cp_source_retry:gettex',(pd.Timestamp.now(tz='UTC')-pd.Timedelta(seconds=1)).isoformat())
  offline=False
  result=gh.download(self.isin,http,parse,self.s)
  self.assertEqual(calls,[gh.PAGE,urls[0],gh.PAGE,*urls])
  self.assertEqual(result['price'].tolist(),[10.0])
 def test_dns_failed_historical_gettex_check_reuses_short_wait(self):
  import gettex_history as gh
  import socket
  from urllib.error import URLError
  now=pd.Timestamp('2026-11-24T14:35:00Z')
  key='data_quality:'+self.isin+':'+self.isin+':15m'
  self.s.set_setting(key,dict(identity=self.g,state='GAP',ranges=[['2026-11-24 15:00','2026-11-24 15:15']]))
  calls=[]
  def http(url):calls.append(url);raise URLError(socket.gaierror(11001,'temporary DNS failure'))
  first=gh.acquire_missing_evidence({'isin':self.isin},self.s,http,now)
  self.assertEqual(first['reason'],'DNS_RESOLUTION_FAILED')
  self.assertEqual(pd.Timestamp(first['retry_after']),now+pd.Timedelta(minutes=1))
  self.assertIsNone(self.s.setting('candlepilot_source_cooldown'))
  blocked=gh.acquire_missing_evidence({'isin':self.isin},self.s,http,now+pd.Timedelta(seconds=30))
  self.assertEqual(blocked['state'],'COOLDOWN')
  again=gh.acquire_missing_evidence({'isin':self.isin},self.s,http,now+pd.Timedelta(minutes=1))
  self.assertEqual(again['reason'],'DNS_RESOLUTION_FAILED')
  self.assertEqual(calls,[gh.PAGE,gh.PAGE])
 def test_gettex_dns_hold_cannot_shorten_existing_source_rate_limit(self):
  now=pd.Timestamp('2026-11-24T14:35Z')
  later=(now+pd.Timedelta(hours=2)).isoformat()
  self.s.set_setting('cp_source_retry:gettex',later)
  self.assertEqual(cs.hold_gettex_dns(self.s,now),later)
  self.assertEqual(self.s.setting('cp_source_retry:gettex'),later)
 def test_non_dns_gettex_transport_errors_keep_existing_retries(self):
  import gettex_history as gh
  calls=[]
  def http(url):
   calls.append(url)
   if len(calls)<3:raise TimeoutError('slow')
   return b'ok'
  with patch.object(gh.time,'sleep') as sleep:
   self.assertEqual(gh.fetch(http,gh.PAGE),b'ok')
  self.assertEqual(len(calls),3)
  self.assertEqual([args[0][0] for args in sleep.call_args_list],[.5,1.])
 def test_benchmark_existing_cadence_preserved(self):
  import benchmarks as bm
  i=bm.CATALOG['INDEX:VIX'];now=pd.Timestamp('2026-10-24T10:00Z')
  # No new intraday close: the pre-existing official Cboe daily cadence stays.
  with patch.object(cs,'decision',return_value={'due':False,'reason':'UNCHANGED'}):
   self.assertEqual(len(cs.planned_feeds(self.s,[i],now)['feeds']),1)
   self.s.set_setting(bm.history_key(i),{'attempted_at':now.isoformat()})
   self.assertFalse(cs.planned_feeds(self.s,[i],now+pd.Timedelta(hours=23))['feeds'])
   self.assertEqual(len(cs.planned_feeds(self.s,[i],now+pd.Timedelta(days=1))['feeds']),1)
 def test_failed_closed_day_check_resumes(self):
  now=pd.Timestamp('2026-10-24T10:00Z');p=cs.decision(self.s,self.isin,self.x,'5m',now)
  cs.finish(self.s,self.isin,self.x,'5m',p,RuntimeError('network'),now)
  self.assertEqual(cs.decision(self.s,self.isin,self.x,'5m',now+pd.Timedelta(minutes=16))['reason'],'RETRY_DUE')
 def test_daily_not_rechecked_every_fifteen_minutes(self):
  now=pd.Timestamp('2026-10-23T10:01Z');p=cs.decision(self.s,self.isin,self.x,'1D',now);cs.finish(self.s,self.isin,self.x,'1D',p,now=now)
  self.assertFalse(cs.decision(self.s,self.isin,self.x,'1D',now+pd.Timedelta(minutes=30))['due'])
 def test_us_early_close(self):
  meta=dict(source='proxy',venue='NMS',currency='USD',symbol='FIXTURE',timezone='America/New_York')
  b=cs.boundary(meta,'1H','2026-11-27T18:01Z');self.assertEqual(b['close_at'],'2026-11-27T18:00:00+00:00');self.assertFalse(b['session_open'])
 def test_proxy_and_benchmark_share_429_gate(self):
  import history_service as hs,benchmarks as bm,requests
  error=requests.HTTPError('limited');response=SimpleNamespace(status_code=429,headers={'Retry-After':'7200'},raise_for_status=lambda:(_ for _ in ()).throw(error));error.response=response
  with patch.object(hs.requests,'get',return_value=response) as get:
   with self.assertRaises(requests.HTTPError):hs.download('SAP.DE',start=pd.Timestamp('2026-10-23T00:00Z'),store=self.s)
   info=next(v for v in bm.CATALOG.values() if v.get('history_source')!='cboe')
   with self.assertRaisesRegex(ValueError,'cooldown'):bm.download(info,store=self.s)
   self.assertEqual(get.call_count,1)
 def test_fresh_work_precedes_bounded_history_in_existing_feed(self):
  import queue
  from contextlib import nullcontext
  from desktop014 import Desktop014
  for failure in [False,True]:
   calls=[]
   def live(isin):
    calls.append('fresh_gettex')
    if failure:raise ValueError('source unavailable')
    return pd.DataFrame()
   obj=SimpleNamespace(feed_slots=nullcontext(),store=self.s,closing=False,events=queue.Queue(),feed_done=lambda *a:None,_download_gettex_trades=live,_recover_gettex_range=lambda *a:None,_http_bytes=lambda u:None,_parse_gettex_csv=lambda *a:None)
   with patch('history_service.recover',lambda *a:(calls.append('fresh_proxy'),[])),patch('gettex_history.acquire_missing_evidence',lambda *a:calls.append('history_files')),patch('gettex_history.classify_cached_session',lambda *a:calls.append('classify')),patch('history_service.inspect_saved_archive',lambda *a,**kw:calls.append('history_audit')):
    Desktop014._run_feed(obj,{'isin':self.isin})
   self.assertEqual(calls,['fresh_gettex','fresh_proxy','history_files','classify','history_audit'])
   self.assertFalse(obj.events.empty())
 def check_aged_queue(self,count):
  import threading,time
  from performance_dispatcher import Dispatcher
  entered=threading.Event();release=threading.Event();done=threading.Event();order=[]
  d=Dispatcher(lambda cb,value:cb(value),workers=4,sample=lambda old:({'cpu':0,'ram':0},None));d.focus('visible')
  def block():entered.set();release.wait(5)
  def record(x):
   order.append(x)
   if len(order)==count+1:done.set()
  try:
   d.submit(block,lambda _:None,lambda e:self.fail(e),key='block',lane='feed_update');self.assertTrue(entered.wait(3))
   for n in range(count):d.submit(lambda n=n:n,record,lambda e:self.fail(e),key=str(n),lane='feed_update',isin=str(n))
   with d.cv:
    for t in d.pending.values():t['queued']=time.monotonic()-301
   d.submit(lambda:'fresh',record,lambda e:self.fail(e),key='fresh',lane='feed_update',isin='visible')
   release.set();self.assertTrue(done.wait(5));self.assertEqual(order[-1],'fresh');self.assertEqual(set(order[:-1]),set(range(count)))
  finally:release.set();d.close()
 def test_aged_feed_jobs_53(self):self.check_aged_queue(53)
 def test_aged_feed_jobs_200(self):self.check_aged_queue(200)
 def test_no_polling_active(self):
  from candlepilot import activity
  c=SimpleNamespace(active=False,active_kind=None,closed=False,process=None)
  cs.planned_feeds(self.s,[{'isin':self.isin}],'2026-10-24T09:00Z');self.assertEqual(activity(c),'IDLE')
 def test_long_run_all_base_tf_and_history_turns(self):
  counts={b:0 for b in ['5m','15m','1H','1D']};history=0;suppressed=0
  info={'isin':self.isin};self.s.set_setting('history_identity:'+self.isin,self.x)
  start=pd.Timestamp('2026-10-22T07:00Z')
  for n in range(600):
   now=start+pd.Timedelta(minutes=5*n)
   plan=cs.planned_feeds(self.s,[info],now)
   if plan['feeds']:
    history+=1;self.s.set_setting('cp_maintenance:'+self.isin,{'at':now.isoformat()})
    gp=cs.decision(self.s,self.isin,self.g,'5m',now)
    if gp['due']:cs.finish(self.s,self.isin,self.g,'5m',gp,now=now)
   for b in counts:
    p=cs.decision(self.s,self.isin,self.x,b,now)
    if p['due']:counts[b]+=1;cs.finish(self.s,self.isin,self.x,b,p,now=now)
    else:suppressed+=1
  self.assertTrue(all(v>0 for v in counts.values()));self.assertGreater(history,10);self.assertGreater(suppressed,1000)
  print('SCHEDULE_MEASURED',counts,'history_turns',history,'suppressed_unit_attempts',suppressed)
 def test_existing_proxy_sync_skips_unchanged_bases(self):
  import history_service as hs
  info={'isin':self.isin,'history_symbol':'SAP.DE'};self.s.set_setting('history_identity:'+self.isin,self.x)
  now=pd.Timestamp('2026-10-23T10:01Z');calls=[]
  def sync(info,store,symbol,base,download):calls.append(base);return {**self.x,'backfill_complete':True}
  with patch('history_backfill.sync',sync),patch.object(cs,'clock',lambda now=None:pd.Timestamp(now) if now is not None else pd.Timestamp('2026-10-23T10:01Z')):
   hs._recover(info,self.s);hs._recover(info,self.s)
  self.assertEqual(calls,['1D','5m','15m','1H'])
if __name__=='__main__':unittest.main()
