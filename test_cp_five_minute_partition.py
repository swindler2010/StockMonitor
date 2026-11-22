"""A unique complete tape partition moves a5m child, never guesses a candle."""
import copy,json,os,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import pandas as pd
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
import cp_reconciliation as rv
import revision_watcher as rw
from test_cp_tv_legacy_reference import FlatStore

class FiveMinutePartition(unittest.TestCase):
 def test_complete_conserved_partition_repairs5m_and_parents_but_rejects_conflicts_and_changed_inputs(self):
  root=Path(__file__).parent.parent.parent/'СМ-Архив';f=tempfile.NamedTemporaryFile(dir=root,prefix='CP partition5m isolated ',suffix='.sqlite3',delete=False);path=Path(f.name);f.close();s=FlatStore(path)
  try:
   isin='US0378331005';info=dict(isin=isin,name='Fixture',kind='stock',history_venue_lock='GETTEX');s.save_instrument(info);now=pd.Timestamp.now(tz='UTC')
   m=dict(source='gettex_chart',symbol='FIX.GTX',venue='GETTEX',currency='EUR',timezone='Europe/Berlin');om={**m,'source':'gettex','symbol':isin}
   first='2026-11-13 16:15:00';end='2026-11-13 16:45:00';a='2026-11-13 16:20:00';last='2026-11-13 16:25:00'
   native={'5m':[(first,100.,100.,100.,100.,20.),('2026-11-13 16:30:00',101.,101.,101.,101.,2.),('2026-11-13 16:35:00',101.,102.,101.,102.,7.),('2026-11-13 16:40:00',100.,100.,100.,100.,1.)],
    '15m':[(first,100.,100.,100.,100.,20.),('2026-11-13 16:30:00',101.,102.,100.,100.,10.)]}
   official={'5m':[native['5m'][0],(last,*native['5m'][1][1:]),*native['5m'][2:]],'15m':[(first,100.,101.,100.,101.,22.),('2026-11-13 16:30:00',101.,102.,100.,100.,8.)]}
   ticks=[('2026-11-13 16:16:00',100.,20.),('2026-11-13 16:26:00',101.,2.),('2026-11-13 16:36:00',101.,3.),('2026-11-13 16:37:00',102.,4.),('2026-11-13 16:41:00',100.,1.)]
   proof=dict(isin=isin,identity=om,basis='complete_published_trade_files',checked_at=now.isoformat(),no_trades=[],trade_intervals=[],zero_quantity_intervals=[],reference={'GETTEX:20261113':{}})
   for t in pd.date_range(first,end,freq='5min',inclusive='left'):
    field='trade_intervals' if str(t) in {r[0] for r in official['5m']} else 'no_trades';proof[field].append([str(t),str(t+pd.Timedelta(minutes=5))])
   for tm in ('15.30','15.45'):
    for board in ('munc','mund'):proof['reference']['GETTEX:20261113'][f'posttrade.20261113.{tm}.{board}.csv.gz']='a'*64
   policy=dict(version=1,approved=True,scope='registered_stocks',exclusive_venue='GETTEX');s.set_setting(rv.SOURCE_POLICY,policy);s.set_setting(rw.RETIME_POLICY,dict(approved=True,version=rw.RETIME_VERSION));s.set_setting('interval_evidence:'+isin,proof)
   for base in native:s.set_setting('gettex_chart:'+isin+':'+base,dict(state='SAVED',identity=m,listing_proof={**m,'isin':isin,'status':'CONFIRMED','verified_at':now.isoformat()}))
   with s.connect() as d:
    for base in native:
     d.executemany('INSERT INTO series VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',[(isin,*[m[k] for k in rw.IDENTITY[:4]],base,*r) for r in native[base]])
     d.executemany('INSERT INTO candles VALUES (?,?,?,?,?,?,?,?,?)',[(isin,'gettex',base,*r) for r in official[base]])
    d.executemany('INSERT INTO trades VALUES (?,?,?,?)',[(isin,*r) for r in ticks]);job,_=rv._enqueue(d,rv._spec(isin,m,'5m',a,last),{'kind':'fixture'},now);inputs=rv._input(d,job)
    old=dict(job);job.update(input_fingerprint=inputs['fingerprint'],fingerprint_version=rv.FINGERPRINT_VERSION,finding=rv._classify(job,inputs,now),status='AWAITING_SOURCE');rv._save_job(d,job,old);d.commit()
    plan=rw.retime_plan(d,job,inputs,now);self.assertIsNotNone(plan);self.assertEqual(plan['reviewed_tf'],'5m');self.assertEqual(plan['mode'],'VERIFIED_NATIVE_TRADE_PARTITION');self.assertEqual(plan['relocations'][0]['new_at'],last)
    protected,_=rv._enqueue(d,rv._spec(isin,m,'5m',a,a),{'kind':'different_visual_scope'},now)
    bound=rv._input(d,protected);protected.update(native_neighbors=bound['neighbors'],observation=dict(result='ABSENT',at=(now-pd.Timedelta(seconds=1)).isoformat(),input_fingerprint=bound['fingerprint'],fingerprint_version=rv.FINGERPRINT_VERSION));rv.write(d,rv.key(protected),protected)
    with self.assertRaisesRegex(ValueError,'TV_BOUND_CONTEXT_CHANGED'):rw.retime_plan(d,job,inputs,now)
    diagnosed={**job,'observation':dict(result='DIFFERENT',at=now.isoformat(),input_fingerprint=inputs['fingerprint'],fingerprint_version=rv.FINGERPRINT_VERSION)}
    self.assertIsNotNone(rw.retime_plan(d,diagnosed,inputs,now))
    other,_=rv._enqueue(d,rv._spec(isin,m,'5m',last,last),{'kind':'contradicting_visual_scope'},now);ob=rv._input(d,other)
    other.update(observation=dict(result='ABSENT',at=now.isoformat(),input_fingerprint=ob['fingerprint'],fingerprint_version=rv.FINGERPRINT_VERSION));rv.write(d,rv.key(other),other)
    with self.assertRaisesRegex(ValueError,'OTHER_CURRENT_TV_REFERENCE_CONFLICT'):rw.retime_plan(d,diagnosed,inputs,now)
    d.rollback()
    conflict={**job,'observation':dict(result='ABSENT',identity_confirmed=True,tv_symbol='GETTEX:FIX',at=now.isoformat(),input_fingerprint=inputs['fingerprint'],fingerprint_version=rv.FINGERPRINT_VERSION)}
    with self.assertRaisesRegex(ValueError,'VISUAL_REFERENCE_CONFLICT'):rw.retime_plan(d,conflict,inputs,now)
    d.execute("UPDATE series SET volume=volume+1 WHERE base_tf='15m'")
    with self.assertRaisesRegex(ValueError,'RETIME_'):rw.publish_retime(d,job,inputs,plan,now)
    d.rollback()
   s.set_setting(rv.SOURCE_POLICY,{**policy,'approved':False})
   with s.connect() as d:self.assertIsNone(rw.retime_plan(d,job,rv._input(d,job),now))
   s.set_setting(rv.SOURCE_POLICY,policy)
   with patch('gettex_history.attach_known_interruptions',return_value=False):
    rv.turn(s,info,now,native_empty_only=True)
    self.assertEqual(s.setting(rv.key(job))['status'],'RESOLVED_NATIVE')
   receipt=s.setting(rw.RETIME_PREFIX+isin+':'+job['id']);self.assertEqual(receipt['relocated'],1);self.assertEqual(receipt['added'],0);self.assertEqual(receipt['changed'],2)
   with s.connect() as d:
    for base in native:self.assertEqual(rw._native(d,isin,m,base,first,end),official[base])
    self.assertTrue(rw.retime_current(d,receipt));self.assertEqual(rv._classify(job,rv._input(d,job),now)['state'],'NATIVE_ROWS_CONFIRMED')
    self.assertGreater(d.execute('select count(*) from candle_revision_history').fetchone()[0],0)
    # A different mixed gap must reuse only the fully validated primary
    # window. A stale raw DQ label must not hide the real restoration.
    mixed=rv._spec(isin,m,'5m',a,'2026-11-13 16:30:00')
    self.assertEqual(rv._classify(mixed,rv._input(d,mixed),now)['state'],'NATIVE_ROWS_CONFIRMED')
    from cp_problem_view import native_source_resolution,confirmed_native_repair,present
    problem=dict(isin=isin,identity=m,tf='5m',kind='archive',state='UNCONFIRMED',range=[a,'2026-11-13 16:30:00'])
    resolution=native_source_resolution(d,problem,[],{});self.assertIsNotNone(resolution)
    verified={**problem,'native_resolution':resolution};self.assertTrue(confirmed_native_repair(verified));self.assertFalse(present(verified,[])['action']['check'])
    # A current contradictory review, partial broader range, changed native
    # candle or revoked proof cannot be converted into a completed problem.
    current=rv._input(d,mixed);dispute={**mixed,'observation':dict(result='DIFFERENT',input_fingerprint=current['fingerprint'],fingerprint_version=rv.FINGERPRINT_VERSION)}
    self.assertIsNone(native_source_resolution(d,problem,[dispute],{}))
    absent=rv._spec(isin,m,'5m',last,last);binding=rv._input(d,absent)
    absent['observation']=dict(result='ABSENT',input_fingerprint=binding['fingerprint'],fingerprint_version=rv.FINGERPRINT_VERSION)
    self.assertIsNone(native_source_resolution(d,problem,[absent],{}))
    broader={**problem,'range':[a,'2026-11-13 16:45:00']};self.assertIsNone(native_source_resolution(d,broader,[],{}))
    d.execute("UPDATE series SET high=high+1 WHERE base_tf='5m' AND ts='2026-11-13 16:35:00'")
    self.assertIsNone(native_source_resolution(d,problem,[],{}));self.assertNotEqual(rv._classify(mixed,rv._input(d,mixed),now)['state'],'NATIVE_ROWS_CONFIRMED');d.rollback()
   self.assertIsNone(s.setting('cp_manual_risk:'+isin))
  finally:
   s.read_cache.close()
   for suffix in ('','-wal','-shm'):Path(str(path)+suffix).unlink(missing_ok=True)

 def test_open_problem_list_refreshes_on_new_evidence_not_unchanged_poll(self):
  from types import SimpleNamespace
  from unittest.mock import Mock
  from cp_problem_view import Dialog
  def run(work,done,*args,**kwargs):done(work());return True
  ctx=SimpleNamespace(win=Mock(),watch_timer=None,closed=False,watch_busy=False,generation=0,
   choice=SimpleNamespace(current=lambda:0),infos=[{'isin':'A'}],
   app=SimpleNamespace(store=SimpleNamespace(path='fixture'),job=run),apply_receipts=Mock(return_value=False),refresh=Mock(),schedule_watch=Mock())
  with patch('cp_problem_view.read_requests',side_effect=[{'__evidence_clock':['proof1',1]},{'__evidence_clock':['proof1',1]},{'__evidence_clock':['proof2',1]}]):
   Dialog.watch(ctx);self.assertEqual(ctx.refresh.call_count,1)
   Dialog.watch(ctx);self.assertEqual(ctx.refresh.call_count,1)
   Dialog.watch(ctx);self.assertEqual(ctx.refresh.call_count,2)
  self.assertEqual(ctx.apply_receipts.call_args.args[0],{})

 def test_short_source_turn_finishes_checked_visual_policy_without_provider_or_core(self):
  from types import SimpleNamespace
  from unittest.mock import Mock
  from feed_worker import FeedContext
  ctx=SimpleNamespace(store=SimpleNamespace(setting=Mock(return_value={})),_calculate=Mock(side_effect=AssertionError('No provider/CORE')))
  info=dict(isin='A',_native_empty_only=True)
  with patch('cp_reconciliation.turn',return_value={'observations_checked':1}),patch('cp_controls.tv_risk_turn',return_value={'analysis_requested_at':'current'}) as review,patch('cp_assessment.request') as request:
   result=FeedContext.calculate(ctx,info)
   review.assert_called_once_with(ctx.store,info);request.assert_called_once_with(ctx.store,'A');self.assertIn('cp_tv_risk',result)
   ctx._calculate.assert_not_called()

if __name__=='__main__':unittest.main()
