"""Persistent diagnostic manifests: exact changes, no source/trade writes."""
import json,sqlite3,unittest
from unittest.mock import patch
import test_cp_bridge_diagnostics as fixture
from cp_bridge_diagnostics import Diagnostics
from cp_diagnostic_baseline import decode

class DurableTests(unittest.TestCase):
 def setUp(self):
  self.f=fixture.Tests();self.f.setUp();self.addCleanup(self.f.tearDown)
  self.cache=self.f.descriptor.with_suffix('.checkpoint.sqlite3');self.f.files.append(self.cache)
  self.f.m.close();self.f.m=Diagnostics(self.cache)
 def full(self,instance='before'):
  m=self.f.m;r=m.read(self.f.path,instance,{})
  s=m.sessions[r['revision']]
  for offset in range(0,len(s.infos),2):m.read(self.f.path,instance,dict(path='/by_stock',offset=offset,limit=2,expected_revision=s.revision))
  return s
 def restart(self):self.f.m.close();self.f.m=Diagnostics(self.cache)
 def add(self,db,at,close=10,**kw):
  row=['A','proxy','GER','EUR','S','1H',at,10,11,9,close,2]
  for key,value in kw.items():row[{'open':7,'high':8,'low':9,'volume':11}[key]]=value
  db.execute('INSERT INTO series VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',row)
 def test_restart_retains_complete_baseline_and_counts_insert_update_delete_exactly(self):
  with self.f.connect() as db:
   self.add(db,'2026-11-20 10:00:00');self.add(db,'2026-11-20 11:00:00')
  first=self.full();old=first.revision;self.restart()
  with self.f.connect() as db:
   db.execute("UPDATE series SET close=10.5 WHERE ts='2026-11-20 09:00:00'")
   db.execute("DELETE FROM series WHERE ts='2026-11-20 10:00:00'")
   self.add(db,'2026-11-20 12:00:00')
  second=self.full('new-instance');c=second.completion()['comparison'];v=c['physical_ohlcv']
  self.assertTrue(c['comparable']);self.assertEqual(c['from_revision'],old)
  self.assertEqual([v[k] for k in ('new_unique_physical_ohlcv','corrected_unique_physical_ohlcv','removed_unique_physical_ohlcv')],[1,1,1])
  self.assertEqual((c['new_unique_ohlcv'],c['corrected_unique_ohlcv']),(1,1))
  with self.assertRaisesRegex(ValueError,'PAGE_SESSION_EXPIRED'):self.f.m.read(self.f.path,'new-instance',dict(expected_revision=old))
 def test_structural_errors_are_exact_and_zero_volume_is_valid(self):
  with self.f.connect() as db:
   self.add(db,'2026-11-20 10:00:00',close=15)
   self.add(db,'2026-11-20 11:00:00',volume=-1)
   self.add(db,'2026-11-20 12:00:00',open=0)
   self.add(db,'2026-11-20 13:00:00',open=None)
   self.add(db,'2026-11-20 14:00:00',volume=float('inf'))
   self.add(db,'2026-11-20 15:00:00',volume=0)
  s=self.full();result=s.completion()['confirmed_ohlcv_errors']
  self.assertEqual(result['status'],'EXACT');self.assertEqual(result['count'],5)
  self.assertEqual(result['source_accuracy'],'NOT_CERTIFIED');self.assertIsNone(s.completion()['unresolved_errors'])
 def test_foreign_listing_is_new_and_removed_not_a_correction(self):
  self.full();self.restart()
  with self.f.connect() as db:db.execute("UPDATE series SET currency='USD'")
  c=self.full('second').completion()['comparison']['physical_ohlcv']
  self.assertEqual((c['new_unique_physical_ohlcv'],c['corrected_unique_physical_ohlcv'],c['removed_unique_physical_ohlcv']),(1,0,1))
 def test_partial_snapshot_does_not_replace_last_complete_checkpoint(self):
  old=self.full().revision
  with self.f.connect() as db:db.execute('INSERT INTO instruments VALUES (?,?)',('B',json.dumps(dict(name='B'))))
  r=self.f.m.read(self.f.path,'before',{});self.f.m.read(self.f.path,'before',dict(path='/by_stock',offset=0,limit=1,expected_revision=r['revision']))
  self.restart();prior=self.f.m.checkpoint.load(self.f.path)
  self.assertEqual(prior['revision'],old);self.assertEqual(len(prior['scope']),1)
 def test_budget_unknown_never_becomes_zero_or_a_baseline(self):
  with patch('cp_diagnostic_baseline.capture',side_effect=TimeoutError('MANIFEST_READ_BUDGET')):
   s=self.full();c=s.completion()
  self.assertIsNone(c['confirmed_ohlcv_errors']['count']);self.assertEqual(c['status'],'COMPLETE_WITH_LIMITS')
  self.assertIsNone(self.f.m.checkpoint.load(self.f.path))
 def test_market_database_is_unchanged_and_cache_contains_hashes_not_prices_or_secrets(self):
  with self.f.connect() as db:before=''.join(db.iterdump())
  with patch('cp_reconciliation.turn',side_effect=AssertionError('repair called')):s=self.full()
  blob,_=self.f.m.checkpoint.get(s.revision,'A');manifest=decode(blob)
  self.assertTrue(all(len(v)==64 and set(v)<=set('0123456789abcdef') for v in manifest['rows'].values()))
  self.assertNotIn('MUST_NOT_LEAK',str(manifest))
  with self.f.connect() as db:self.assertEqual(before,''.join(db.iterdump()))
  with self.assertRaises(sqlite3.OperationalError):s.db.execute('DELETE FROM series')
 def test_corrupt_checkpoint_does_not_mint_a_comparison(self):
  self.full();self.f.m.checkpoint.db.execute("UPDATE checkpoint SET payload='{}'");self.f.m.checkpoint.db.commit();self.restart()
  s=self.full('after');self.assertFalse(s.completion()['comparison']['comparable'])
  self.assertEqual(self.f.m.checkpoint_status,'SAVED')
 def test_checkpoint_is_bound_to_original_database(self):
  self.full();self.assertRaisesRegex(ValueError,'CHECKPOINT_BINDING_MISMATCH',self.f.m.checkpoint.load,self.f.descriptor)
 def test_53_200_catalogs_restore_without_trading_or_false_changes(self):
  for count in (53,200):
   with self.f.connect() as db:
    db.execute('DELETE FROM instruments')
    db.executemany('INSERT INTO instruments VALUES (?,?)',[(str(n),json.dumps(dict(name=str(n)))) for n in range(count)])
   a=self.full();self.restart();b=self.full('next'+str(count))
   self.assertEqual(len(b.rows),count);self.assertTrue(b.completion()['comparison']['comparable'])
   self.assertEqual(b.completion()['comparison']['physical_ohlcv']['new_unique_physical_ohlcv'],0)
   self.assertFalse(b.completion()['execution_permission'])

 def test_conflicting_native_copies_remain_unknown_not_zero(self):
  with self.f.connect() as db:
   db.execute("INSERT INTO candles VALUES ('A','gettex','15m','2026-11-20 09:00:00',10,11,9,10,0)")
   db.execute("INSERT INTO series VALUES ('A','gettex','GETTEX','EUR','A','15m','2026-11-20 09:00:00',10,11,9,10,0)")
  s=self.full();self.assertEqual(len(decode(self.f.m.checkpoint.get(s.revision,'A')[0])['rows']),2)
  self.restart()
  with self.f.connect() as db:db.execute("UPDATE candles SET close=10.5")
  s=self.full('changed');c=s.completion()['comparison']['physical_ohlcv']
  self.assertEqual(c['status'],'UNKNOWN');self.assertIsNone(c['corrected_unique_physical_ohlcv'])
  self.assertEqual(s.row(0)['ohlcv_integrity']['ambiguous_physical_keys'],1)
 def test_corrupt_blob_is_unknown_and_current_capture_repairs_cache(self):
  self.full();self.restart()
  with self.f.connect() as db:db.execute('UPDATE series SET close=10.5')
  self.f.m.checkpoint.db.execute("UPDATE blobs SET payload=x'00'");self.f.m.checkpoint.db.commit()
  s=self.full('changed');self.assertEqual(s.completion()['comparison']['physical_ohlcv']['status'],'UNKNOWN')
  self.restart();s=self.full('next');self.assertEqual(s.completion()['comparison']['physical_ohlcv']['status'],'EXACT')
 def test_future_checkpoint_is_rejected_without_extending_freshness(self):
  s=self.full();baseline=s.baseline();baseline['generated_at']='2099-01-01T00:00:00+00:00'
  self.f.m.checkpoint.publish(self.f.path,baseline);self.restart()
  s=self.full('fresh');self.assertFalse(s.completion()['comparison']['comparable'])
  self.assertLess(s.generated_at,'2099')
 def test_storage_failure_preserves_unknown_and_is_read_only(self):
  with patch.object(self.f.m.checkpoint,'put',side_effect=sqlite3.OperationalError('disk full')):
   s=self.full();c=s.completion()
  self.assertEqual(c['status'],'COMPLETE_WITH_LIMITS');self.assertIsNone(c['confirmed_ohlcv_errors']['count'])
  self.assertIsNone(self.f.m.checkpoint.load(self.f.path));self.assertFalse(c['execution_permission'])

 def test_full_catalog_metadata_over_8mb_restores_and_pruning_keeps_baseline(self):
  import hashlib
  s=self.full();baseline=s.baseline()
  baseline['unverified']={hashlib.sha256(str(n).encode()).digest() for n in range(256000)}
  self.f.m.checkpoint.publish(self.f.path,baseline);self.f.m.checkpoint.prune(set())
  self.restart();restored=self.f.m.checkpoint.load(self.f.path)
  self.assertEqual(restored['unverified'],baseline['unverified']);self.assertEqual(restored['revision'],s.revision)
  self.assertIsNotNone(self.f.m.checkpoint.get(s.revision,'A'))
 def test_metadata_size_limit_is_explicit_and_preserves_previous_baseline(self):
  s=self.full();previous=self.f.m.checkpoint.load(self.f.path)
  with patch('cp_diagnostic_baseline.MAX_METADATA_RAW',1):
   self.assertRaisesRegex(ValueError,'CHECKPOINT_SIZE_LIMIT',self.f.m.checkpoint.publish,self.f.path,s.baseline())
  self.assertEqual(self.f.m.checkpoint.load(self.f.path)['revision'],previous['revision'])

if __name__=='__main__':unittest.main()
