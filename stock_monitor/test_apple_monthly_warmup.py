import copy,json,hashlib,tempfile,unittest
from pathlib import Path
import pandas as pd
import gettex_chart as g,analytics as a,data_quality as dq
from storage import Store
from snapshot_worker import SnapshotContext
from performance_cache import capture_closed_inputs,validate_closed_inputs,InputRevisionChanged
E=Path(__file__).resolve().parents[1]/'history64-evidence/apple-analysis11-30'

class AppleMonthlyWarmup(unittest.TestCase):
 @classmethod
 def setUpClass(cls):
  cls.receipt=json.loads((E/'native-monthly-probe.json').read_text(encoding='utf8'))
  cls.now=pd.Timestamp(cls.receipt['at']);cls.meta=cls.receipt['identity'];cls.isin='US0378331005'
  cls.sha=hashlib.sha256((E/'native-monthly-probe.json').read_bytes()).hexdigest()
  cls.native=g.decode_monthly_warmup(cls.receipt['payload'],cls.meta,cls.isin,cls.now,cls.sha)
  cls.daily=pd.read_pickle(E/'full1D.pkl')
  cls.native=cls.native.loc[cls.native.index<'2021-01-01'].copy()
  cls.native.attrs['native_monthly']={**cls.native.attrs['native_monthly'],'last':str(cls.native.index[-1])}
  cls.info={'isin':cls.isin,'name':'Apple','history_venue_lock':'GETTEX'}
  cls.policy=json.loads((E/'applied.json').read_text(encoding='utf8'))['policy'];cls.policy['older_monthly_warmup']=True
  cls.current=pd.read_pickle(E/'1M.pkl');cls.current.attrs['initialization_policy']=cls.policy
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.store=Store(self.tmp.name)
  self.store.save_instrument(self.info);self.store.save_series(self.info,self.native,'1M')
  self.store.save_series(self.info,self.daily,'1D')
  self.store.set_setting(g.state_key(self.isin,'1D'),json.loads((E/'daily-state.json').read_text(encoding='utf8')))
  self.store.set_setting('cp_manual_risk:'+self.isin,{'initialization_policy':self.policy})
  self.store.set_setting(g.state_key(self.isin,'1M'),dict(state='PARTIAL',identity=self.meta,listing_proof=self.native.attrs['listing_proof'],native_monthly=self.native.attrs['native_monthly'],series_digest=g.monthly_digest(self.native)))
 def tearDown(self):self.store.read_cache.close();self.tmp.cleanup()
 def test_native_receipt_and_exact_overlap_preserve_current_rows(self):
  self.assertEqual(len(self.native),71)
  self.assertEqual(self.native.attrs['native_monthly']['excluded_invalid_months'],['2015-01-01 00:00:00'])
  out=g.monthly_warmup(self.store,self.info,self.current,self.now)
  self.assertEqual(out.attrs['data_quality']['state'],'READY');self.assertEqual(len(out),140)
  self.assertTrue(out.loc[self.current.index,list(g.FIELDS)].equals(self.current[list(g.FIELDS)]))
  self.assertEqual(out.attrs['analysis_scope'],self.current.attrs['analysis_scope'])
  self.assertEqual(out.attrs['warmup_context']['analysis_bars'],59)
 def test_weekly_monthly_profiles_pass_actual_core_with_warmup(self):
  frames={tf:pd.read_pickle(E/(tf+'.pkl')) for tf in ('1D','1W','1M')}
  for f in frames.values():f.attrs['initialization_policy']=self.policy
  frames['1M']=g.monthly_warmup(self.store,self.info,frames['1M'],self.now)
  for tf in ('1W','1M'):
   out=a.analyse_multiframe(frames,tf,now=self.now)
   self.assertEqual(out['status'],'READY',out['blockers'])
   self.assertTrue(out['demo_initialization']['consistent'])
   self.assertGreater(out['demo_initialization']['scenarios_checked'],1)
   self.assertEqual(out['timeframe_context']['1M']['warmup_context']['native_months'],71)
   self.assertEqual(out['timeframe_context']['1M']['warmup_context']['daily_derived_months'],10)
 def test_invalid_newest_month_is_not_ignored(self):
  raw=copy.deepcopy(self.receipt['payload']);raw['data'][-1]['HIGH_1']='0'
  with self.assertRaisesRegex(ValueError,'WARMUP_TOO_SHORT'):g.decode_monthly_warmup(raw,self.meta,self.isin,self.now,self.sha)
 def test_missing_month_stays_gap(self):
  raw=copy.deepcopy(self.receipt['payload']);raw['data'].pop(30)
  with self.assertRaisesRegex(ValueError,'NATIVE_MONTHLY_DQ'):g.decode_monthly_warmup(raw,self.meta,self.isin,self.now,self.sha)
 def test_wrong_currency_source_samples_duplicate_and_future_rejected(self):
  for mode in ('currency','source','samples','duplicate','future'):
   raw=copy.deepcopy(self.receipt['payload']);meta=dict(self.meta);now=self.now
   if mode in ('currency','source'):meta[mode]='USD' if mode=='currency' else 'proxy'
   elif mode=='samples':raw['request']['samples']='D'
   elif mode=='duplicate':raw['data'].append(raw['data'][-1])
   elif mode=='future':now=pd.Timestamp('2026-09-15',tz='UTC')
   with self.subTest(mode=mode),self.assertRaises(ValueError):g.decode_monthly_warmup(raw,meta,self.isin,now,self.sha)
 def test_default_calendar_not_relaxed_for_unproven_history(self):
  f=self.native.copy();f.attrs.pop('native_monthly',None)
  self.assertEqual(dq.audit(f,'1M',now=self.now)['state'],'INVALID')
 def test_changed_stored_warmup_or_overlap_blocks(self):
  f=self.current.copy();f.iloc[-1,f.columns.get_loc('close')]+=.01
  with self.assertRaisesRegex(ValueError,'OVERLAP_CHANGED'):g.monthly_warmup(self.store,self.info,f,self.now)
  with self.store.connect() as db:db.execute('update series set close=close+0.01 where base_tf=? and ts=?',('1M','2015-02-01 00:00:00'))
  with self.assertRaisesRegex(ValueError,'REVISION_UNCONFIRMED'):g.monthly_warmup(self.store,self.info,self.current,self.now)
 def test_bad_current_quality_and_missing_authorization_not_bypassed(self):
  f=self.current.copy();f.attrs['data_quality']={'state':'INVALID'}
  self.assertNotIn('warmup_context',g.monthly_warmup(self.store,self.info,f,self.now).attrs)
  self.store.set_setting('cp_manual_risk:'+self.isin,{'initialization_policy':{**self.policy,'older_monthly_warmup':False}})
  self.assertEqual(len(g.monthly_warmup(self.store,self.info,self.current,self.now)),59)
 def test_closed_dependency_covers_older_monthly_rows(self):
  ctx=SnapshotContext();ctx.store=self.store;ctx.set_analysis_scope(self.now)
  with self.store.read_cache.track_inputs() as token:
   ctx.closed_frame(self.info,self.current,'1M')
   self.store.read_cache.depend('instrument:'+self.isin)
   with self.store.connect() as db:guard=capture_closed_inputs(db,list(ctx.closed_specs.values()),token)
  self.assertTrue(any(s['spec']['base_tf']=='1M' and s['spec']['from']=='2015-02-01 00:00:00' for s in guard['series']))
  with self.store.connect() as db:db.execute('update series set close=close+0.01 where base_tf=? and ts=?',('1M','2015-02-01 00:00:00'))
  with self.store.connect() as db:
   with self.assertRaises(InputRevisionChanged):validate_closed_inputs(db,guard)

 def test_native_receipt_persists_and_generic_dq_sees_same_proof(self):
  other=Store(self.tmp.name)
  try:
   frame=other.load_series(self.isin,'1M','gettex_chart','AAPL.GTX','GETTEX','EUR')
   self.assertEqual(g.monthly_digest(frame),g.monthly_digest(self.native))
   self.assertEqual(dq.audit(frame,'1M',now=self.now,check_stale=False)['state'],'READY')
   self.assertEqual(frame.attrs['native_monthly']['excluded_invalid_months'],['2015-01-01 00:00:00'])
  finally:other.read_cache.close()

 def test_disputed_native_month_is_not_used_and_daily_revision_revokes(self):
  ctx=SnapshotContext();ctx.store=self.store;ctx.set_analysis_scope(self.now)
  with self.store.read_cache.track_inputs() as token:
   out=ctx.closed_frame(self.info,self.current,'1M')
   self.store.read_cache.depend('instrument:'+self.isin)
   with self.store.connect() as db:guard=capture_closed_inputs(db,list(ctx.closed_specs.values()),token)
  self.assertEqual(out.loc[pd.Timestamp('2021-08-01'),'open'],123.5)
  self.assertEqual(out.loc[pd.Timestamp('2021-08-01'),'volume'],106291)
  self.assertEqual(out.loc[pd.Timestamp('2021-08-01'),'low'],122.3)
  self.assertNotIn(pd.Timestamp('2021-08-01'),self.native.index)
  with self.store.connect() as db:db.execute('update series set high=high+0.01 where base_tf=? and ts=?',('1D','2021-08-02 00:00:00'))
  with self.store.connect() as db:
   with self.assertRaises(InputRevisionChanged):validate_closed_inputs(db,guard)

 def test_invalid_old_daily_warmup_is_not_bypassed(self):
  with self.store.connect() as db:db.execute('update series set high=0 where base_tf=? and ts=?',('1D','2021-08-02 00:00:00'))
  with self.assertRaisesRegex(ValueError,'DAILY_REFERENCE_UNCONFIRMED'):g.monthly_warmup(self.store,self.info,self.current,self.now)

if __name__=='__main__':unittest.main()
