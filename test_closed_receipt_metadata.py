"""GETTEX transport receipts cannot change a proven closed calculation input."""
import copy,json,sqlite3,sys,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
import pandas as pd
from performance_cache import capture_closed_inputs,validate_closed_inputs,InputRevisionChanged
from calculation_window import candidate_state,SCOPE_POLICY
from data_quality import XFRA_SESSION_REVISION


class ClosedReceiptMetadata(unittest.TestCase):
 def setUp(self):
  self.db=sqlite3.connect(':memory:');self.addCleanup(self.db.close)
  self.db.executescript('CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT);'
   'CREATE TABLE input_revisions(scope TEXT PRIMARY KEY,revision INTEGER);'
   'CREATE TABLE series(isin,source,venue,currency,symbol,base_tf,ts,open,high,low,close,volume);')
  self.meta=dict(source='gettex_chart',symbol='TEST.GTX',venue='GETTEX',currency='EUR',timezone='Europe/Berlin')
  self.key='gettex_chart:TEST:15m'
  self.saved=dict(identity=self.meta,listing_proof=dict(self.meta,isin='TEST',status='CONFIRMED',verified_at='old',evidence='confirmed'),
   state='PARTIAL',quality='INVALID',archive_invalid=dict(state='INVALID',rejected_timestamps=['2026-10-20 23:00:00']),
   invalid_receipts=['old.json'],last='2026-11-10 10:00:00',count=2,added=0,changed=0,
   input_revisions_before={'market:TEST:15m':1},input_revisions_after={'market:TEST:15m':1},requested_from='old')
  for k in ('instrument:TEST','market:TEST:15m','setting:'+self.key):self.db.execute('INSERT INTO input_revisions VALUES(?,1)',(k,))
  self.db.execute('INSERT INTO series VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',('TEST','gettex_chart','GETTEX','EUR','TEST.GTX','15m','2026-11-10 10:00:00',10,11,9,10,100))
  self.db.execute('INSERT INTO settings VALUES(?,?)',(self.key,json.dumps(self.saved)))
  spec=dict(isin='TEST',identity=self.meta,base_tf='15m',timeframe='15m',through='2026-11-10T09:15Z')
  token=dict(self.db.execute('SELECT scope,revision FROM input_revisions'))
  self.guard=capture_closed_inputs(self.db,[spec],token)
 def update(self,value):
  self.db.execute('UPDATE settings SET value=? WHERE key=?',(json.dumps(value),self.key))
  self.db.execute('UPDATE input_revisions SET revision=revision+1 WHERE scope=?',('setting:'+self.key,))
 def test_new_receipt_without_changed_inputs_does_not_revoke(self):
  changed=copy.deepcopy(self.saved)
  changed.update(invalid_receipts=['old.json','new.json'],count=1,added=0,changed=0,requested_from='new',
   input_revisions_before={'market:TEST:15m':2},input_revisions_after={'market:TEST:15m':2},received_at='new',receipt='new.json')
  changed['listing_proof']['verified_at']='new';self.update(changed)
  current=validate_closed_inputs(self.db,self.guard)
  self.assertGreater(current['setting:'+self.key],self.guard['revisions']['setting:'+self.key])
 def test_identity_listing_status_and_evidence_changes_revoke(self):
  for field,value in [('source','unknown'),('venue','FRA'),('currency','USD'),('symbol','OTHER')]:
   changed=copy.deepcopy(self.saved);changed['identity'][field]=value;self.update(changed)
   with self.assertRaises(InputRevisionChanged):validate_closed_inputs(self.db,self.guard)
  for field,value in [('status','UNCONFIRMED'),('evidence','new conflicting proof')]:
   changed=copy.deepcopy(self.saved);changed['listing_proof'][field]=value;self.update(changed)
   with self.assertRaises(InputRevisionChanged):validate_closed_inputs(self.db,self.guard)
 def test_quality_rejected_ranges_and_unrecognized_fields_revoke(self):
  for mutation in [dict(quality='READY'),dict(state='SOURCE_UNAVAILABLE'),
    dict(archive_invalid=dict(state='INVALID',rejected_timestamps=['2026-11-10 10:00:00'])),
    dict(native_monthly={'provenance':'different'}),dict(new_unknown_field='not reviewed')]:
   self.update({**copy.deepcopy(self.saved),**mutation})
   with self.assertRaises(InputRevisionChanged):validate_closed_inputs(self.db,self.guard)
 def test_ohlcv_changes_never_hidden_by_transport_receipt(self):
  changed=copy.deepcopy(self.saved);changed['invalid_receipts'].append('new.json');self.update(changed)
  self.db.execute('UPDATE series SET close=10.1')
  self.db.execute("UPDATE input_revisions SET revision=revision+1 WHERE scope='market:TEST:15m'")
  with self.assertRaisesRegex(InputRevisionChanged,'Closed candle changed'):validate_closed_inputs(self.db,self.guard)
 def test_deadline_remains_and_serialized_proof_does_not_grant_execution(self):
  changed=copy.deepcopy(self.saved);changed['invalid_receipts'].append('new.json');self.update(changed)
  record=dict(isin='TEST',execution_version=1,calendar_revision=XFRA_SESSION_REVISION,
   analysis_scope=dict(policy=SCOPE_POLICY,start='2021-11-10',timezone='Europe/Berlin'))
  candidate=dict(input_binding_version=2,context='full_closed_reference_v1',
   closed_inputs=json.loads(json.dumps(self.guard)),expires_at='2026-11-10T09:30Z',trade_ready=False)
  self.assertEqual(candidate_state(self.db,record,candidate,pd.Timestamp('2026-11-10T09:20Z')),'CURRENT')
  self.assertEqual(candidate_state(self.db,record,candidate,pd.Timestamp('2026-11-10T09:30Z')),'EXPIRED')
  self.assertFalse(candidate['trade_ready'])

if __name__=='__main__':unittest.main()
