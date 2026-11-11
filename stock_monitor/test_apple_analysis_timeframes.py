"""New all-TF analysis scope; frozen real Apple data, no market requests."""
import copy,json,unittest
from pathlib import Path
from unittest.mock import patch
import pandas as pd
import analytics as a,cp_controls as cp
from timeframes import LABELS

E=Path(__file__).resolve().parents[1]/'history64-evidence/apple-analysis11-30'
class AppleAnalysisTimeframes(unittest.TestCase):
 @classmethod
 def setUpClass(cls):
  cls.now=pd.Timestamp(json.loads((E/'diagnostic.json').read_text(encoding='utf8'))['at'])
  cls.policy=json.loads((E.parent/'apple-admission30/policy.json').read_text(encoding='utf8'))
  cls.policy['analysis_timeframes']=list(LABELS)
  cls.frames={tf:pd.read_pickle(E/(tf+'.pkl')) for tf in ('10m','30m','1H','2H','3H','4H','1D','1W','1M')}
  for f in cls.frames.values():f.attrs['initialization_policy']=copy.deepcopy(cls.policy)
 def test_explicit_analysis_scope_keeps_execution_timeframes(self):
  self.assertTrue(cp.demo_initialization_policy(self.policy,self.policy['isin']))
  self.assertEqual(self.policy['entry_timeframes'],['5m','15m'])
  for delta in ({'analysis_timeframes':['1M']},{'entry_timeframes':['1M']},{'approved':False},{'account':'other'}):
   self.assertFalse(cp.demo_initialization_policy({**self.policy,**delta},self.policy['isin']))
 def test_clean_weekly_seed_proof_needs_no_false_gap_waiver(self):
  f=self.frames['1W'];self.assertNotIn('accepted_risk',f.attrs)
  _,out=a._safe_features(f,{},self.now,prove_scope=True,demo_initialization=True)
  self.assertTrue(out['sufficient_history'],out['reason'])
  self.assertEqual(out['data_quality']['state'],'READY')
  self.assertGreater(len(out['_demo_seed_trials']),1)
 def test_all_new_selected_profiles_use_existing_core(self):
  for tf in self.frames:
   with self.subTest(tf=tf):
    out=a.analyse_multiframe(self.frames,tf,now=self.now)
    self.assertEqual(out['status'],'DATA BLOCKED' if tf in ('1W','1M') else 'READY',out['blockers'])
    if tf in ('1W','1M'):
     self.assertIn('INSUFFICIENT_RECURSIVE_WARMUP_5Y',';'.join(out['blockers']))
    else:self.assertTrue(out['demo_initialization']['consistent'])
 def test_monthly_instability_remains_blocked(self):
  _,out=a._safe_features(self.frames['1M'],{},self.now,prove_scope=True,demo_initialization=True)
  self.assertFalse(out['sufficient_history']);self.assertFalse(out['scope_initialization']['stable'])
 def test_receipt_does_not_accept_invalid_stale_or_unreviewed_gap(self):
  for state in ('INVALID','STALE','UNCONFIRMED'):
   f=self.frames['1W'].copy();f.attrs=copy.deepcopy(f.attrs)
   f.attrs['data_quality']={**f.attrs['data_quality'],'state':state}
   _,out=a._safe_features(f,{},self.now,prove_scope=True,demo_initialization=True)
   self.assertFalse(out['sufficient_history'],state)
 def test_wrong_identity_cannot_use_initialization_model(self):
  for key,value in (('source','proxy'),('venue','Frankfurt'),('currency','USD'),('symbol','APC.F')):
   f=self.frames['1W'].copy();f.attrs[key]=value
   self.assertIsNone(cp.initialization_for(f))
  f=self.frames['1W'].copy();f.attrs['isin']='US88160R1014'
  self.assertIsNone(cp.initialization_for(f))
 def test_no_policy_does_not_expand_old_demo_scope(self):
  f=self.frames['1W'].copy();f.attrs.pop('initialization_policy',None)
  _,out=a._safe_features(f,{},self.now,prove_scope=True,demo_initialization=True)
  self.assertFalse(out['sufficient_history'])
 def test_new_readonly_projection_has_no_execution_permission(self):
  import analytical_read as read
  from types import SimpleNamespace
  info={'isin':self.policy['isin'],'name':'Apple'}
  class Cache:
   def depend(self,*args):pass
   def dependencies(self):return {}
  class Store:
   read_cache=Cache()
   def setting(self,key,default=None):return {'initialization_policy':self_policy} if key.startswith('cp_manual_risk:') else default
  self_policy=self.policy;store=Store()
  f=self.frames['1D'];engine={'status':'READY','analysis_identity':self.policy['identity']}
  values={'watchlist':[info],'symbol':'Apple','timeframe':'1D','period':'Max','request':{'isin':info['isin'],'timeframe':'1D','limit':1}}
  with patch.object(read.SnapshotContext,'frame_for',return_value=f),patch.object(read.SnapshotContext,'user_state',return_value={}),patch.object(read.SnapshotContext,'trading_analysis',return_value=engine),patch.object(read,'read_time',return_value=self.now),patch('candle_timing.annotate_page',return_value=0):
   out=read.calculate(store,values)
  self.assertIsNone(out['execution_context'])
  self.assertTrue(out['analysis_context']['analysis_ready'])
  self.assertFalse(out['analysis_context']['execution_permission'])
  self.assertEqual(out['analysis_context']['identity'],self.policy['identity'])

 def test_five_minute_analysis_does_not_change_executor_source(self):
  from snapshot_worker import SnapshotContext,Value
  from desktop014 import Desktop014
  info={'isin':self.policy['isin'],'name':'Apple','eur_history_auto':True}
  chart=self.frames['10m'].head(1).copy();chart.attrs['timeframe']='5m'
  official=chart.copy();official.attrs.update(source='gettex',symbol=info['isin'])
  class Cache:
   keys=[]
   def depend(self,*args):pass
   def dependencies(self,**kwargs):return {}
   def get(self,key,load,**kwargs):self.keys.append(key);return load()
  policy=self.policy
  class Store:
   read_cache=Cache()
   def setting(self,key,default=None):return {'initialization_policy':policy} if key.startswith('cp_manual_risk:') else default
  ctx=SnapshotContext();ctx.store=Store();ctx.timeframe=Value('5m');ctx.period=Value('неделя');ctx.analysis_at=self.now
  ctx.user_state=lambda _: {'benchmark_id':'not_selected'};ctx.source_mode=lambda _:'gettex'
  ctx.frame_for=lambda *args:official;ctx.frame_for_entry=lambda i,t,f:f
  ctx.closed_frame=lambda i,f,t:f
  observed=[]
  def engine(frames,*args,**kwargs):
   observed.append(frames['5m'].attrs['source']);return {'status':'DATA BLOCKED','warnings':[]}
  with patch.object(Desktop014,'_frame_uncached',return_value=chart),patch.object(a,'analyse_multiframe',side_effect=engine):
   ctx.analysis_only=False;ctx._trading_analysis(info)
   ctx.analysis_only=True;ctx._trading_analysis(info)
  self.assertEqual(observed,['gettex','gettex_chart'])
  self.assertNotEqual(ctx.store.read_cache.keys[0],ctx.store.read_cache.keys[1])

 def test_entry_read_keeps_blocked_execution_separate_from_analysis(self):
  import analytical_read as read
  from snapshot_worker import SnapshotContext
  from types import SimpleNamespace
  info={'isin':self.policy['isin'],'name':'Apple'};policy=self.policy
  cache=SimpleNamespace(depend=lambda *args:None,dependencies=lambda:{})
  store=SimpleNamespace(read_cache=cache,setting=lambda key,default=None:{'initialization_policy':policy} if key.startswith('cp_manual_risk:') else default)
  values={'watchlist':[info],'symbol':'Apple','timeframe':'5m','period':'Max','request':{'isin':info['isin'],'timeframe':'5m','limit':1}}
  with patch.object(SnapshotContext,'frame_for',return_value=self.frames['10m']),patch.object(SnapshotContext,'user_state',return_value={}),patch.object(SnapshotContext,'trading_analysis',return_value={'status':'READY','analysis_identity':policy['identity']}) as calc,patch.object(read,'read_time',return_value=self.now),patch('candle_timing.annotate_page',return_value=0),patch('calculation_window.build',return_value={'status':'BLOCKED'}) as execute:
   out=read.calculate(store,values)
  self.assertEqual(out['execution_context']['status'],'BLOCKED')
  self.assertTrue(out['analysis_context']['analysis_ready'])
  self.assertFalse(out['analysis_context']['execution_permission'])
  calc.assert_called_once_with(info,analysis_only=True)
  execute.assert_called_once()

 def test_owner_policy_revision_change_rejects_running_analysis(self):
  import tempfile
  from storage import Store
  from snapshot_worker import SnapshotContext
  from performance_cache import InputRevisionChanged
  info={'isin':self.policy['isin'],'name':'Apple'}
  with tempfile.TemporaryDirectory() as folder:
   store=Store(folder);store.save_instrument(info)
   key='cp_manual_risk:'+info['isin'];store.set_setting(key,{'initialization_policy':self.policy})
   ctx=SnapshotContext();ctx.store=store
   def changed(*args):
    store.read_cache.depend('setting:'+key)
    store.set_setting(key,{'initialization_policy':{**self.policy,'approved':False}})
    return {'status':'READY'}
   with patch.object(SnapshotContext,'_trading_analysis',side_effect=changed):
    with self.assertRaises(InputRevisionChanged):ctx.trading_analysis(info,analysis_only=True)
   store.read_cache.close()

 def test_fixed_read_clock_initializes_closed_dependencies_without_changing_scope(self):
  from snapshot_worker import SnapshotContext
  info={'isin':self.policy['isin'],'name':'Apple'}
  ctx=SnapshotContext();ctx.analysis_at=self.now
  frame=self.frames['10m']
  result=ctx.closed_frame(info,frame,'10m')
  self.assertTrue(ctx.closed_specs)
  self.assertFalse(getattr(ctx,'five_year_scope',False))
  self.assertTrue(all(a.bar_ends(result)<=self.now))
  first=dict(ctx.closed_specs)
  ctx.closed_frame(info,frame,'10m')
  self.assertEqual(first,ctx.closed_specs)

if __name__=='__main__':unittest.main()
