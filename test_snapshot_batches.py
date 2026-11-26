"""Bounded aggregate requests retain scope/cutoff and never reuse stale output."""
import json,sqlite3,sys,unittest,tempfile
from pathlib import Path
from contextlib import contextmanager
from unittest.mock import patch
from types import SimpleNamespace
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
from snapshot_worker import run_cycle,_calculate


class Store:
 def __init__(self,count):
  self.path='fixture';self.values={};self.db=sqlite3.connect(':memory:')
  self.db.execute('CREATE TABLE input_revisions(scope TEXT PRIMARY KEY,revision INTEGER)')
  self.db.executemany('INSERT INTO input_revisions VALUES (?,1)',[('instrument:'+str(i),) for i in range(count)])
  self.db.commit()
 def setting(self,key,default=None):return self.values.get(key,default)
 def set_setting(self,key,value):self.values[key]=dict(value)
 @contextmanager
 def connect(self):
  self.db.execute('PRAGMA query_only=ON')
  try:yield self.db
  finally:self.db.rollback()


def inputs(count):
 return dict(watchlist=[dict(isin=str(i),name='Stock '+str(i)) for i in range(count)],
             symbol='Stock 0',timeframe='4H',period='Max',drafts={},feed_errors={})


class Client:
 def __init__(self,fail=(),wrong_cutoff=(),wrong_view=(),mutate=None):
  self.calls=[];self.fail=set(fail);self.wrong_cutoff=set(wrong_cutoff);self.wrong_view=set(wrong_view);self.mutate=mutate
 def calculate(self,kind,values,last_client=None,progress=None):
  self.calls.append(values);isin=values['_snapshot_targets'][0]
  if self.mutate:self.mutate(len(self.calls))
  if isin in self.fail:raise RuntimeError('READ_CALCULATION_TIMEOUT:snapshot')
  guard=dict(version=1,revisions={'instrument:'+isin:1},strict={'instrument:'+isin:1},settings={},settings_start={},settings_projection={},series=[])
  stock=dict(isin=isin,name='Stock '+isin,timeframe='4H',candles=[],count=0,
             _closed_inputs=guard,trading_engine={'status':'READY','signal':'WAIT'})
  if progress:progress(dict(phase='CALCULATED',completed=1,total=1))
  return dict(stocks=[stock],selected=dict(isin='0',timeframe='4H',period='Max'),ready=True,_snapshot_view_id='other' if isin in self.wrong_view else 'fixture',
              market_context={'state':'NO_BENCHMARK'},_calculation_timing={},
              analysis_snapshot={'captured_at':'wrong' if isin in self.wrong_cutoff else values['_analysis_cycle_at'],'as_of':[]})


class Tests(unittest.TestCase):
 def run_fixture(self,count,client):
  store=Store(count)
  try:
   with patch('revision_diagnostics.safe_observe'):
    result=run_cycle(client,store,inputs(count),None)
   return result,store.values
  finally:store.db.close()
 def test_53_and_200_serial_requests_share_cutoff_and_keep_full_context(self):
  for count in (53,200):
   c=Client();result,settings=self.run_fixture(count,c)
   self.assertEqual(len(c.calls),count)
   self.assertTrue(all(len(v['watchlist'])==count and len(v['_snapshot_targets'])==1 for v in c.calls))
   self.assertEqual(len({v['_analysis_cycle_at'] for v in c.calls}),1)
   self.assertEqual([s['isin'] for s in result['stocks']],[str(i) for i in range(count)])
   self.assertEqual(result['analysis_snapshot']['validated_instruments'],count)
   self.assertFalse(result['analysis_snapshot']['execution_permission'])
   self.assertEqual(result['diagnostic_status'],'COMPLETE')
   self.assertEqual(settings['analysis_cycle']['state'],'COMPUTED')
 def test_one_slow_instrument_does_not_discard_other_results_or_grant_ready(self):
  c=Client(fail={'3'});r,_=self.run_fixture(53,c)
  self.assertEqual(len(c.calls),53);self.assertEqual(len(r['stocks']),53)
  self.assertEqual(r['diagnostic_status'],'COMPLETE_WITH_LIMITS');self.assertFalse(r['ready'])
  self.assertEqual(r['stocks'][3]['input_state'],'DIAGNOSTIC_UNAVAILABLE')
  self.assertEqual(r['stocks'][3]['trading_engine']['signal'],'WAIT')
  self.assertNotIn('data_quality',r['stocks'][3])
  self.assertEqual(r['analysis_snapshot']['validated_instruments'],52)
 def test_relevant_input_changed_between_batches_revokes_only_affected_stock(self):
  store=Store(53)
  def mutate(turn):
   if turn==2:store.db.execute("UPDATE input_revisions SET revision=2 WHERE scope='instrument:0'");store.db.commit()
  try:
   with patch('revision_diagnostics.safe_observe'):
    r=run_cycle(Client(mutate=mutate),store,inputs(53),None)
   self.assertEqual(r['stocks'][0]['input_state'],'INVALIDATED')
   self.assertEqual(r['stocks'][0]['trading_engine']['signal'],'WAIT')
   self.assertEqual(r['analysis_snapshot']['validated_instruments'],52)
   self.assertEqual(r['stocks'][1]['trading_engine']['status'],'READY')
  finally:store.db.close()
 def test_wrong_cutoff_cannot_be_relabelled_as_same_analysis(self):
  c=Client(wrong_cutoff={'2'});r,_=self.run_fixture(53,c)
  self.assertEqual(r['stocks'][2]['input_state'],'DIAGNOSTIC_UNAVAILABLE')
  self.assertEqual(r['analysis_snapshot']['validated_instruments'],52)
 def test_restarted_sql_view_cannot_masquerade_as_original_capture(self):
  c=Client(wrong_view={'2'});r,_=self.run_fixture(53,c)
  self.assertEqual(r['stocks'][2]['input_state'],'DIAGNOSTIC_UNAVAILABLE')
  self.assertEqual(r['stocks'][2]['diagnostic_status'],'SNAPSHOT_CAPTURE_VIEW_LOST')
  self.assertEqual(r['analysis_snapshot']['validated_instruments'],52)
 def test_late_loaded_rows_excluded_across_batches_until_next_capture(self):
  from compute_process import SnapshotReads
  from performance_cache import ReadCache
  archive=Path(__file__).resolve().parents[2]/'СМ-Архив'
  with tempfile.NamedTemporaryFile(prefix='snapshot-wal-',suffix='.sqlite3',dir=archive,delete=False) as f:path=Path(f.name)
  writer=sqlite3.connect(path);writer.execute('PRAGMA journal_mode=WAL')
  writer.executescript('CREATE TABLE input_revisions(scope TEXT PRIMARY KEY,revision INTEGER); CREATE TABLE facts(at TEXT,value INTEGER); INSERT INTO facts VALUES("2026-11-01",1);')
  cache=ReadCache(path);reads=SnapshotReads(SimpleNamespace(path=path,read_cache=cache))
  try:
   with reads.scope('capture-A') as (captured,first_id):
    first_db=captured.read_cache.db
    self.assertEqual(first_db.execute('SELECT count(*) FROM facts').fetchone()[0],1)
   writer.execute('INSERT INTO facts VALUES("2026-11-02",2)');writer.commit()
   with reads.scope('capture-A',last=True) as (captured,next_id):
    self.assertEqual(first_id,next_id)
    self.assertEqual(captured.read_cache.db.execute('SELECT count(*) FROM facts').fetchone()[0],1)
    with self.assertRaises(sqlite3.DatabaseError):captured.read_cache.db.execute('DELETE FROM facts')
   with self.assertRaises(sqlite3.ProgrammingError):first_db.execute('SELECT 1')
   with reads.scope('capture-B',last=True) as (captured,last_id):
    self.assertNotEqual(last_id,first_id)
    self.assertEqual(captured.read_cache.db.execute('SELECT count(*) FROM facts').fetchone()[0],2)
  finally:
   reads.close();cache.close();writer.close()
   for p in (path,Path(str(path)+'-wal'),Path(str(path)+'-shm')):p.unlink(missing_ok=True)
 def test_all_failed_requests_never_create_ready_envelope(self):
  store=Store(17)
  try:
   with patch('revision_diagnostics.safe_observe'),self.assertRaisesRegex(RuntimeError,'SNAPSHOT_ALL_INSTRUMENTS_UNAVAILABLE'):
    run_cycle(Client(fail=map(str,range(17))),store,inputs(17),None)
   self.assertEqual(store.values['analysis_cycle']['state'],'FAILED')
  finally:store.db.close()
 def test_unknown_or_duplicate_targets_rejected_before_calculation(self):
  for wanted in ([],['missing'],['0','0']):
   with self.assertRaisesRegex(ValueError,'INVALID_SNAPSHOT_TARGETS'):
    _calculate(None,{**inputs(53),'_snapshot_targets':wanted},None)
 def test_parent_final_validation_uses_query_only_transaction(self):
  import performance_cache as pc
  store=Store(17);original=pc.validate_closed_inputs;seen=[]
  @contextmanager
  def ordinary_connection():
   try:yield store.db
   finally:store.db.rollback()
  store.connect=ordinary_connection
  def validate(db,proof,memo=None):
   seen.append((db.in_transaction,db.execute('PRAGMA query_only').fetchone()[0]))
   with self.assertRaises(sqlite3.OperationalError):db.execute('DELETE FROM input_revisions')
   return original(db,proof,memo)
  try:
   with patch('performance_cache.validate_closed_inputs',side_effect=validate),patch('revision_diagnostics.safe_observe'):
    r=run_cycle(Client(),store,inputs(17),None)
   self.assertEqual(r['analysis_snapshot']['validated_instruments'],17)
   self.assertEqual(seen,[(True,1)]*17)
   self.assertEqual(store.db.execute('SELECT count(*) FROM input_revisions').fetchone()[0],17)
  finally:store.db.close()
 def test_revision_diagnostic_connections_close_even_on_empty_early_return(self):
  import revision_diagnostics as rd
  archive=Path(__file__).resolve().parents[2]/'СМ-Архив'
  with tempfile.NamedTemporaryFile(prefix='revision-handles-',suffix='.sqlite3',dir=archive,delete=False) as f:path=Path(f.name)
  real_connect=sqlite3.connect;opened=[]
  class Tracked(sqlite3.Connection):
   def close(self):self.closed_explicitly=True;super().close()
  def connect(*args,**kwargs):
   db=real_connect(*args,**kwargs,factory=Tracked);opened.append(db);return db
  try:
   with patch.object(rd.sqlite3,'connect',connect):
    self.assertEqual(rd.probes(path),[])
    self.assertEqual(rd.history(path),[])
    self.assertEqual(rd._saved_records(path,['A']),{})
   self.assertEqual(len(opened),3)
   self.assertTrue(all(db.closed_explicitly for db in opened))
   for db in opened:
    with self.assertRaises(sqlite3.ProgrammingError):db.execute('SELECT 1')
  finally:path.unlink()


if __name__=='__main__':unittest.main()
