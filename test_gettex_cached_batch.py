"""Bounded local recovery on the existing writer; no network/user files."""
import gzip,unittest
from types import SimpleNamespace
from unittest.mock import patch
from test_cp_gettex_universe import CachedHistory,APPLE,POLICY,OWNER_HISTORY_POLICY,gh,pd

class CachedBatch(unittest.TestCase):
 def setUp(self):
  self.f=CachedHistory();self.f.setUp();self.addCleanup(self.f.doCleanups)
  self.s=self.f.s;self.now=pd.Timestamp('2026-11-03T22:30Z')
 def seed(self,n):
  for j in range(1,n):
   at=pd.Timestamp('2026-11-03T10:15Z')+pd.Timedelta(minutes=15*j)
   raw=f'{APPLE},{(at-pd.Timedelta(minutes=10)).strftime("%H:%M:%S")},EUR,252,1\n'.encode()
   for mic in ('munc','mund'):self.f.file(mic,at.strftime('%Y%m%d.%H.%M')).write_bytes(gzip.compress(raw))
 def run_batch(self,**kwargs):return gh.accumulate_cached_batch(self.s,self.f.parse,self.now,**kwargs)
 def test_many_saved_pairs_progress_in_one_history_turn(self):
  self.seed(20)
  with patch.object(gh.time,'monotonic',return_value=0):out=self.run_batch(max_pairs=1000,max_seconds=1000)
  self.assertEqual(out['pairs_processed'],16);self.assertEqual(out['stop_reason'],'PAIR_BUDGET')
  self.assertEqual(out['seconds_budget'],5);self.assertEqual(len(self.s.setting('gettex_cached_history')['pairs']),16)
 def test_time_budget_yields_only_between_atomic_pairs(self):
  self.seed(5)
  with patch.object(gh.time,'monotonic',side_effect=[0,1,6,6]):out=self.run_batch()
  self.assertEqual(out['pairs_processed'],2);self.assertEqual(out['stop_reason'],'TIME_BUDGET')
  self.assertEqual(len(self.s.setting('gettex_cached_history')['pairs']),2)
 def test_slow_pair_finishes_then_yields_without_cancelling_writer(self):
  self.seed(3)
  with patch.object(gh.time,'monotonic',side_effect=[0,8,8]):out=self.run_batch()
  self.assertEqual(out['pairs_processed'],1);self.assertEqual(out['seconds'],8)
  self.assertEqual(len(self.f.rows()),3)
 def test_restart_resumes_each_checkpoint_without_rewrite(self):
  self.seed(7)
  with patch.object(gh.time,'monotonic',return_value=0):first=self.run_batch(max_pairs=3)
  old=list(self.f.rows());writes=self.s.db.execute('SELECT COUNT(*) FROM writes').fetchone()[0]
  other=SimpleNamespace(directory=self.s.directory,connect=self.s.connect,setting=self.s.setting,set_setting=self.s.set_setting,instruments=self.s.instruments)
  with patch.object(gh.time,'monotonic',return_value=0):second=gh.accumulate_cached_batch(other,self.f.parse,self.now)
  self.assertEqual((first['pairs_processed'],second['pairs_processed']),(3,4))
  self.assertTrue(set(old)<=set(self.f.rows()));self.assertEqual(self.s.db.execute("SELECT COUNT(*) FROM writes WHERE kind='update'").fetchone()[0],0)
  self.assertEqual(self.s.setting('gettex_cached_history')['last_batch'],second)
  self.assertGreater(self.s.db.execute('SELECT COUNT(*) FROM writes').fetchone()[0],writes)
 def test_idle_unchanged_batches_do_not_write_or_parse_again(self):
  self.run_batch();writes=self.s.db.total_changes
  result=gh.accumulate_cached_batch(self.s,lambda *a,**k:self.fail('repeat parse'),self.now)
  self.assertEqual(result['state'],'IDLE');self.assertEqual(writes,self.s.db.total_changes)
 def test_bad_pair_does_not_starve_other_pairs_or_become_no_trades(self):
  self.seed(3);self.f.mund.write_bytes(gzip.compress(b''))
  out=self.run_batch();self.assertEqual(out['pairs_processed'],3);self.assertEqual(out['blocked_pairs'],1)
  progress=self.s.setting('gettex_cached_history');self.assertEqual(progress['pairs']['20261103.10.15']['state'],'BLOCKED')
  self.assertGreater(out['candles_added'],0);self.assertIsNone(self.s.setting('interval_evidence:'+APPLE))
 def test_conflicting_ohlcv_retained_while_independent_pairs_advance(self):
  self.seed(2);self.s.db.execute('INSERT INTO candles VALUES (?,?,?,?,?,?,?,?,?)',(APPLE,'gettex','15m','2026-11-03 11:00:00',249,249,249,249,2));self.s.db.commit()
  old=self.f.rows();out=self.run_batch();self.assertEqual(out['conflicts'],1);self.assertTrue(set(old)<=set(self.f.rows()))
  self.assertGreater(out['candles_added'],0);self.assertEqual(self.s.db.execute("SELECT COUNT(*) FROM trades WHERE ts<'2026-11-03 11:15:00'").fetchone()[0],0)
 def test_unapproved_policy_never_imports(self):
  self.s.set_setting(OWNER_HISTORY_POLICY,dict(POLICY,approved=False));before=self.s.db.total_changes
  self.assertEqual(self.run_batch()['state'],'NOT_ENABLED');self.assertEqual(self.s.db.total_changes,before);self.assertFalse(self.f.rows())
 def test_input_change_stops_without_permission_or_rows(self):
  def race(*a,**k):
   out=self.f.parse(*a,**k);self.s.set_setting(OWNER_HISTORY_POLICY,dict(POLICY,approved=False));return out
  out=gh.accumulate_cached_batch(self.s,race,self.now)
  self.assertEqual(out['state'],'INPUT_CHANGED');self.assertEqual(out['pairs_processed'],0);self.assertFalse(self.f.rows());self.assertIsNone(self.s.setting('gettex_cached_history'))
 def test_local_batch_precedes_network_on_same_existing_turn(self):
  calls=[]
  with patch.object(gh,'accumulate_cached_batch',side_effect=lambda *a:calls.append('local') or {'pairs_processed':4}),patch.object(gh,'classify_cached_session',side_effect=lambda *a:calls.append('classify') or {}),patch.object(gh,'acquire_missing_evidence',side_effect=lambda *a:calls.append('network') or {}):
   result=gh.maintain_evidence({},self.s,lambda *a:self.fail('unexpected HTTP'),self.f.parse,self.now)
  self.assertEqual(calls,['local','classify','network']);self.assertEqual(result['cached_history']['pairs_processed'],4)
 def test_empty_closed_files_reclassified_once_with_calendar_and_hashes(self):
  self.f.munc.unlink();self.f.mund.unlink()
  for mic in ('munc','mund'):self.f.file(mic,'20261021.05.30').write_bytes(gzip.compress(b''))
  files={mic:self.f.file(mic,'20261021.05.30') for mic in ('munc','mund')}
  import hashlib,json
  universe=hashlib.sha256(json.dumps([APPLE]).encode()).hexdigest()
  old=dict(state='BLOCKED',reason='EMPTY_FILE_UNCONFIRMED',universe=universe,
           signature={mic:[p.stat().st_size,p.stat().st_mtime_ns] for mic,p in files.items()})
  self.s.set_setting('gettex_cached_history',{'pairs':{'20261021.05.30':old}})
  out=self.run_batch();row=self.s.setting('gettex_cached_history')['last_result']
  self.assertEqual(out['pairs_processed'],1);self.assertEqual(out['blocked_pairs'],0)
  self.assertEqual(row['state'],'MARKET_CLOSED');self.assertEqual(len(row['sha256']),2);self.assertFalse(self.f.rows())
  self.assertEqual(self.s.setting('gettex_cached_history')['current_universe_pairs'],1)
  changes=self.s.db.total_changes;self.assertEqual(self.run_batch()['state'],'IDLE');self.assertEqual(changes,self.s.db.total_changes)
 def test_closed_calendar_cannot_hide_nonempty_or_unconfirmed_calendar(self):
  self.f.munc.unlink();self.f.mund.unlink()
  for label in ('20261021.05.30','20270105.05.30'):
   for mic in ('munc','mund'):self.f.file(mic,label).write_bytes(gzip.compress(b''))
  self.f.file('mund','20261021.05.30').write_bytes(gzip.compress(b'DE0007236101,05:29:00,EUR,200,4\n'))
  out=gh.accumulate_cached_batch(self.s,self.f.parse,pd.Timestamp('2027-01-06T12:00Z'))
  self.assertEqual(out['blocked_pairs'],2);self.assertFalse(self.f.rows());self.assertIsNone(self.s.setting('interval_evidence:'+APPLE))

if __name__=='__main__':unittest.main()
