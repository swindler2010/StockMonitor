"""Accepted gaps, actual existing aggregation/indicators/CORE; isolated SQLite."""
import copy,sys,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace as NS
import pandas as pd
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
import cp_controls as cp
import data_quality as dq
import calculation_window as cw
from storage import Store
from analytics import resample,analyse_multiframe,_safe_features
from snapshot_worker import SnapshotContext,Value
from performance_cache import InputRevisionChanged

class AcceptedAnalysis(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.store=Store(self.tmp.name)
  self.now=pd.Timestamp('2026-10-27T10:01Z');self.isin='DE0007164600'
  self.meta=dict(source='proxy',symbol='SAP.DE',venue='GER',currency='EUR',timezone='Europe/Berlin')
  self.info=dict(isin=self.isin,name='SAP',position={'quantity':0},source_mode='proxy',eur_history_auto=True,
    eur_listings=[dict(self.meta,isin=self.isin,status='CONFIRMED')])
  self.store.save_instrument(self.info);self.store.set_setting('history_identity:'+self.isin,self.meta)
  self.raw={}
  for tf,n in [('5m',1000),('1H',2200),('1D',900)]:
   idx=dq.expected_index(self.meta,tf,pd.Timestamp('2021-10-27'),pd.Timestamp('2026-10-27 12:00'),self.now)[-n:]
   f=pd.DataFrame(dict(open=100.,high=101.,low=99.,close=100.,volume=100.,source='proxy'),index=idx)
   f=f.drop(f.index[40]);f.attrs=dict(self.meta,timeframe=tf)
   f.attrs['data_quality']=dq.audit(f,tf,now=self.now)
   self.raw[tf]=f;self.store.save_series(self.info,f,tf)
   self.store.set_setting(dq.key(self.info,self.meta['symbol'],tf),f.attrs['data_quality'])
 def tearDown(self):self.store.read_cache.close();self.tmp.cleanup()
 def grant(self):
  with self.store.connect() as db:p=cp.problems(db,self.isin)
  cp.accept(self.store,self.isin,p,True)
 def frame(self,tf,raw=None):
  base='1H' if tf=='4H' else tf
  raw=(raw if raw is not None else self.raw[base]).copy()
  raw=cw.scope_raw(raw,base,self.now)
  raw=cp.accepted_gap_input(self.store,self.info,raw,base,self.now)
  return cp.accepted_gap_output(resample(raw,base,tf,self.now),tf,self.now)
 def core(self):return analyse_multiframe({tf:self.frame(tf) for tf in ('5m','4H','1D')},'5m',self.info,now=self.now)
 def test_real_core_runs_only_after_acceptance_dq_and_candles_unchanged(self):
  self.assertEqual(self.core()['status'],'DATA BLOCKED');original=copy.deepcopy(self.raw)
  self.grant();r=self.core()
  self.assertEqual(r['status'],'READY',r['blockers']);self.assertEqual(r['calculation_context'],cp.RISK_CONTEXT)
  self.assertNotEqual(r['signal'],'BUY') # no invented market trigger
  for tf,f in self.raw.items():
   pd.testing.assert_frame_equal(f,original[tf]);self.assertEqual(f.attrs,original[tf].attrs)
   self.assertEqual(self.store.setting(dq.key(self.info,self.meta['symbol'],tf)),f.attrs['data_quality'])
  self.assertEqual(r['timeframe_context']['5m']['data_quality']['state'],'GAP')
 def test_derived_gap_omits_bucket_without_fabricating_ohlcv(self):
  self.grant();raw=self.raw['1H'];out=self.frame('4H');ordinary=resample(cw.scope_raw(raw,'1H',self.now),'1H','4H',self.now)
  pd.testing.assert_frame_equal(out,ordinary)
  self.assertTrue(out.bar_complete.all());self.assertTrue(out.attrs['data_quality']['excluded_incomplete'])
  self.assertTrue(cp.analysis_quality_allowed(out.attrs['data_quality'],out.attrs['accepted_risk']))
 def test_unaccepted_new_gap_and_revoke_block(self):
  self.grant();f=self.raw['5m'].drop(self.raw['5m'].index[70])
  out=self.frame('5m',f);self.assertNotIn('accepted_risk',out.attrs)
  cp.revoke(self.store,self.isin);self.assertEqual(self.core()['status'],'DATA BLOCKED')
 def test_bad_ohlcv_identity_and_unconfirmed_listing_never_waived(self):
  self.grant()
  for kind in ('ohlcv','currency','venue','symbol'):
   f=self.raw['5m'].copy()
   if kind=='ohlcv':f.iloc[5,f.columns.get_loc('high')]=1.
   else:f.attrs[kind]='WRONG'
   with self.subTest(kind=kind):self.assertNotIn('accepted_risk',self.frame('5m',f).attrs)
 def test_stale_missing_tail_and_insufficient_actual_warmup_stay_blocked(self):
  self.grant();f=self.raw['5m'].iloc[:-30].copy();out=self.frame('5m',f)
  _,features=_safe_features(out,self.info,self.now,prove_scope=True)
  self.assertFalse(features['sufficient_history'])
  self.assertFalse(features['scope_initialization']['checked']);self.assertNotEqual(features['reason'],'INSUFFICIENT_RECURSIVE_WARMUP_5Y')
  out=self.frame('5m').tail(20).copy();_,features=_safe_features(out,self.info,self.now,prove_scope=True)
  self.assertFalse(features['sufficient_history'])
 def test_revision_token_captures_acceptance_and_revokes_cached_result(self):
  self.grant()
  with self.store.read_cache.track_inputs(isolated=True) as token:
   self.frame('5m')
   self.assertIn('setting:cp_manual_risk:'+self.isin,token)
   cp.revoke(self.store,self.isin)
   with self.assertRaises(InputRevisionChanged):self.store.read_cache.assert_current(token)
 def test_snapshot_context_uses_same_grant_and_actual_core(self):
  self.grant();ctx=SnapshotContext();ctx.store=self.store;ctx.watchlist=[self.info];ctx.drafts={}
  ctx.symbol=Value('SAP');ctx.timeframe=Value('5m');ctx.period=Value('Max');ctx.set_analysis_scope(self.now)
  r=ctx.trading_analysis(self.info)
  self.assertEqual(r['status'],'READY',r['blockers']);self.assertEqual(r['calculation_context'],cp.RISK_CONTEXT)
  # Existing manual contract now returns the real engine, not NOT_USED.
  contract=cw.build(ctx,self.info,'5m',self.info,self.now)
  self.assertTrue(contract['analysis_ready']);self.assertEqual(contract['trading_engine']['status'],'READY')
 def test_existing_smart_assessment_keeps_risk_label_and_recursive_proof(self):
  self.grant();row=cw.bounded_features(self.info,self.frame('4H'),'4H',self.now)
  self.assertTrue(row['full_reference']['analysis_ready'],row)
  self.assertTrue(row['full_reference']['scope_initialization']['stable'])
  self.assertEqual(row['accepted_risk']['context'],cp.RISK_CONTEXT)
 def test_partial_grant_does_not_approve_another_tf(self):
  with self.store.connect() as db:p=[x for x in cp.problems(db,self.isin) if x['tf']=='5m']
  cp.accept(self.store,self.isin,p)
  self.assertIn('accepted_risk',self.frame('5m').attrs);self.assertNotIn('accepted_risk',self.frame('4H').attrs)
  self.assertEqual(self.core()['status'],'DATA BLOCKED')
 def test_unconfirmed_missing_interval_requires_exact_owner_grant(self):
  f=self.raw['5m'].copy();f.attrs['classify_absence']=True
  f.attrs['data_quality']=dq.audit(f,'5m',now=self.now)
  self.assertEqual(f.attrs['data_quality']['state'],'UNCONFIRMED')
  self.store.set_setting(dq.key(self.info,self.meta['symbol'],'5m'),f.attrs['data_quality'])
  self.assertNotIn('accepted_risk',self.frame('5m',f).attrs)
  self.grant();out=self.frame('5m',f);_,features=_safe_features(out,self.info,self.now,prove_scope=True)
  self.assertTrue(features['sufficient_history']);self.assertEqual(features['data_quality']['state'],'UNCONFIRMED')
 def test_recursive_seed_sensitivity_not_waived(self):
  import numpy as np
  self.grant();f=self.raw['5m'].tail(50).copy();p=100+np.sin(np.arange(50))
  f['close']=p;f['open']=p;f['high']=p+1;f['low']=p-1
  f.attrs['data_quality']=dq.audit(f,'5m',now=self.now)
  out=self.frame('5m',f);_,features=_safe_features(out,self.info,self.now,prove_scope=True)
  self.assertFalse(features['sufficient_history']);self.assertEqual(features['reason'],'INSUFFICIENT_RECURSIVE_WARMUP_5Y')
 def test_source_gap_requires_matching_grant_but_outage_never_waived(self):
  f=self.raw['5m'].copy();f.attrs['data_quality']['state']='SOURCE_GAP'
  self.store.set_setting(dq.key(self.info,self.meta['symbol'],'5m'),f.attrs['data_quality']);self.grant()
  out=self.frame('5m',f);_,features=_safe_features(out,self.info,self.now,prove_scope=True)
  self.assertTrue(features['sufficient_history']);self.assertEqual(features['data_quality']['state'],'SOURCE_GAP')
  f.attrs['data_quality']['state']='SOURCE_UNAVAILABLE'
  self.assertNotIn('accepted_risk',self.frame('5m',f).attrs)

class ExactRanges(unittest.TestCase):
 def test_acceptance_does_not_bridge_two_reviewed_ranges(self):
  a=[['2026-10-26 10:00','2026-10-26 10:05'],['2026-10-26 10:15','2026-10-26 10:20']]
  self.assertTrue(cp._covered([['2026-10-26 10:00','2026-10-26 10:05']],a))
  self.assertFalse(cp._covered([['2026-10-26 10:00','2026-10-26 10:20']],a))
  self.assertFalse(cp._covered([['2026-10-26 10:10','2026-10-26 10:10']],a))
  self.assertFalse(cp._covered([['2026-10-26 09:55','2026-10-26 10:00']],a))

if __name__=='__main__':unittest.main()
