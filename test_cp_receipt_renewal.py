"""Renewal counters are not a change to an already verified owner decision."""
import copy,json,unittest
from unittest.mock import patch
import test_closed_receipt_metadata as fx
from performance_cache import capture_closed_inputs,validate_closed_inputs,InputRevisionChanged,_digest
import cp_controls as ctl


class ReceiptRenewal(unittest.TestCase):
 setUp_base=fx.ClosedReceiptMetadata.setUp
 update_source=fx.ClosedReceiptMetadata.update
 def setUp(self):
  self.setUp_base()
  self.grant_key='cp_manual_risk:TEST'
  self.put(ctl.TV_POLICY,dict(enabled=True,approved=True,version=ctl.TV_VERSION))
  self.scopes=['market:TEST:15m','instrument:TEST','setting:'+ctl.TV_POLICY,'setting:'+self.key]
  self.grant=dict(account='five-eur-10000-v1',accepted={'gap':dict(actor=ctl.TV_ACTOR,
   problem=dict(isin='TEST',tf='15m',kind='archive',identity=self.meta,state='UNCONFIRMED',range=['2026-10-28 12:15:00']*2),
   evidence=dict(status='OWNER_ACCEPTED_TV_ABSENCE',native_binding='exact-reviewed-inputs',price_model=ctl.TV_PRICE_MODEL),
   input_revisions=ctl._revisions(self.db,self.scopes))})
  self.put(self.grant_key,self.grant)
  self.specs=[s['spec'] for s in self.guard['series']]
  self.guard=self.capture()
 def put(self,key,value):
  self.db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)',(key,json.dumps(value)))
  self.db.execute('INSERT INTO input_revisions VALUES (?,1) ON CONFLICT(scope) DO UPDATE SET revision=revision+1',('setting:'+key,))
 def capture(self):return capture_closed_inputs(self.db,self.specs,dict(self.db.execute('SELECT scope,revision FROM input_revisions')))
 def renew(self):
  grant=copy.deepcopy(self.grant)
  grant['accepted']['gap']['input_revisions']=ctl._revisions(self.db,self.scopes)
  self.put(self.grant_key,grant)
  return grant
 def append_outside(self):
  self.db.execute("INSERT INTO series SELECT isin,source,venue,currency,symbol,base_tf,'2026-11-10 11:00:00',open,high,low,close,volume FROM series LIMIT 1")
  self.db.execute("UPDATE input_revisions SET revision=revision+1 WHERE scope='market:TEST:15m'")
 def test_same_verified_receipt_after_later_bar_keeps_closed_proof(self):
  self.append_outside();renewed=self.renew()
  current=validate_closed_inputs(self.db,self.guard)
  self.assertNotEqual(current['market:TEST:15m'],self.guard['revisions']['market:TEST:15m'])
  saved=json.loads(self.db.execute('SELECT value FROM settings WHERE key=?',(self.grant_key,)).fetchone()[0])
  self.assertEqual(saved,renewed) # Fingerprinting never rewrites the grant.
  self.assertIsInstance(saved['accepted']['gap']['input_revisions']['market:TEST:15m'],int)
 def test_receipt_transport_renewal_keeps_same_evidence(self):
  self.update_source({**self.saved,'received_at':'later','receipt':'new.json'})
  self.renew();validate_closed_inputs(self.db,self.guard)
 def test_changed_used_candle_cannot_be_hidden_by_renewal(self):
  self.db.execute('UPDATE series SET close=10.1')
  self.db.execute("UPDATE input_revisions SET revision=revision+1 WHERE scope='market:TEST:15m'")
  self.renew()
  with self.assertRaisesRegex(InputRevisionChanged,'Closed candle changed'):validate_closed_inputs(self.db,self.guard)
 def test_changed_decision_origin_or_unknown_field_still_revokes(self):
  for mutation in ('range','actor','native_binding','unknown','scope','remove'):
   with self.subTest(mutation=mutation):
    g=copy.deepcopy(self.grant);e=g['accepted']['gap']
    if mutation=='range':e['problem']['range']=['2026-10-29 12:15:00']*2
    elif mutation=='actor':e['actor']='UNCONFIRMED_ORIGIN'
    elif mutation=='native_binding':e['evidence']['native_binding']='changed'
    elif mutation=='unknown':e['new_unreviewed_field']=True
    elif mutation=='scope':e['input_revisions']['market:OTHER:15m']=0
    else:g['accepted']={}
    self.put(self.grant_key,g)
    with self.assertRaises(InputRevisionChanged):validate_closed_inputs(self.db,self.guard)
 def test_unvalidated_receipt_and_disabled_policy_remain_blocked(self):
  self.append_outside()
  self.put(self.grant_key,self.grant) # Stale receipt is not a validated renewal.
  with self.assertRaises(InputRevisionChanged):validate_closed_inputs(self.db,self.guard)
  self.put(ctl.TV_POLICY,dict(enabled=False,approved=True,version=ctl.TV_VERSION));self.renew()
  with self.assertRaises(InputRevisionChanged):validate_closed_inputs(self.db,self.guard)
 def test_old_proof_is_not_upgraded_and_raw_execution_token_changes(self):
  legacy=copy.deepcopy(self.guard);legacy['settings']['setting:'+self.grant_key]=_digest(self.grant)
  self.append_outside();self.renew()
  with self.assertRaises(InputRevisionChanged):validate_closed_inputs(self.db,legacy)
  self.assertNotEqual(ctl._revisions(self.db,self.guard['revisions']),self.guard['revisions'])
 def test_execution_revalidates_its_own_strict_token_after_renewal(self):
  from calculation_window import execution_eligibility
  record=dict(context='bounded_200_exact_reference_v1',isin='TEST',candidates=[dict(
   timeframe='15m',input_binding_version=2,execution_revisions=copy.deepcopy(self.guard['revisions']))])
  self.put('cp_eligibility:TEST',record);self.append_outside();self.renew()
  with patch('paper_account.registered_instrument'),patch('calculation_window.candidate_state',return_value='CURRENT'):
   with self.assertRaisesRegex(ValueError,'SMART_EXECUTION_INPUT_CHANGED'):
    execution_eligibility(self.db,{'isin':'TEST'},'15m',self.meta,fx.pd.Timestamp('2026-11-10T09:20Z'))
 def test_53_and_200_independent_receipts_remain_separate(self):
  # No new cache/worker; exercise the existing batched validation memo.
  from performance_cache import _setting_value
  for count in (53,200):
   with self.subTest(count=count):
    proofs=[]
    for n in range(count):
     key='cp_manual_risk:TEST'+str(n);self.put(key,self.grant)
     scope='setting:'+key
     proofs.append(capture_closed_inputs(self.db,[],{scope:ctl._revisions(self.db,[scope])[scope]}))
    memo={}
    for n,guard in enumerate(proofs):
     key='cp_manual_risk:TEST'+str(n);self.put(key,self.grant)
     validate_closed_inputs(self.db,guard,memo)
    self.assertEqual(len(memo),count)
    changed=copy.deepcopy(self.grant);changed['accepted']['gap']['evidence']['native_binding']='changed'
    self.put('cp_manual_risk:TEST0',changed)
    with self.assertRaises(InputRevisionChanged):validate_closed_inputs(self.db,proofs[0],memo)
    validate_closed_inputs(self.db,proofs[-1],memo)


if __name__=='__main__':unittest.main()
