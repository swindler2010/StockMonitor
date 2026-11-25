import copy,json,sys,tempfile,unittest
from pathlib import Path
import numpy as np
import pandas as pd
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
import data_quality as dq
from analytics import resample,_safe_features
from calculation_window import scope_raw,scope_report,bounded_features,scope_current,SCOPE_POLICY
from storage import Store
from snapshot_worker import SnapshotContext,Value
from performance_cache import frame_dependency,capture_closed_inputs,validate_closed_inputs,InputRevisionChanged

class FiveYearScope(unittest.TestCase):
 def setUp(self):
  self.now=pd.Timestamp('2026-10-26T19:00Z');self.meta=dict(source='proxy',symbol='TEST.DE',venue='GER',currency='EUR',timezone='Europe/Berlin')
  self.info=dict(isin='TEST',name='test',source_mode='proxy',eur_history_auto=True,eur_listings=[dict(self.meta,isin='TEST',status='CONFIRMED')])
  idx=dq.expected_index(self.meta,'1D',pd.Timestamp('2011-01-01'),pd.Timestamp('2026-10-26'),self.now)
  p=100+np.arange(len(idx))*.03+np.sin(np.arange(len(idx))*.17)
  self.full=pd.DataFrame(dict(open=p-.2,high=p+1,low=p-1,close=p,volume=100.,source='proxy'),index=idx)
  self.full.attrs={**self.meta,'timeframe':'1D'}
 def test_old_gap_is_outside_but_inside_gap_stays_blocked_and_archive_unchanged(self):
  old=self.full.drop(pd.Timestamp('2012-10-03'));old.attrs['data_quality']=dq.audit(old,'1D',now=self.now)
  before=old.copy();attrs=copy.deepcopy(old.attrs);self.assertEqual(attrs['data_quality']['state'],'GAP')
  scoped=scope_raw(old,'1D',self.now);report=dq.audit(scoped,'1D',now=self.now)
  self.assertEqual(report['state'],'READY');scoped.attrs['data_quality']=report
  result=bounded_features(self.info,scoped,'1D',self.now)
  self.assertEqual(result['archive_state'],'GAP')
  self.assertTrue(result['full_reference']['analysis_ready'],result)
  self.assertTrue(result['full_reference']['scope_initialization']['stable'])
  recent=scoped.drop(pd.Timestamp('2026-04-20'));recent.attrs['data_quality']=dq.audit(recent,'1D',now=self.now)
  self.assertEqual(recent.attrs['data_quality']['state'],'GAP');self.assertFalse(bounded_features(self.info,recent,'1D',self.now).get('full_reference',{}).get('analysis_ready',False))
  pd.testing.assert_frame_equal(old,before);self.assertEqual(old.attrs,attrs)
 def test_calendar_boundary_partial_week_month_and_warmup_remain_strict(self):
  raw=scope_raw(self.full,'1D',self.now);raw.attrs['data_quality']=dq.audit(raw,'1D',now=self.now)
  self.assertEqual(str(raw.index[0].date()),'2021-10-26')
  for tf in ('1W','1M'):
   out=resample(raw,'1D',tf,self.now);result=bounded_features(self.info,out,tf,self.now)
   self.assertIn('excluded_leading_partial',result);self.assertNotIn('DERIVED_PROVENANCE_NOT_PROVEN',result['blockers'])
   self.assertIn('INSUFFICIENT_RECURSIVE_WARMUP_5Y',result['blockers']);self.assertFalse(result['analysis_ready'])
  damaged=raw.copy();damaged.loc[damaged.index[0],'high']=0
  self.assertEqual(dq.audit(damaged,'1D',now=self.now)['state'],'INVALID')
 def test_only_localized_old_reports_are_excluded_and_midnight_is_local(self):
  report=dict(state='INVALID',repairable=True,ranges=[['2012-10-03','2012-10-03']],identity=self.meta)
  self.assertEqual(scope_report(report,'2021-10-26')['state'],'OUTSIDE_ANALYSIS_SCOPE')
  global_error={**report,'repairable':False};self.assertEqual(scope_report(global_error,'2021-10-26'),global_error)
  boundary={**report,'ranges':[['2021-10-25T23:30Z','2021-10-25T23:30Z']]}
  self.assertEqual(scope_report(boundary,'2021-10-26'),boundary)
  record={'analysis_scope':{'policy':SCOPE_POLICY,'start':'2021-10-26','timezone':'Europe/Berlin'}}
  self.assertTrue(scope_current(record,self.now));self.assertFalse(scope_current(record,pd.Timestamp('2026-10-26T23:00Z')))
 def test_actual_frame_path_is_scoped_without_changing_max_cache_or_sqlite(self):
  with tempfile.TemporaryDirectory() as folder:
   store=Store(folder);store.save_instrument(self.info);store.save_series(self.info,self.full,'1D');store.set_setting('history_identity:TEST',self.meta)
   ctx=SnapshotContext();ctx.store=store;ctx.watchlist=[self.info];ctx.drafts={};ctx.symbol=Value('test');ctx.timeframe=Value('1D');ctx.period=Value('Max')
   archive=ctx.frame_for(self.info,'1D');count=len(archive)
   ctx.set_analysis_scope(self.now);scoped=ctx.frame_for(self.info,'1D')
   self.assertLess(len(scoped),count);self.assertEqual(scoped.attrs['analysis_scope']['start'],'2021-10-26')
   self.assertEqual(len(store.load_series('TEST','1D','proxy','TEST.DE','GER','EUR')),len(self.full))
   with store.read_cache.track_inputs(isolated=True) as token:
    store.read_cache.depend('instrument:TEST');store.load_series('TEST','1D','proxy','TEST.DE','GER','EUR')
    with store.connect() as db:guard=capture_closed_inputs(db,[frame_dependency('TEST',scoped,'1D',self.now)],token)
   with store.connect() as db:
    db.execute('BEGIN');db.execute("UPDATE series SET close=99 WHERE ts LIKE '2012-10-03%'");validate_closed_inputs(db,guard)
    db.execute("UPDATE series SET close=99 WHERE ts LIKE '2021-10-26%'")
    with self.assertRaises(InputRevisionChanged):validate_closed_inputs(db,guard)
    db.rollback()
   store.read_cache.close()
 def test_wrong_currency_and_short_history_never_gain_ready(self):
  raw=scope_raw(self.full,'1D',self.now);raw.attrs['data_quality']=dq.audit(raw,'1D',now=self.now)
  wrong=raw.copy();wrong.attrs['currency']='USD'
  self.assertFalse(bounded_features(self.info,wrong,'1D',self.now)['analysis_ready'])
  short=raw.tail(34).copy();_,features=_safe_features(short,self.info,self.now,prove_scope=True)
  self.assertFalse(features['sufficient_history']);self.assertEqual(features['reason'],'INSUFFICIENT_RECURSIVE_WARMUP_5Y')

 def test_snapshot_uses_same_boundary_proof_and_keeps_internal_damage(self):
  raw=scope_raw(self.full,'1D',self.now);raw.attrs['data_quality']=dq.audit(raw,'1D',now=self.now)
  ctx=SnapshotContext();ctx.set_analysis_scope(self.now)
  ctx.store=Store(self.enterContext(tempfile.TemporaryDirectory()));self.addCleanup(ctx.store.read_cache.close)
  for tf in ('1W','1M'):
   original=resample(raw,'1D',tf,self.now);before=original.copy()
   clean=ctx.closed_frame(self.info,original,tf)
   self.assertIn('excluded_leading_partial',clean.attrs)
   self.assertEqual(clean.attrs['data_quality']['state'],'READY')
   pd.testing.assert_frame_equal(original,before)
   broken=original.copy();broken.loc[broken.index[-4],'bar_complete']=False
   self.assertEqual(ctx.closed_frame(self.info,broken,tf).attrs['data_quality']['state'],'INVALID')
   unknown=original.copy();unknown.attrs['daily_provenance']={**unknown.attrs['daily_provenance'],'raw_state':'GAP'}
   self.assertEqual(len(ctx.closed_frame(self.info,unknown,tf)),len(original.loc[__import__('analytics').bar_ends(original)<=self.now]))

if __name__=='__main__':unittest.main()
