import sys,tempfile,unittest,gzip,json
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError
import pandas as pd
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
from storage import Store
import gettex_history as gh
from candlepilot import activity,presentation

class CP(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.s=Store(self.tmp.name)
  self.info={'isin':'DE0007164600'};self.now=pd.Timestamp('2026-10-24T08:00Z')
  self.meta=dict(source='gettex',venue='GETTEX',currency='EUR',symbol=self.info['isin'],timezone='Europe/Berlin')
  self.key='data_quality:DE0007164600:DE0007164600:15m'
  self.report=dict(state='UNCONFIRMED',identity=self.meta,ranges=[['2026-10-14 22:45','2026-10-14 22:45']])
  self.s.set_setting(self.key,self.report);self.calls=[]
 def tearDown(self):self.s.read_cache.close();self.tmp.cleanup()
 def http(self,url):
  self.calls.append(url)
  return b'<a href="https://erdk.bayerische-boerse.de/data/posttrade.20261023.06.00.munc.csv.gz">file</a>' if url==gh.PAGE else gzip.compress(b'fixture')
 def test_bounded_acquisition_preserves_dq(self):
  r=gh.acquire_missing_evidence(self.info,self.s,self.http,self.now)
  self.assertEqual(r['downloaded'],8);self.assertEqual(len(self.calls),9)
  self.assertEqual(self.s.setting(self.key),self.report)
  self.assertEqual(self.s.setting('interval_evidence:'+self.info['isin'],{}),{})
 def test_missing_source_cooldown_survives_reopen(self):
  def unavailable(url):
   if url==gh.PAGE:return self.http(url)
   raise HTTPError(url,404,'missing',{},None)
  r=gh.acquire_missing_evidence(self.info,self.s,unavailable,self.now);self.assertEqual(r['state'],'UNCONFIRMED')
  other=Store(self.tmp.name)
  try:self.assertEqual(gh.acquire_missing_evidence(self.info,other,lambda u:self.fail('cooldown'),self.now)['state'],'COOLDOWN')
  finally:other.read_cache.close()
 def test_rate_limit_shared_across_dates(self):
  def limited(url):raise HTTPError(url,429,'limited',{},None)
  gh.acquire_missing_evidence(self.info,self.s,limited,self.now)
  self.report['ranges']=[['2026-10-19 22:45','2026-10-19 22:45']];self.s.set_setting(self.key,self.report)
  self.assertEqual(gh.acquire_missing_evidence(self.info,self.s,lambda u:self.fail('429 retry'),self.now)['state'],'COOLDOWN')
 def test_wrong_identity_no_request(self):
  self.report['identity']['currency']='USD';self.s.set_setting(self.key,self.report)
  self.assertEqual(gh.acquire_missing_evidence(self.info,self.s,lambda u:self.fail('identity'),self.now)['state'],'IDLE')
 def test_restart_continues_missing_files_only(self):
  gh.acquire_missing_evidence(self.info,self.s,self.http,self.now)
  self.calls=[]
  r=gh.acquire_missing_evidence(self.info,self.s,self.http,self.now+pd.Timedelta(minutes=16))
  self.assertEqual(r['missing_before'],116);self.assertEqual(r['downloaded'],8)
 def test_bad_compressed_response_not_saved(self):
  r=gh.acquire_missing_evidence(self.info,self.s,lambda u:self.http(u) if u==gh.PAGE else b'error HTML',self.now)
  self.assertEqual(r['state'],'UNCONFIRMED');self.assertEqual(list((self.s.directory/'gettex_files').glob('*.gz')),[])
 def test_cached_historical_files_reach_existing_recovery(self):
  cache=self.s.directory/'gettex_files';cache.mkdir();(cache/'posttrade.20261014.20.45.munc.csv.gz').write_bytes(gzip.compress(b'real fixture'))
  def parse(raw,isin,file_time):return pd.DataFrame({'price':[10.0],'volume':[2]},index=pd.to_datetime(['2026-10-14 22:40']))
  f=gh.download(self.info['isin'],lambda u:b'',parse,self.s,pd.Timestamp('2026-10-14T20:30Z'),pd.Timestamp('2026-10-14T21:00Z'))
  self.assertEqual(len(f),1);self.assertEqual(f.iloc[0]['price'],10)
 def test_acquired_files_use_existing_classifier_only_when_complete(self):
  from app import StockMonitor
  self.s.save_instrument(self.info)
  def published(url):
   if url==gh.PAGE:return self.http(url)
   name=url.rsplit('/',1)[-1];parts=name.split('.');stamp=parts[2]+':'+parts[3]+':00'
   raw='DE0007236101,'+stamp+',EUR,10,1\n'
   if '.05.45.' in name:raw+=self.info['isin']+',05:30:00,EUR,10,1\n'
   return gzip.compress(raw.encode())
  parse=lambda *a,**k:StockMonitor._parse_gettex_csv(None,*a,**k)
  gh.acquire_missing_evidence(self.info,self.s,published,self.now)
  self.assertEqual(gh.classify_cached_session(self.info,self.s,parse,self.now)['state'],'UNCONFIRMED')
  for n in range(1,16):gh.acquire_missing_evidence(self.info,self.s,published,self.now+pd.Timedelta(minutes=16*n))
  result=gh.classify_cached_session(self.info,self.s,parse,self.now+pd.Timedelta(hours=5))
  self.assertEqual((result['state'],result['no_trades']),('DONE',185))
  self.assertEqual(self.s.setting(self.key),self.report) # acquisition cannot publish READY itself
 def test_retry_after_respected(self):
  def limited(url):raise HTTPError(url,429,'limited',{'Retry-After':'172800'},None)
  gh.acquire_missing_evidence(self.info,self.s,limited,self.now)
  self.assertEqual(pd.Timestamp(self.s.setting('candlepilot_source_cooldown')),self.now+pd.Timedelta(days=2))
 def test_untrusted_file_host_not_requested(self):
  def untrusted(url):
   self.assertEqual(url,gh.PAGE)
   return b'<a href="https://untrusted.example/posttrade.20261023.06.00.munc.csv.gz">x</a>'
  r=gh.acquire_missing_evidence(self.info,self.s,untrusted,self.now)
  self.assertEqual(r['state'],'UNCONFIRMED');self.assertIn('NO_PUBLISHED_OFFICIAL',r['reason'])
 def test_activity_lifecycle_no_polling_active(self):
  c=SimpleNamespace(closed=False,active=False,active_kind=None,process=None)
  self.assertEqual(activity(c),'IDLE');self.assertEqual(activity(None),'UNKNOWN')
  c.active=True;self.assertEqual(activity(c),'UNKNOWN')
  c.process=SimpleNamespace(poll=lambda:None);c.active_kind='feed_update';self.assertEqual(activity(c),'ACTIVE')
  c.active=False;self.assertEqual(activity(c,error='failed'),'ERROR')
  c.process.poll=lambda:1;self.assertEqual(activity(c),'ERROR')
  c.closed=True;self.assertEqual(activity(c),'STOPPED')
  self.assertEqual(activity(c,closing=True),'STOPPED')
 def run_wrapper(self,calculate):
  import ast,queue
  from contextlib import nullcontext
  tree=ast.parse(Path('stock_monitor/desktop014.py').read_text(encoding='utf8'))
  method=next(n for n in ast.walk(tree) if isinstance(n,ast.FunctionDef) and n.name=='_gettex_worker')
  code=ast.Module(body=[method],type_ignores=[]);scope={};exec(compile(code,'wrapper','exec'),scope)
  obj=SimpleNamespace(feed_slots=nullcontext(),closing=False,compute_feed_update=SimpleNamespace(calculate=calculate),events=queue.Queue(),feed_done=lambda *a:None,cp_error='old')
  scope['_gettex_worker'](obj,self.info);return obj
 def test_dq_block_is_not_worker_malfunction(self):
  obj=self.run_wrapper(lambda *a:{'errors':['15m UNCONFIRMED']})
  self.assertIsNone(obj.cp_error);self.assertFalse(obj.events.empty())
 def test_worker_exception_is_error(self):
  def fail(*a):raise RuntimeError('worker exited')
  self.assertEqual(self.run_wrapper(fail).cp_error,'worker exited')
 def test_real_tk_binding_all_states(self):
  import ast,tkinter as tk
  from tkinter import ttk
  root=tk.Tk();root.withdraw()
  try:
   tree=ast.parse(Path('stock_monitor/desktop014.py').read_text(encoding='utf8'))
   method=next(n for n in ast.walk(tree) if isinstance(n,ast.FunctionDef) and n.name=='_update_candlepilot')
   scope={};exec(compile(ast.Module(body=[method],type_ignores=[]),'binding','exec'),scope)
   c=SimpleNamespace(closed=False,active=False,active_kind=None,process=None)
   obj=SimpleNamespace(root=root,cp_label=ttk.Label(root),compute_feed_update=c,closing=False,cp_error=None,_update_candlepilot=lambda:None)
   for state in ['IDLE','ACTIVE','ERROR','STOPPED','UNKNOWN']:
    c.closed=state=='STOPPED';c.active=state=='ACTIVE';c.active_kind='feed_update'
    c.process=SimpleNamespace(poll=lambda:None) if c.active else None
    obj.compute_feed_update=None if state=='UNKNOWN' else c;obj.cp_error='failure' if state=='ERROR' else None
    scope['_update_candlepilot'](obj);root.update_idletasks()
    self.assertIn(state,obj.cp_label.cget('text'));self.assertEqual(str(obj.cp_label.cget('foreground')),presentation(state)[0])
  finally:root.destroy()
 def test_only_active_green(self):
  for state in ['IDLE','STOPPED','ERROR','UNKNOWN']:self.assertEqual(presentation(state)[0],'#ef4444')
  self.assertEqual(presentation('ACTIVE')[0],'#22c55e')
if __name__=='__main__':unittest.main()
