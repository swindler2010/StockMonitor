"""Issue19: unrelated evidence maintenance must not revoke an exact receipt."""
import sys,copy,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
from cp_controls import digest
import revision_watcher as rw

class ScopedReceipts(unittest.TestCase):
 def setUp(self):
  self.source={'rows':{'5m':[['2026-10-21 19:30:00',100,100,100,100,2]],'15m':[['2026-10-21 19:30:00',100,100,100,100,2]]},
   'trades':[['2026-10-21 19:31:00',100,2]],'proof':{'identity':{'source':'gettex','venue':'GETTEX','currency':'EUR','symbol':'TEST','timezone':'Europe/Berlin'},
   'no_trades':[['2026-10-21 19:35:00','2026-10-21 19:45:00']],
   'trade_intervals':[['2026-10-21 19:30:00','2026-10-21 19:35:00']],
   'zero_quantity_intervals':[],'incomplete_publication_intervals':[],
   'reference':{'GETTEX:20261021':{'file':'a'*64}}}}
  self.receipt={'first':'2026-10-21 19:30:00','end':'2026-10-21 19:45:00','source':copy.deepcopy(self.source),'source_digest':digest(self.source)}
 def test_unrelated_notice_recheck_and_outside_range_preserve_receipt(self):
  source=copy.deepcopy(self.source)
  source['proof'].update(exchange_closures=[{'start':'2026-10-26 07:30:00','end':'2026-10-26 09:55:00','reference':'official notice'}],closure_checked_at='2026-11-20T12:00:00Z')
  source['proof']['no_trades'].append(['2026-11-19 10:00:00','2026-11-19 10:05:00'])
  self.assertTrue(rw.receipt_source_current(source,self.receipt))
 def test_equivalent_split_merged_coverage_and_ten_rechecks(self):
  source=copy.deepcopy(self.source);source['proof']['no_trades']=[['2026-10-21 19:40:00','2026-10-21 20:00:00'],['2026-10-21 19:35:00','2026-10-21 19:40:00']]
  for _ in range(10):self.assertTrue(rw.receipt_source_current(source,self.receipt))
 def test_real_local_evidence_rows_files_identity_and_unknown_changes_reject(self):
  for kind in ('row','trade','file','identity','gap','zero','incomplete','notice','unknown','stored_tamper','digest_tamper'):
   source=copy.deepcopy(self.source);r=copy.deepcopy(self.receipt)
   if kind=='row':source['rows']['5m'][0][-1]=3
   elif kind=='trade':source['trades'][0][-1]=3
   elif kind=='file':source['proof']['reference']['GETTEX:20261021']['file']='b'*64
   elif kind=='identity':source['proof']['identity']['currency']='USD'
   elif kind=='gap':source['proof']['no_trades'][0][0]='2026-10-21 19:40:00'
   elif kind=='zero':source['proof']['zero_quantity_intervals']=[['2026-10-21 19:35:00','2026-10-21 19:40:00']]
   elif kind=='incomplete':source['proof']['incomplete_publication_intervals']=[['2026-10-21 19:35:00','2026-10-21 19:40:00']]
   elif kind=='notice':source['proof']['exchange_closures']=[{'start':'2026-10-21 19:35:00','end':'2026-10-21 19:40:00'}]
   elif kind=='unknown':source['proof']['new_authoritative_fact']='changed'
   elif kind=='stored_tamper':r['source']['proof']['no_trades']=[]
   else:r['source_digest']='0'*64
   with self.subTest(kind=kind):self.assertFalse(rw.receipt_source_current(source,r))

class EvidenceReadTransport(unittest.TestCase):
 def test_existing_snapshot_alias_pins_instrument_and_page_without_writes(self):
  import queue,json
  from unittest.mock import patch
  from urllib.request import Request,build_opener,ProxyHandler
  from urllib.error import HTTPError
  from bridge import Bridge
  class Leaf:
   def write_text(self,text,**kw):self.text=text
   def read_text(self,**kw):return self.text
   def unlink(self):self.text=''
  class Directory:
   def __init__(self):self.files={}
   def mkdir(self,**kw):pass
   def __truediv__(self,key):return self.files.setdefault(key,Leaf())
  events=queue.Queue();directory=Directory()
  bridge=Bridge(directory,events,lambda *a:self.fail('read dispatched a command'))
  def close():
   bridge.close();bridge.server.shutdown();bridge.server.server_close()
  self.addCleanup(close)
  def projection(path,isin,page):return {'isin':isin,'page':page,'evidence':[{'status':'VALID'}],'trade_permission':False}
  def request(args):
   data=json.dumps({'action':'snapshot','arguments':args}).encode()
   req=Request('http://127.0.0.1:'+str(bridge.server.server_port)+'/bridge',data,headers={'Authorization':'Bearer '+bridge.token,'Content-Type':'application/json'})
   with build_opener(ProxyHandler({})).open(req,timeout=5) as response:return json.load(response)
  with patch('dq_evidence.collect',side_effect=projection) as collect:
   first=request({'path':'/@dq-evidence/US0378331005/0','limit':50})
   revision=first['revision'];self.assertFalse(events.qsize());self.assertEqual(collect.call_count,1)
   again=request({'path':'/@dq-evidence/US0378331005/0/evidence','expected_revision':revision})
   self.assertEqual(collect.call_count,1);self.assertEqual(again['items'][0]['value']['status'],'VALID')
   for wrong in ('/@dq-evidence/US67066G1040/0','/@dq-evidence/US0378331005/1'):
    with self.assertRaises(HTTPError) as error:request({'path':wrong,'expected_revision':revision})
    self.assertEqual(error.exception.code,400)
   for invalid in ('/@dq-evidence/../../0','/@dq-evidence/US0378331005/-1'):
    with self.assertRaises(HTTPError):request({'path':invalid})
  self.assertTrue(events.empty())

class FullReceiptPipeline(unittest.TestCase):
 def test_real_source_revalidation_refresh_restart_and_changed_native_row(self):
  for isin in ('US0378331005','US67066G1040','US0231351067','US30303M1027','DE0007164600','GB0000000001'):
   with self.subTest(isin=isin):self._exercise(isin)

 def _exercise(self,isin):
  import tempfile,json,pandas as pd
  import data_quality as dq,cp_reconciliation as rv
  from test_cp_tv_legacy_reference import FlatStore
  root=Path('C:/Users/panus/Documents/Codex/СМ-Архив')
  if not root.is_dir():root=Path(tempfile.gettempdir())
  with tempfile.NamedTemporaryFile(dir=root,suffix='.sqlite3',delete=False) as f:path=Path(f.name)
  store=None
  try:
   store=FlatStore(path);info=dict(isin=isin,name='Fixture',kind='stock',history_venue_lock='GETTEX');store.save_instrument(info)
   meta=dict(source='gettex_chart',venue='GETTEX',currency='EUR',symbol='AAPL.GTX',timezone='Europe/Berlin');official={**meta,'source':'gettex','symbol':isin}
   now=pd.Timestamp('2026-11-20T12:00Z');first='2026-10-21 19:45:00';lo='2026-10-21 19:30:00';end='2026-10-21 20:15:00'
   proof=dict(isin=isin,identity=official,basis='complete_published_trade_files',checked_at='2026-11-19T12:00Z',no_trades=[],trade_intervals=[],zero_quantity_intervals=[],reference={'GETTEX:20261021':{}})
   rows=[('2026-10-21 19:30:00',100.,100.,100.,100.,2.),('2026-10-21 20:00:00',101.,101.,101.,101.,3.)]
   for at in pd.date_range(lo,end,freq='5min',inclusive='left'):
    field='trade_intervals' if str(at) in {r[0] for r in rows} else 'no_trades';proof[field].append([str(at),str(at+pd.Timedelta(minutes=5))])
   for at in pd.date_range(lo,end,freq='15min',inclusive='left'):
    right=(at+pd.Timedelta(minutes=15)).tz_localize('Europe/Berlin').tz_convert('UTC')
    for board in ('munc','mund'):proof['reference']['GETTEX:20261021']['posttrade.'+right.strftime('%Y%m%d.%H.%M')+'.'+board+'.csv.gz']='a'*64
   store.set_setting('interval_evidence:'+isin,proof);store.set_setting(rw.RETIME_POLICY,dict(version=rw.RETIME_VERSION,approved=True))
   for base in ('5m','15m'):store.set_setting('gettex_chart:'+isin+':'+base,dict(state='SAVED',identity=meta,listing_proof={**meta,'isin':isin,'status':'CONFIRMED','verified_at':'2026-11-19T12:00Z'}))
   store.set_setting('data_quality:'+isin+':AAPL.GTX:15m',dict(identity=meta,state='UNCONFIRMED',ranges=[[first,first]]))
   with store.connect() as db:
    for base in ('5m','15m'):
     db.executemany('INSERT INTO candles VALUES (?,?,?,?,?,?,?,?,?)',[(isin,'gettex',base,*r) for r in rows])
     db.executemany('INSERT INTO series VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',[(isin,*[meta[k] for k in rw.IDENTITY[:4]],base,*r) for r in rows])
    db.executemany('INSERT INTO trades VALUES (?,?,?,?)',[(isin,str(pd.Timestamp(r[0])+pd.Timedelta(minutes=1)),r[1],r[5]) for r in rows])
    job=rv._spec(isin,meta,'15m',first,first);plan=rw.retime_plan(db,job,rv._input(db,job),now)
    db.commit();db.execute('BEGIN IMMEDIATE');result=rw.publish_retime(db,job,rv._input(db,job),plan,now)
   receipt=store.setting(result['receipt_key'])
   # A later official correction supersedes a stale visual observation; it is
   # not a new error and does not create a second authority/permission.
   job['observation']={'at':'2026-11-01T12:00Z','result':'ABSENT','identity_confirmed':True,'tv_symbol':'GETTEX:TEST','input_fingerprint':'old'}
   store.set_setting(rv.key(job),job)
   from dq_evidence import collect
   evidence=collect(path,isin)['evidence']
   self.assertEqual(next(r for r in evidence if r['kind']=='tv')['status'],'SUPERSEDED_BY_OFFICIAL')
   before=store.load_series(isin,'15m',meta['source'],meta['symbol'],'GETTEX','EUR')
   self.assertEqual(dq.audit(before,'15m',meta,now,check_stale=False)['state'],'READY')
   proof['exchange_closures']=[dict(start='2026-10-26 07:30:00',end='2026-10-26 09:55:00',reference='notice')];proof['closure_checked_at']=now.isoformat();store.set_setting('interval_evidence:'+isin,proof)
   for _ in range(10):
    with store.connect() as db:self.assertTrue(rw.retime_current(db,receipt,now))
   after=store.load_series(isin,'15m',meta['source'],meta['symbol'],'GETTEX','EUR')
   self.assertEqual(dq.audit(after,'15m',meta,now,check_stale=False)['state'],'READY')
   self.assertNotIn(pd.Timestamp(first),after.index)
   pd.testing.assert_frame_equal(pd.DataFrame(before,copy=False),pd.DataFrame(after,copy=False))
   # Same parent aggregation before/after evidence-only maintenance.
   from timeframes import aggregate
   for target in ('30m','1H','2H','3H','4H'):
    left=aggregate(before,'15m',target,now);right=aggregate(after,'15m',target,now)
    pd.testing.assert_frame_equal(pd.DataFrame(left,copy=False),pd.DataFrame(right,copy=False))
   # Backfill outside the exact receipt is not a local input mutation.
   with store.connect() as db:
    db.execute('INSERT INTO series VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',(isin,*[meta[k] for k in rw.IDENTITY[:4]],'15m','2026-10-22 09:00:00',102,102,102,102,1))
    self.assertTrue(rw.retime_current(db,receipt,now))
   store.read_cache.close();store=FlatStore(path)
   with store.connect() as db:
    self.assertTrue(rw.retime_current(db,receipt,now));db.execute("UPDATE series SET volume=volume+1 WHERE base_tf='15m'")
   with store.connect() as db:self.assertFalse(rw.retime_current(db,receipt,now))
  finally:
   if store is not None:store.read_cache.close()
   for suffix in ('','-wal','-shm'):Path(str(path)+suffix).unlink(missing_ok=True)

class EvidenceRequest(unittest.TestCase):
 def test_existing_queue_async_idempotent_and_final_deadline_guard(self):
  import tempfile,queue,time
  from types import SimpleNamespace
  from bridge_queue import CommandQueue
  from dq_evidence import dispatch
  from cp_assessment import request,PREFIX
  from test_cp_tv_legacy_reference import FlatStore
  root=Path('C:/Users/panus/Documents/Codex/СМ-Архив')
  if not root.is_dir():root=Path(tempfile.gettempdir())
  with tempfile.NamedTemporaryFile(dir=root,suffix='.sqlite3',delete=False) as f:path=Path(f.name)
  store=None
  try:
   store=FlatStore(path);isin='US0378331005';store.save_instrument({'isin':isin,'name':'Fixture'})
   events=queue.Queue();jobs=[];writable=[True]
   app=SimpleNamespace(store=store,closing=False)
   def job(work,done,failed,**options):jobs.append((work,done,failed));return True
   app.job=job
   commands=CommandQueue('fixture',events,lambda action,args:dispatch(app,args),lambda:writable[0])
   app.bridge=SimpleNamespace(commands=commands)
   deadline=time.time()+30;envelope={'instance_id':'fixture','command_id':'first','deadline':deadline}
   result=commands.submit('reconcile_dq_evidence',{'isin':isin},envelope,wait=0)
   # wait=0 would expire before UI dispatch, as designed; use a pending call
   self.assertEqual(result['status'],'EXPIRED');callback,args=events.get();callback(*args)
   self.assertFalse(jobs);self.assertEqual(store.setting(PREFIX+isin,{}),{})
   # Execute an actual async command through the ordinary event pump.
   import threading
   def submit():return commands.submit('reconcile_dq_evidence',{'isin':isin},{**envelope,'command_id':'live'},wait=5)
   box={};thread=threading.Thread(target=lambda:box.update(result=submit()));thread.start()
   callback,args=events.get(timeout=3);callback(*args)
   work,done,failed=jobs.pop();done(work());thread.join(3)
   answer=box['result'];self.assertEqual(answer['status'],'COMPLETED');self.assertEqual(answer['result']['status'],'QUEUED')
   saved=store.setting(PREFIX+isin)
   self.assertIn('explicit_recheck_at',saved)
   # Same queue, bounded 2:1 rotation; explicit owner work does not starve
   # an ordinary instrument even with 200 older background requests.
   from cp_assessment import choose,CURSOR
   with store.connect() as db:
    import json
    for i in range(200):
     other='ZZ'+str(i).zfill(10)
     db.execute('INSERT INTO settings VALUES (?,?)',(PREFIX+other,json.dumps({'isin':other,'requested_at':1})))
    self.assertEqual(choose(db)['isin'],isin)
    db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)',(CURSOR,'2'))
    self.assertNotEqual(choose(db)['isin'],isin)
   self.assertEqual(request(store,isin)['requested_at'],saved['requested_at'])
   self.assertFalse(answer['result']['grants_changed']);self.assertFalse(answer['result']['candles_changed'])
   with self.assertRaisesRegex(ValueError,'EVIDENCE_REQUEST_EXPIRED'):request(store,isin,lambda:False)
   self.assertEqual(store.setting(PREFIX+isin),saved)
   store.set_setting(PREFIX+isin,{})
   writable[0]=False
   with self.assertRaisesRegex(ValueError,'EVIDENCE_REQUEST_EXPIRED'):request(store,isin,lambda:commands.proposal_valid('live'))
   self.assertEqual(store.setting(PREFIX+isin),{})
   commands.close()
  finally:
   if store is not None:store.read_cache.close()
   for suffix in ('','-wal','-shm'):Path(str(path)+suffix).unlink(missing_ok=True)

class PolicyPublication(unittest.TestCase):
 def test_factual_assessment_binds_required_owner_policy_before_publication(self):
  import tempfile,pandas as pd
  from unittest.mock import patch
  import calculation_window as cw
  from cp_controls import DEMO_ALL_POLICY
  from test_cp_tv_legacy_reference import FlatStore
  root=Path('C:/Users/panus/Documents/Codex/СМ-Архив')
  if not root.is_dir():root=Path(tempfile.gettempdir())
  with tempfile.NamedTemporaryFile(dir=root,suffix='.sqlite3',delete=False) as f:path=Path(f.name)
  store=None
  try:
   store=FlatStore(path);info={'isin':'US0378331005','name':'Fixture','kind':'stock'};store.save_instrument(info)
   store.set_setting(DEMO_ALL_POLICY,{'approved':True,'version':1})
   store.set_setting('cp_manual_risk:'+info['isin'],{'accepted':{'old':{'actor':'fixture'}}})
   def factual(context,info,now):
    # Fully factual path does not enter the old owner-risk fallback at all.
    context.closed_specs={}
    return {'isin':info['isin'],'checked_at':now.isoformat(),'expires_at':(now+pd.Timedelta(minutes=5)).isoformat(),
     'calculation_policy':'gettex_first_v1','reference_binding_version':1,'candidates':[],
     'analysis_scope':{'policy':cw.SCOPE_POLICY,'timezone':'Europe/Berlin','start':(now.tz_convert('Europe/Berlin').normalize()-pd.DateOffset(years=5)).date().isoformat()}}
   with patch.object(cw,'assess_all',side_effect=factual),patch.object(cw,'attach_execution'):
    result=cw.calculate_eligibility(store,info)
   self.assertNotIn('error',result)
   saved=cw.publish_eligibility(store,info,result)
   self.assertEqual(saved['publication_state'],'CURRENT')
   self.assertIn('setting:'+DEMO_ALL_POLICY,result['_input_revisions'])
   self.assertIn('setting:cp_manual_risk:'+info['isin'],result['_input_revisions'])
   # A real policy change still rejects the old result; no extension/override.
   deadline=result['expires_at'];store.set_setting(DEMO_ALL_POLICY,{'approved':True,'version':2})
   with self.assertRaisesRegex(ValueError,'ELIGIBILITY_INPUT_CHANGED: STALE'):cw.publish_eligibility(store,info,result)
   self.assertEqual(result['expires_at'],deadline)
  finally:
   if store is not None:store.read_cache.close()
   for suffix in ('','-wal','-shm'):Path(str(path)+suffix).unlink(missing_ok=True)

if __name__=='__main__':unittest.main()

