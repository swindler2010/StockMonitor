"""Regression for approved CORE dependencies falling back to legacy sources."""
import copy, unittest
from types import SimpleNamespace
from unittest.mock import patch, Mock
import pandas as pd
import analytics, cp_controls as cp
from desktop014 import Desktop014
from snapshot_worker import SnapshotContext, Value
from timeframes import LABELS
from trading_engine import profile
from paper_account import ACCOUNT

class ApprovedDependencyPin(unittest.TestCase):
 def setUp(self):
  self.info={'isin':'US00287Y1091','name':'AbbVie','kind':'stock'}
  self.identity=dict(source='gettex_chart',symbol='4AB.GTX',venue='GETTEX',currency='EUR',timezone='Europe/Berlin')
  self.policy=dict(version=cp.DEMO_ALL_INITIALIZATION,isin=self.info['isin'],identity=self.identity,
   account=ACCOUNT,approved=True,authority=cp.DEMO_ALL_POLICY,entry_timeframes=['15m'],analysis_timeframes=list(LABELS),older_verified_warmup=True)
  self.c=SnapshotContext();self.c.analysis_only=True;self.c.analysis_at=pd.Timestamp('2026-11-02T22:00Z')
  cache=SimpleNamespace(depend=lambda *a:None,dependencies=lambda **k:{},get=lambda k,f,**kw:f(),token_for=lambda scopes:{})
  self.c.store=SimpleNamespace(read_cache=cache,setting=lambda k,d=None:d)
  self.c.timeframe=Value('15m');self.c.period=Value('Max')
  self.c.user_state=lambda i:{'benchmark_id':'missing'};self.c.source_mode=lambda i:'gettex'
  self.c.closed_frame=lambda i,f,t:f
  self.c.frame_for=Mock(side_effect=lambda i,t:self.frame(t,'gettex'))
  self.c.frame_for_entry=Mock(side_effect=lambda i,t,f:self.frame(t,'gettex'))
  self.loaded=[];self.captured=[]
 def frame(self,tf,source='gettex_chart'):
  f=pd.DataFrame();f.attrs={**self.identity,'source':source,'symbol':self.identity['symbol'] if source=='gettex_chart' else self.info['isin'],'timeframe':tf,'data_quality':{'state':'UNCONFIRMED'}}
  return f
 def load(self,c,info,tf,required_identity=None):
  self.loaded.append((tf,copy.deepcopy(required_identity)));return self.frame(tf)
 def calc(self,frames,tf,state,*a,**kw):
  self.captured.append((frames,copy.deepcopy(state)))
  return {'status':'DATA BLOCKED','warnings':[]}
 def run_context(self,policy=None,loader=None):
  with patch.object(cp,'initialization_policy_for',return_value=self.policy if policy is None else policy),patch.object(Desktop014,'_frame_uncached',side_effect=loader or self.load),patch.object(analytics,'analyse_multiframe',side_effect=self.calc):
   return self.c._trading_analysis(self.info)
 def test_all_approved_tf_dependencies_use_exact_pin_without_legacy_auto_flag(self):
  for tf in LABELS:
   with self.subTest(tf=tf):
    self.loaded=[];self.c.timeframe=Value(tf);self.run_context()
    self.assertEqual([t for t,p in self.loaded],list(dict.fromkeys((tf,*profile(tf),'1D'))))
    self.assertTrue(all(p==self.identity for t,p in self.loaded))
  self.c.frame_for.assert_not_called();self.c.frame_for_entry.assert_not_called()
 def test_unapproved_context_keeps_original_selection(self):
  p={**self.policy,'approved':False};self.run_context(p)
  self.assertEqual(self.loaded,[]);self.c.frame_for.assert_called_once()
  self.c.frame_for_entry.assert_called()
 def test_execution_context_is_not_switched_by_analysis_permission(self):
  self.c.analysis_only=False;out=self.run_context()
  self.assertEqual(self.loaded,[]);self.assertNotIn('analysis_only',out)
 def test_wrong_account_currency_or_isin_cannot_pin_dependencies(self):
  for change in ({'account':'another'},{'isin':'other'},{'identity':{**self.identity,'currency':'USD'}}):
   self.run_context({**self.policy,**change});self.assertEqual(self.loaded,[])
 def test_missing_pinned_dependency_does_not_fallback(self):
  def load(c,i,t,required_identity=None):
   if t=='4H':raise ValueError('SOURCE_UNAVAILABLE: confirmed GETTEX archive absent')
   return self.load(c,i,t,required_identity)
  out=self.run_context(loader=load);frames,state=self.captured[-1]
  self.assertEqual(out['status'],'DATA BLOCKED');self.assertEqual(frames['4H'].attrs['data_quality']['state'],'INVALID')
  self.assertIn('SOURCE_UNAVAILABLE',frames['4H'].attrs['data_quality']['issues'][0]);self.c.frame_for_entry.assert_not_called()
 def test_wrong_actual_identity_is_not_relabelled(self):
  def load(c,i,t,required_identity=None):
   f=self.load(c,i,t,required_identity)
   if t=='4H':f.attrs['venue']='Frankfurt'
   return f
  self.run_context(loader=load);frames,state=self.captured[-1]
  self.assertEqual(frames['4H'].attrs['venue'],'Frankfurt');self.assertEqual(state['expected_identity'],self.identity)
 def test_quality_is_preserved_for_each_required_frame(self):
  for bad in ('STALE','GAP','INVALID','UNCONFIRMED','SOURCE_UNAVAILABLE'):
   def load(c,i,t,required_identity=None):
    f=self.load(c,i,t,required_identity);f.attrs['data_quality']={'state':bad};return f
   out=self.run_context(loader=load)
   self.assertEqual(out['status'],'DATA BLOCKED')
   self.assertTrue(all(f.attrs['data_quality']['state']==bad for f in self.captured[-1][0].values()))

if __name__=='__main__':unittest.main()
