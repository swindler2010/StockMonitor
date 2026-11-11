import sys,json,copy,tempfile,time,threading,unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch
import pandas as pd
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
import cp_controls as cp, data_quality as dq, candle_schedule as cs, gettex_chart as gc
from storage import Store
import test_cp_controls as fixture

class Review(unittest.TestCase):
 setUp=fixture.Controls.setUp
 tearDown=fixture.Controls.tearDown
 rows=fixture.Controls.rows
 grant=fixture.Controls.grant
 gate=fixture.Controls.gate
 def begin(self):
  self.grant();g=self.store.setting('cp_manual_risk:'+self.isin)
  cp.begin_historical_review(self.store,self.isin,cp.digest(g));return g
 def test_suspend_preserves_original_receipts_and_blocks_new_risk_execution(self):
  original=self.begin()
  with self.assertRaisesRegex(ValueError,'REVIEW_PENDING'):self.gate()
  current=self.store.setting('cp_manual_risk:'+self.isin)
  self.assertEqual(current['accepted'],original['accepted'])
  self.assertEqual(current['historical_review']['targets'],original['accepted'])
  with self.assertRaisesRegex(ValueError,'INPUT_CHANGED'):cp.begin_historical_review(self.store,self.isin,'outdated')
 def test_unknown_not_irrecoverable_then_real_repair_revokes_only_exception(self):
  self.begin();r=cp.historical_review_turn(self.store,self.info,self.now)
  self.assertEqual(r['summary'],{'UNCONFIRMED':1});self.assertEqual(self.store.setting('cp_manual_risk:'+self.isin)['accepted'],{})
  f=self.frame.copy();f.index=pd.to_datetime(['2026-10-26 10:00']);self.store.save_frame(self.isin,'gettex','5m',f)
  r=cp.historical_review_turn(self.store,self.info,self.now)
  self.assertEqual(r['summary'],{'RESOLVED':1})
  with patch.object(dq,'audit',side_effect=AssertionError('unchanged data audited')):
   self.assertEqual(cp.historical_review_turn(self.store,self.info,self.now)['state'],'UNCHANGED')
 def test_invalid_real_candle_and_misaligned_range_stay_blocked(self):
  f=self.frame.copy();f.index=pd.to_datetime(['2026-10-26 10:00']);f['high']=90.;self.store.save_frame(self.isin,'gettex','5m',f)
  self.begin();r=cp.historical_review_turn(self.store,self.info,self.now)
  self.assertEqual(r['summary'],{'INVALID':1});self.assertFalse(self.store.setting('cp_manual_risk:'+self.isin)['accepted'])
 def test_closed_calendar_and_proven_no_trades_do_not_need_exceptions(self):
  self.q['ranges']=[['2026-10-25 10:00:00','2026-10-25 10:00:00'],['2026-10-26 10:00:00','2026-10-26 10:00:00']]
  self.store.set_setting(self.qkey,self.q)
  self.store.set_setting('interval_evidence:'+self.isin,dict(isin=self.isin,identity=self.meta,basis='complete_published_trade_files',reference='fixture-file-sha',checked_at=self.now.isoformat(),no_trades=[['2026-10-26 10:00:00','2026-10-26 10:05:00']],trade_intervals=[]))
  self.begin();r=cp.historical_review_turn(self.store,self.info,self.now)
  self.assertEqual(r['summary'],{'MARKET_CLOSED':1,'NO_TRADES':1})
 def test_empty_grid_inside_open_session_not_market_closed(self):
  self.q['ranges']=[['2026-10-26 10:03:00','2026-10-26 10:03:00']];self.store.set_setting(self.qkey,self.q)
  self.begin();r=cp.historical_review_turn(self.store,self.info,self.now)
  self.assertEqual(r['summary'],{'UNCONFIRMED':1})
 def test_retry_after_is_waiting_not_irrecoverable(self):
  self.q['next_retry']=(self.now+pd.Timedelta(hours=1)).isoformat();self.store.set_setting(self.qkey,self.q)
  self.begin();r=cp.historical_review_turn(self.store,self.info,self.now)
  self.assertEqual(r['summary'],{'WAITING':1});self.assertFalse(self.store.setting('cp_manual_risk:'+self.isin)['accepted'])
 def test_revision_race_does_not_commit_old_review(self):
  self.begin();audit=dq.audit
  def changing(*a,**kw):
   result=audit(*a,**kw)
   with self.store.connect() as db:db.execute('UPDATE input_revisions SET revision=revision+1 WHERE scope=?',('market:'+self.isin+':5m',))
   return result
  with patch.object(dq,'audit',side_effect=changing),self.assertRaisesRegex(ValueError,'INPUT_CHANGED'):cp.historical_review_turn(self.store,self.info,self.now)
  self.assertEqual(self.store.setting('cp_manual_risk:'+self.isin)['historical_review']['results'],{})
 def test_review_rechecks_once_after_retry_deadline_without_new_revision(self):
  end=self.now+pd.Timedelta(minutes=5)
  self.q['next_retry']=end.isoformat();self.store.set_setting(self.qkey,self.q)
  self.begin();self.assertEqual(cp.historical_review_turn(self.store,self.info,self.now)['summary'],{'WAITING':1})
  self.assertEqual(cp.historical_review_due(self.store,[self.info],self.now),[])
  self.assertEqual(len(cp.historical_review_due(self.store,[self.info],end)),1)
  result=cp.historical_review_turn(self.store,self.info,end)
  self.assertEqual(result['summary'],{'UNCONFIRMED':1})
  self.assertEqual(cp.historical_review_due(self.store,[self.info],end),[])
  self.assertFalse(self.store.setting('cp_manual_risk:'+self.isin)['accepted'])
 def test_batched_progress_survives_restart(self):
  q=dict(self.q,base_tf='15m');self.store.set_setting(dq.key(self.info,self.isin,'15m'),q)
  self.begin();first=cp.historical_review_turn(self.store,self.info,self.now);self.assertEqual(first['reviewed'],1)
  self.store.read_cache.close();self.store=Store(self.tmp.name)
  second=cp.historical_review_turn(self.store,self.info,self.now);self.assertEqual(second['reviewed'],2);self.assertEqual(second['state'],'COMPLETE')
  self.assertEqual(len(self.store.setting('cp_manual_risk:'+self.isin)['historical_review']['targets']),2)
 def test_changed_children_reopen_review_until_all_groups_current(self):
  self.store.set_setting(dq.key(self.info,self.isin,'15m'),dict(self.q,base_tf='15m'))
  self.begin()
  for _ in range(2):cp.historical_review_turn(self.store,self.info,self.now)
  with self.store.connect() as db:db.execute('INSERT OR REPLACE INTO input_revisions VALUES (?,?)',('market:'+self.isin+':1m',99))
  first=cp.historical_review_turn(self.store,self.info,self.now);self.assertEqual(first['state'],'RUNNING')
  with self.assertRaisesRegex(ValueError,'REVIEW_PENDING'):self.gate()
  second=cp.historical_review_turn(self.store,self.info,self.now);self.assertEqual(second['state'],'COMPLETE')
 def test_existing_worker_resumes_review_without_network_or_chart_open(self):
  from feed_worker import FeedContext
  self.begin();due=cp.historical_review_due(self.store,[self.info]);self.assertEqual(len(due),1)
  context=FeedContext(self.store)
  with patch.object(context,'_run_feed',side_effect=AssertionError('Unexpected HTTP turn')),patch('calculation_window.eligibility_turn') as analysis:
   result=context._calculate(due[0]);self.assertEqual(result['cp_review']['state'],'COMPLETE');analysis.assert_called_once()
  self.assertEqual(cp.historical_review_due(self.store,[self.info]),[])

class Registration(unittest.TestCase):
 def test_durable_before_feed_without_saving_other_drafts(self):
  from features import Features
  with tempfile.TemporaryDirectory() as directory:
   s=Store(directory);self.addCleanup(s.read_cache.close)
   old=dict(isin='OLD',name='old',drawings=[]);s.save_instrument(old)
   proof=dict(isin='DE0007164600',source='proxy',symbol='SAP.DE',venue='GER',currency='EUR',timezone='Europe/Berlin',status='CONFIRMED')
   item=dict(isin=proof['isin'],name='SAP',history_symbol='SAP.DE',history_venue='GER',history_currency='EUR',eur_listings=[proof])
   app=NS(store=s,watchlist=[dict(old,drawings=['unsaved'])],persisted=[old],user_dirty=True)
   calls=[]
   def feed(i):self.assertEqual(s.instruments()[-1],item);calls.append(i['isin'])
   app.start_feed=feed;Features.register_confirmed_instrument(app,item)
   self.assertEqual(calls,[item['isin']]);self.assertEqual(s.instruments()[0],old);self.assertTrue(app.user_dirty)
   self.assertEqual(app.persisted[-1],item)
   with self.assertRaisesRegex(ValueError,'already exists'):Features.register_confirmed_instrument(app,item)
   with self.assertRaisesRegex(ValueError,'IDENTITY'):Features.register_confirmed_instrument(app,dict(item,isin='WRONG'))
   s.read_cache.close()

class ReviewAfterRecovery(unittest.TestCase):
 def test_existing_local_recovery_invalidates_review_and_preserves_exact_real_ohlcv(self):
  import test_local_recovery_cooldown30 as old
  fixture=old.LocalRecoveryCooldown();fixture.setUp()
  self.addCleanup(fixture.doCleanups)
  store=fixture.store;info=fixture.info;meta=fixture.meta
  report=dq.inspect_cached(info,store,meta['symbol'],'15m',meta,fixture.now)[1]
  store.set_setting(dq.key(info,meta['symbol'],'15m'),report)
  with store.connect() as db:problems=cp.view(db,info['isin'])
  cp.accept(store,info['isin'],problems,True)
  grant=store.setting('cp_manual_risk:'+info['isin'])
  cp.begin_historical_review(store,info['isin'],cp.digest(grant))
  before=cp.historical_review_turn(store,info,fixture.now)
  self.assertNotIn('RESOLVED',before['summary'])
  fixture.turn()
  after=cp.historical_review_turn(store,info,fixture.now)
  self.assertEqual(after['summary'],{'RESOLVED':1})
  pd.testing.assert_frame_equal(fixture.rows()[list(dq.FIELDS)],fixture.expected[list(dq.FIELDS)],check_dtype=False)
  self.assertFalse(store.setting('cp_manual_risk:'+info['isin'])['accepted'])
  with store.connect() as db:self.assertEqual(db.execute('SELECT count(*) FROM candle_recoveries').fetchone()[0],1)

class Activity(unittest.TestCase):
 def test_actual_worker_metadata_only_while_active_and_error_separate(self):
  from desktop014 import Desktop014
  labels=[];client=NS(closed=False,process=NS(poll=lambda:None),active=True,active_kind='feed_update')
  app=NS(compute_feed_update=client,closing=False,cp_error=None,bridge=NS(),
    _cp_active_feed={'isin':'US0378331005','phase':'Переоценка исторических разрешений'},
    cp_label=NS(configure=lambda **kw:labels.append(kw)),root=NS(after=lambda *args:None),_update_candlepilot=lambda:None)
  Desktop014._update_candlepilot(app)
  self.assertEqual(app.bridge.cp_activity['isin'],'US0378331005');self.assertEqual(labels[-1]['foreground'],'#22c55e')
  client.active=False;Desktop014._update_candlepilot(app)
  self.assertEqual(app.bridge.cp_activity['state'],'IDLE');self.assertNotIn('isin',app.bridge.cp_activity)
  app.cp_error='provider failure';Desktop014._update_candlepilot(app)
  self.assertEqual(app.bridge.cp_activity['reason'],'provider failure');self.assertEqual(app.bridge.cp_activity['state'],'ERROR')

class CleanMonthlyDescription(unittest.TestCase):
 def test_clean_real_monthly_core_does_not_claim_revoked_gap_permissions(self):
  import test_apple_monthly_warmup as fixture, analytics as a
  f=fixture.AppleMonthlyWarmup();f.setUpClass();f.setUp();self.addCleanup(f.tearDown)
  frames={tf:pd.read_pickle(fixture.E/(tf+'.pkl')) for tf in ('1D','1W','1M')}
  for frame in frames.values():frame.attrs['initialization_policy']=f.policy
  frames['1M']=fixture.g.monthly_warmup(f.store,f.info,frames['1M'],f.now)
  result=a.analyse_multiframe(frames,'1M',now=f.now)
  self.assertEqual(result['status'],'READY');self.assertFalse(result.get('accepted_risks'))
  self.assertTrue(any('без принятых пропусков' in w for w in result['warnings']))
  self.assertFalse(any('принят риск пропусков' in w for w in result['warnings']))

class HealthReview(unittest.TestCase):
 def test_permission_review_never_overwrites_problem_paging_or_writes_database(self):
  import candlepilot_health as health
  f=Review();f.setUp();self.addCleanup(f.tearDown);f.begin()
  before=f.store.setting('cp_manual_risk:'+f.isin)
  out=health.collect(f.store.path)
  self.assertIsNone(out['problem_review'])
  self.assertEqual(out['owner_controls'][f.isin]['historical_review']['total'],1)
  selected=health.collect(f.store.path,problem_isin=f.isin)
  self.assertEqual(selected['problem_review']['page'],0)
  self.assertEqual(selected['problem_review']['count'],1)
  self.assertEqual(f.store.setting('cp_manual_risk:'+f.isin),before)

class IndependentSeedPolicy(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.store=Store(self.tmp.name)
  self.addCleanup(self.tmp.cleanup);self.addCleanup(self.store.read_cache.close)
  E=Path(__file__).parent/'history64-evidence/apple-admission30'
  self.policy=json.loads((E/'policy.json').read_text(encoding='utf8'))
  self.now=pd.Timestamp(json.loads((E/'comparison.json').read_text(encoding='utf8'))['at'])
  self.frames={t:pd.read_pickle(E/('policy-'+t+'.pkl')) for t in ('15m','1D')}
  self.info=dict(isin=self.policy['isin'],name='Apple');self.store.save_instrument(self.info)
  self.grant=dict(account=self.policy['account'],accepted={},initialization_policy=self.policy)
  self.store.set_setting('cp_manual_risk:'+self.info['isin'],self.grant)
 def test_clean_real_history_keeps_separate_seed_policy_after_all_gaps_revoked(self):
  out=cp.accepted_gap_input(self.store,self.info,self.frames['1D'],'1D',self.now)
  self.assertEqual(out.attrs['initialization_policy'],self.policy)
  self.assertNotIn('accepted_gap_input',out.attrs);self.assertNotIn('accepted_risk',out.attrs)
  pd.testing.assert_frame_equal(out,self.frames['1D'])
 def test_unaccepted_gap_invalid_and_wrong_policy_not_promoted(self):
  for mode in ('gap','invalid','currency','revoked_policy'):
   with self.subTest(mode=mode):
    f=self.frames['15m'].copy();self.store.set_setting('cp_manual_risk:'+self.info['isin'],self.grant)
    if mode=='invalid':f.iloc[-1,f.columns.get_loc('high')]=0
    if mode=='currency':f.attrs['currency']='USD'
    if mode=='revoked_policy':self.store.set_setting('cp_manual_risk:'+self.info['isin'],{**self.grant,'initialization_policy':{**self.policy,'approved':False}})
    out=cp.accepted_gap_input(self.store,self.info,f,'15m',self.now)
    self.assertNotIn('accepted_gap_input',out.attrs);self.assertNotIn('accepted_risk',out.attrs)
    if mode!='gap':self.assertNotIn('initialization_policy',out.attrs)
    self.assertFalse(cp.analysis_quality_allowed(dq.audit(out,'15m',now=self.now),out.attrs.get('accepted_risk')))
 def test_only_old_owner_projection_requeued_once_without_unrelated_recalculation(self):
  import calculation_window as cw
  prior=dict(input_binding_version=2,closed_input_semantics=2,checked_at=self.now.isoformat(),candidates=[dict(owner_reviewed_analysis=True)])
  self.store.set_setting('cp_eligibility:'+self.info['isin'],prior)
  with patch.object(cw,'eligibility_state',return_value='CURRENT'):
   self.assertEqual(cw.eligibility_due(self.store,[self.info],self.now),[self.info])
   prior['candidates'][0]['owner_initialization_semantics']=2;self.store.set_setting('cp_eligibility:'+self.info['isin'],prior)
   self.assertEqual(cw.eligibility_due(self.store,[self.info],self.now),[])
   prior['candidates']=[{}];self.store.set_setting('cp_eligibility:'+self.info['isin'],prior)
   self.assertEqual(cw.eligibility_due(self.store,[self.info],self.now),[])

class Priority(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.s=Store(self.tmp.name)
  self.addCleanup(self.tmp.cleanup);self.addCleanup(self.s.read_cache.close)
  self.now=pd.Timestamp('2026-10-30T12:01Z');self.infos=[dict(isin=f'US{i:010d}',name=str(i)) for i in range(3)]
  for info in self.infos:
   self.s.save_instrument(info)
   for tf in ('5m','15m'):
    meta=gc.identity(info['isin'],'AAPL.GTX');self.s.set_setting(gc.state_key(info['isin'],tf),dict(state='SAVED',identity=meta,listing_proof=dict(meta,isin=info['isin'],status='CONFIRMED')))
  self.position=dict(isin='US0000000000',quantity='1',timeframe='15m',identity=gc.identity('US0000000000','AAPL.GTX'))
 def test_position_before_focus_then_fair_history_and_resume(self):
  focus=dict(isin='US0000000001',timeframe='15m');seen=[]
  with patch('paper_account.ledger',return_value={'positions':[self.position]}):
   for n in range(3):
    job,_=cs.fresh_candidate(self.s,self.infos,self.now,focus);seen.append(job['isin']);cs.fresh_finished(self.s,job,job['_cp_focus']['source'],'UNCHANGED',self.now)
   self.assertEqual(seen,['US0000000000','US0000000000','US0000000001'])
   self.assertIsNone(cs.fresh_candidate(self.s,self.infos,self.now,focus)[0])
   other=Store(self.tmp.name)
   try:self.assertIsNone(cs.fresh_candidate(other,self.infos,self.now,focus)[0])
   finally:other.read_cache.close()
   cs.history_finished(self.s,self.now)
   self.assertEqual(cs.fresh_candidate(self.s,self.infos,self.now,focus)[0]['isin'],'US0000000000')
 def test_position_does_not_bypass_cooldown_identity_or_closed_market(self):
  self.s.set_setting('cp_source_retry:gettex_chart',(self.now+pd.Timedelta(hours=1)).isoformat())
  self.s.set_setting('cp_source_retry:gettex',(self.now+pd.Timedelta(hours=1)).isoformat())
  with patch('paper_account.ledger',return_value={'positions':[self.position]}):
   self.assertIsNone(cs.fresh_candidate(self.s,self.infos,self.now)[0])
   self.assertIsNone(cs.fresh_candidate(self.s,self.infos,pd.Timestamp('2026-11-01T12:00Z'))[0])
 def test_single_writer_priority_and_drain_53_200(self):
  from performance_dispatcher import Dispatcher
  for count in (53,200):
   gate=threading.Event();started=threading.Event();order=[]
   d=Dispatcher(lambda cb,v:cb(v),sample=lambda _ : ({'cpu':0,'ram':0},None))
   try:
    d.submit(lambda:(started.set(),gate.wait(5)),lambda _:None,lambda e:self.fail(e),key='running',lane='feed_update')
    self.assertTrue(started.wait(3))
    for n in range(count):d.submit(lambda n=n:order.append(n),lambda _:None,lambda e:self.fail(e),key=str(n),isin=str(n),lane='feed_update')
    d.prioritize_feed('2',1.5);d.prioritize_feed('1',1.3);d.prioritize_feed('0',1.1);gate.set()
    until=time.monotonic()+5
    while len(order)<count and time.monotonic()<until:time.sleep(.01)
    self.assertEqual(order[:3],[0,1,2]);self.assertEqual(len(set(order)),count)
   finally:
    gate.set();d.close();d.monitor.join(3)
    for worker in d.threads:worker.join(3)

if __name__=='__main__':unittest.main()


