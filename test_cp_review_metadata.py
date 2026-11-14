"""Unchanged v4 reconciliation evidence survives a bookkeeping-only recheck."""
import copy,json,unittest
from unittest.mock import patch
import test_closed_receipt_metadata as fx
from performance_cache import capture_closed_inputs,validate_closed_inputs,InputRevisionChanged


class ReviewMetadata(unittest.TestCase):
 def setUp(self):
  fx.ClosedReceiptMetadata.setUp(self)
  self.source_key=self.key;self.key='cp_reconciliation_job:TEST:review'
  self.saved=dict(version=1,fingerprint_version=4,isin='TEST',identity=self.meta,tf='15m',
   first='2026-10-28 12:15:00',end='2026-10-28 12:30:00',input_fingerprint='a'*64,
   status='AWAITING_SOURCE',review_required=False,native_rows=[],native_neighbors={'before':[10],'after':[11]},
   observation=dict(result='ABSENT',identity_confirmed=True,fingerprint_version=4,input_fingerprint='a'*64,
    tv_symbol='GETTEX:TEST',at='2026-10-28T13:00Z',actor='human_assisted',evidence=['confirmed-image']),
   finding=dict(state='UNCONFIRMED',market_state='UNCONFIRMED',reason='No authoritative trade evidence',
    can_affect_dq=False,observation_current=True,evidence_digest='b'*64,evidence_checked_at='2026-11-10T08:00Z'),
   checked_at='2026-11-10T08:01Z',next_check='2026-11-10T09:01Z',checks=1,native_retry='2026-11-10T08:30Z',
   history=[{'at':'2026-11-10T08:01Z','finding':'old'}],
   source_comparison=dict(native_checked_at='2026-11-10T08:01Z',presence_match=True,ohlcv_match=None),
   monthly_review=dict(external_due_at='2026-11-17T08:00Z',external_state='NOT_DUE'))
  self.put(self.key,self.saved);self.specs=[s['spec'] for s in self.guard['series']];self.guard=self.capture()
 def put(self,key,value):
  self.db.execute('INSERT OR REPLACE INTO settings VALUES(?,?)',(key,json.dumps(value)))
  self.db.execute('INSERT INTO input_revisions VALUES(?,1) ON CONFLICT(scope) DO UPDATE SET revision=revision+1',('setting:'+key,))
 def capture(self):return capture_closed_inputs(self.db,self.specs,dict(self.db.execute('SELECT scope,revision FROM input_revisions')))
 def renewed(self):
  changed=copy.deepcopy(self.saved)
  changed.update(checked_at='2026-11-10T09:01Z',next_check='2026-11-10T10:01Z',checks=2,native_retry='later',history=[{'at':'later','finding':'same'}])
  changed['finding'].update(evidence_digest='c'*64,evidence_checked_at='2026-11-10T09:00Z')
  changed['source_comparison']['native_checked_at']='later'
  return changed
 def test_verified_v4_recheck_keeps_closed_proof_and_original_record(self):
  changed=self.renewed();self.put(self.key,changed)
  current=validate_closed_inputs(self.db,self.guard)
  self.assertGreater(current['setting:'+self.key],self.guard['revisions']['setting:'+self.key])
  self.assertEqual(json.loads(self.db.execute('SELECT value FROM settings WHERE key=?',(self.key,)).fetchone()[0]),changed)
 def test_changed_decision_identity_evidence_and_unknown_fields_revoke(self):
  for name in ('identity','fingerprint','observation','classification','review','unknown','comparison','expiry'):
   with self.subTest(name=name):
    v=self.renewed()
    if name=='identity':v['identity']['currency']='USD'
    elif name=='fingerprint':v['input_fingerprint']='d'*64;v['observation']['input_fingerprint']='d'*64
    elif name=='observation':v['observation']['evidence']=['changed-image']
    elif name=='classification':v['finding']['state']='INVALID'
    elif name=='review':v['review_required']=True
    elif name=='unknown':v['finding']['new_unreviewed_field']=True
    elif name=='comparison':v['source_comparison']['presence_match']=False
    else:v['monthly_review']['external_due_at']='2026-11-10T09:00Z'
    self.put(self.key,v)
    with self.assertRaises(InputRevisionChanged):validate_closed_inputs(self.db,self.guard)
 def test_changed_native_rows_neighbors_or_used_ohlcv_revoke(self):
  for name in ('native_rows','native_neighbors'):
   v=self.renewed();v[name]=['changed'];self.put(self.key,v)
   with self.assertRaises(InputRevisionChanged):validate_closed_inputs(self.db,self.guard)
  self.put(self.key,self.renewed());self.db.execute('UPDATE series SET close=10.1')
  self.db.execute("UPDATE input_revisions SET revision=revision+1 WHERE scope='market:TEST:15m'")
  with self.assertRaisesRegex(InputRevisionChanged,'Closed candle changed'):validate_closed_inputs(self.db,self.guard)
 def test_legacy_unbound_or_unconfirmed_records_remain_strict(self):
  for name in ('legacy','unbound','finding','malformed','identity','blocked','malformed_observation','malformed_finding'):
   v=copy.deepcopy(self.saved)
   if name=='legacy':v['fingerprint_version']=3
   elif name=='unbound':v['observation']['input_fingerprint']='e'*64
   elif name=='finding':v['finding']['observation_current']=False
   elif name=='malformed':v['input_fingerprint']='not-a-proof'
   elif name=='identity':v['observation']['identity_confirmed']=False
   elif name=='malformed_observation':v['observation']=['invalid']
   elif name=='malformed_finding':v['finding']=['invalid']
   else:v['status']='BLOCKED'
   self.put(self.key,v);guard=self.capture();v['checks']+=1;self.put(self.key,v)
   with self.subTest(name=name),self.assertRaises(InputRevisionChanged):validate_closed_inputs(self.db,guard)
 def test_deadline_and_strict_execution_revision_are_not_extended(self):
  from calculation_window import candidate_state,execution_eligibility
  record=dict(context='bounded_200_exact_reference_v1',isin='TEST',execution_version=1,
   calendar_revision=fx.XFRA_SESSION_REVISION,analysis_scope=dict(policy=fx.SCOPE_POLICY,start='2021-11-10',timezone='Europe/Berlin'))
  candidate=dict(timeframe='15m',input_binding_version=2,closed_inputs=self.guard,
   expires_at='2026-11-10T09:30Z',execution_revisions=copy.deepcopy(self.guard['revisions']))
  record['candidates']=[candidate];self.put('cp_eligibility:TEST',record);self.put(self.key,self.renewed())
  self.assertEqual(candidate_state(self.db,record,candidate,fx.pd.Timestamp('2026-11-10T09:20Z')),'CURRENT')
  self.assertEqual(candidate_state(self.db,record,candidate,fx.pd.Timestamp('2026-11-10T09:30Z')),'EXPIRED')
  with patch('paper_account.registered_instrument'),patch('calculation_window.candidate_state',return_value='CURRENT'):
   with self.assertRaisesRegex(ValueError,'SMART_EXECUTION_INPUT_CHANGED'):
    execution_eligibility(self.db,{'isin':'TEST'},'15m',self.meta,fx.pd.Timestamp('2026-11-10T09:20Z'))
 def test_authoritative_evidence_change_revokes_before_job_recheck(self):
  key='interval_evidence:TEST';self.put(key,dict(no_trades=[],identity=self.meta))
  guard=self.capture();self.put(self.key,self.renewed())
  self.put(key,dict(no_trades=[],trade_intervals=[['2026-10-28 12:15:00','2026-10-28 12:30:00']],identity=self.meta))
  with self.assertRaisesRegex(InputRevisionChanged,'interval_evidence:TEST'):validate_closed_inputs(self.db,guard)
 def test_prior_full_metadata_proof_requires_normal_recalculation(self):
  from performance_cache import _digest
  guard=copy.deepcopy(self.guard);guard['settings']['setting:'+self.key]=_digest(self.saved)
  self.put(self.key,self.renewed())
  with self.assertRaises(InputRevisionChanged):validate_closed_inputs(self.db,guard)
 def test_53_and_200_jobs_remain_independent_with_validation_memo(self):
  for count in (53,200):
   with self.subTest(count=count):
    guards=[]
    for n in range(count):
     k=self.key+str(n);self.put(k,self.saved);scope='setting:'+k
     revision=self.db.execute('SELECT revision FROM input_revisions WHERE scope=?',(scope,)).fetchone()[0]
     guards.append(capture_closed_inputs(self.db,[],{scope:revision}))
    memo={}
    for n,guard in enumerate(guards):self.put(self.key+str(n),self.renewed());validate_closed_inputs(self.db,guard,memo)
    self.assertEqual(len(memo),count)
    v=self.renewed();v['finding']['reason']='Conflicting evidence';self.put(self.key+'0',v)
    with self.assertRaises(InputRevisionChanged):validate_closed_inputs(self.db,guards[0],memo)
    validate_closed_inputs(self.db,guards[-1],memo)


if __name__=='__main__':unittest.main()
