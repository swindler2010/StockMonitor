"""New owner-approved EUR noise tolerance; no market-data edits or trades."""
import copy,json,sqlite3,sys,unittest
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
import pandas as pd
import analytics as a

E=Path(__file__).parent/'history64-evidence/apple-admission30'

class DemoMoneyTolerance(unittest.TestCase):
 @classmethod
 def setUpClass(cls):
  cls.now=pd.Timestamp(json.loads((E/'comparison.json').read_text(encoding='utf8'))['at'])
  cls.frames={tf:pd.read_pickle(E/('policy-'+tf+'.pkl')) for tf in ('15m','4H','1D')}

 def proof(self,delta=.0000002,field='ema10',mutate=None):
  frame=self.frames['4H'].copy(deep=True)
  enriched,result=a._analyse_features(frame,{},self.now)
  original=a._analyse_features
  # A real confirmed archive, controlled only at the newly changed comparison.
  # Values straddle the cent-rounding midpoint; all other guards remain real.
  if field=='stop':result[field]=300.0049999
  else:enriched.loc[enriched.index[-2:],field]=300.0049999
  def shifted(*args,**kwargs):
   x,r=original(*args,**kwargs)
   if field=='stop':r[field]=300.0049999+delta
   else:x.loc[x.index[-2:],field]=300.0049999+delta
   if mutate:mutate(x,r)
   return x,r
  with patch.object(a,'_analyse_features',side_effect=shifted):
   return a._demo_seed_trials(frame,{},self.now,enriched,result)

 def test_tiny_eur_difference_across_cent_boundary_is_not_a_failure(self):
  proof=self.proof()
  self.assertIsNotNone(proof)
  self.assertEqual(proof[1]['absolute_tolerance_eur'],'0.001')

 def test_absolute_boundary_and_previous_cent_approval(self):
  self.assertTrue(a._demo_eur_seed_equivalent(300.0045,300.0055))
  self.assertFalse(a._demo_eur_seed_equivalent(300.0045,300.0055001))
  # Existing approved monthly seed precision must not be tightened incidentally.
  self.assertTrue(a._demo_eur_seed_equivalent(300.001,300.002492165944))
  for bad in (float('nan'),float('inf'),float('-inf')):
   self.assertFalse(a._demo_eur_seed_equivalent(bad,bad))

 def test_meaningful_eur_change_is_rejected(self):
  self.assertIsNone(self.proof(.00101))

 def test_stop_noise_allowed_but_risk_predicate_change_rejected(self):
  self.assertIsNotNone(self.proof(field='stop'))
  def change(_,r):r['facts']['room']=not r['facts']['room']
  self.assertIsNone(self.proof(field='stop',mutate=change))

 def test_currency_tolerance_not_applied_to_rsi_or_volume(self):
  self.assertIsNone(self.proof(field='rsi'))
  self.assertIsNone(self.proof(field='volume_mean20'))

 def test_tiny_macd_sign_change_still_blocks_actual_core(self):
  original=a._safe_features
  def features(frame,*args,**kwargs):
   x,r=original(frame,*args,**kwargs)
   if frame.attrs['timeframe']=='15m':
    r['facts']['histogram']=.0000001;r['facts']['histogram_delta']=.0000001
    trial=copy.deepcopy(r)
    trial['facts']['histogram']=-.0000001;trial['facts']['histogram_delta']=-.0000001
    r['_demo_seed_trials']=[trial]
   return x,r
  with patch.object(a,'_safe_features',side_effect=features):
   out=a.analyse_multiframe(self.frames,'15m',now=self.now)
  self.assertEqual(out['status'],'DATA BLOCKED')
  self.assertIn('DEMO_SEED_SIGNAL_OR_RISK_UNSTABLE',out['blockers'])

 def test_existing_queue_rechecks_old_model_once_then_preserves_current(self):
  import calculation_window as cw
  from paper_account import UNIVERSE_VERSION
  db=sqlite3.connect(':memory:');self.addCleanup(db.close)
  db.execute('CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT)')
  info={'isin':'US0378331005'};store=SimpleNamespace(connect=lambda:db)
  receipt=dict(paper_universe_version=UNIVERSE_VERSION,input_binding_version=2,closed_input_semantics=2,
    candidates=[dict(owner_reviewed_analysis=True,owner_initialization_semantics=2,analysis_ready=True)])
  def save():db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)',('cp_eligibility:'+info['isin'],json.dumps(receipt)));db.commit()
  save()
  with patch.object(cw,'eligibility_state',return_value='CURRENT'):
   self.assertEqual(cw.eligibility_due(store,[info],self.now),[info])
   db.commit();receipt['candidates'][0]['owner_initialization_semantics']=3;save()
   self.assertEqual(cw.eligibility_due(store,[info],self.now),[])

 def test_model_upgrade_does_not_bypass_error_cooldown(self):
  import calculation_window as cw
  db=sqlite3.connect(':memory:');self.addCleanup(db.close)
  db.execute('CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT)')
  info={'isin':'US0378331005'};store=SimpleNamespace(connect=lambda:db)
  receipt=dict(error='provider unavailable',expires_at=(self.now+pd.Timedelta(minutes=5)).isoformat(),
    candidates=[dict(owner_reviewed_analysis=True,owner_initialization_semantics=2)])
  db.execute('INSERT INTO settings VALUES (?,?)',('cp_eligibility:'+info['isin'],json.dumps(receipt)));db.commit()
  self.assertEqual(cw.eligibility_due(store,[info],self.now),[])

if __name__=='__main__':unittest.main()
