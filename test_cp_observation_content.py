"""New semantic binding cases; no old tests or market network."""
import sys,copy,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
import cp_reconciliation as rv

class EvidenceContent(unittest.TestCase):
 def fixture(self):
  return dict(info={'isin':'US00287Y1091'},listing={'source':'gettex','venue':'GETTEX','currency':'EUR','listing_proof':{'status':'CONFIRMED','verified_at':'old'}},rows=[],neighbors={'before':['2026-11-02 09:00:00',263.6,263.6,263.6,263.6,0],'after':['2026-11-02 09:30:00',263.6,263.85,263.6,263.85,8]},proof={'checked_at':'2026-11-02T18:40:30Z','basis':'complete_published_trade_files','no_trades':[],'trade_intervals':[],'reference':{'official':'same bytes'},'identity':{'venue':'GETTEX'}})
 def job(self,inputs,version=3):
  return dict(fingerprint_version=version,observation={'fingerprint_version':version,'input_fingerprint':rv._fingerprint(inputs,legacy=version==2)},finding={'evidence_checked_at':inputs['proof']['checked_at']},history=[])
 def bind(self,x):x['fingerprint']=rv._fingerprint(x);return x
 def test_same_evidence_new_checked_at_keeps_visual_binding_but_actual_time(self):
  x=self.fixture();j=self.job(x);x['proof']['checked_at']='2026-11-02T21:43:17Z'
  self.assertTrue(rv._observation_current(j,self.bind(x)));self.assertEqual(x['proof']['checked_at'],'2026-11-02T21:43:17Z')
 def test_v2_timestamp_migration_requires_exact_reconstruction(self):
  x=self.fixture();j=self.job(x,2);x['proof']['checked_at']='2026-11-02T21:43:17Z'
  self.assertTrue(rv._observation_current(j,self.bind(x)))
  j['finding']={};self.assertFalse(rv._observation_current(j,x))
 def test_saved_history_can_reconstruct_old_v2_timestamp(self):
  x=self.fixture();j=self.job(x,2);j['history']=[{'finding':j['finding']}];j['finding']={}
  x['proof']['checked_at']='2026-11-02T21:43:17Z';self.assertTrue(rv._observation_current(j,self.bind(x)))
 def test_changes_to_native_rows_neighbors_identity_or_proof_revoke(self):
  for version in (2,3):
   for change in ('row','neighbor','instrument','venue','currency','source','reference','trade','no_trades','basis'):
    with self.subTest(version=version,change=change):
     x=self.fixture();j=self.job(x,version);x['proof']['checked_at']='2026-11-02T21:43:17Z'
     if change=='row':x['rows']=[['2026-11-02 09:15:00',263,264,262,263,5]]
     elif change=='neighbor':x['neighbors']['before'][1]+=1
     elif change=='instrument':x['info']['name']='changed'
     elif change in ('venue','currency','source'):x['listing'][change]='wrong'
     elif change=='reference':x['proof']['reference']['official']='changed bytes'
     elif change in ('trade','no_trades'):x['proof']['trade_intervals' if change=='trade' else 'no_trades']=[['09:15','09:30']]
     else:x['proof']['basis']='unconfirmed'
     self.assertFalse(rv._observation_current(j,self.bind(x)))
 def test_listing_status_and_closure_time_remain_dependencies(self):
  for change in ('status','closure'):
   x=self.fixture();j=self.job(x)
   if change=='status':x['listing']['listing_proof']['status']='UNCONFIRMED'
   else:x['proof']['closure_checked_at']='2026-11-02T21:43Z'
   self.assertFalse(rv._observation_current(j,self.bind(x)))
 def test_unverifiable_legacy_hash_not_rebound(self):
  x=self.fixture();j=self.job(x,2);j['observation']['input_fingerprint']='unknown'
  self.assertFalse(rv._observation_current(j,self.bind(x)))

if __name__=='__main__':unittest.main(verbosity=2)
