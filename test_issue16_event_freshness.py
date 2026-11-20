"""Real event clocks versus replay/receipt/bar time. RAM-only diagnostic DBs."""
import sys,json,sqlite3,unittest
from pathlib import Path
from contextlib import contextmanager
from unittest.mock import patch
import pandas as pd
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
import delivery_observations as obs
import freshness_view as view

ISIN='US0378331005'
HASHES={'munc':'a'*64,'mund':'b'*64}

class EventFreshnessTests(unittest.TestCase):
 def setUp(self):
  self.main=sqlite3.connect(':memory:');self.journal=sqlite3.connect(':memory:')
  self.addCleanup(self.main.close);self.addCleanup(self.journal.close)
  self.main.executescript('CREATE TABLE instruments(isin TEXT,payload TEXT);CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT);CREATE TABLE series(isin TEXT,source TEXT,venue TEXT,currency TEXT,symbol TEXT,base_tf TEXT);')
  self.main.execute('INSERT INTO instruments VALUES (?,?)',(ISIN,json.dumps(dict(isin=ISIN,name='Apple'))));self.main.commit()
  @contextmanager
  def close(db):
   try:yield db
   finally:db.rollback();db.execute('PRAGMA query_only=OFF')
  def connect(path,**kwargs):return self.journal if 'delivery_observations' in str(path) else self.main
  self.enterContext(patch.object(obs.sqlite3,'connect',side_effect=connect))
  self.enterContext(patch.object(obs,'closing',close));self.enterContext(patch.object(view,'closing',close))
  self.enterContext(patch.object(Path,'exists',return_value=True))
 def frame(self,at='2026-11-20 11:01:00',volume=1):
  f=pd.DataFrame({'price':[100.],'volume':[volume]},index=pd.DatetimeIndex([at]));f.attrs=dict(source='gettex',venue='GETTEX',currency='EUR',symbol=ISIN,timezone='Europe/Berlin');return f
 def record(self,at='2026-11-20 11:01:00',volume=1,checked='2026-11-20T10:15:00Z',anchor='2026-11-20T10:15:00Z'):
  return obs.official_trades('diagnostic-fixture',ISIN,self.frame(at,volume),HASHES,anchor,checked)
 def read(self,now='2026-11-20T10:16:00Z',session='OPEN'):
  return view.collect('diagnostic-fixture',now,session=lambda _:session)
 def test_inclusive_15_20_25_boundaries_all_timeframes(self):
  for minutes in (15,20,25):
   for tf in ('5m','10m','15m','30m','1H','2H','3H','4H','1D','1W','1M'):
    for delta,want in ((-1,'WITHIN_LIMIT'),(0,'WITHIN_LIMIT'),(1,'STALE'),(.000001,'STALE')):
     with self.subTest(minutes=minutes,tf=tf,delta=delta):
      now=pd.Timestamp('2026-11-20T10:01:00Z')+pd.Timedelta(seconds=minutes*60+delta)
      self.assertEqual(view.freshness_state('2026-11-20T10:01:00Z',now.isoformat(),'OPEN',minutes),want)
  self.assertEqual(view.freshness_state(None,'2026-11-20T10:01:00Z','OPEN'),'UNKNOWN')
  self.assertEqual(view.freshness_state('2026-11-20T10:02:00Z','2026-11-20T10:01:00Z','OPEN'),'UNKNOWN')
 def test_exact_event_not_pair_end_and_replay_backfill_restart_cannot_reset(self):
  r=self.record();self.assertEqual(r['confirmed_market_update_at'],'2026-11-20T10:01:00+00:00')
  self.assertEqual(self.read()['stocks'][0]['status'],'WITHIN_LIMIT')
  self.assertEqual(self.record(checked='2026-11-20T10:16:01Z')['tail_observation'],'UNCHANGED')
  self.assertTrue(obs.confirmed_event(self.record(at='2026-11-19 11:02:00',checked='2026-11-20T10:17:00Z',anchor='2026-11-19T10:15:00Z')))
  # A fresh facade reuses only persisted exchange event time, not first receipt.
  r=self.read('2026-11-20T10:16:01Z');self.assertEqual(r['stocks'][0]['status'],'STALE')
  self.assertEqual(r['sources'][0]['confirmed_market_update']['age_seconds'],901)
  self.assertEqual(self.journal.execute('SELECT market_at FROM confirmed_latest').fetchone()[0],'2026-11-20T10:01:00+00:00')
 def test_empty_error_zero_volume_and_other_source_do_not_grant_freshness(self):
  obs.append('diagnostic-fixture',obs.event(ISIN,'trades',self.frame(),checked_at='2026-11-20T10:15:00Z'))
  self.assertEqual(self.read()['stocks'][0]['status'],'UNKNOWN')
  self.assertEqual(self.record(volume=0)['status'],'NO_EXECUTED_TRADE')
  self.assertEqual(self.read()['stocks'][0]['status'],'UNKNOWN')
  row=self.record();row['source']='gettex_chart';self.assertFalse(obs.confirmed_event(row))
  row['source']='gettex';row['market_time_evidence']=[];self.assertFalse(obs.confirmed_event(row))
  self.assertEqual(self.read(session='MARKET_CLOSED')['stocks'][0]['status'],'MARKET_CLOSED')
 def test_failure_does_not_replace_valid_clock_and_no_trading_permission(self):
  self.record()
  row=obs.event(ISIN,'trades',self.frame().iloc[:0],checked_at='2026-11-20T10:17:00Z')
  obs.append('diagnostic-fixture',row)
  r=self.read('2026-11-20T10:17:00Z');self.assertEqual(r['stocks'][0]['status'],'STALE');self.assertFalse(r['trade_permission'])
  self.assertIsNone(r['sources'][0]['response_received']['at'])
 def test_configuration_requires_owner_approval_and_persists_in_store(self):
  self.record()
  def policy(value):
   self.main.execute('INSERT OR REPLACE INTO settings VALUES (?,?)',('market_freshness_policy',json.dumps(value)));self.main.commit()
  policy({'minutes':20});self.assertEqual(self.read()['threshold_minutes'],15)
  policy({'minutes':20,'owner_approved':True});self.assertEqual(self.read('2026-11-20T10:21:00Z')['stocks'][0]['status'],'WITHIN_LIMIT')
  self.assertEqual(self.read('2026-11-20T10:21:01Z')['stocks'][0]['status'],'STALE')
  policy({'minutes':True});self.assertEqual(self.read()['stocks'][0]['status'],'UNKNOWN')
 def test_53_200_identities_stay_separate_and_legacy_is_not_promoted(self):
  for count in (53,200):
   self.main.execute('DELETE FROM instruments')
   for n in range(count):
    isin=f'DE{n:010d}';self.main.execute('INSERT INTO instruments VALUES (?,?)',(isin,json.dumps(dict(isin=isin,name=isin))))
   self.main.commit();r=self.read();self.assertEqual(r['stock_count'],count);self.assertEqual(r['status_counts'],{'UNKNOWN':count});self.assertEqual(r['confirmed_within_minutes'],{'15':0,'20':0,'25':0})

class ProducerIntegrationTests(unittest.TestCase):
 def test_only_committed_valid_source_rows_emit_event_clock(self):
  import test_cp_gettex_universe as fixture
  f=fixture.CachedHistory();f.setUp();self.addCleanup(f.doCleanups)
  def receive(directory,isin,frame,hashes,anchor,checked):
   self.assertFalse(f.s.db.in_transaction)
   self.assertGreater(f.s.db.execute('SELECT count(*) FROM trades WHERE isin=?',(isin,)).fetchone()[0],0)
   self.assertEqual(set(hashes),{'munc','mund'})
  with patch.object(obs,'official_trades',side_effect=receive) as emitted:
   f.run_turn();self.assertEqual(emitted.call_count,1)
   f.run_turn();self.assertEqual(emitted.call_count,1)
 def test_conflicting_slice_never_emits_confirmed_market_clock(self):
  import test_cp_gettex_universe as fixture
  f=fixture.CachedHistory();f.setUp();self.addCleanup(f.doCleanups)
  f.s.db.execute('INSERT INTO candles VALUES (?,?,?,?,?,?,?,?,?)',(ISIN,'gettex','15m','2026-11-03 11:00:00',249,249,249,249,2));f.s.db.commit()
  with patch.object(obs,'official_trades') as emitted:
   f.run_turn();emitted.assert_not_called()

if __name__=='__main__':unittest.main()
