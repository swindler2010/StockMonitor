import json,unittest
from unittest.mock import patch
from test_cp_diagnostic_cleanup import Memory
import cp_diagnostic_cleanup as cleanup

class Retirement(unittest.TestCase):
 def setUp(self):
  self.s=Memory();self.addCleanup(self.s.db.close)
  self.key='data_quality:REMOVED:OLD.F:15m';self.raw='{"state":"INVALID", "issues":["original"], "checked_at":"2020-01-01"}'
  with self.s.connect() as db:
   db.execute('INSERT INTO settings VALUES (?,?)',(self.key,self.raw))
   db.execute('INSERT INTO series VALUES (?,?,?,?,?,?,?,?)',('REMOVED','proxy','FRA','EUR','OLD.F','15m','2020-01-01',100))
 def test_exact_before_preserved_only_diagnostic_retired_no_market_changes(self):
  before=self.s.db.execute('SELECT * FROM series').fetchall()
  out=cleanup.turn(self.s,{'isin':'TEST'})
  self.assertEqual(out['state'],'RETIRED');self.assertFalse(out['quality_repaired'])
  self.assertIsNone(self.s.get(self.key));self.assertEqual(self.s.get(out['receipt_key'])['before_raw'],self.raw)
  self.assertEqual(self.s.db.execute('SELECT * FROM series').fetchall(),before)
  from candlepilot_health import retired_diagnostic_count,diagnostic_scope_text
  self.assertEqual(retired_diagnostic_count(self.s.db),1)
  self.assertIn('не исправление свечей',diagnostic_scope_text({'retired_reports':1}))
  self.assertEqual(cleanup.turn(self.s,{'isin':'TEST'})['state'],'NO_MATCH')
 def test_reregistered_instrument_is_never_retired(self):
  with self.s.connect() as db:db.execute('INSERT INTO instruments VALUES (?,?)',('REMOVED','{}'))
  self.assertEqual(cleanup.turn(self.s,{'isin':'TEST'})['state'],'NO_MATCH');self.assertIsNotNone(self.s.get(self.key))
 def test_receipt_cap_preserves_diagnostic_and_old_receipts(self):
  self.s.put('cp_retired_diagnostic:existing',{'unique':'keep'})
  with patch.object(cleanup,'RETIRE_RECEIPT_LIMIT',1):self.assertEqual(cleanup.turn(self.s,{'isin':'TEST'})['state'],'RECEIPT_LIMIT')
  self.assertIsNotNone(self.s.get(self.key));self.assertEqual(self.s.get('cp_retired_diagnostic:existing'),{'unique':'keep'})
 def test_delete_conflict_rolls_back_new_receipt(self):
  self.s.db.execute("CREATE TRIGGER reject_delete BEFORE DELETE ON settings BEGIN SELECT RAISE(IGNORE); END;")
  self.assertEqual(cleanup.turn(self.s,{'isin':'TEST'})['state'],'UNKNOWN')
  self.assertIsNotNone(self.s.get(self.key));self.assertEqual(self.s.db.execute("SELECT count(*) FROM settings WHERE key LIKE 'cp_retired_diagnostic:%'").fetchone()[0],0)
 def test_one_per_turn_and_original_receipt_not_overwritten(self):
  self.s.put('data_quality:SECOND:SECOND.F:1D',{'state':'GAP'})
  out=cleanup.turn(self.s,{'isin':'TEST'});saved=self.s.get(out['receipt_key'])
  self.assertIsNotNone(self.s.get('data_quality:SECOND:SECOND.F:1D'))
  with self.s.connect() as db:db.execute('INSERT INTO settings VALUES (?,?)',(self.key,self.raw))
  again=cleanup.turn(self.s,{'isin':'TEST'});self.assertEqual(again['receipt_key'],out['receipt_key'])
  self.assertEqual(self.s.get(out['receipt_key']),saved)
 def test_malformed_prefix_keys_cannot_starve_valid_orphan(self):
  for n in range(8):self.s.put('data_quality:000'+str(n),{'state':'INVALID'})
  self.assertEqual(cleanup.turn(self.s,{'isin':'TEST'})['state'],'RETIRED')
  self.assertIsNone(self.s.get(self.key))
  self.assertEqual(self.s.db.execute("SELECT count(*) FROM settings WHERE key LIKE 'data_quality:000%'").fetchone()[0],8)

if __name__=='__main__':unittest.main()
