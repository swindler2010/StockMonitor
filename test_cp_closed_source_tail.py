"""New provider tail does not change an independently proven closed context."""
import copy,json,time,unittest
from unittest.mock import patch
import test_closed_receipt_metadata as fx
from performance_cache import capture_closed_inputs,validate_closed_inputs,InputRevisionChanged,_setting_value,_digest


class ClosedSourceTail(unittest.TestCase):
 def setUp(self):
  fx.ClosedReceiptMetadata.setUp(self)
  self.isin='US0378331005';self.key='gettex_chart:'+self.isin+':15m'
  self.db.execute('UPDATE series SET isin=?',(self.isin,))
  self.db.execute('DELETE FROM settings');self.db.execute('DELETE FROM input_revisions')
  self.saved['listing_proof']['isin']=self.isin;self.saved.update(state='SAVED',quality='READY')
  self.saved.pop('archive_invalid');self.saved.pop('invalid_receipts')
  self.spec=dict(isin=self.isin,identity=self.meta,base_tf='15m',timeframe='1H',through='2026-11-10T09:15Z')
  for k in ('instrument:'+self.isin,'market:'+self.isin+':15m'):
   self.db.execute('INSERT INTO input_revisions VALUES(?,1)',(k,))
  self.put(self.saved);self.guard=self.capture()
 def put(self,value):
  self.db.execute('INSERT OR REPLACE INTO settings VALUES(?,?)',(self.key,json.dumps(value)))
  self.db.execute('INSERT INTO input_revisions VALUES(?,1) ON CONFLICT(scope) DO UPDATE SET revision=revision+1',('setting:'+self.key,))
 def capture(self):return capture_closed_inputs(self.db,[self.spec],dict(self.db.execute('SELECT scope,revision FROM input_revisions')))
 def append_tail(self):
  self.db.execute('INSERT INTO series VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',(self.isin,'gettex_chart','GETTEX','EUR','TEST.GTX','15m','2026-11-10 10:15:00',10,11,9,10,100))
  self.bump();self.put({**self.saved,'last':'2026-11-10 10:15:00','added':1,'count':3})
 def bump(self):self.db.execute('UPDATE input_revisions SET revision=revision+1 WHERE scope=?',('market:'+self.isin+':15m',))
 def test_append_outside_closed_window_keeps_exact_proof_not_database_rewrite(self):
  self.append_tail();before=self.db.execute('SELECT value FROM settings WHERE key=?',(self.key,)).fetchone()[0]
  current=validate_closed_inputs(self.db,self.guard)
  self.assertGreater(current['market:'+self.isin+':15m'],self.guard['revisions']['market:'+self.isin+':15m'])
  self.assertEqual(before,self.db.execute('SELECT value FROM settings WHERE key=?',(self.key,)).fetchone()[0])
  self.assertEqual(self.db.execute('SELECT count(*) FROM series').fetchone()[0],2)
 def test_saved_and_unchanged_are_same_success_but_partial_remains_distinct(self):
  self.put({**self.saved,'state':'UNCHANGED'});validate_closed_inputs(self.db,self.guard)
  self.put({**self.saved,'state':'PARTIAL','quality':'INVALID'})
  with self.assertRaises(InputRevisionChanged):validate_closed_inputs(self.db,self.guard)
  partial=self.capture();self.put({**self.saved,'state':'PARTIAL','quality':'INVALID','last':'2026-11-10 10:15:00'})
  validate_closed_inputs(self.db,partial)
 def test_used_correction_deletion_and_late_insertion_still_revoke(self):
  for mutation in ('UPDATE series SET close=10.2','DELETE FROM series',
   "INSERT INTO series SELECT isin,source,venue,currency,symbol,base_tf,'2026-11-10 09:45:00',open,high,low,close,volume FROM series"):
   self.db.execute('SAVEPOINT changed')
   self.db.execute(mutation);self.bump();self.put({**self.saved,'state':'UNCHANGED','last':'2026-11-10 10:15:00'})
   with self.subTest(mutation=mutation),self.assertRaisesRegex(InputRevisionChanged,'Closed candle changed'):
    validate_closed_inputs(self.db,self.guard)
   self.db.execute('ROLLBACK TO changed');self.db.execute('RELEASE changed')
 def test_identity_quality_evidence_and_unknown_changes_remain_blocked(self):
  for field in ('venue','currency','source','symbol','isin','evidence','quality','fault','unknown','error'):
   value=copy.deepcopy(self.saved)
   if field in ('venue','currency','source','symbol'):value['identity'][field]='OTHER'
   elif field in ('isin','evidence'):value['listing_proof'][field]='changed'
   elif field=='quality':value['quality']='INVALID'
   elif field=='fault':value['archive_invalid']={'state':'INVALID','rejected_timestamps':['2026-11-10 10:00:00']}
   elif field=='error':value['state']='SOURCE_UNAVAILABLE'
   else:value['new_unknown']='must invalidate'
   self.put(value)
   with self.subTest(field=field),self.assertRaises(InputRevisionChanged):validate_closed_inputs(self.db,self.guard)
 def test_unconfirmed_and_malformed_provider_records_remain_strict(self):
  for variant in ('status','identity','proof','isin'):
   value=copy.deepcopy(self.saved)
   if variant=='status':value['listing_proof']['status']='UNCONFIRMED'
   elif variant=='identity':value['identity']={}
   elif variant=='proof':value['listing_proof']=None
   else:value['listing_proof']['isin']='OTHER'
   self.put(value);old=self.capture();self.put({**value,'last':'later'})
   with self.subTest(variant=variant),self.assertRaises(InputRevisionChanged):validate_closed_inputs(self.db,old)
 def test_expiry_and_raw_execution_guards_are_not_extended(self):
  from calculation_window import candidate_state,execution_eligibility
  record=dict(isin=self.isin,context='bounded_200_exact_reference_v1',execution_version=1,calendar_revision=fx.XFRA_SESSION_REVISION,
   analysis_scope=dict(policy=fx.SCOPE_POLICY,start='2021-11-10',timezone='Europe/Berlin'))
  candidate=dict(timeframe='15m',input_binding_version=2,closed_inputs=self.guard,context='full_closed_reference_v1',
   expires_at='2026-11-10T09:30Z',execution_revisions=copy.deepcopy(self.guard['revisions']))
  record['candidates']=[candidate];self.append_tail()
  self.db.execute('INSERT INTO settings VALUES(?,?)',('cp_eligibility:'+self.isin,json.dumps(record)))
  self.assertEqual(candidate_state(self.db,record,candidate,fx.pd.Timestamp('2026-11-10T09:20Z')),'CURRENT')
  self.assertEqual(candidate_state(self.db,record,candidate,fx.pd.Timestamp('2026-11-10T09:30Z')),'EXPIRED')
  with patch('paper_account.registered_instrument'),patch('calculation_window.candidate_state',return_value='CURRENT'):
   with self.assertRaisesRegex(ValueError,'SMART_EXECUTION_INPUT_CHANGED'):
    execution_eligibility(self.db,{'isin':self.isin},'15m',self.meta,fx.pd.Timestamp('2026-11-10T09:20Z'))
 def test_serialized_proof_survives_tail_but_old_semantics_require_recalculation(self):
  prior=copy.deepcopy(self.guard);key='setting:'+self.key
  canonical=_setting_value(self.db,self.key);canonical.update(last=self.saved['last'],state='SAVED')
  prior['settings'][key]=_digest(canonical);self.append_tail()
  validate_closed_inputs(self.db,json.loads(json.dumps(self.guard)))
  with self.assertRaises(InputRevisionChanged):validate_closed_inputs(self.db,prior)
 def test_53_and_200_scopes_revoke_only_changed_closed_series(self):
  for size in (53,200):
   guards=[]
   for n in range(size):
    isin='US'+str(n).zfill(10);key='gettex_chart:'+isin+':15m'
    value=copy.deepcopy(self.saved);value['listing_proof']['isin']=isin
    for scope in ('instrument:'+isin,'market:'+isin+':15m','setting:'+key):
     self.db.execute('INSERT OR IGNORE INTO input_revisions VALUES(?,1)',(scope,))
    self.db.execute('INSERT OR REPLACE INTO settings VALUES(?,?)',(key,json.dumps(value)))
    spec={**self.spec,'isin':isin};token={k:1 for k in ('instrument:'+isin,'market:'+isin+':15m','setting:'+key)}
    guards.append(capture_closed_inputs(self.db,[spec],token))
    value.update(last='2026-11-10 10:15:00',state='UNCHANGED')
    self.db.execute('UPDATE settings SET value=? WHERE key=?',(json.dumps(value),key))
    self.db.execute('UPDATE input_revisions SET revision=2 WHERE scope=?',('setting:'+key,))
   key='gettex_chart:US0000000000:15m';self.db.execute('UPDATE settings SET value=? WHERE key=?',(json.dumps({'state':'INVALID'}),key))
   failures=[];memo={};start=time.perf_counter()
   for n,g in enumerate(guards):
    try:validate_closed_inputs(self.db,g,memo)
    except InputRevisionChanged:failures.append(n)
   self.assertEqual(failures,[0]);print('metadata_jobs',size,round(time.perf_counter()-start,4))
   self.db.execute("DELETE FROM input_revisions WHERE scope LIKE '%US000%'")


if __name__=='__main__':unittest.main()
