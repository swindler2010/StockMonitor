"""CandlePilot diagnostic transport: isolation, coherent views and exact deltas."""
import hashlib,json,queue,sqlite3,sys,tempfile,time,unittest
from pathlib import Path
from contextlib import closing
from unittest.mock import patch
from urllib.request import Request,urlopen
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
from cp_bridge_diagnostics import Diagnostics,Session,TFS,budget,ReadConnection,MAX_READ_ATTEMPTS
from cp_bridge_comparison import revision_coverage
from performance_cache import install_input_revisions
from test_issue22_mcp import MappedDirectory
from bridge import Bridge

ARCHIVE=Path(__file__).resolve().parents[2]/'СМ-Архив'
if not ARCHIVE.is_dir():ARCHIVE=Path(tempfile.gettempdir())

class Tests(unittest.TestCase):
 def setUp(self):
  self.files=[]
  for suffix in ('.sqlite3','.json'):
   f=tempfile.NamedTemporaryFile(prefix='cp-diag-fixture-',suffix=suffix,dir=ARCHIVE,delete=False);f.close();self.files.append(Path(f.name))
  self.path,self.descriptor=self.files;self.m=Diagnostics()
  with self.connect() as d:
   d.executescript('''PRAGMA journal_mode=WAL;
    CREATE TABLE instruments(isin TEXT PRIMARY KEY,payload TEXT);
    CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT);
    CREATE TABLE series(isin TEXT,source TEXT,venue TEXT,currency TEXT,symbol TEXT,base_tf TEXT,ts TEXT,open REAL,high REAL,low REAL,close REAL,volume REAL,PRIMARY KEY(isin,source,venue,currency,symbol,base_tf,ts));
    CREATE TABLE candles(isin TEXT,source TEXT,base_tf TEXT,ts TEXT,open REAL,high REAL,low REAL,close REAL,volume REAL,PRIMARY KEY(isin,source,base_tf,ts));
    CREATE TABLE candle_revision_history(id INTEGER PRIMARY KEY,detected_at TEXT,isin TEXT,identity TEXT,base_tf TEXT,ts TEXT,old_ohlcv TEXT,new_ohlcv TEXT,revision_before INTEGER,revision_after INTEGER);''')
   install_input_revisions(d)
   d.execute('INSERT INTO instruments VALUES (?,?)',('A',json.dumps(dict(name='A',kind='stock'))))
   self.put(d,'data_quality:A:S:1H',dict(state='UNCONFIRMED',identity=dict(source='proxy',venue='GER',currency='EUR',symbol='S',timezone='Europe/Berlin'),base_tf='1H',ranges=[['2026-11-20 09:00:00','2026-11-20 09:00:00']]))
   self.put(d,'private-secret',dict(token='MUST_NOT_LEAK'))
   d.execute('INSERT INTO series VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',('A','proxy','GER','EUR','S','1H','2026-11-20 09:00:00',10,11,9,10,2))
  self.bridge=None
 def connect(self):return closing_connection(self.path)
 def put(self,d,key,value):d.execute('INSERT OR REPLACE INTO settings VALUES (?,?)',(key,json.dumps(value)))
 def root(self):return self.m.read(self.path,'fixture',{})
 def page(self,rev,**kw):return self.m.read(self.path,'fixture',dict(path='/by_stock',expected_revision=rev,**kw))
 def full(self):
  r=self.root();s=self.m.sessions[r['revision']]
  for n in range(0,len(s.infos),2):self.page(r['revision'],offset=n,limit=2)
  return s
 def tearDown(self):
  self.m.close()
  if self.bridge:
   if hasattr(self.bridge,'cp_diagnostics'):self.bridge.cp_diagnostics.close()
   self.bridge.commands.close();self.bridge.server.shutdown();self.bridge.server.server_close()
  for p in [*self.files,self.descriptor.with_suffix('.diagnostic-baseline.sqlite3')]:
   for q in (p,Path(str(p)+'-wal'),Path(str(p)+'-shm')):
    if q.exists():q.unlink()
 def test_full_catalog_53_and_200_paged_no_analytical_engine(self):
  for count in (53,200):
   with self.connect() as d:
    d.execute('DELETE FROM instruments')
    d.executemany('INSERT INTO instruments VALUES (?,?)',[(str(i).zfill(4),json.dumps(dict(name=str(i),kind='benchmark' if i<6 else 'stock'))) for i in range(count)])
   with patch('calculation_window.eligibility_state',return_value='UNKNOWN'),patch('mcp_observability.exact_reconciliation',return_value=dict(evidence_status='EXACT_SCOPES_RECONCILED')):
    s=self.full()
   self.assertEqual(len(s.rows),count);self.assertEqual(s.completion()['benchmarks'],6)
   self.assertEqual(sum(len(r['readiness']) for r in s.rows.values()),count*11)
   self.assertTrue(all(set(r['readiness'])==set(TFS) for r in s.rows.values()))
 def test_pinned_view_fresh_second_snapshot_and_exact_storage_delta(self):
  first=self.full();stamp=first.generated_at
  with self.connect() as d:
   d.execute('UPDATE series SET close=10.5')
   d.execute("INSERT INTO series SELECT isin,source,venue,currency,symbol,base_tf,'2026-11-20 10:00:00',open,high,low,close,volume FROM series")
   self.put(d,'data_quality:A:S:1H',dict(state='READY',base_tf='1H',identity={}))
  self.assertEqual(first.row(0)['raw_dq_states'],{'UNCONFIRMED':1})
  second=self.full();delta=second.completion()['comparison']['physical_ohlcv']
  self.assertEqual(delta['status'],'EXACT');self.assertEqual(delta['new_unique_physical_ohlcv'],1);self.assertEqual(delta['corrected_unique_physical_ohlcv'],1)
  self.assertNotEqual(second.revision,first.revision);self.assertGreater(second.generated_at,stamp)
  self.assertEqual(second.row(0)['raw_dq_states'],{'READY':1})
 def test_dq_receipt_only_change_not_a_candle_repair(self):
  self.full()
  with self.connect() as d:self.put(d,'cp_recovery_counter',999999);self.put(d,'data_quality:A:S:1H',dict(state='READY'))
  c=self.full().completion()['comparison'];self.assertEqual(c['physical_ohlcv']['corrected_unique_physical_ohlcv'],0);self.assertEqual(c['physical_ohlcv']['new_unique_physical_ohlcv'],0)
 def test_distinct_currency_symbol_tf_and_duplicate_copies(self):
  self.full()
  with self.connect() as d:
   for currency,symbol,tf in [('USD','S','1H'),('EUR','OTHER','1H'),('EUR','S','15m')]:
    d.execute('INSERT INTO series VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',('A','proxy','GER',currency,symbol,tf,'2026-11-20 09:00:00',10,11,9,10,2))
   # Identical official candle in both storage tables is one physical key.
   d.execute('INSERT INTO candles VALUES (?,?,?,?,?,?,?,?,?)',('A','gettex','15m','2026-11-20 09:00:00',10,11,9,10,2))
   d.execute('INSERT INTO series VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',('A','gettex','GETTEX','EUR','A','15m','2026-11-20 09:00:00',10,11,9,10,2))
  delta=self.full().row(0)['ohlcv_comparison'];self.assertEqual(delta['new_unique_physical_ohlcv'],4)
 def test_conflicting_storage_copy_is_ambiguous_not_a_false_repair(self):
  self.full()
  with self.connect() as d:
   d.execute('INSERT INTO candles VALUES (?,?,?,?,?,?,?,?,?)',('A','gettex','15m','t',10,11,9,10,2))
   d.execute('INSERT INTO series VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',('A','gettex','GETTEX','EUR','A','15m','t',10,11,9,10.5,2))
  second=self.full();row=second.row(0);self.assertEqual(row['ohlcv_comparison']['ambiguous_physical_keys'],1);self.assertIsNone(second.completion()['comparison']['physical_ohlcv']['new_unique_physical_ohlcv'])
 def test_trigger_coverage_missing_is_unknown_not_zero(self):
  self.full()
  with self.connect() as d:d.execute('DROP TRIGGER input_series_update')
  self.assertIsNone(self.full().completion()['comparison']['physical_ohlcv']['new_unique_physical_ohlcv'])
 def test_partial_snapshot_is_not_comparison_baseline(self):
  self.root();self.assertFalse(self.m.previous)
  s=self.full();self.assertFalse(s.completion()['comparison']['comparable'])
 def test_read_only_no_secret_and_no_repair_calls(self):
  with self.connect() as d:before=''.join(d.iterdump())
  with patch('cp_reconciliation.turn',side_effect=AssertionError('writer called')):
   s=self.full()
  self.assertNotIn('MUST_NOT_LEAK',json.dumps(s.header())+json.dumps(s.row(0)))
  with self.connect() as d:after=''.join(d.iterdump())
  self.assertEqual(before,after)
  with self.assertRaises(sqlite3.OperationalError):s.db.execute('DELETE FROM series')
 def test_expired_candidates_do_not_hash_large_dependencies(self):
  with self.connect() as d:self.put(d,'cp_eligibility:A',dict(isin='A',candidates=[dict(timeframe='15m',expires_at='2000-01-01T00:00:00Z',analysis_ready=True,signal_ready=True,trade_ready=True)],timeframes={}))
  with patch('calculation_window.candidate_state',side_effect=AssertionError('expired proof was hashed')):
   row=self.full().row(0)
  self.assertEqual(row['readiness']['15m']['validity'],'EXPIRED');self.assertFalse(row['readiness']['15m']['trade_ready'])
 def test_current_chart_proof_can_finish_within_original_request_budget(self):
  # Four real SQL validations taking >0.6s each used to hit the same inner
  # deadline on every retry. No HTTP/outer SQL budget or retry count changes.
  with self.connect() as d:
   self.put(d,'cp_eligibility:A',dict(isin='A',timeframes={tf:dict(chart_ready=True,chart_closed_inputs={'proof':tf},valid_until='2099-01-01T00:00:00Z') for tf in ('10m','30m','1H','4H')}))
  def validate(db,record,tf,now,memo):
   end=time.monotonic()+0.7
   while time.monotonic()<end:
    db.execute('WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM n WHERE x<1000) SELECT sum(x) FROM n').fetchone()
   return 'CURRENT'
  with patch('calculation_window.chart_state',side_effect=validate),patch('calculation_window.eligibility_state',return_value='EXPIRED'):
   s=self.full();r=s.row(0)
  self.assertFalse(r['retryable']);self.assertEqual(r['read_attempts'],1)
  for tf in ('10m','30m','1H','4H'):
   self.assertTrue(r['readiness'][tf]['chart_ready'])
   self.assertIsNone(r['readiness'][tf]['trade_ready'])
  self.assertLess(r['read_seconds'],4)
 def test_expired_chart_proof_does_not_hash_sources_or_grant_readiness(self):
  with self.connect() as d:self.put(d,'cp_eligibility:A',dict(isin='A',timeframes={'1H':dict(chart_ready=True,chart_closed_inputs={'proof':'old'},valid_until='2000-01-01T00:00:00Z')}))
  with patch('calculation_window.chart_state',side_effect=AssertionError('expired chart was hashed')):
   r=self.full().row(0)['readiness']['1H']
  self.assertEqual(r['chart_validity'],'EXPIRED');self.assertIsNone(r['chart_ready'])
 def test_four_readiness_flags_independent_and_actual_execution_gate_used(self):
  with self.connect() as d:
   token=dict(d.execute('SELECT scope,revision FROM input_revisions'))
   self.put(d,'cp_eligibility:A',dict(isin='A',checked_at='2026-11-23',candidates=[dict(timeframe='15m',expires_at='2099-01-01T00:00:00Z',analysis_ready=True,signal_ready=False,trade_ready=False)],timeframes={'15m':dict(chart_ready=True,_input_revisions={'instrument:A':token['instrument:A']})}))
  with patch('calculation_window.eligibility_state',return_value='STALE'),patch('calculation_window.candidate_state',return_value='CURRENT'):
   r=self.full().row(0)['readiness']['15m']
  self.assertEqual([r[k] for k in ('chart_ready','analysis_ready','signal_ready','trade_ready')],[True,True,False,False])
 def test_one_malformed_instrument_does_not_block_catalog(self):
  with self.connect() as d:d.execute('INSERT INTO instruments VALUES (?,?)',('B',json.dumps(dict(name='B'))));d.execute("UPDATE settings SET value='not-json' WHERE key='data_quality:A:S:1H'")
  s=self.full();self.assertEqual(len(s.rows),2);self.assertEqual(s.row(0)['data_status'],'INSTRUMENT_DIAGNOSTIC_UNAVAILABLE');self.assertIsNone(s.row(0)['readiness']['15m']['analysis_ready']);self.assertEqual(s.row(1)['data_status'],'AVAILABLE');self.assertEqual(s.completion()['status'],'COMPLETE_WITH_LIMITS')
 def test_sql_budgets_nested_restore_and_keep_pinned_transaction(self):
  s=self.full()
  with budget(s.db,0):
   with self.assertRaises(sqlite3.OperationalError):s.db.execute('WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM n WHERE x<10000) SELECT sum(x) FROM n').fetchone()
  self.assertTrue(s.db.in_transaction);self.assertEqual(s.db.execute('SELECT 1').fetchone()[0],1)
 def test_expiry_instance_and_invalid_cursor_fail_explicitly(self):
  r=self.root()
  with self.assertRaisesRegex(ValueError,'REVISION_REQUIRED'):self.m.read(self.path,'fixture',dict(path='/by_stock'))
  with self.assertRaisesRegex(ValueError,'PATH_NOT_FOUND'):self.m.read(self.path,'fixture',dict(path='/by_stock/-1',expected_revision=r['revision']))
  s=self.m.sessions[r['revision']];s.touched-=121
  with self.assertRaisesRegex(ValueError,'PAGE_SESSION_EXPIRED'):self.page(r['revision'])
  r=self.root()
  with self.assertRaisesRegex(ValueError,'PAGE_SESSION_EXPIRED'):self.m.read(self.path,'new-instance',dict(expected_revision=r['revision']))
 def test_busy_reader_returns_bounded_retry(self):
  with self.m.lock:r=self.m.read(self.path,'fixture',{})
  self.assertEqual(r['status'],'BUSY');self.assertEqual(r['retry_after_seconds'],1)
 def test_exact_active_tv_and_owner_scopes_excluded_and_revocation_visible(self):
  import cp_controls as ctl
  from mcp_observability import exact_reconciliation
  import pandas as pd
  with self.connect() as d:
   q=json.loads(d.execute('SELECT value FROM settings WHERE key=?',('data_quality:A:S:1H',)).fetchone()[0]);p=dict(isin='A',kind='archive',tf='1H',identity=q['identity'],state=q['state'],range=q['ranges'][0],report_key='data_quality:A:S:1H')
   self.put(d,ctl.TV_POLICY,dict(enabled=True,approved=True,version=ctl.TV_VERSION))
   token={k:v for k,v in d.execute('SELECT scope,revision FROM input_revisions') if k in ('instrument:A','market:A:1H','setting:'+ctl.TV_POLICY)}
   self.put(d,'cp_manual_risk:A',dict(accepted={'issuer-id':dict(actor=ctl.TV_ACTOR,problem=p,input_revisions=token)}))
  s=self.full();self.assertEqual(s.row(0)['reconciliation']['unverified_saved_scopes'],0);self.assertEqual(s.detail(0,'problems'),[])
  # Same interval with another currency/symbol/TF or shifted endpoint stays open.
  for change in ({'currency':'USD'},{'symbol':'OTHER'}):
   with self.connect() as d:
    qq={**q,'identity':{**q['identity'],**change}};self.put(d,'data_quality:A:S:1H',qq)
   self.assertEqual(self.full().row(0)['reconciliation']['unverified_saved_scopes'],1)
  with self.connect() as d:
   self.put(d,'data_quality:A:S:1H',q);d.execute('UPDATE series SET close=10.25')
  self.assertEqual(self.full().row(0)['reconciliation']['unverified_saved_scopes'],1)
 def approval_fixture(self):
  import cp_controls as ctl
  with self.connect() as d:
   self.put(d,ctl.TV_POLICY,dict(enabled=True,approved=True,version=ctl.TV_VERSION))
   token={k:v for k,v in d.execute('SELECT scope,revision FROM input_revisions') if k in ('instrument:A','market:A:1H','setting:'+ctl.TV_POLICY)}
   grant=dict(accepted={'receipt':dict(actor=ctl.TV_ACTOR,input_revisions=token,problem={'isin':'A','tf':'1H'})})
   self.put(d,'cp_manual_risk:A',grant)
  s=self.m.sessions[self.root()['revision']]
  return ctl,s,grant
 def test_approval_result_reused_across_tf_memos_without_mutable_aliases(self):
  ctl,s,grant=self.approval_fixture();fresh=json.loads(json.dumps(grant))
  with patch.object(ctl,'_revisions',wraps=ctl._revisions) as checks:
   first=ctl.effective_accepted(s.db,'A',grant,_read_memo={})
   first['receipt']['input_revisions']['instrument:A']='CURRENT'
   second=ctl.effective_accepted(s.db,'A',fresh,_read_memo={})
  self.assertEqual(checks.call_count,1);self.assertEqual(set(second),{'receipt'})
  self.assertIsInstance(second['receipt']['input_revisions']['instrument:A'],int)
 def test_changed_grant_content_never_reuses_cached_acceptance(self):
  ctl,s,grant=self.approval_fixture()
  self.assertTrue(ctl.effective_accepted(s.db,'A',grant,_read_memo={}))
  changed=json.loads(json.dumps(grant));changed['accepted']['receipt']['input_revisions']['instrument:A']=999
  self.assertEqual(ctl.effective_accepted(s.db,'A',changed,_read_memo={}),{})
 def test_default_validation_does_not_use_diagnostic_cache(self):
  ctl,s,grant=self.approval_fixture()
  ctl.effective_accepted(s.db,'A',grant,_read_memo={})
  with patch.object(ctl,'_revisions',wraps=ctl._revisions) as checks:
   ctl.effective_accepted(s.db,'A',grant);ctl.effective_accepted(s.db,'A',grant)
  self.assertEqual(checks.call_count,2)
 def test_revocation_in_new_view_cannot_reuse_old_approval_result(self):
  ctl,s,grant=self.approval_fixture();memo={}
  self.assertTrue(ctl.effective_accepted(s.db,'A',grant,_read_memo=memo))
  with self.connect() as d:self.put(d,ctl.TV_POLICY,dict(enabled=False,approved=True,version=ctl.TV_VERSION))
  new=self.m.sessions[self.root()['revision']]
  self.assertTrue(ctl.effective_accepted(s.db,'A',grant,_read_memo=memo))
  self.assertEqual(ctl.effective_accepted(new.db,'A',grant,_read_memo=memo),{})
 def test_interrupted_validation_does_not_cache_partial_approval(self):
  ctl,s,grant=self.approval_fixture()
  with patch.object(ctl,'_revisions',side_effect=sqlite3.OperationalError('interrupted')):
   with self.assertRaises(sqlite3.OperationalError):ctl.effective_accepted(s.db,'A',grant,_read_memo={})
  self.assertEqual(s.db._cp_approval_results,{})
  self.assertTrue(ctl.effective_accepted(s.db,'A',grant,_read_memo={}))
 def test_approval_cache_view_cannot_be_unpinned_or_made_writable(self):
  ctl,s,grant=self.approval_fixture()
  ctl.effective_accepted(s.db,'A',grant,_read_memo={})
  for sql in ('ROLLBACK','COMMIT','PRAGMA query_only=OFF'):
   with self.assertRaises(sqlite3.DatabaseError):s.db.execute(sql)
  self.assertTrue(s.db.in_transaction);self.assertEqual(s.db.execute('PRAGMA query_only').fetchone()[0],1)
  self.assertTrue(ctl.effective_accepted(s.db,'A',grant,_read_memo={}))
 def test_53_and_200_approvals_resume_only_completed_entry_checks(self):
  for count in (53,200):
   ctl,s,template=self.approval_fixture()
   grant=dict(accepted={str(i):json.loads(json.dumps(template['accepted']['receipt'])) for i in range(count)})
   original=ctl._revisions;completed=0;attempts=0
   while True:
    calls=[0]
    def bounded(db,token):
     calls[0]+=1
     if calls[0]>25:raise sqlite3.OperationalError('interrupted')
     return original(db,token)
    attempts+=1
    try:
     with patch.object(ctl,'_revisions',side_effect=bounded):result=ctl.effective_accepted(s.db,'A',grant,_read_memo={})
     completed+=calls[0];break
    except sqlite3.OperationalError:completed+=calls[0]-1
    self.assertLess(attempts,MAX_READ_ATTEMPTS)
   self.assertEqual(set(result),set(grant['accepted']))
   self.assertEqual(completed,count);self.assertGreater(attempts,1)
 def test_expired_budget_cannot_cache_a_swallowed_negative_check(self):
  ctl,s,grant=self.approval_fixture()
  grant['accepted']['receipt']['input_revisions']['instrument:A']=999
  s.db.deadlines=[time.monotonic()-1]
  try:
   with patch.object(ctl,'_reviewed_market_tail_current',return_value=False):
    with self.assertRaises(sqlite3.OperationalError):ctl.effective_accepted(s.db,'A',grant,_read_memo={})
   self.assertEqual(s.db._cp_approval_results,{})
  finally:s.db.deadlines=[]
 def test_retry_reuses_base_only_in_original_view_and_keeps_raw_dq(self):
  r=self.root();s=self.m.sessions[r['revision']];queries=[];s.db.set_trace_callback(queries.append)
  with patch.object(s,'_physical',side_effect=sqlite3.OperationalError('interrupted')):self.page(r['revision'])
  self.assertIn(0,s.base_values)
  first_counts=sum('GROUP BY' in q for q in queries)
  with self.connect() as d:self.put(d,'data_quality:A:S:1H',dict(state='READY'))
  self.page(r['revision'])
  self.assertEqual(s.row(0)['raw_dq_states'],{'UNCONFIRMED':1})
  self.assertEqual(sum('GROUP BY' in q for q in queries),first_counts)
  self.assertNotIn(0,s.base_values)
  self.assertEqual(self.full().row(0)['raw_dq_states'],{'READY':1})
 def test_incomplete_base_retention_bounded_for_53_and_200_instruments(self):
  for count in (53,200):
   with self.connect() as d:
    d.execute('DELETE FROM instruments')
    d.executemany('INSERT INTO instruments VALUES (?,?)',[(str(i).zfill(4),json.dumps(dict(name=str(i)))) for i in range(count)])
   s=self.m.sessions[self.root()['revision']]
   with patch.object(s,'_physical',side_effect=sqlite3.OperationalError('interrupted')):
    for n in range(count):
     row=s.row(n);self.assertEqual(row['data_status'],'INSTRUMENT_DIAGNOSTIC_UNAVAILABLE')
     self.assertLessEqual(len(s.base_values),4)
   self.assertEqual(s.completion()['status'],'COMPLETE_WITH_LIMITS')
 def test_readiness_checkpoint_survives_reopen_and_never_becomes_fresh_result(self):
  self.m.close();cache=self.descriptor.with_suffix('.diagnostic-baseline.sqlite3');self.m=Diagnostics(cache)
  with self.connect() as d:
   token=dict(d.execute('SELECT scope,revision FROM input_revisions'))
   self.put(d,'cp_eligibility:A',dict(isin='A',checked_at='2026-11-23T12:00:00+00:00',
    candidates=[dict(timeframe='1H',expires_at='2099-01-01T00:00:00Z',analysis_ready=True,signal_ready=False,trade_ready=False)],
    timeframes={'1H':dict(chart_ready=True,_input_revisions={'instrument:A':token['instrument:A']})}))
  with patch('calculation_window.eligibility_state',return_value='CURRENT'),patch('calculation_window.candidate_state',return_value='CURRENT'):
   first=self.full();self.assertTrue(first.row(0)['readiness']['1H']['analysis_ready'])
  old_revision=first.revision;old_at=first.generated_at;self.m.close();self.m=Diagnostics(cache)
  second=self.full();historical=second.completion()['comparison']['readiness_baseline']
  self.assertEqual(historical['status'],'ARCHIVED_CHECKPOINT');self.assertEqual(historical['revision'],old_revision)
  self.assertEqual(historical['generated_at'],old_at);self.assertFalse(historical['execution_permission'])
  self.assertTrue(historical['by_stock']['A']['readiness']['1H']['analysis_ready'])
  self.assertIsNot(second.row(0)['readiness']['1H']['analysis_ready'],True)
  self.assertEqual(second.completion()['comparison']['physical_ohlcv']['corrected_unique_physical_ohlcv'],0)
 def test_journal_deduplicated_net_ohlcv_and_never_total_ingestion(self):
  self.full()
  meta=json.dumps(dict(source='proxy',venue='GER',currency='EUR',symbol='S',timezone='Europe/Berlin'))
  a=dict(open=10,high=11,low=9,close=10,volume=2);b=dict(a,close=10.5);c=dict(a,close=10.75)
  with self.connect() as d:
   for n,(old,new) in enumerate(((a,b),(b,c),(c,c)),1):d.execute('INSERT INTO candle_revision_history VALUES (?,?,?,?,?,?,?,?,?,?)',(n,'now','A',meta,'1H','2026-11-20 09:00:00',json.dumps(old),json.dumps(new),n,n+1))
  diff=self.full().completion()['comparison'];self.assertEqual(diff['journal_events'],3);self.assertEqual(diff['journal_corrected_unique_physical_ohlcv'],1);self.assertIsNone(diff['corrected_unique_ohlcv']);self.assertEqual(diff['physical_ohlcv']['corrected_unique_physical_ohlcv'],0)
 def test_permission_memo_is_query_only_and_preserves_native_negative_cases(self):
  import cp_controls as ctl
  from test_cp_scoped_proof_revision import ScopedProof
  from test_cp_scoped_tail import ScopedTail
  original=ctl.effective_accepted
  def checked(db,isin,grant):
   expected=original(db,isin,grant);changes=db.total_changes
   pinned=db.in_transaction;query_only=db.execute('PRAGMA query_only').fetchone()[0]
   if not pinned:db.execute('BEGIN')
   db.execute('PRAGMA query_only=ON')
   try:actual=original(db,isin,grant,_read_memo={})
   finally:
    db.execute('PRAGMA query_only='+str(query_only))
    if not pinned:db.rollback()
   self.assertEqual(actual,expected);self.assertEqual(db.total_changes,changes)
   return actual
  with patch('cp_controls.effective_accepted',side_effect=checked):
   for case in (ScopedProof,ScopedTail):
    result=unittest.TestResult();unittest.defaultTestLoader.loadTestsFromTestCase(case).run(result)
    self.assertEqual(result.errors+result.failures,[])
 def test_unknown_identity_reports_are_not_merged_as_one_feed(self):
  with self.connect() as d:
   for symbol in ('UNKNOWN_A','UNKNOWN_B'):
    self.put(d,'data_quality:A:'+symbol+':1H',dict(identity={},base_tf='1H',state='INVALID',issues=['unconfirmed listing']))
  row=self.full().row(0)
  self.assertEqual(row['reconciliation']['unverified_saved_scopes'],3)
  self.assertEqual(len(self.m.sessions[next(reversed(self.m.sessions))].detail(0,'problems')),3)
 def test_previous_imported_validator_remains_compatible(self):
  import cp_controls as ctl
  original=ctl.effective_accepted
  def legacy(db,isin,grant):return original(db,isin,grant)
  with patch('cp_controls.effective_accepted',legacy):
   row=self.full().row(0)
  self.assertEqual(row['data_status'],'AVAILABLE')
  self.assertEqual(row['reconciliation']['evidence_status'],'EXACT_SCOPES_RECONCILED')
 def test_permission_memo_rejects_writer_or_unpinned_connection(self):
  import cp_controls as ctl
  with self.connect() as d:
   with self.assertRaisesRegex(ValueError,'READ_ONLY_RECONCILIATION_MEMO'):ctl.effective_accepted(d,'A',{},_read_memo={})
 def test_transient_budget_retry_keeps_same_sql_view_and_recovers_completion(self):
  original=Session._row;calls=[]
  def flaky(session,n):
   calls.append(n)
   if len(calls)==1:raise sqlite3.OperationalError('interrupted')
   return original(session,n)
  with patch.object(Session,'_row',flaky):
   s=self.full();self.assertEqual(s.completion()['status'],'COMPLETE_WITH_LIMITS');self.assertFalse(self.m.previous)
   self.assertEqual(s.completion()['retryable_instruments'][0]['path'],'/by_stock/0')
   with self.connect() as d:self.put(d,'data_quality:A:S:1H',dict(state='READY'))
   revision=s.revision;self.page(revision)
   self.assertEqual(s.revision,revision);self.assertEqual(s.row(0)['read_attempts'],2)
   self.assertEqual(s.row(0)['raw_dq_states'],{'UNCONFIRMED':1})
   self.assertEqual(s.completion()['status'],'COMPLETE');self.assertTrue(s.evidence_complete)
   self.assertEqual(self.m.previous['fixture']['revision'],revision)
 def test_transient_retry_is_capped_by_supported_phases(self):
  with patch.object(Session,'_row',side_effect=sqlite3.OperationalError('interrupted')) as attempt:
   r=self.root()
   for _ in range(MAX_READ_ATTEMPTS+5):self.page(r['revision'])
  self.assertEqual(attempt.call_count,MAX_READ_ATTEMPTS)
  s=self.m.sessions[r['revision']];self.assertFalse(s.row(0)['retryable']);self.assertEqual(s.row(0)['retry_remaining'],0)
  self.assertEqual(s.completion()['retryable_instruments'],[]);self.assertFalse(self.m.previous)
 def test_cold_late_phase_can_complete_after_five_reads_in_original_view(self):
  original=Session._row;attempts=[]
  def cold(session,n):
   attempts.append(n)
   if len(attempts)<=6:raise sqlite3.OperationalError('interrupted')
   return original(session,n)
  with patch.object(Session,'_row',cold):
   r=self.root();s=self.m.sessions[r['revision']]
   for _ in range(6):self.page(r['revision'])
   self.assertFalse(self.m.previous);self.assertEqual(s.completion()['status'],'COMPLETE_WITH_LIMITS')
   with self.connect() as d:self.put(d,'data_quality:A:S:1H',dict(state='READY'))
   self.page(r['revision'])
   self.assertEqual(s.completion()['status'],'COMPLETE');self.assertEqual(s.row(0)['read_attempts'],7)
   self.assertEqual(s.row(0)['raw_dq_states'],{'UNCONFIRMED':1})
   self.assertEqual(self.m.previous['fixture']['revision'],r['revision'])
 def test_partial_diagnostics_never_become_delta_baseline(self):
  with patch.object(Session,'_row',side_effect=sqlite3.OperationalError('interrupted')):partial=self.full()
  self.assertFalse(self.m.previous)
  complete=self.full();self.assertFalse(complete.completion()['comparison']['comparable'])
  self.assertEqual(complete.completion()['comparison']['reason'],'NO_PREVIOUS_COMPLETE_SNAPSHOT')
 def test_execution_read_budget_is_retried_without_granting_permission(self):
  with self.connect() as d:
   self.put(d,'cp_eligibility:A',dict(isin='A',checked_at='2026-11-23',candidates=[dict(timeframe='15m',expires_at='2099-01-01T00:00:00Z',analysis_ready=True,signal_ready=True,trade_ready=True)],timeframes={'15m':dict(chart_ready=True)}))
  with patch('calculation_window.eligibility_state',return_value='CURRENT'),patch('calculation_window.candidate_state',return_value='CURRENT'),patch('calculation_window.execution_eligibility',side_effect=[sqlite3.OperationalError('interrupted'),{}]) as gate:
   s=self.full();self.assertEqual(s.completion()['budget_limited_instruments'],1);self.assertIsNone(s.rows[0]['readiness']['15m']['trade_ready'])
   self.assertFalse(self.m.previous);self.page(s.revision)
   self.assertEqual(gate.call_count,2);self.assertTrue(s.row(0)['readiness']['15m']['trade_ready'])
   self.assertEqual(s.completion()['status'],'COMPLETE');self.assertFalse(s.row(0)['execution_permission'])
 def test_comparison_retry_retains_original_complete_baseline(self):
  import cp_bridge_comparison as cmp
  first=self.full();original=cmp.compare_instrument;calls=[]
  def slow(current,previous,isin):
   calls.append(isin)
   if len(calls)==1:return dict(status='UNKNOWN',reason='COMPARISON_READ_BUDGET')
   return original(current,previous,isin)
  with patch('cp_bridge_comparison.compare_instrument',slow):
   second=self.full();self.assertEqual(second.completion()['status'],'COMPLETE_WITH_LIMITS')
   self.assertEqual(self.m.previous['fixture']['revision'],first.revision)
   self.page(second.revision)
  self.assertEqual(second.completion()['status'],'COMPLETE')
  self.assertEqual(second.completion()['comparison']['from_revision'],first.revision)
  self.assertEqual(self.m.previous['fixture']['revision'],second.revision)
 def test_manifest_retry_resumes_completed_proofs_in_same_view(self):
  from cp_diagnostic_baseline import Checkpoint,capture
  cache=self.path.with_suffix('.diagnostic-baseline.sqlite3')
  self.m.close();self.m=Diagnostics(cache)
  calls=[]
  def once(session,isin):
   calls.append(isin)
   if len(calls)==1:raise TimeoutError('MANIFEST_READ_BUDGET')
   return capture(session,isin)
  with patch('cp_diagnostic_baseline.capture',side_effect=once),patch('mcp_observability.exact_reconciliation',wraps=__import__('mcp_observability').exact_reconciliation) as reconcile:
   s=self.full();self.assertEqual(s.completion()['status'],'COMPLETE_WITH_LIMITS')
   self.assertEqual(reconcile.call_count,1)
   with self.connect() as d:
    d.execute('UPDATE series SET close=10.5')
    self.put(d,'data_quality:A:S:1H',dict(state='READY'))
   self.page(s.revision)
   self.assertEqual(reconcile.call_count,1)
   self.assertEqual(s.row(0)['raw_dq_states'],{'UNCONFIRMED':1})
   self.assertEqual(s.row(0)['read_attempts'],2)
   self.assertEqual(s.completion()['status'],'COMPLETE')
   from cp_diagnostic_baseline import decode
   manifest=decode(self.m.checkpoint.get(s.revision,'A')[0])['rows']
   self.assertIn(hashlib.sha256(repr((10.,11.,9.,10.,2.)).encode()).hexdigest(),manifest.values())
   self.assertEqual(self.m.checkpoint.load(self.path)['revision'],s.revision)
 def test_completed_manifest_is_not_rebuilt_on_comparison_retry(self):
  from cp_diagnostic_baseline import capture
  self.m.close();self.m=Diagnostics(self.path.with_suffix('.diagnostic-baseline.sqlite3'))
  self.full()
  import cp_bridge_comparison as cmp
  original=cmp.compare_instrument;attempts=[]
  def once(*args):
   attempts.append(1)
   return dict(status='UNKNOWN',reason='COMPARISON_READ_BUDGET') if len(attempts)==1 else original(*args)
  with patch('cp_diagnostic_baseline.capture',wraps=capture) as manifest,patch('cp_bridge_comparison.compare_instrument',side_effect=once):
   s=self.full();self.assertEqual(s.completion()['status'],'COMPLETE_WITH_LIMITS')
   self.page(s.revision)
   self.assertEqual(manifest.call_count,1)
   self.assertEqual(s.completion()['status'],'COMPLETE')
 def test_readiness_retry_retains_completed_tf_only_in_this_view(self):
  with self.connect() as d:self.put(d,'cp_eligibility:A',dict(candidates=[dict(timeframe=tf,expires_at='2099-01-01T00:00:00Z',analysis_ready=True) for tf in ('5m','1H')]))
  calls=[]
  def gate(db,record,candidate,now,memo):
   tf=candidate['timeframe'];calls.append(tf)
   if tf=='1H' and calls.count('1H')<=3:raise sqlite3.OperationalError('interrupted')
   return 'CURRENT'
  with patch('calculation_window.eligibility_state',return_value='STALE'),patch('calculation_window.candidate_state',side_effect=gate):
   s=self.full();self.assertTrue(s.rows[0]['retryable']);self.assertEqual(calls.count('5m'),1)
   self.page(s.revision)
   self.assertEqual(calls.count('5m'),1);self.assertEqual(calls.count('1H'),4)
   self.assertEqual(s.completion()['status'],'COMPLETE');self.assertEqual(s.readiness_values,{})
 def test_reconciliation_resume_does_not_cache_interruption_as_unverified(self):
  from mcp_observability import exact_reconciliation
  import pandas as pd
  meta=dict(source='gettex_chart',symbol='S',venue='GETTEX',currency='EUR',timezone='Europe/Berlin')
  reports={'dq':dict(identity=meta,base_tf='5m',state='UNCONFIRMED',ranges=[['2026-11-20 09:00:00','2026-11-20 09:00:00'],['2026-11-20 09:05:00','2026-11-20 09:05:00']])}
  root=self.root();s=self.m.sessions[root['revision']];memo={}
  with patch('cp_native_resolution.native_source_resolution',side_effect=[{'current':True},sqlite3.OperationalError('interrupted')]):
   with self.assertRaisesRegex(sqlite3.OperationalError,'interrupted'):exact_reconciliation(s.db,s.infos[0],reports,pd.Timestamp(s.generated_at),time.monotonic()+3,_read_memo=memo)
  with patch('cp_native_resolution.native_source_resolution',return_value={'current':True}) as native:
   result=exact_reconciliation(s.db,s.infos[0],reports,pd.Timestamp(s.generated_at),time.monotonic()+3,_read_memo=memo)
  self.assertEqual(native.call_count,1);self.assertEqual(result['official_confirmed_scopes'],2)
  self.assertEqual(result['unverified_saved_scopes'],0)
 def test_problem_expansion_reuses_exact_classifications_not_source_revalidation(self):
  s=self.full()
  with patch('mcp_observability.exact_reconciliation',side_effect=AssertionError('repeated completed proof')):
   problems=s.detail(0,'problems')
  self.assertEqual(len(problems),s.rows[0]['reconciliation']['unverified_saved_scopes'])
  self.assertTrue(all(p['classification']=='UNVERIFIED_SAVED_SCOPE_NOT_CONFIRMED_ERROR' for p in problems))
 def test_http_auth_and_complete_two_consecutive_reads(self):
  self.bridge=Bridge(MappedDirectory(self.path,self.descriptor),queue.Queue(),lambda a,b:None);audit=[];self.bridge.audit=audit.append
  def request(args,token=None):
   req=Request(f'http://127.0.0.1:{self.bridge.server.server_port}/bridge',data=json.dumps(dict(action='snapshot',arguments=dict(section='candlepilot_health',**args))).encode(),headers={'Authorization':'Bearer '+(token or self.bridge.token),'Content-Type':'application/json'})
   with urlopen(req,timeout=5) as r:return json.load(r)
  from urllib.error import HTTPError
  with self.assertRaises(HTTPError):request({},'wrong')
  revisions=[]
  for _ in range(2):
   r=request({});rev=r['revision'];revisions.append(rev)
   r=request(dict(path='/by_stock',expected_revision=rev));self.assertEqual(r['diagnostic_status'],'COMPLETE')
   self.assertEqual(r['instrument_total'],1);self.assertFalse(r['execution_permission'])
   done=request(dict(path='/completion',expected_revision=rev,limit=50));values={i['key']:i.get('value') for i in done['items'] if i['expanded']};self.assertEqual(values['completed'],1)
  self.assertNotEqual(*revisions);self.assertTrue(audit);self.assertTrue(all('seconds' in a and a['response_bytes']<65000 for a in audit))

class closing_connection:
 def __init__(self,path):self.path=path
 def __enter__(self):self.db=sqlite3.connect(self.path);return self.db
 def __exit__(self,t,v,b):
  try:
   if t is None:self.db.commit()
   else:self.db.rollback()
  finally:self.db.close()

if __name__=='__main__':unittest.main()
