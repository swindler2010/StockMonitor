"""Owner controls only. Disposable SQLite; no live quotes, accounts or network."""
import copy,json,sys,tempfile,threading,time,unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch
import pandas as pd
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
from storage import Store
import cp_controls as cp
import paper_account as paper
import data_quality as dq
import calculation_window as cw

class Controls(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.store=Store(self.tmp.name)
  self.isin='DE0007164600';self.now=pd.Timestamp('2026-10-27T10:01Z')
  self.meta=dict(source='gettex',symbol=self.isin,venue='GETTEX',currency='EUR',timezone='Europe/Berlin')
  self.info=dict(isin=self.isin,name='SAP');self.store.save_instrument(self.info)
  self.qkey=dq.key(self.info,self.isin,'5m')
  self.q=dict(state='GAP',identity=self.meta,base_tf='5m',issues=['Пропущенные свечи: 1'],ranges=[['2026-10-26 10:00:00','2026-10-26 10:00:00']],checked_at=self.now.isoformat())
  self.store.set_setting(self.qkey,self.q)
  self.frame=pd.DataFrame(dict(open=100.,high=101.,low=99.,close=100.,volume=100.),index=pd.DatetimeIndex(['2026-10-27 10:55']))
  self.frame.attrs=dict(self.meta,timeframe='5m');self.store.save_frame(self.isin,'gettex','5m',self.frame)
  self.result=dict(isin=self.isin,identity=self.meta,timeframe='5m',data_quality=self.q,bars=[dict(open=100.,high=101.,low=99.,close=100.,volume=100.,closed=True,open_at='2026-10-27T09:55:00Z',close_at='2026-10-27T10:00:00Z')])
 def tearDown(self):self.store.read_cache.close();self.tmp.cleanup()
 def rows(self):
  with self.store.connect() as db:return cp.view(db,self.isin)
 def grant(self):return cp.accept(self.store,self.isin,self.rows(),True)
 def gate(self,meta=None):
  with self.store.connect() as db:return cp.manual_gate(db,self.isin,meta or self.meta)
 def contract(self):
  with self.store.read_cache.track_inputs(isolated=True) as token:
   self.store.read_cache.depend('instrument:'+self.isin);self.store.read_cache.depend('market:'+self.isin+':5m')
   ctx=NS(store=self.store,frame_for=lambda i,t:self.frame,trading_analysis=lambda i:dict(status="READY",signal="WAIT",calculation_context=cp.RISK_CONTEXT))
   result=cw.build(ctx,self.info,'5m',{},self.now)
  self.result.update(execution_context=result,_input_revisions=dict(token));return result
 def test_review_compare_is_atomic_and_partial_not_whole_permission(self):
  old=self.rows();self.q['ranges'].append(['2026-10-26 11:00:00','2026-10-26 11:00:00']);self.store.set_setting(self.qkey,self.q)
  with self.assertRaisesRegex(ValueError,'изменился'):cp.accept(self.store,self.isin,old,True)
  cp.accept(self.store,self.isin,old)
  with self.assertRaisesRegex(ValueError,'UNACCEPTED'):self.gate()
 def test_accept_is_separate_from_dq_and_persists_after_reopen(self):
  self.grant();receipt=self.gate();self.assertFalse(receipt['automatic_signal'])
  self.assertEqual(self.store.setting(self.qkey),self.q)
  self.store.read_cache.close();self.store=Store(self.tmp.name)
  self.assertEqual(receipt,self.gate());cp.revoke(self.store,self.isin)
  with self.assertRaisesRegex(ValueError,'NOT_GRANTED'):self.gate()
 def test_new_problem_and_listing_revoke_manual_permission(self):
  self.grant()
  with self.assertRaisesRegex(ValueError,'LISTING'):self.gate(dict(self.meta,venue='FRA'))
  with self.assertRaisesRegex(ValueError,'IDENTITY'):self.gate(dict(self.meta,currency='USD'))
  self.q['state']='INVALID';self.store.set_setting(self.qkey,self.q)
  with self.assertRaisesRegex(ValueError,'UNACCEPTED'):self.gate()
 def test_manual_context_has_actual_core_and_revisions(self):
  self.grant();c=self.contract()
  self.assertEqual(c['status'],'MANUAL_RISK_ACCEPTED');self.assertEqual(c['trading_engine']['signal'],'WAIT');self.assertTrue(c['analysis_ready'])
  self.assertIn('setting:cp_manual_risk:'+self.isin,self.result['_input_revisions'])
  self.assertIn('setting:'+self.qkey,self.result['_input_revisions'])
 def test_quote_allows_reviewed_history_but_keeps_normal_dq_block(self):
  with self.assertRaisesRegex(ValueError,'DATA_BLOCKED'):paper.quote(self.info,self.result,self.now.timestamp())
  self.assertEqual(paper.quote(self.info,self.result,self.now.timestamp(),True)[1]['close'],100.)
 def test_execution_quote_guards_are_not_waivable(self):
  for field,value in [('high',90.),('volume',0.),('open_at','2026-10-27T09:56Z'),('close_at','2026-10-27T09:00Z'),('closed',False)]:
   r=copy.deepcopy(self.result);r['bars'][0][field]=value
   with self.subTest(field=field),self.assertRaises(ValueError):paper.quote(self.info,r,self.now.timestamp(),True)
  r=copy.deepcopy(self.result);r['identity']['currency']='USD'
  with self.assertRaises(ValueError):paper.quote(self.info,r,self.now.timestamp(),True)
 def test_transaction_contract_and_rollback_no_persisted_fill(self):
  self.grant();self.contract();args=dict(isin=self.isin,timeframe='5m',side='BUY',quantity=1,request_id='isolated-owner-control',_deadline=self.now.timestamp()+60)
  with self.store.connect() as db:
   paper.initialize(db);db.commit();db.execute('BEGIN IMMEDIATE')
   answer=paper.execute(db,self.info,self.result,args,self.now.timestamp())
   self.assertEqual(answer['fill']['manual_risk']['mode'],'MANUAL_RISK_ACCEPTED')
   self.assertTrue(paper.execute(db,self.info,self.result,args,self.now.timestamp())['idempotent'])
   db.rollback()
  with self.store.connect() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM paper_fills').fetchone()[0],0)
 def test_revision_changed_before_transaction_stays_blocked(self):
  self.grant();self.contract();self.store.set_setting(self.qkey,dict(self.q,state='INVALID'))
  with self.store.connect() as db:
   paper.initialize(db);db.commit();db.execute('BEGIN IMMEDIATE')
   with self.assertRaisesRegex(ValueError,'STALE_INPUTS'):paper.execute(db,self.info,self.result,dict(isin=self.isin,timeframe='5m',side='BUY',quantity=1,request_id='stale',_deadline=self.now.timestamp()+60),self.now.timestamp())
   db.rollback()
 def test_manual_risk_without_actual_core_cannot_execute(self):
  self.grant();contract=self.contract();contract['trading_engine']={'status':'DATA BLOCKED','signal':'WAIT'};contract['analysis_ready']=False
  with self.store.connect() as db:
   paper.initialize(db);db.commit();db.execute('BEGIN IMMEDIATE')
   with self.assertRaisesRegex(ValueError,'ENGINE_DATA_BLOCKED'):paper.execute(db,self.info,self.result,dict(isin=self.isin,timeframe='5m',side='BUY',quantity=1,request_id='no-core',_deadline=self.now.timestamp()+60),self.now.timestamp())
   db.rollback()
 def test_no_implicit_extension_of_demo_allowlist(self):
  with self.assertRaisesRegex(ValueError,'пяти'):cp.accept(self.store,'US88160R1014',self.rows(),True)
 def test_all_review_digest_cannot_accept_new_unseen_problem(self):
  review=cp.digest(sorted(p['id'] for p in self.rows()))
  self.q['ranges'].append(['2026-10-26 12:00:00','2026-10-26 12:00:00']);self.store.set_setting(self.qkey,self.q)
  with self.assertRaisesRegex(ValueError,'изменился'):cp.accept(self.store,self.isin,[],True,review)
 def test_health_read_is_paged_and_does_not_write(self):
  from candlepilot_health import collect
  self.q['ranges']=[[f'2026-10-26 {h:02d}:{m:02d}:00']*2 for h in range(8,23) for m in range(0,60,5)]
  self.store.set_setting(self.qkey,self.q)
  with self.store.connect() as db:before=db.execute('SELECT * FROM settings ORDER BY key').fetchall()
  compact=collect(self.store.path);a=collect(self.store.path,problem_isin=self.isin);b=collect(self.store.path,problem_isin=self.isin,problem_page=1)
  self.assertEqual(compact['problems'],[]);self.assertEqual(len(a['problems']),150);self.assertEqual(len(b['problems']),30)
  self.assertEqual(a['problem_review']['digest'],b['problem_review']['digest'])
  with self.store.connect() as db:self.assertEqual(before,db.execute('SELECT * FROM settings ORDER BY key').fetchall())
 def test_repair_duplicate_resume_cooldown_and_unchanged_grants(self):
  p=self.rows()[0];first=cp.request_repair(self.store,p);self.assertEqual(cp.request_repair(self.store,p),first)
  self.store.set_setting('cp_source_retry:gettex',(self.now+pd.Timedelta(minutes=10)).isoformat())
  with patch.object(dq,'repair') as repair:
   result=cp.service_request(self.info,self.store,lambda *a:None,self.now);repair.assert_not_called()
  self.assertEqual(result['state'],'WAITING')
  self.store.read_cache.close();self.store=Store(self.tmp.name)
  with self.store.connect() as db:
   self.assertIsNone(cp.due_request(db,now=self.now));self.assertIsNotNone(cp.due_request(db,now=self.now+pd.Timedelta(minutes=11)))
   self.assertIsNone(cp.read(db,'cp_manual_risk:'+self.isin))
 def test_retry_survives_quality_state_change_and_uses_exact_target(self):
  p=self.rows()[0];cp.request_repair(self.store,p);self.q['state']='SOURCE_UNAVAILABLE';self.store.set_setting(self.qkey,self.q)
  with patch.object(dq,'repair',return_value=dict(self.q,next_retry=(self.now+pd.Timedelta(minutes=5)).isoformat())) as repair:
   result=cp.service_request(self.info,self.store,lambda *a:None,self.now)
   self.assertEqual(repair.call_args.kwargs['target_range'],p['range']);self.assertEqual(result['state'],'WAITING')
 def test_changed_identity_does_not_run_old_repair(self):
  cp.request_repair(self.store,self.rows()[0]);self.q['identity']=dict(self.meta,venue='FRA');self.store.set_setting(self.qkey,self.q)
  with patch.object(dq,'repair') as repair:
   result=cp.service_request(self.info,self.store,lambda *a:None,self.now);repair.assert_not_called()
  self.assertEqual(result['state'],'SUPERSEDED')
 def test_context_request_reuses_eligibility_without_false_ready(self):
  self.store.set_setting('cp_eligibility:'+self.isin,dict(timeframes={'4H':dict(identity=self.meta,blockers=['INSUFFICIENT_WARMUP'],count=12)}))
  p=next(p for p in self.rows() if p['kind']=='context');cp.request_repair(self.store,p)
  with patch.object(cw,'eligibility_turn',return_value=dict(timeframes={'4H':dict(blockers=['INSUFFICIENT_WARMUP'])})) as calculate:
   result=cp.service_request(self.info,self.store,lambda *a:None,self.now);calculate.assert_called_once()
  self.assertEqual(result['state'],'BLOCKED');self.assertIn('INSUFFICIENT_WARMUP',result['reason'])
 def test_scheduler_resumes_owner_request_without_refresh(self):
  import candle_schedule
  cp.request_repair(self.store,self.rows()[0]);self.store.read_cache.close();self.store=Store(self.tmp.name)
  with patch.object(cw,'eligibility_due',return_value=[]):result=candle_schedule.planned_feeds(self.store,[self.info],self.now)
  self.assertEqual(result['priority']['isin'],self.isin)
 def test_targeted_recovery_uses_actual_dq_and_preserves_other_gap(self):
  full=pd.DataFrame(dict(open=100.,high=101.,low=99.,close=100.,volume=100.),index=pd.date_range('2026-10-27 10:00',periods=12,freq='5min'));full.attrs=dict(self.meta,timeframe='5m')
  self.store.save_frame(self.isin,'gettex','5m',full.drop(full.index[[1,3]]));self.store.set_setting(self.qkey,{})
  _,before=dq.inspect_cached(self.info,self.store,self.isin,'5m',self.meta,self.now)
  target=before['ranges'][0];calls=[]
  def fetch(*a,**kw):calls.append((a,kw));return full
  with patch('history_service.recover_from_children',return_value=None):
   after=dq.repair(self.info,self.store,self.isin,'5m',self.meta,fetch,self.now,max_ranges=1,target_range=target)
  self.assertEqual(len(calls),1);self.assertEqual(len(after['ranges']),1);self.assertNotEqual(after['state'],'READY')
  with self.store.connect() as db:count=db.execute("SELECT count(*) FROM candles WHERE isin=?",(self.isin,)).fetchone()[0]
  self.assertEqual(count,11)

class Priority(unittest.TestCase):
 def test_53_and_200_jobs_selected_first_no_duplicate_then_normal(self):
  from performance_dispatcher import Dispatcher
  for count in (53,200):
   with self.subTest(count=count):
    done=[];release=threading.Event();running=threading.Event()
    dispatcher=Dispatcher(lambda f,x:f(x),sample=lambda p:({},None))
    try:
     def work():running.set();release.wait(5);return -1
     dispatcher.submit(work,done.append,done.append,key='running',lane='feed_update');self.assertTrue(running.wait(2))
     for n in range(count):dispatcher.submit(lambda n=n:n,done.append,done.append,key=f'feed:{n}',lane='feed_update')
     self.assertTrue(dispatcher.prioritize_feed(f'feed:{count-1}'))
     self.assertFalse(dispatcher.prioritize_feed('running'))
     self.assertFalse(dispatcher.submit(lambda:0,done.append,done.append,key=f'feed:{count-1}',lane='feed_update'))
     self.assertEqual(done,[]);release.set()
     until=time.monotonic()+5
     while len(done)<count+1 and time.monotonic()<until:time.sleep(.01)
     self.assertEqual(done[0:2],[-1,count-1]);self.assertEqual(sorted(done),list(range(-1,count)))
    finally:release.set();dispatcher.close()

if __name__=='__main__':unittest.main()
