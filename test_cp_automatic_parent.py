"""Unreviewed15m parents must reach the existing strict GETTEX writer."""
import copy,os,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import pandas as pd
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
import cp_reconciliation as rv
import revision_watcher as rw
from test_cp_tv_legacy_reference import FlatStore

class AutomaticParent(unittest.TestCase):
 def fixture(self):
  root=Path(__file__).resolve().parents[2]/'СМ-Архив'
  f=tempfile.NamedTemporaryFile(dir=root,prefix='CP parent isolated ',suffix='.sqlite3',delete=False);path=Path(f.name);f.close();s=FlatStore(path)
  self.addCleanup(lambda:[Path(str(path)+suffix).unlink(missing_ok=True) for suffix in ('','-wal','-shm')])
  self.addCleanup(s.read_cache.close)
  isin='US0378331005';now=pd.Timestamp.now(tz='UTC');info=dict(isin=isin,name='Fixture',kind='stock',history_venue_lock='GETTEX');s.save_instrument(info)
  meta=dict(source='gettex_chart',venue='GETTEX',currency='EUR',symbol='APC.GTX',timezone='Europe/Berlin');official={**meta,'source':'gettex','symbol':isin}
  first,end,gap='2026-11-13 09:00:00','2026-11-13 09:45:00','2026-11-13 09:15:00'
  children=[(first,100.,100.,100.,100.,2.),('2026-11-13 09:30:00',101.,101.,101.,101.,3.)];rows={'5m':children,'15m':children}
  proof=dict(isin=isin,identity=official,basis='complete_published_trade_files',checked_at=now.isoformat(),no_trades=[],trade_intervals=[],zero_quantity_intervals=[],reference={'GETTEX:20261113':{}})
  for at in pd.date_range(first,end,freq='5min',inclusive='left'):
   proof['trade_intervals' if str(at) in {r[0] for r in children} else 'no_trades'].append([str(at),str(at+pd.Timedelta(minutes=5))])
  for tm in ('08.15','08.30','08.45'):
   for board in ('munc','mund'):proof['reference']['GETTEX:20261113']['posttrade.20261113.'+tm+'.'+board+'.csv.gz']='a'*64
  s.set_setting(rw.RETIME_POLICY,dict(version=rw.RETIME_VERSION,approved=True));s.set_setting(rv.SOURCE_POLICY,dict(version=1,approved=True,scope='registered_stocks',exclusive_venue='GETTEX'));s.set_setting('interval_evidence:'+isin,proof)
  for base in rows:s.set_setting('gettex_chart:'+isin+':'+base,dict(state='SAVED',identity=meta,listing_proof={**meta,'isin':isin,'status':'CONFIRMED','verified_at':now.isoformat()}))
  with s.connect() as db:
   for base in rows:
    db.executemany('INSERT INTO candles VALUES (?,?,?,?,?,?,?,?,?)',[(isin,'gettex',base,*r) for r in rows[base]])
    db.executemany('INSERT INTO series VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',[(isin,*[meta[k] for k in rw.IDENTITY[:4]],base,*r) for r in rows[base]])
   db.executemany('INSERT INTO trades VALUES (?,?,?,?)',[(isin,str(pd.Timestamp(r[0])+pd.Timedelta(minutes=1)),r[1],r[5]) for r in children])
   job,_=rv._enqueue(db,rv._spec(isin,meta,'15m',gap,gap),{'kind':'fixture'},now-pd.Timedelta(minutes=1));old=copy.deepcopy(job);job.update(status='AWAITING_USER',finding=rv._classify(job,rv._input(db,job),now),checks=1);rv._save_job(db,job,old)
  return s,info,meta,proof,job,now
 def test_due_parent_is_processed_without_tv_and_guard_failures_stay_unresolved(self):
  for variant in ('complete','missing_pair','changed_parent','policy_revoked'):
   with self.subTest(variant=variant):
    s,info,meta,proof,job,now=self.fixture();isin=info['isin']
    if variant=='missing_pair':
     proof['reference']['GETTEX:20261113'].pop('posttrade.20261113.08.30.mund.csv.gz');s.set_setting('interval_evidence:'+isin,proof)
    if variant=='changed_parent':
     with s.connect() as db:db.execute("UPDATE series SET volume=volume+1 WHERE base_tf='15m'")
    if variant=='policy_revoked':s.set_setting(rw.RETIME_POLICY,dict(version=rw.RETIME_VERSION,approved=False))
    with s.connect() as db:
     before=db.execute('SELECT * FROM series ORDER BY base_tf,ts').fetchall();selected=rv.native_empty_batches(db,[info]);self.assertEqual(bool(selected),variant!='policy_revoked')
    if variant=='policy_revoked':continue
    with patch('gettex_history.attach_known_interruptions',side_effect=AssertionError('No provider download')):result=rv.turn(s,info,now,native_empty_only=True)
    self.assertEqual(result['checked'],1);saved=s.setting(rv.key(job));self.assertEqual(saved['automatic_parent_algorithm'],1)
    with s.connect() as db:
     self.assertEqual(db.execute('SELECT * FROM series ORDER BY base_tf,ts').fetchall(),before);self.assertEqual(db.execute('SELECT count(*) FROM candle_revision_history').fetchone()[0],0)
     self.assertFalse(rv._native_empty_pending(db,isin,8))
    self.assertIsNone(s.setting('cp_manual_risk:'+isin))
    if variant=='complete':
     self.assertEqual(saved['status'],'RESOLVED_NATIVE');self.assertEqual(saved['finding']['state'],'NO_TRADES');receipt=s.setting(rw.RETIME_PREFIX+isin+':'+job['id']);self.assertEqual((receipt['added'],receipt['changed']),(0,0))
     with s.connect() as db:self.assertTrue(rw.retime_current(db,receipt,now))
    else:self.assertNotEqual(saved['status'],'RESOLVED_NATIVE');self.assertIn('RETIME_',saved['official_timebase_repair']['reason'])
 def test_53_200_due_parents_are_bounded_and_do_not_repeat_completed_attempt(self):
  for count in (53,200):
   with self.subTest(count=count):
    s,info,meta,proof,job,now=self.fixture();isin=info['isin']
    with s.connect() as db:
     for n in range(count):
      stamp=str(pd.Timestamp('2026-10-01 10:00')+pd.Timedelta(minutes=15*n));spec=rv._spec(isin,meta,'15m',stamp,stamp);j,_=rv._enqueue(db,spec,{'kind':'fixture'},now-pd.Timedelta(minutes=1));old=copy.deepcopy(j);j.update(status='AWAITING_SOURCE',finding=dict(state='SOURCE_TIMEBASE_UNCONFIRMED'));rv._save_job(db,j,old)
     first=rv._native_empty_pending(db,isin,8);self.assertEqual(len(first),8)
     for key,raw in first:
      import json
      j=json.loads(raw);old=copy.deepcopy(j);j['automatic_parent_algorithm']=1;rv._save_job(db,j,old)
     second=rv._native_empty_pending(db,isin,8);self.assertEqual(len(second),8);self.assertFalse({k for k,v in first}&{k for k,v in second})

 def test_current_official_resolution_is_shared_with_diagnostics_and_revokes_on_real_change(self):
  import json,time
  from mcp_observability import exact_reconciliation
  s,info,meta,proof,job,now=self.fixture();isin=info['isin']
  with patch('gettex_history.attach_known_interruptions',side_effect=AssertionError('No provider')):rv.turn(s,info,now,native_empty_only=True)
  reports={'data_quality:fixture:15m':dict(state='UNCONFIRMED',identity=meta,base_tf='15m',ranges=[[job['first'],job['last']]])}
  def snapshot():
   with s.connect() as db:
    db.execute('PRAGMA query_only=ON');db.execute('BEGIN')
    return exact_reconciliation(db,info,reports,now,time.monotonic()+3)
  current=snapshot();self.assertEqual(current['official_confirmed_scopes'],1);self.assertEqual(current['unverified_saved_scopes'],0)
  with s.connect() as db:db.execute("UPDATE series SET volume=volume+1 WHERE base_tf='15m'")
  changed=snapshot();self.assertEqual(changed['official_confirmed_scopes'],0);self.assertEqual(changed['unverified_saved_scopes'],1)
  with s.connect() as db:db.execute("UPDATE series SET volume=volume-1 WHERE base_tf='15m'")
  reviewed={**job,'id':'conflicting-review','observation':dict(result='PRESENT',identity_confirmed=True),'source_comparison':dict(observation_current=True)}
  with s.connect() as db:rv.write(db,rv.key(reviewed),reviewed)
  conflict=snapshot();self.assertEqual(conflict['official_confirmed_scopes'],0);self.assertEqual(conflict['unverified_saved_scopes'],1)
  self.assertIsNone(conflict['unresolved_errors'])

 def test_diagnostic_retry_reuses_only_same_readonly_view_without_skipping_validation(self):
  import time
  from mcp_observability import exact_reconciliation
  s,info,meta,proof,job,now=self.fixture();isin=info['isin']
  with patch('gettex_history.attach_known_interruptions',side_effect=AssertionError('No provider')):rv.turn(s,info,now,native_empty_only=True)
  reports={'scope:15m':dict(state='UNCONFIRMED',identity=meta,base_tf='15m',ranges=[[job['first'],job['last']]])};memo={}
  with s.connect() as db:
   db.execute('PRAGMA query_only=ON');db.execute('BEGIN')
   with patch('revision_watcher.retime_current',wraps=rw.retime_current) as validator:
    first=exact_reconciliation(db,info,reports,now,time.monotonic()+3,_read_memo=memo)
    second=exact_reconciliation(db,info,reports,now,time.monotonic()+3,_read_memo=memo)
    self.assertEqual(first['official_confirmed_scopes'],1);self.assertEqual(second['official_confirmed_scopes'],1);self.assertEqual(validator.call_count,1)
  with s.connect() as db:
   db.execute('PRAGMA query_only=ON');db.execute('BEGIN')
   with self.assertRaisesRegex(ValueError,'VIEW_CHANGED'):exact_reconciliation(db,info,reports,now,time.monotonic()+3,_read_memo=memo)
  with s.connect() as db:
   with self.assertRaisesRegex(ValueError,'READONLY_VIEW'):exact_reconciliation(db,info,reports,now,time.monotonic()+3,_read_memo={})

if __name__=='__main__':unittest.main()
