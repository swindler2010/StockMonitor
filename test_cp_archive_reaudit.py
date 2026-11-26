import json,unittest
from unittest.mock import patch
from test_cp_local_completion import LocalCompletion,APPLE,pd,hs,dq

class Reaudit(unittest.TestCase):
 def setUp(self):
  f=LocalCompletion();f.setUp();self.addCleanup(f.doCleanups)
  self.f=f;self.s=f.s
 def test_code_version_invalidates_once_without_market_change(self):
  self.f.seed();self.f.turn();before=self.f.f.rows()
  progress=self.s.setting('history_archive_progress:'+APPLE)
  for unit in progress['series'].values():unit.pop('audit_version',None)
  self.s.set_setting('history_archive_progress:'+APPLE,progress)
  self.assertEqual(self.f.turn()['state'],'AUDITED')
  self.assertEqual(self.f.turn()['state'],'IDLE');self.assertEqual(self.f.f.rows(),before)
 def test_proof_only_change_rechecks_without_ohlcv_change(self):
  self.f.seed();self.f.turn();before=self.f.f.rows()
  meta=dict(source='gettex',venue='GETTEX',currency='EUR',symbol=APPLE,timezone='Europe/Berlin')
  self.s.set_setting(hs.parent_provenance_key(APPLE,meta,'15m'),{'changed_proof':True})
  self.assertEqual(self.f.turn()['state'],'AUDITED');self.assertEqual(self.f.f.rows(),before)
 def test_gettex_chart_saved_archive_is_rechecked_without_http_or_repair(self):
  self.s.db.execute('INSERT INTO series VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',(APPLE,'gettex_chart','GETTEX','EUR','AAPL.GTX','15m','2026-11-03 11:00:00',100,101,99,100,2));self.s.db.commit()
  def load(isin,base,source,symbol,venue,currency):
   f=pd.DataFrame([[100,101,99,100,2]],index=pd.to_datetime(['2026-11-03 11:00:00']),columns=dq.FIELDS)
   f.attrs=dict(source=source,symbol=symbol,venue=venue,currency=currency,timezone='Europe/Berlin');return f
  self.s.load_series=load
  result=self.f.turn();self.assertEqual(result['state'],'AUDITED')
  progress=self.s.setting('history_archive_progress:'+APPLE)
  unit=progress['series'][json.dumps(['gettex_chart','GETTEX','EUR','AAPL.GTX','15m'])]
  self.assertEqual(unit['audit_version'],hs.ARCHIVE_AUDIT_VERSION)
  self.assertEqual(self.f.turn()['state'],'IDLE')

 def test_local_audit_does_not_consume_pending_recovery(self):
  self.f.seed(missing=True);self.f.turn()
  with patch.object(dq,'repair',return_value={'state':'UNCONFIRMED'}) as repair:
   result=hs.inspect_saved_archive({'isin':APPLE},self.s,self.f.now,recover_gettex=lambda *a:None)
   repair.assert_called_once()
   self.assertEqual(result['recovery'],'UNCONFIRMED')
   again=hs.inspect_saved_archive({'isin':APPLE},self.s,self.f.now,recover_gettex=lambda *a:None)
   self.assertEqual(again['state'],'DONE');repair.assert_called_once()

 def test_unserved_recovery_respects_existing_future_retry(self):
  self.f.seed(missing=True);self.f.turn()
  self.s.set_setting(dq.key({'isin':APPLE},APPLE,'15m'),{'next_retry':(self.f.now+pd.Timedelta(hours=1)).isoformat(),'attempts':2})
  with patch.object(dq,'repair',side_effect=AssertionError('cooldown bypass')):
   result=hs.inspect_saved_archive({'isin':APPLE},self.s,self.f.now,recover_gettex=lambda *a:None)
  self.assertEqual(result['state'],'DONE')
  self.assertEqual(self.s.setting(dq.key({'isin':APPLE},APPLE,'15m'))['attempts'],2)

 def test_missing_adapter_is_bounded_but_new_adapter_retries(self):
  self.f.seed(missing=True);self.f.turn()
  with patch.object(dq,'repair',return_value={'state':'UNCONFIRMED'}) as repair:
   first=hs.inspect_saved_archive({'isin':APPLE},self.s,self.f.now)
   self.assertEqual(first['recovery'],'NO_CONFIRMED_ADAPTER');repair.assert_not_called()
   self.assertEqual(hs.inspect_saved_archive({'isin':APPLE},self.s,self.f.now)['state'],'DONE')
   hs.inspect_saved_archive({'isin':APPLE},self.s,self.f.now,recover_gettex=lambda *a:None)
   repair.assert_called_once()

 def test_recovery_does_not_overwrite_concurrent_audit(self):
  self.f.seed(missing=True);self.f.turn()
  def race(*args,**kwargs):
   p=self.s.setting('history_archive_progress:'+APPLE)
   for e in p['series'].values():e['concurrent_marker']='new audit'
   self.s.set_setting('history_archive_progress:'+APPLE,p)
   return {'state':'UNCONFIRMED'}
  with patch.object(dq,'repair',side_effect=race):
   result=hs.inspect_saved_archive({'isin':APPLE},self.s,self.f.now,recover_gettex=lambda *a:None)
  self.assertEqual(result['recovery_progress'],'CHANGED_DURING_RECOVERY')
  entry=next(iter(self.s.setting('history_archive_progress:'+APPLE)['series'].values()))
  self.assertEqual(entry['concurrent_marker'],'new audit');self.assertNotIn('recovery_checked_at',entry)

 def test_chart_binding_ignores_poll_clock_but_tracks_proof_validity(self):
  meta=dict(source='gettex_chart',symbol='AAPL.GTX',venue='GETTEX',currency='EUR',timezone='Europe/Berlin')
  proof=dict(meta,isin=APPLE,status='CONFIRMED')
  saved=dict(state='SAVED',identity=meta,listing_proof=proof,received_at='2026-11-03T10:00Z')
  self.s.set_setting('gettex_chart:'+APPLE+':15m',saved)
  first=hs._archive_recovery_binding({'isin':APPLE},self.s,meta,'15m')
  self.assertTrue(first['confirmed'])
  saved.update(state='UNCHANGED',received_at='2026-11-03T11:00Z',next_request='2026-11-03T12:00Z')
  self.s.set_setting('gettex_chart:'+APPLE+':15m',saved)
  self.assertEqual(hs._archive_recovery_binding({'isin':APPLE},self.s,meta,'15m'),first)
  saved['listing_proof']={**proof,'currency':'USD'};self.s.set_setting('gettex_chart:'+APPLE+':15m',saved)
  self.assertFalse(hs._archive_recovery_binding({'isin':APPLE},self.s,meta,'15m')['confirmed'])

 def test_changed_market_reaudit_keeps_real_repair_network_cooldown(self):
  import tempfile
  from pathlib import Path
  from storage import Store
  archive=Path(__file__).resolve().parents[2]/'СМ-Архив'
  with tempfile.TemporaryDirectory(dir=archive if archive.is_dir() else None) as directory:
   store=Store(directory)
   try:
    info={'isin':'TEST','name':'test'};store.save_instrument(info)
    with store.connect() as db:
     for stamp in ('11:00','11:30'):
      db.execute('INSERT INTO candles VALUES (?,?,?,?,?,?,?,?,?)',('TEST','gettex','15m','2026-11-03 '+stamp+':00',100,101,99,100,2))
    hs.inspect_saved_archive(info,store,self.f.now,local_only=True)
    key=dq.key(info,'TEST','15m');deadline=(self.f.now+pd.Timedelta(hours=1)).isoformat()
    store.set_setting(key,{'state':'UNCONFIRMED','next_retry':deadline})
    with store.connect() as db:db.execute("UPDATE candles SET close=100.5 WHERE ts='2026-11-03 11:00:00'")
    with patch('requests.get',side_effect=AssertionError('HTTP before cooldown')),patch('history_service.recover_from_children',return_value=None):
     out=hs.inspect_saved_archive(info,store,self.f.now,recover_gettex=lambda *a:self.fail('provider before cooldown'))
    self.assertEqual(out['state'],'AUDITED');self.assertEqual(store.setting(key)['next_retry'],deadline)
   finally:store.read_cache.close()

if __name__=='__main__':unittest.main()
