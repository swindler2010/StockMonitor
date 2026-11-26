"""New TV-presence to existing recovery handoff, isolated memory SQLite."""
import sys,json,copy,unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
from test_cp_fresh_fair_share import Memory
import cp_reconciliation as rv
import cp_controls as cc
import pandas as pd

class ReviewRecoveryRoute(unittest.TestCase):
 def setUp(self):
  self.s=Memory();self.addCleanup(self.s.db.close)
  self.s.db.executescript('CREATE TABLE instruments(isin TEXT PRIMARY KEY,payload TEXT);CREATE TABLE candles(isin TEXT,source TEXT,base_tf TEXT,ts TEXT,open REAL,high REAL,low REAL,close REAL,volume REAL);')
  self.info={'isin':'NL0010273215','name':'ASML','kind':'stock'}
  self.meta=dict(source='gettex',symbol=self.info['isin'],venue='GETTEX',currency='EUR',timezone='Europe/Berlin')
  self.s.db.execute('INSERT INTO instruments VALUES (?,?)',(self.info['isin'],json.dumps(self.info)))
  self.s.db.execute('INSERT INTO candles VALUES (?,?,?,?,?,?,?,?,?)',(self.info['isin'],'gettex','15m','2026-11-03 08:15:00',1362,1366,1362,1364.8,14));self.s.db.commit()
  self.spec=rv._spec(self.info['isin'],self.meta,'15m','2026-11-03 08:30:00','2026-11-03 08:30:00')
  self.report_key='data_quality:'+self.info['isin']+':'+self.info['isin']+':15m'
  self.s.set_setting(self.report_key,dict(state='UNCONFIRMED',identity=self.meta,base_tf='15m',ranges=[[self.spec['first'],self.spec['last']]],issues=['saved gap']))
  self.request_key='cp_repair_requests:'+self.info['isin'];self.now=pd.Timestamp('2026-11-03T11:45Z')
  guard=patch('gettex_history.attach_known_interruptions',return_value=False);guard.start();self.addCleanup(guard.stop)
 def observe(self,result='PRESENT'):
  with self.s.connect() as db:
   db.execute('BEGIN IMMEDIATE');j,_=rv._enqueue(db,self.spec,{'kind':'fixture'},self.now);n=rv._input(db,j)
   rv._record_observation(db,j,n,result,'GETTEX:ASME','isolated fixture',[],self.now.isoformat())
 def turn(self):return rv.turn(self.s,self.info,self.now)
 def requests(self):return self.s.setting(self.request_key,{})
 def test_present_missing_interval_enters_existing_queue_without_data_or_grant_writes(self):
  self.observe();before=self.s.db.execute('select * from candles').fetchall();report=self.s.setting(self.report_key)
  self.turn();items=list(self.requests().values());self.assertEqual(len(items),1)
  self.assertEqual(items[0]['state'],'QUEUED');self.assertEqual(items[0]['problem']['range'],[self.spec['first'],self.spec['last']])
  self.assertEqual(items[0]['origin']['kind'],'CURRENT_TV_PRESENCE');self.assertEqual(report,self.s.setting(self.report_key))
  self.assertEqual(before,self.s.db.execute('select * from candles').fetchall());self.assertIsNone(self.s.setting('cp_manual_risk:'+self.info['isin']))
 def test_absent_different_and_unconfirmed_do_not_request_prices(self):
  for result in ['ABSENT','DIFFERENT','UNCONFIRMED','ZERO_VOLUME']:
   with self.subTest(result=result):self.observe(result);self.turn();self.assertFalse(self.requests())
 def test_changed_native_neighbor_does_not_enqueue_stale_observation(self):
  self.observe();self.s.db.execute('UPDATE candles SET volume=15');self.s.db.commit();self.turn();self.assertFalse(self.requests())
 def test_rows_already_present_are_not_downloaded_again(self):
  self.observe();self.s.db.execute('INSERT INTO candles VALUES (?,?,?,?,?,?,?,?,?)',(self.info['isin'],'gettex','15m',self.spec['first'],1362.8,1365.4,1355.6,1364.,122));self.s.db.commit();self.turn();self.assertFalse(self.requests())
 def test_different_report_identity_and_range_do_not_match(self):
  self.observe();original=self.s.setting(self.report_key)
  for modified in [dict(original,identity=dict(self.meta,venue='FRA')),dict(original,identity=dict(self.meta,currency='USD')),dict(original,ranges=[['2026-11-02 08:30:00','2026-11-02 08:30:00']]),dict(original,state='READY')]:
   with self.subTest(report=modified):
    self.s.set_setting(self.report_key,modified);self.observe();self.turn();self.assertFalse(self.requests())
 def test_existing_waiting_request_keeps_cooldown_and_attempts(self):
  self.observe();problem=cc.problems(self.s.db,self.info['isin'])[0];key=cc.repair_id(problem)
  prior=dict(problem=problem,state='WAITING',requested_at='2026-11-03T10:00Z',next_retry='2026-11-03T14:00Z',attempts=3)
  self.s.set_setting(self.request_key,{key:prior});self.turn();self.assertEqual(self.requests()[key],prior)
 def test_repeated_same_evidence_does_not_restart_finished_failure(self):
  self.observe();self.turn();requests=self.requests();self.assertEqual(len(requests),1)
  key=next(iter(requests));requests[key].update(state='BLOCKED',attempts=1,reason='provider unavailable');self.s.set_setting(self.request_key,requests)
  self.now+=pd.Timedelta(hours=2);self.turn();self.assertEqual(self.requests(),requests)
 def test_native_service_obeys_source_cooldown_after_automatic_handoff(self):
  self.observe();self.turn();deadline='2026-11-03T14:00:00+00:00'
  self.s.set_setting('cp_source_retry:gettex',deadline)
  with patch('data_quality.repair',side_effect=AssertionError('must not request during cooldown')):
   receipt=cc.service_request(self.info,self.s,lambda *args: self.fail('network forbidden'),self.now)
  self.assertEqual(receipt['state'],'WAITING');self.assertEqual(receipt['next_retry'],deadline)
  self.assertEqual(receipt['attempts'],0)
 def test_existing_owner_retry_remains_explicit_and_deduplicated(self):
  problem=cc.problems(self.s.db,self.info['isin'])[0]
  first=cc.request_repair(self.s,problem);self.assertEqual(cc.request_repair(self.s,problem),first)
  requests=self.requests();key=cc.repair_id(problem);requests[key].update(state='BLOCKED',attempts=2);self.s.set_setting(self.request_key,requests)
  retried=cc.request_repair(self.s,problem);self.assertEqual(retried['state'],'QUEUED');self.assertEqual(retried['attempts'],0)

if __name__=='__main__':unittest.main(verbosity=2)
