"""Only new saved-observation scheduling cases; no market requests or old suites."""
import sys,json,sqlite3,unittest,threading,time
from pathlib import Path
from contextlib import contextmanager
from unittest.mock import patch
import pandas as pd
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
import candle_schedule as cs
import cp_reconciliation as rv
from cp_controls import write,read,digest

class MemoryStore:
 def __init__(self):
  self.db=sqlite3.connect(':memory:')
  self.db.executescript('CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT);CREATE TABLE instruments(isin TEXT PRIMARY KEY,payload TEXT);CREATE TABLE candles(isin,source,base_tf,ts,open,high,low,close,volume);CREATE TABLE series(isin,source,venue,currency,symbol,base_tf,ts,open,high,low,close,volume);')
 @contextmanager
 def connect(self):
  with self.db:yield self.db
 def setting(self,k,default=None):return read(self.db,k,default)
 def set_setting(self,k,v):
  with self.db:write(self.db,k,v)

class ObservationPriority(unittest.TestCase):
 def setUp(self):
  self.s=MemoryStore();self.addCleanup(self.s.db.close)
  self.now=pd.Timestamp('2026-11-02T21:45Z');self.infos=[{'isin':'US00287Y1091'},{'isin':'US0378331005'}]
  for i in self.infos:self.s.db.execute('INSERT INTO instruments VALUES (?,?)',(i['isin'],json.dumps(i)))
  self.s.db.commit()
 def add(self,isin=None,at='2026-11-02T20:47Z'):
  isin=isin or self.infos[0]['isin'];m=cs.gettex_meta(isin)
  j=rv._spec(isin,m,'15m','2026-11-02 12:45:00','2026-11-02 12:45:00')
  j.update(status='AWAITING_USER',created_at='2026-11-02T19:18Z')
  with self.s.connect() as d:
   inputs=rv._input(d,j);rv._save_job(d,j)
   rv._record_observation(d,j,inputs,'ABSENT','GETTEX:4AB','fixture',[],at)
  return j
 def due(self):return rv.due_observation(self.s.db,self.infos,self.now)
 def test_saved_evidence_receives_priority_without_any_write(self):
  j=self.add();before=self.s.db.iterdump();before=list(before)
  p=self.due();self.assertEqual(p['job_id'],j['id']);self.assertEqual(p['reason'],'SAVED_OBSERVATION_RECHECK')
  self.assertEqual(before,list(self.s.db.iterdump()));self.assertIsNone(self.s.setting('cp_manual_risk:'+j['isin']))
 def test_completed_future_and_missing_evidence_not_prioritized(self):
  j=self.add()
  for changes in ({'status':'AWAITING_SOURCE'},{'next_check':'2026-11-02T22:00Z'},{'observation':{}},{'observation':{'at':'2026-11-02T20:47Z','identity_confirmed':False}}):
   with self.subTest(changes=changes):
    self.s.set_setting(rv.key(j),{**j,**changes});self.assertIsNone(self.due())
 def test_instrument_removed_or_benchmark_not_scheduled(self):
  self.add();self.assertIsNone(rv.due_observation(self.s.db,[self.infos[1]],self.now))
  self.assertIsNone(rv.due_observation(self.s.db,[{**self.infos[0],'kind':'benchmark'}],self.now))
 def test_oldest_observation_first_and_bounded_turn_spacing(self):
  a=self.add();b=self.add(self.infos[1]['isin'],'2026-11-02T21:00Z')
  self.assertEqual(self.due()['isin'],a['isin'])
  p=self.s.setting(rv.PROGRESS+a['isin']);p['last_turn']=self.now.isoformat();self.s.set_setting(rv.PROGRESS+a['isin'],p)
  self.assertEqual(self.due()['isin'],b['isin'])
  self.assertEqual(rv.due_observation(self.s.db,self.infos,self.now+pd.Timedelta(minutes=5))['isin'],a['isin'])
 def plan(self,urgent=None):
  with patch('cp_controls.due_request',return_value=urgent),patch('cp_controls.historical_review_due',return_value=[]),patch('calculation_window.eligibility_due',return_value=[]),patch.object(cs,'fresh_candidate',return_value=(None,{})):
   return cs.planned_feeds(self.s,self.infos,self.now)
 def test_existing_owner_recovery_precedes_review_without_removing_history(self):
  self.add();p=self.plan({'problem':{'isin':self.infos[1]['isin']}})
  self.assertEqual(p['priority'],self.infos[1]);self.assertIsNone(p['observation_recheck'])
  self.assertEqual(len(p['feeds']),2)
  p=self.plan();self.assertEqual(p['priority'],self.infos[0]);self.assertEqual(len(p['feeds']),2)
 def test_changed_native_rows_not_authorized_by_scheduling(self):
  j=self.add();self.assertIsNotNone(self.due())
  with self.s.connect() as d:d.execute('INSERT INTO candles VALUES (?,?,?,?,?,?,?,?,?)',(j['isin'],'gettex','15m',j['first'],100,99,101,100,1))
  with patch('gettex_history.attach_known_interruptions',return_value=False):rv.turn(self.s,self.infos[0],self.now)
  after=self.s.setting(rv.key(j));self.assertFalse(after['finding']['observation_current'])
  self.assertFalse(after['finding']['can_affect_dq']);self.assertIsNone(self.s.setting('cp_manual_risk:'+j['isin']))
 def test_changed_instrument_identity_is_not_auto_granted(self):
  j=self.add();self.assertIsNotNone(self.due())
  with self.s.connect() as d:d.execute('UPDATE instruments SET payload=? WHERE isin=?',(json.dumps({**self.infos[0],'name':'changed'}),j['isin']))
  with patch('gettex_history.attach_known_interruptions',return_value=False):rv.turn(self.s,self.infos[0],self.now)
  self.assertFalse(self.s.setting(rv.key(j))['finding']['observation_current'])
  self.assertIsNone(self.s.setting('cp_manual_risk:'+j['isin']))
 def queue(self,count):
  from performance_dispatcher import Dispatcher
  d=Dispatcher(lambda cb,v:cb(v),sample=lambda _:({'cpu':0,'ram':0},None));gate=threading.Event();entered=threading.Event();done=threading.Event();order=[];errors=[]
  def record(n):
   order.append(n)
   if len(order)==count:done.set()
  try:
   d.submit(lambda:(entered.set(),gate.wait(5)),lambda _:None,errors.append,key='running',lane='feed_update');self.assertTrue(entered.wait(3))
   for n in range(count):d.submit(lambda n=n:n,record,errors.append,key=str(n),lane='feed_update',isin=str(n))
   self.assertFalse(d.prioritize_feed('running'));self.assertTrue(d.prioritize_feed(str(count-1)))
   self.assertFalse(d.submit(lambda:None,record,errors.append,key=str(count-1),lane='feed_update'))
   gate.set();self.assertTrue(done.wait(5));self.assertFalse(errors)
   self.assertEqual(order[0],count-1);self.assertEqual(sorted(order),list(range(count)))
  finally:
   gate.set();d.close();d.monitor.join(3)
   for t in d.threads:t.join(3)
 def test_priority_existing_serial_queue_53(self):self.queue(53)
 def test_priority_existing_serial_queue_200(self):self.queue(200)

if __name__=='__main__':unittest.main(verbosity=2)
