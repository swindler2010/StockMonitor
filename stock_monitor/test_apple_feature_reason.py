"""Only the newly corrected feature explanation; no change to admission."""
import copy,json,unittest
from pathlib import Path
import pandas as pd
import analytics as a

E=Path(__file__).resolve().parents[1]/'history64-evidence/apple-admission30'
class FeatureReason(unittest.TestCase):
 @classmethod
 def setUpClass(cls):
  cls.saved=json.loads((E/'comparison.json').read_text(encoding='utf8'))
  cls.now=pd.Timestamp(cls.saved['at'])
 def test_real_15m_success_has_no_insufficient_history_message(self):
  f=pd.read_pickle(E/'15m.pkl');_,result=a._safe_features(f,{},self.now,prove_scope=True)
  self.assertTrue(result['sufficient_history']);self.assertIn('Индикаторы рассчитаны',result['reason'])
  old=copy.deepcopy(self.saved['frames']['15m']['strict_core_features'])
  # This is an explanation-only change; every factual output stays identical.
  old.pop('reason');result.pop('reason')
  self.assertEqual(json.loads(json.dumps(result,default=str)),old)
 def test_short_history_still_blocked(self):
  f=pd.read_pickle(E/'15m.pkl').tail(34).copy();f.attrs.pop('accepted_risk',None)
  f.attrs['data_quality']={'state':'READY'}
  _,result=a._safe_features(f,{},self.now)
  self.assertFalse(result['sufficient_history']);self.assertIn('35',result['reason'])
 def test_actual_4h_seed_block_remains(self):
  f=pd.read_pickle(E/'4H.pkl');_,result=a._safe_features(f,{},self.now,prove_scope=True)
  self.assertFalse(result['sufficient_history']);self.assertEqual(result['reason'],'INSUFFICIENT_RECURSIVE_WARMUP_5Y')
 def test_invalid_and_unreviewed_gap_remain_blocked(self):
  for state in ('INVALID','UNCONFIRMED'):
   f=pd.read_pickle(E/'15m.pkl');f.attrs.pop('accepted_risk',None)
   f.attrs['data_quality']={'state':state}
   _,result=a._safe_features(f,{},self.now)
   self.assertFalse(result['sufficient_history']);self.assertIn(state,result['reason'])
if __name__=='__main__':unittest.main()
