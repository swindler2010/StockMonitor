"""Shared recent GETTEX ingestion and severe diagnostics; memory only."""
import gzip,sys,unittest,copy
from pathlib import Path
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
import test_cp_gettex_universe as fixtures
from test_cp_gettex_universe import APPLE,SAP,gh,pd
from storage import Store
import cp_reconciliation as rv

class FreshPriorityTests(unittest.TestCase):
 def fixture(self):
  f=fixtures.CachedHistory();f.setUp();self.addCleanup(f.doCleanups);return f
 def test_recent_pair_precedes_old_archive_and_old_history_resumes(self):
  f=self.fixture();s=f.s
  for mic in ('munc','mund'):f.file(mic,'20261101.10.15').write_bytes(f.file(mic).read_bytes())
  Store.save_instrument(s,dict(isin=SAP,name='SAP'))
  out=gh.accumulate_cached_batch(s,f.parse,f.now,max_pairs=1,max_seconds=1,recent_only=True)
  self.assertEqual(out['pairs'],['20261103.10.15']);self.assertEqual(out['updated_isins'],sorted([APPLE,SAP]))
  self.assertNotIn('20261101.10.15',s.setting('gettex_cached_history')['pairs'])
  self.assertEqual(gh.accumulate_cached_batch(s,f.parse,f.now,max_pairs=1,recent_only=True)['pairs_processed'],0)
  before=f.rows();old=gh.accumulate_cached_batch(s,f.parse,f.now,max_pairs=1)
  self.assertEqual(old['pairs'],['20261101.10.15']);self.assertTrue(all(row in f.rows() for row in before))
 def test_one_pair_updates_53_and_200_registered_stocks_without_network(self):
  for count in (53,200):
   f=self.fixture();identifiers=[f'DE{n:010d}' for n in range(count)]
   for isin in identifiers:Store.save_instrument(f.s,dict(isin=isin,name=isin))
   payload=''.join(f'{isin},10:02:00,EUR,100,1\n' for isin in identifiers).encode()
   for mic in ('munc','mund'):f.file(mic).write_bytes(gzip.compress(payload))
   out=gh.accumulate_cached_batch(f.s,f.parse,f.now,max_pairs=1,recent_only=True)
   self.assertEqual(out['updated_isins'],sorted(identifiers));self.assertEqual(out['pairs_processed'],1)
   self.assertEqual(out['conflicts'],0)
   self.assertEqual(gh.accumulate_cached_batch(f.s,f.parse,f.now,max_pairs=1,recent_only=True)['candles_added'],0)

class SuspiciousTests(unittest.TestCase):
 def test_only_proven_severe_gettex_anomalies_and_withdrawal(self):
  meta=dict(source='gettex_chart',venue='GETTEX',currency='EUR');progress={}
  jobs=[dict(id=str(n),isin='TEST',identity=meta,finding={'state':state,'market_state':'GAP' if state=='GAP' else 'UNCONFIRMED'})
        for n,state in enumerate(('INVALID','GAP','UNCONFIRMED','SOURCE_TIMEBASE_UNCONFIRMED','NO_TRADES','NATIVE_ROWS_CONFIRMED','SOURCE_DISCREPANCY'))]
  original=copy.deepcopy(jobs);rv.update_suspicious(progress,jobs)
  self.assertEqual(set(progress['suspicious']),{'0','1'});self.assertEqual(original,jobs)
  for count in (53,200):
   for _ in range(count):rv.update_suspicious(progress,jobs)
  tv=dict(id='TV',identity=meta,observation={'result':'ABSENT','identity_confirmed':True},finding={'state':'UNCONFIRMED','observation_current':True})
  self.assertIsNone(rv.suspicious_finding(tv))
  self.assertIsNone(rv.suspicious_finding(dict(jobs[0],identity={'source':'proxy','venue':'FRA'})))
  jobs[0]['finding']={'state':'NATIVE_ROWS_CONFIRMED'};rv.update_suspicious(progress,jobs)
  self.assertEqual(set(progress['suspicious']),{'1'})
  # A conflicting official-trades proof remains suspicious even when TV lacks a bar.
  self.assertEqual(rv.suspicious_finding(dict(jobs[1],observation=tv['observation'])),'CONFIRMED_TRADES_WITHOUT_NATIVE_BAR')

class WorkerTests(unittest.TestCase):
 def test_official_recent_pair_requests_existing_queue_only_for_changed_stocks(self):
  import test_cp_local_completion as routes
  from contextlib import ExitStack
  from unittest.mock import patch
  from feed_worker import FeedContext
  fixture=routes.WorkerRoutes();host=fixture.host();self.addCleanup(fixture.doCleanups)
  host._calculate.side_effect=lambda _: {'errors':[]}
  with ExitStack() as stack:
   fixture.stages(stack)
   batch=stack.enter_context(patch('gettex_history.accumulate_cached_batch',return_value={'updated_isins':[APPLE,SAP]}))
   queue=stack.enter_context(patch('cp_assessment.request'))
   FeedContext.calculate(host,dict(isin=APPLE,_gettex_poll_only=True,_cp_focus={'source':'gettex'}))
   self.assertEqual(batch.call_args.kwargs,dict(max_pairs=1,max_seconds=1,recent_only=True))
   self.assertEqual([c.args[1] for c in queue.call_args_list],[APPLE,SAP])
   batch.reset_mock();queue.reset_mock()
   for _ in range(53):FeedContext.calculate(host,dict(isin=APPLE,_gettex_poll_only=True,_cp_focus={'source':'gettex_chart'}))
   batch.assert_not_called();queue.assert_not_called()

if __name__=='__main__':unittest.main()
