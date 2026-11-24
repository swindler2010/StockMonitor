"""A pending repair follow-up must not wait for all-stock market maintenance."""
import json,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import pandas as pd
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
import cp_reconciliation as rv
import revision_watcher as rw
from cp_controls import write,read
from test_cp_tv_legacy_reference import FlatStore

class RepairedScopeWakeup(unittest.TestCase):
 def test_receipt_wakes_owned_lane_once_and_changed_proof_cannot_authorize_repair(self):
  root=Path(__file__).parent.parent.parent/'СМ-Архив';f=tempfile.NamedTemporaryFile(dir=root,prefix='CP repair wake isolated ',suffix='.sqlite3',delete=False);path=Path(f.name);f.close();s=FlatStore(path)
  try:
   info=dict(isin='A',name='Fixture',kind='stock');s.save_instrument(info);now=pd.Timestamp.now(tz='UTC');key=rw.RETIME_PREFIX+'A:receipt'
   receipt=dict(isin='A',job_id='receipt',state='APPLIED',mode='VERIFIED_NATIVE_CLOSING_TICK',created_at=(now-pd.Timedelta(minutes=30)).isoformat())
   with s.connect() as d:
    write(d,key,receipt);write(d,rv.PROGRESS+'A',dict(counts={},last_turn=(now-pd.Timedelta(minutes=20)).isoformat()))
    self.assertIsNone(rv.due_observation(d,[dict(isin='B')],now))
    self.assertEqual(rv.due_observation(d,[info],now),dict(isin='A',job_id='receipt',reason='NATIVE_REPAIR_FOLLOWUP'))
    write(d,rv.PROGRESS+'A',dict(counts={},last_turn=now.isoformat()))
    self.assertIsNone(rv.due_observation(d,[info],now))
    write(d,rv.PROGRESS+'A',dict(counts={},last_turn=(now-pd.Timedelta(minutes=20)).isoformat()))
    with patch('revision_watcher.retime_current',return_value=False),patch.object(rv,'_queue_repaired_scopes') as queue:
     rv._resume_repaired_scopes(d,'A',now);queue.assert_not_called()
    self.assertIsNone(rv.due_observation(d,[info],now+pd.Timedelta(minutes=6)))
    self.assertEqual(read(d,rv.PROGRESS+'A')['last_native_repair_rechecks']['reason'],'REPAIR_PROOF_CHANGED')
    self.assertIsNone(read(d,'cp_manual_risk:A'))
    self.assertEqual(d.execute('select count(*) from series').fetchone()[0],0)
    self.assertEqual(read(d,key),receipt)
  finally:
   s.read_cache.close()
   for suffix in ('','-wal','-shm'):Path(str(path)+suffix).unlink(missing_ok=True)

if __name__=='__main__':unittest.main()
