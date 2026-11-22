"""A terminal repair must wake for new SAME-source children, not foreign tails."""
import json,sys,unittest
from pathlib import Path
from unittest.mock import Mock,patch
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
import pandas as pd
import cp_controls as cp
import test_cp_automatic_repair as fixture

class ChildArrival(unittest.TestCase):
 def setUp(self):
  self.f=fixture.AutomaticRepair();self.f.setUp();self.addCleanup(self.f.doCleanups)
  f=self.f
  # Private saved market replay stays local. CI uses explicit test-only values,
  # never an export of account data or a synthetic production candle.
  path=Path('C:/Users/panus/Documents/Codex/СМ-Архив/CP child arrival actual fixture.json')
  self.actual=json.loads(path.read_text(encoding='utf8')) if path.is_file() else {
   'parent':['2026-11-19 08:00:00',100,104,99,103,9],
   'neighbors':[['2026-11-19 07:45:00',99,101,98,100,1],['2026-11-19 08:15:00',103,105,102,104,1]],
   'children':[['2026-11-19 08:00:00',100,102,99,101,2],['2026-11-19 08:05:00',101,103,100,102,3],['2026-11-19 08:10:00',102,104,101,103,4]]}
  import data_quality as dq
  from performance_cache import install_input_revisions
  from candle_timing import install
  from history_service import attach_parent_provenance
  from types import SimpleNamespace
  install_input_revisions(f.s.db);install(f.s.db);f.s.db.commit()
  def token(scopes):return {k:(f.s.db.execute('SELECT revision FROM input_revisions WHERE scope=?',(k,)).fetchone() or [0])[0] for k in scopes}
  f.s.read_cache=SimpleNamespace(token_for=token,depend=lambda *a:None)
  def load(isin,base='15m'):
   rows=f.s.db.execute("SELECT ts,open,high,low,close,volume FROM candles WHERE isin=? AND source='gettex' AND base_tf=? ORDER BY ts",(isin,base)).fetchall()
   frame=pd.DataFrame(rows,columns=['ts',*dq.FIELDS]).set_index('ts');frame.index=pd.to_datetime(frame.index);frame.attrs=f.meta.copy()
   return attach_parent_provenance(f.s,dq.attach_interval_evidence(f.s,frame,isin),isin,base)
  f.s.load_official=load;f.s.load_series=lambda *a,**k:pd.DataFrame()
  f.s.db.execute('DELETE FROM candles')
  f.s.db.executemany('INSERT INTO candles VALUES (?,?,?,?,?,?,?,?,?)',[(f.isin,'gettex','15m',*r) for r in self.actual['neighbors']]);f.s.db.commit()
  f.span=[self.actual['parent'][0]]*2;f.now=pd.Timestamp('2026-11-22T08:00:00Z')
  f.s.set_setting(f.qkey,{**f.q,'ranges':[f.span]});f.s.set_setting('cp_eligibility:'+f.isin,{})
  f.plan();f.terminal()
 def add_children(self,source='gettex',shift=0):
  f=self.f
  rows=[(f.isin,source,'5m',str(pd.Timestamp(r[0])+pd.Timedelta(days=shift)),*r[1:]) for r in self.actual['children']]
  f.s.db.executemany('INSERT INTO candles VALUES (?,?,?,?,?,?,?,?,?)',rows);f.s.db.commit()
 def test_new_real_children_reopen_and_recover_without_provider_or_user(self):
  f=self.f;self.add_children()
  self.assertEqual(f.plan()['state'],'QUEUED')
  fetch=Mock(side_effect=AssertionError('Local confirmed children must suffice'))
  with patch('requests.get',side_effect=AssertionError('No network replay')):
   result=cp.service_request(f.info,f.s,fetch,f.now)
   if result is None:result=cp.service_request(f.info,f.s,fetch,f.now)
  self.assertEqual(result['state'],'COMPLETED');fetch.assert_not_called()
  self.assertEqual(f.s.db.execute("SELECT ts,open,high,low,close,volume FROM candles WHERE source='gettex' AND base_tf='15m' AND ts=?",(f.span[0],)).fetchone(),tuple(self.actual['parent']))
  self.assertIsNone(f.s.setting('cp_manual_risk:'+f.isin))
 def test_unrelated_or_foreign_children_do_not_reset_terminal_attempts(self):
  f=self.f;before=f.jobs();self.add_children(shift=1);self.add_children(source='foreign')
  self.assertEqual(f.plan()['state'],'CHECKED');self.assertEqual(f.jobs(),before)
  f.s.set_setting('data_quality:'+f.isin+':'+f.isin+':5m',{'state':'UNCONFIRMED','ranges':[['2026-10-01 08:00:00']*2],'checked_at':f.now.isoformat()})
  self.assertEqual(f.plan()['state'],'CHECKED');self.assertEqual(f.jobs(),before)
 def test_legacy_terminal_migration_keeps_attempts_and_weekly_deadline(self):
  f=self.f;jobs=f.jobs();key=next(iter(jobs));jobs[key]['origin'].pop('children_fingerprint')
  jobs[key]['attempts']=3;f.s.set_setting(f.key,jobs)
  before={k:jobs[key].get(k) for k in ('state','attempts','recheck_after','requested_at')}
  self.assertEqual(f.plan()['state'],'CHECKED')
  after=f.jobs()[key];self.assertEqual({k:after.get(k) for k in before},before)
  self.assertIn('children_fingerprint',after['origin'])
  stable=f.jobs();f.plan();self.assertEqual(f.jobs(),stable)
  self.add_children();self.assertEqual(f.plan()['state'],'QUEUED')
 def test_binding_supports_every_existing_ui_timeframe(self):
  from timeframes import LABELS,base_for
  from cp_reconciliation import _spec
  f=self.f
  for tf in LABELS:
   with self.subTest(tf=tf):
    base=base_for(tf,'gettex');at='2026-11-19 00:00:00' if base=='1D' else f.span[0]
    job=_spec(f.isin,f.meta,base,at,at)
    with f.s.connect() as db:self.assertTrue(cp.repair_children_fingerprint(db,job))

if __name__=='__main__':unittest.main()

