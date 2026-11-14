"""Automatic dependency repair: isolated SQLite, no provider/working-account I/O."""
import copy,json,sqlite3,sys,unittest
from contextlib import contextmanager,ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch,Mock
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
import pandas as pd
import cp_controls as cp
from feed_worker import FeedContext

class Memory:
 def __init__(self):
  self.db=sqlite3.connect(':memory:',uri=True)
  self.db.executescript('''CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT);
   CREATE TABLE instruments(isin TEXT PRIMARY KEY,payload TEXT);
   CREATE TABLE candles(isin TEXT,source TEXT,base_tf TEXT,ts TEXT,open REAL,high REAL,low REAL,close REAL,volume REAL,PRIMARY KEY(isin,source,base_tf,ts));
   CREATE TABLE series(isin TEXT,source TEXT,venue TEXT,currency TEXT,symbol TEXT,base_tf TEXT,ts TEXT,open REAL,high REAL,low REAL,close REAL,volume REAL,PRIMARY KEY(isin,source,venue,currency,symbol,base_tf,ts));''')
 @contextmanager
 def connect(self):
  with self.db:yield self.db
 def setting(self,k,d=None):return cp.read(self.db,k,d)
 def set_setting(self,k,v):
  with self.connect() as db:cp.write(db,k,v)

class AutomaticRepair(unittest.TestCase):
 def setUp(self):
  self.s=Memory();self.addCleanup(self.s.db.close);self.isin='US0378331005';self.info=dict(isin=self.isin,name='fixture')
  self.meta=dict(source='gettex',symbol=self.isin,venue='GETTEX',currency='EUR',timezone='Europe/Berlin')
  self.now=pd.Timestamp('2026-11-10T19:00Z');self.qkey='data_quality:'+self.isin+':'+self.isin+':15m';self.key='cp_repair_requests:'+self.isin
  self.s.db.execute('INSERT INTO instruments VALUES (?,?)',(self.isin,json.dumps(self.info)));self.s.db.commit()
  self.s.set_setting('cp_gettex_source_policy',dict(version=1,approved=True,scope='registered_stocks',exclusive_venue='GETTEX'))
  self.span=['2026-11-09 11:15:00','2026-11-09 11:15:00']
  self.q=dict(identity=self.meta,base_tf='15m',state='UNCONFIRMED',ranges=[self.span])
  self.s.set_setting(self.qkey,self.q)
  self.spec=dict(isin=self.isin,identity=self.meta,base_tf='15m',timeframe='4H',**{'from':'2026-11-01','through':self.now.isoformat()})
  self.assessment=dict(isin=self.isin,timeframes={'4H':dict(analysis_ready=False,blockers=['WINDOW_UNCONFIRMED'],_closed_input=self.spec)})
  self.s.set_setting('cp_eligibility:'+self.isin,self.assessment)
 def jobs(self):return self.s.setting(self.key,{})
 def host(self,result=None):return SimpleNamespace(store=self.s,_parse_gettex_csv=Mock(),_calculate=Mock(return_value=result or {'errors':[]}))
 def stages(self,stack):
  for n in ('cp_controls.automatic_turn','cp_controls.tv_risk_turn','cp_controls.reviewed_risk_turn','cp_reconciliation.turn','history_service.inspect_changed_official_archive'):
   stack.enter_context(patch(n,return_value={}))
 def test_saved_blocked_dependency_enters_existing_queue_without_user_button(self):
  with ExitStack() as stack:
   self.stages(stack);FeedContext.calculate(self.host(),self.info)
  self.assertEqual(len(self.jobs()),1)
  task=next(iter(self.jobs().values()));self.assertEqual(task['state'],'QUEUED');self.assertEqual(task['problem']['range'],self.span)
  self.assertEqual(task['problem']['identity'],self.meta);self.assertIsNone(self.s.setting('cp_manual_risk:'+self.isin))
 def test_completed_repair_requests_existing_analysis_without_manual_refresh(self):
  with ExitStack() as stack:
   self.stages(stack);assess=stack.enter_context(patch('calculation_window.eligibility_turn',return_value={'timeframes':{}}))
   stack.enter_context(patch('cp_reconciliation.complete_analysis_request'))
   FeedContext.calculate(self.host({'errors':[],'cp_priority':{'state':'COMPLETED','finished_at':self.now.isoformat()}}),self.info)
  assess.assert_called_once()
  self.assertEqual(self.s.setting('cp_reconciliation_progress:'+self.isin)['analysis_requested_at'],self.now.isoformat())
 def plan(self,now=None):return cp.automatic_repair_turn(self.s,self.info,now or self.now)
 def task(self):return next(iter(self.jobs().values()))
 def terminal(self,state='BLOCKED'):
  jobs=self.jobs();job=next(iter(jobs.values()));job.update(state=state,recheck_after=(self.now+pd.Timedelta(days=7)).isoformat());self.s.set_setting(self.key,jobs)
 def test_unchanged_terminal_restart_and_unrelated_tail_do_not_repeat(self):
  self.plan();self.terminal();before=copy.deepcopy(self.jobs())
  self.assertEqual(self.plan()['state'],'CHECKED');self.assertEqual(self.jobs(),before)
  self.s.db.execute('INSERT INTO candles VALUES (?,?,?,?,?,?,?,?,?)',(self.isin,'gettex','15m','2026-11-09 11:30:00',100,101,99,100,1));self.s.db.commit()
  # First new neighbour is relevant and may unlock a real child/native repair.
  self.assertEqual(self.plan()['state'],'QUEUED');self.terminal();before=copy.deepcopy(self.jobs())
  self.s.db.execute('INSERT INTO candles VALUES (?,?,?,?,?,?,?,?,?)',(self.isin,'gettex','15m','2026-11-10 19:30:00',100,101,99,100,1));self.s.db.commit()
  restored=Memory();self.addCleanup(restored.db.close);self.s.db.backup(restored.db)
  self.assertEqual(cp.automatic_repair_turn(restored,self.info,self.now)['state'],'CHECKED');self.assertEqual(restored.setting(self.key),before)
  self.assertEqual(self.plan(self.now+pd.Timedelta(days=7))['state'],'QUEUED')
 def test_identity_window_and_optional_tfs_never_create_permissions(self):
  for change in ('foreign_isin','currency','venue','source','outside','optional','ready','unconfirmed_identity'):
   with self.subTest(change=change):
    spec=copy.deepcopy(self.spec);row=dict(analysis_ready=False,blockers=['blocked'],_closed_input=spec);tf='4H'
    if change=='foreign_isin':spec['isin']='OTHER'
    if change in ('currency','venue','source'):
     spec['identity'][change]={'currency':'USD','venue':'FRA','source':'proxy'}[change]
    if change=='outside':spec['from']='2026-11-10'
    if change=='optional':tf='5m'
    if change=='ready':row['analysis_ready']=True
    if change=='unconfirmed_identity':
     spec['identity']['source']='gettex_chart';q=copy.deepcopy(self.q);q['identity']=spec['identity'];self.s.set_setting(self.qkey,q)
    self.s.set_setting('cp_eligibility:'+self.isin,dict(isin=self.isin,timeframes={tf:row}))
    self.plan();self.assertFalse(self.jobs())
  self.assertIsNone(self.s.setting('cp_manual_risk:'+self.isin));self.assertEqual(self.s.db.execute('SELECT count(*) FROM candles').fetchone()[0],0)
 def test_actual_5m_dependency_allowed_but_foreign_benchmark_not_planned(self):
  spec=dict(self.spec,base_tf='5m');q=dict(self.q,base_tf='5m');qkey=self.qkey[:-3]+'5m';self.s.set_setting(qkey,q)
  self.s.set_setting(self.qkey,dict(self.q,state='READY'))
  candidate=dict(analysis_ready=False,closed_inputs={'series':[{'spec':spec},{'spec':dict(spec,isin='INDEX:SOX')}]})
  self.s.set_setting('cp_eligibility:'+self.isin,dict(isin=self.isin,candidates=[candidate]))
  self.plan();self.assertEqual(self.task()['problem']['tf'],'5m');self.assertEqual(len(self.jobs()),1)
 def test_three_failed_attempts_then_wait_for_new_evidence_or_weekly_due(self):
  self.plan()
  for n in range(3):
   at=self.now+pd.Timedelta(minutes=n*20)
   report=dict(state='UNCONFIRMED',ranges=[self.span],recovery_provider_attempted=True,next_retry=(at+pd.Timedelta(minutes=5)).isoformat())
   with patch('data_quality.repair',return_value=report) as repair:
    result=cp.service_request(self.info,self.s,Mock(),at)
    if result is None:result=cp.service_request(self.info,self.s,Mock(),at)
   repair.assert_called_once();self.assertEqual(result['attempts'],n+1)
  self.assertEqual(result['state'],'BLOCKED');self.assertIsNotNone(result['recheck_after']);self.assertIsNone(cp.due_request(self.s.db,self.isin,at+pd.Timedelta(days=1)))
  self.assertEqual(self.plan(at+pd.Timedelta(days=1))['state'],'CHECKED');self.assertIsNone(self.s.setting('cp_manual_risk:'+self.isin))
 def test_retry_after_blocks_network_without_consuming_attempt(self):
  self.plan();self.s.set_setting('cp_source_retry:gettex',(self.now+pd.Timedelta(hours=1)).isoformat())
  with patch('data_quality.repair',return_value=dict(state='UNCONFIRMED',ranges=[self.span])) as repair:result=cp.service_request(self.info,self.s,Mock(),self.now)
  self.assertTrue(repair.call_args.kwargs['local_only']);self.assertEqual(result['state'],'WAITING');self.assertEqual(result['attempts'],0)
  self.assertIsNone(cp.due_request(self.s.db,self.isin,self.now))
 def test_daily_subset_is_revalidated_and_changed_identity_or_resolved_range_skipped(self):
  q=dict(self.q,ranges=[['2026-11-06 11:15:00','2026-11-09 11:15:00']]);self.s.set_setting(self.qkey,q)
  self.plan();job=self.task();self.assertLess(pd.Timestamp(job['problem']['range'][1])-pd.Timestamp(job['problem']['range'][0]),pd.Timedelta(days=1))
  with patch('data_quality.repair',return_value=dict(state='READY',ranges=[])) as repair:
   result=cp.service_request(self.info,self.s,Mock(),self.now)
  repair.assert_called_once();self.assertEqual(result['state'],'COMPLETED')
  self.s.set_setting(self.key,{});self.plan();self.s.set_setting(self.qkey,dict(q,identity=dict(self.meta,currency='USD')))
  with patch('data_quality.repair') as repair:
   result=cp.service_request(self.info,self.s,Mock(),self.now)
   if result is None:result=cp.service_request(self.info,self.s,Mock(),self.now)
  repair.assert_not_called();self.assertEqual(result['state'],'SUPERSEDED')
 def test_manual_request_priority_and_automatic_regular_turn_alternation(self):
  self.plan();self.assertIsNone(cp.due_request(self.s.db,now=self.now,include_automatic=False))
  p=copy.deepcopy(self.task()['problem']);p['range']=['2026-11-09 12:15:00']*2
  with self.s.connect() as db:cp._queue_repair(db,p,self.now.isoformat())
  self.assertEqual(cp.due_request(self.s.db,now=self.now)['problem']['range'],p['range'])
  jobs=self.jobs();jobs.pop(cp.repair_id(p));self.s.set_setting(self.key,jobs)
  with patch('data_quality.repair',return_value=dict(state='UNCONFIRMED',ranges=[self.span],next_retry=self.now.isoformat())) as repair:
   first=cp.service_request(self.info,self.s,Mock(),self.now)
   second=cp.service_request(self.info,self.s,Mock(),self.now)
  self.assertEqual(first['state'],'WAITING');self.assertIsNone(second);repair.assert_called_once()
 def test_fresh_route_neither_plans_nor_consumes_repairs(self):
  with ExitStack() as stack:
   self.stages(stack);planner=stack.enter_context(patch.object(cp,'automatic_repair_turn'))
   FeedContext.calculate(self.host(),dict(self.info,_gettex_poll_only=True))
  planner.assert_not_called();self.assertFalse(self.jobs())
 def test_revoked_source_policy_does_not_execute_saved_automatic_request(self):
  self.plan();before=copy.deepcopy(self.jobs());self.s.set_setting('cp_gettex_source_policy',{})
  with patch('data_quality.repair') as repair:
   self.assertEqual(self.plan()['state'],'NOT_ENABLED');self.assertIsNone(cp.service_request(self.info,self.s,Mock(),self.now))
  repair.assert_not_called();self.assertEqual(self.jobs(),before)
 def test_bounded_scan_rotates_more_than_four_terminal_ranges(self):
  spans=[[f'2026-11-09 {h:02d}:15:00']*2 for h in range(8,16)];self.s.set_setting(self.qkey,dict(self.q,ranges=spans))
  seen=set()
  for _ in range(8):
   result=self.plan();self.assertEqual(result['state'],'QUEUED');seen.add(result['request_id'])
   jobs=self.jobs();jobs[result['request_id']].update(state='BLOCKED',recheck_after=(self.now+pd.Timedelta(days=7)).isoformat());self.s.set_setting(self.key,jobs)
  self.assertEqual(len(seen),8);self.assertLessEqual(self.plan()['checked'],4)
 def test_53_and_200_stocks_get_regular_repair_slots_without_owner_lane_flood(self):
  import time
  timings=[]
  for size in (53,200):
   self.s.db.execute('DELETE FROM settings WHERE key LIKE "cp_repair_requests:%"');self.s.db.commit()
   started=time.perf_counter();infos=[]
   for n in range(size):
    isin='US'+str(n).zfill(10);info=dict(isin=isin);infos.append(info);meta=dict(self.meta,symbol=isin)
    self.s.db.execute('INSERT OR REPLACE INTO instruments VALUES (?,?)',(isin,json.dumps(info)));self.s.db.commit()
    self.s.set_setting('data_quality:'+isin+':'+isin+':15m',dict(self.q,identity=meta))
    spec=dict(self.spec,isin=isin,identity=meta)
    self.s.set_setting('cp_eligibility:'+isin,dict(isin=isin,timeframes={'4H':dict(blockers=['blocked'],_closed_input=spec)}))
    self.assertEqual(cp.automatic_repair_turn(self.s,info,self.now)['state'],'QUEUED')
   self.assertIsNone(cp.due_request(self.s.db,now=self.now,include_automatic=False))
   with patch('data_quality.repair',return_value=dict(state='UNCONFIRMED',ranges=[self.span],next_retry=self.now.isoformat())) as repair:
    for info in infos:self.assertEqual(cp.service_request(info,self.s,Mock(),self.now)['state'],'WAITING')
    for info in infos:self.assertIsNone(cp.service_request(info,self.s,Mock(),self.now))
   self.assertEqual(repair.call_count,size);timings.append((size,round(time.perf_counter()-started,3)))
  print('Automatic repair fairness (memory, seconds):',timings)

 def real_fixture(self):
  import data_quality as dq
  from performance_cache import install_input_revisions
  from candle_timing import install
  from history_service import attach_parent_provenance
  archive=Path(__file__).parent.parent.parent/'СМ-Архив'
  fixture=json.loads((archive/'CP автоматический адресный recovery реальные свечи.json').read_text(encoding='utf8'))
  rows=fixture['rows'];self.span=[rows[1][0]]*2
  self.s.set_setting(self.qkey,dict(self.q,ranges=[self.span]))
  self.s.db.executemany('INSERT INTO candles VALUES (?,?,?,?,?,?,?,?,?)',[(self.isin,'gettex','15m',*r) for r in (rows[0],rows[2])])
  install_input_revisions(self.s.db);install(self.s.db);self.s.db.commit()
  def token(scopes):return {k:(self.s.db.execute('SELECT revision FROM input_revisions WHERE scope=?',(k,)).fetchone() or [0])[0] for k in scopes}
  self.s.read_cache=SimpleNamespace(token_for=token,depend=lambda *a:None)
  def load(isin,base='15m'):
   data=self.s.db.execute("SELECT ts,open,high,low,close,volume FROM candles WHERE isin=? AND base_tf=? AND source='gettex' ORDER BY ts",(isin,base)).fetchall()
   frame=pd.DataFrame(data,columns=['ts',*dq.FIELDS]).set_index('ts');frame.index=pd.to_datetime(frame.index);frame.attrs=self.meta.copy()
   return attach_parent_provenance(self.s,dq.attach_interval_evidence(self.s,frame,isin),isin,base)
  self.s.load_official=load;self.s.load_series=lambda *a,**k:pd.DataFrame()
  reply=pd.DataFrame([rows[1][1:]],columns=dq.FIELDS,index=pd.to_datetime([rows[1][0]]));reply.attrs=self.meta.copy()
  return rows,reply
 def test_saved_real_gettex_missing_row_is_recovered_once_then_warmup_stays_blocked(self):
  import data_quality as dq
  import calculation_window
  rows,reply=self.real_fixture();self.plan();fetch=Mock(return_value=reply)
  with patch('requests.get',side_effect=AssertionError('No external HTTP in replay')):
   result=cp.service_request(self.info,self.s,fetch,self.now)
  self.assertEqual(result['state'],'COMPLETED');fetch.assert_called_once()
  self.assertEqual(self.s.db.execute('SELECT ts,open,high,low,close,volume FROM candles ORDER BY ts').fetchall(),[tuple(r) for r in rows])
  self.assertEqual(self.s.db.execute('SELECT count(*) FROM candle_recoveries').fetchone()[0],1)
  self.assertEqual(self.plan()['state'],'NO_ADDRESSABLE_DEPENDENCY');self.assertIsNone(cp.service_request(self.info,self.s,fetch,self.now))
  self.assertEqual(fetch.call_count,1);self.assertIsNone(self.s.setting('cp_manual_risk:'+self.isin))
  frame=self.s.load_official(self.isin);frame.attrs.update(timeframe='15m',data_quality=self.s.setting(self.qkey))
  admission=calculation_window.inspect(self.info,frame,'15m',self.now)
  self.assertEqual(admission['status'],'BLOCKED');self.assertIn('INSUFFICIENT_CLOSED_BARS',admission['blockers'])
 def test_changed_inputs_during_real_recovery_prevent_ohlcv_write(self):
  rows,reply=self.real_fixture();self.plan()
  def changed(*a):
   self.s.db.execute('UPDATE instruments SET payload=? WHERE isin=?',(json.dumps(dict(self.info,name='changed')),self.isin));self.s.db.commit();return reply
  with patch('requests.get',side_effect=AssertionError('No external HTTP in replay')):
   result=cp.service_request(self.info,self.s,changed,self.now)
  self.assertNotEqual(result['state'],'COMPLETED');self.assertEqual(self.s.db.execute('SELECT count(*) FROM candles').fetchone()[0],2)
  if self.s.db.execute("SELECT 1 FROM sqlite_master WHERE name='candle_recoveries'").fetchone():
   self.assertEqual(self.s.db.execute('SELECT count(*) FROM candle_recoveries').fetchone()[0],0)

 def test_short_provider_wait_keeps_deadline_then_records_unconfirmed_retry(self):
  import data_quality as dq
  rows,reply=self.real_fixture();self.plan()
  wait=self.now+pd.Timedelta(seconds=30)
  self.s.set_setting('cp_source_retry:gettex',wait.isoformat())
  fetch=Mock(return_value=reply.iloc[:0].copy())
  with patch('requests.get',side_effect=AssertionError('No external HTTP in replay')):
   first=cp.service_request(self.info,self.s,fetch,self.now)
   fetch.assert_not_called()
   self.assertEqual(first['state'],'WAITING');self.assertEqual(first['attempts'],0)
   self.assertEqual(pd.Timestamp(first['next_retry']),wait)
   self.assertIn('WAITING_PROVIDER',first['reason'])
   self.assertEqual(self.s.setting(self.qkey)['recovery_wait'],'PROVIDER')
   self.assertIsNone(cp.due_request(self.s.db,self.isin,wait-pd.Timedelta(seconds=1)))
   self.assertIsNotNone(cp.due_request(self.s.db,self.isin,wait))
   # Keep the existing regular-feed turn between automatic recovery visits.
   self.assertIsNone(cp.service_request(self.info,self.s,fetch,wait))
   # A later authoritative provider wait must still be respected.
   later=wait+pd.Timedelta(seconds=30)
   self.s.set_setting('cp_source_retry:gettex',later.isoformat())
   delayed=cp.service_request(self.info,self.s,fetch,wait)
   self.assertEqual(pd.Timestamp(delayed['next_retry']),later)
   self.assertEqual(delayed['attempts'],0);fetch.assert_not_called()
   self.assertIsNone(cp.service_request(self.info,self.s,fetch,later))
   result=cp.service_request(self.info,self.s,fetch,later)
  fetch.assert_called_once()
  self.assertEqual(result['attempts'],1);self.assertEqual(result['state'],'WAITING')
  self.assertGreater(pd.Timestamp(result['next_retry']),later)
  self.assertTrue(result['reason']);self.assertNotEqual(result['quality'],'READY')
  self.assertEqual(self.jobs()[cp.repair_id(result['problem'])],result)
  self.assertEqual(self.s.db.execute('SELECT count(*) FROM candles').fetchone()[0],2)
  self.assertIsNone(self.s.setting('cp_manual_risk:'+self.isin))

if __name__=='__main__':unittest.main()
