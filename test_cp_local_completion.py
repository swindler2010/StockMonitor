"""Bounded automatic archive work on existing CP history routes, memory only."""
import json,unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import patch,Mock
from test_cp_gettex_universe import CachedHistory,APPLE,SAP,POLICY,pd
import history_service as hs
import data_quality as dq
from performance_cache import install_input_revisions
from feed_worker import FeedContext

class LocalCompletion(unittest.TestCase):
 def setUp(self):
  self.f=CachedHistory();self.f.setUp();self.addCleanup(self.f.doCleanups);self.s=self.f.s
  self.s.db.execute('CREATE TABLE series(isin TEXT,source TEXT,venue TEXT,currency TEXT,symbol TEXT,base_tf TEXT,ts TEXT,open REAL,high REAL,low REAL,close REAL,volume REAL)')
  install_input_revisions(self.s.db);self.s.db.commit()
  def load(isin,base):
   rows=self.s.db.execute("SELECT ts,open,high,low,close,volume FROM candles WHERE isin=? AND source='gettex' AND base_tf=? ORDER BY ts",(isin,base)).fetchall()
   frame=pd.DataFrame(rows,columns=['ts',*dq.FIELDS]).set_index('ts');frame.index=pd.to_datetime(frame.index)
   frame.attrs=dict(source='gettex',venue='GETTEX',currency='EUR',symbol=isin,timezone='Europe/Berlin')
   return frame
  self.s.load_official=load;self.now=pd.Timestamp('2026-11-03T12:00Z')
 def seed(self,isin=APPLE,base='15m',missing=False,invalid=False):
  self.s.db.execute('INSERT OR IGNORE INTO instruments VALUES (?,?)',(isin,json.dumps(dict(isin=isin,name=isin))))
  for at in ('11:00','11:15','11:30'):
   if missing and at=='11:15':continue
   self.s.db.execute('INSERT OR REPLACE INTO candles VALUES (?,?,?,?,?,?,?,?,?)',(isin,'gettex',base,'2026-11-03 '+at+':00',100,99 if invalid else 101,99,100,2))
  self.s.db.commit()
 def turn(self):
  with patch('requests.get',side_effect=AssertionError('unexpected HTTP')),patch.object(dq,'repair',side_effect=AssertionError('unexpected recovery')):
   return hs.inspect_changed_official_archive(self.s,self.now)
 def test_real_missing_bar_changes_report_without_manual_stock_selection(self):
  self.seed(missing=True);first=self.turn();self.assertEqual(first['state'],'AUDITED');self.assertNotEqual(first['quality'],'READY')
  self.seed();second=self.turn();self.assertEqual(second['quality'],'READY');self.assertEqual(second['count'],3)
  self.assertIsNone(self.s.setting('cp_eligibility:'+APPLE));self.assertIsNone(self.s.setting('cp_owner_risk:'+APPLE))
 def test_invalid_and_unknown_remain_blocked_no_ohlcv_mutation(self):
  self.seed(invalid=True);before=self.f.rows();self.assertEqual(self.turn()['quality'],'INVALID');self.assertEqual(self.f.rows(),before)
  self.seed(missing=True);self.s.db.execute("DELETE FROM candles WHERE ts='2026-11-03 11:15:00'");self.s.db.commit()
  self.assertIn(self.turn()['quality'],('GAP','UNCONFIRMED'));self.assertIsNone(self.s.setting('interval_evidence:'+APPLE))
 def test_restart_and_unchanged_revision_skip_audit_and_writes(self):
  self.seed();self.turn();before=self.s.db.total_changes
  # No transient cursor/cache is needed: a new facade uses the durable progress.
  other=SimpleNamespace(**self.s.__dict__,setting=self.s.setting,connect=self.s.connect,instruments=self.s.instruments)
  with patch.object(dq,'audit',side_effect=AssertionError('repeat audit')):
   self.assertEqual(hs.inspect_changed_official_archive(other,self.now)['state'],'IDLE')
  self.assertEqual(self.s.db.total_changes,before)
 def test_changed_evidence_rechecks_same_unchanged_ohlcv(self):
  self.seed(missing=True);self.turn();before=self.f.rows()
  self.s.set_setting('interval_evidence:'+APPLE,{'revision_note':'new unresolved evidence'})
  self.assertEqual(self.turn()['state'],'AUDITED');self.assertEqual(self.f.rows(),before)
 def test_revision_race_does_not_publish_stale_report(self):
  self.seed();original=dq.audit
  def raced(*a,**k):
   result=original(*a,**k)
   self.s.db.execute("UPDATE candles SET close=100.5 WHERE ts='2026-11-03 11:00:00'");self.s.db.commit()
   return result
  with patch.object(dq,'audit',side_effect=raced),self.assertRaisesRegex(ValueError,'input changed'):self.turn()
  self.assertIsNone(self.s.setting('history_archive_progress:'+APPLE));self.assertIsNone(self.s.setting('gettex_cached_history'))
 def test_expired_network_retry_is_not_reset_or_used_by_local_audit(self):
  self.seed(missing=True);key=dq.key(dict(isin=APPLE),APPLE,'15m');retry={'next_retry':'2020-01-01T00:00Z','attempts':4,'state':'GAP'}
  self.s.set_setting(key,retry);self.turn();self.assertEqual(self.s.setting(key),retry)
  self.assertEqual(self.turn()['state'],'IDLE')
 def test_unregistered_and_other_provider_archives_are_not_selected(self):
  self.seed(SAP);self.s.db.execute('DELETE FROM instruments WHERE isin=?',(SAP,))
  self.s.db.execute('INSERT INTO series VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',(APPLE,'proxy','FRA','EUR','APC.F','15m','2026-11-03 11:00:00',100,101,99,100,2));self.s.db.commit()
  self.assertEqual(self.turn()['state'],'IDLE');self.assertIsNone(self.s.setting('history_archive_progress:'+SAP))
 def test_disabled_policy_cannot_start_local_stage(self):
  self.seed();self.s.set_setting('cp_gettex_source_policy',dict(POLICY,approved=False));before=self.s.db.total_changes
  self.assertEqual(self.turn()['state'],'NOT_ENABLED');self.assertEqual(before,self.s.db.total_changes)
 def test_targeted_audit_does_not_erase_other_pending_units(self):
  self.seed(base='5m');self.seed(base='15m')
  first=self.turn();progress=self.s.setting('history_archive_progress:'+APPLE)
  self.assertEqual(progress['pending'],1);self.assertEqual(len(progress['series']),1)
  second=self.turn();self.assertNotEqual(first['base'],second['base'])
  self.assertEqual(self.s.setting('history_archive_progress:'+APPLE)['pending'],0)
 def test_53_and_200_changed_jobs_are_fair_under_continuous_first_stock_updates(self):
  for size in (53,200):
   with self.subTest(size=size):
    self.s.db.execute('DELETE FROM candles');self.s.db.execute("DELETE FROM settings WHERE key LIKE 'history_archive_progress:%'");self.s.db.commit()
    for i in range(size):self.seed('US'+str(i).zfill(10))
    seen=set()
    for _ in range(size):
     out=self.turn();seen.add(out['isin'])
     self.s.db.execute("UPDATE candles SET close=CASE WHEN close=100 THEN 100.1 ELSE 100 END WHERE isin='US0000000000'");self.s.db.commit()
    self.assertEqual(len(seen),size)

class WorkerRoutes(unittest.TestCase):
 def host(self):
  from test_cp_fresh_fair_share import Memory
  s=Memory();self.addCleanup(s.db.close)
  return SimpleNamespace(store=s,_parse_gettex_csv=Mock(),_calculate=Mock(return_value={'errors':[]}))
 def stages(self,stack):
  for name in ('cp_controls.automatic_turn','cp_controls.tv_risk_turn','cp_controls.reviewed_risk_turn','cp_reconciliation.turn'):
   stack.enter_context(patch(name,return_value={}))
 def test_short_history_jobs_import_recheck_then_analyze_without_network(self):
  for route in ('_eligibility_only','_review_only'):
   host=self.host();calls=[];host._calculate.side_effect=lambda info:calls.append('analyze') or {'errors':[]}
   with ExitStack() as stack:
    self.stages(stack)
    batch=stack.enter_context(patch('gettex_history.accumulate_cached_batch',side_effect=lambda *a,**k:calls.append('import') or {'pairs_processed':2}))
    stack.enter_context(patch.object(hs,'inspect_changed_official_archive',side_effect=lambda *a:calls.append('audit') or {'state':'AUDITED'}))
    out=FeedContext.calculate(host,{'isin':APPLE,route:True})
   self.assertEqual(calls,['import','audit','analyze']);self.assertEqual(batch.call_args.kwargs,dict(max_pairs=2,max_seconds=1))
   self.assertEqual(out['cp_local_history']['archive_quality']['state'],'AUDITED')
 def test_fresh_price_route_has_no_extra_archive_work(self):
  with ExitStack() as stack:
   self.stages(stack);batch=stack.enter_context(patch('gettex_history.accumulate_cached_batch'));audit=stack.enter_context(patch.object(hs,'inspect_changed_official_archive'))
   FeedContext.calculate(self.host(),{'isin':APPLE,'_gettex_poll_only':True})
  batch.assert_not_called();audit.assert_not_called()
 def test_normal_route_does_not_duplicate_existing_import(self):
  with ExitStack() as stack:
   self.stages(stack);batch=stack.enter_context(patch('gettex_history.accumulate_cached_batch'));audit=stack.enter_context(patch.object(hs,'inspect_changed_official_archive',return_value={'state':'IDLE'}))
   FeedContext.calculate(self.host(),{'isin':APPLE})
  batch.assert_not_called();audit.assert_called_once()
 def test_local_error_visible_and_does_not_stop_other_feed_work(self):
  host=self.host()
  with ExitStack() as stack:
   self.stages(stack);stack.enter_context(patch.object(hs,'inspect_changed_official_archive',side_effect=ValueError('changed revision')))
   out=FeedContext.calculate(host,{'isin':APPLE})
  host._calculate.assert_called_once();self.assertEqual(out['cp_local_history']['error'],'changed revision')
  self.assertEqual(out['errors'],['CP local history: changed revision'])

if __name__=='__main__':unittest.main()
