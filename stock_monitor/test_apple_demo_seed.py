import copy,json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import pandas as pd
import analytics as a,cp_controls as cp
from storage import Store
from paper_account import ACCOUNT

E=Path(__file__).resolve().parents[1]/'history64-evidence/apple-admission30'
class DemoSeedTests(unittest.TestCase):
 @classmethod
 def setUpClass(cls):
  cls.now=pd.Timestamp(json.loads((E/'comparison.json').read_text(encoding='utf8'))['at'])
  cls.policy=json.loads((E/'policy.json').read_text(encoding='utf8'))
  cls.frames={tf:pd.read_pickle(E/('policy-'+tf+'.pkl')) for tf in ('15m','4H','1D')}
 def test_real_approved_profile_ready_but_not_unconditional_buy(self):
  out=a.analyse_multiframe(self.frames,'15m',now=self.now)
  self.assertEqual(out['status'],'READY',out['blockers'])
  self.assertTrue(out['demo_initialization']['consistent'])
  self.assertEqual(out['calculation_context'],cp.DEMO_INITIALIZATION)
  self.assertGreater(out['demo_initialization']['scenarios_checked'],2)
  self.assertEqual(out['timeframe_context']['4H']['data_quality']['state'],'UNCONFIRMED')
  self.assertFalse(out['demo_initialization']['automatic_permission'])
 def test_default_strict_context_stays_blocked(self):
  _,out=a._safe_features(self.frames['4H'],{},self.now,prove_scope=True)
  self.assertFalse(out['sufficient_history']);self.assertEqual(out['reason'],'INSUFFICIENT_RECURSIVE_WARMUP_5Y')
 def test_other_entry_timeframe_cannot_activate_policy(self):
  frames={**self.frames,'1H':self.frames['15m']}
  out=a.analyse_multiframe(frames,'4H',now=self.now)
  self.assertEqual(out['status'],'DATA BLOCKED');self.assertNotIn('demo_initialization',out)
 def test_missing_wrong_source_currency_or_owner_receipt_rejected(self):
  for change in (None,{'approved':False},{'isin':'US88160R1014'}, {'identity':{**self.policy['identity'],'currency':'USD'}}, {'identity':{**self.policy['identity'],'source':'proxy'}}):
   f=self.frames['4H'].copy();risk=copy.deepcopy(f.attrs['accepted_risk']);f.attrs['accepted_risk']=risk
   risk['initialization_policy']=None if change is None else {**self.policy,**change}
   _,out=a._safe_features(f,{},self.now,prove_scope=True,demo_initialization=True)
   self.assertFalse(out['sufficient_history'])
 def test_risk_cent_change_rejects_seed_proof(self):
  f=self.frames['4H'];enriched,result=a._analyse_features(f,{},self.now)
  original=a._analyse_features
  def changed(*args,**kwargs):
   x,r=original(*args,**kwargs);r['stop']+=.02;return x,r
  with patch.object(a,'_analyse_features',side_effect=changed):
   self.assertIsNone(a._demo_seed_trials(f,{},self.now,enriched,result))
 def test_actual_core_signal_predicate_change_is_blocked(self):
  original=a._safe_features
  def features(frame,*args,**kwargs):
   x,r=original(frame,*args,**kwargs)
   if frame.attrs['timeframe']=='15m':
    trial=copy.deepcopy(r);trial['facts']['rsi']=20 if r['facts']['rsi']>=45 else 60
    r['_demo_seed_trials']=[trial]
   return x,r
  with patch.object(a,'_safe_features',side_effect=features):out=a.analyse_multiframe(self.frames,'15m',now=self.now)
  self.assertEqual(out['status'],'DATA BLOCKED');self.assertIn('DEMO_SEED_SIGNAL_OR_RISK_UNSTABLE',out['blockers'])
 def test_ohlcv_invalid_and_unaccepted_gaps_still_block(self):
  for mode in ('INVALID','NO_GRANT'):
   frames={tf:f.copy() for tf,f in self.frames.items()};f=frames['15m']
   if mode=='INVALID':f.iloc[-1,f.columns.get_loc('high')]=0
   else:f.attrs.pop('accepted_risk',None)
   out=a.analyse_multiframe(frames,'15m',now=self.now)
   self.assertEqual(out['status'],'DATA BLOCKED')
 def test_policy_acceptance_is_atomic_and_revision_bound(self):
  with tempfile.TemporaryDirectory() as folder:
   s=Store(folder);isin=self.policy['isin'];s.save_instrument({'isin':isin,'name':'Apple'})
   f=self.frames['15m'];report=f.attrs['data_quality']
   s.set_setting('data_quality:'+isin+':AAPL.GTX:15m',report)
   with s.connect() as db:
    reviewed=cp.problems(db,isin);token=cp._revisions(db,['instrument:'+isin])
   cp.accept(s,isin,reviewed,initialization_policy=self.policy,expected_revisions=token)
   before=s.setting('cp_manual_risk:'+isin)
   self.assertEqual(before['initialization_policy'],self.policy)
   s.save_instrument({'isin':isin,'name':'Changed'})
   with self.assertRaisesRegex(ValueError,'RISK_INPUT_REVISIONS_CHANGED'):
    cp.accept(s,isin,reviewed,initialization_policy=self.policy,expected_revisions=token)
   self.assertEqual(s.setting('cp_manual_risk:'+isin),before)
   with s.connect() as db:
    self.assertEqual(cp.manual_gate(db,isin,self.policy['identity'])['account'],ACCOUNT)
   s.read_cache.close()
if __name__=='__main__':unittest.main()
