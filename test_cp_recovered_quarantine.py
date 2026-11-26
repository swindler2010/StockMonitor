"""Native DQ repair must retire only its exactly repaired quarantined source rows."""
import copy,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import pandas as pd
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
import data_quality as dq
import gettex_chart as g
import cp_experience
from test_cp_tv_legacy_reference import FlatStore

class RecoveredQuarantine(unittest.TestCase):
 def setUp(self):
  root=Path(__file__).parent.parent.parent/'��-�����'
  f=tempfile.NamedTemporaryFile(dir=root,prefix='CP repaired quarantine ',suffix='.sqlite3',delete=False)
  self.path=Path(f.name);f.close();self.store=FlatStore(self.path);self.addCleanup(self.cleanup)
  self.info=dict(isin='US0378331005',name='Fixture',kind='stock')
  self.store.save_instrument(self.info);self.meta=g.identity(self.info['isin'],'APC.GTX')
  self.now=pd.Timestamp('2026-11-13T12:00Z');self.at='2026-11-13 11:35:00'
  self.key=g.state_key(self.info['isin'],'5m')
  report=dict(state='INVALID',identity=self.meta,base_tf='5m',issues=['����������� OHLC/����/������'],
       ranges=[[self.at,self.at]],repairable=True,rejected_timestamps=[self.at])
  self.saved=dict(state='PARTIAL',quality='INVALID',identity=self.meta,
       listing_proof={**self.meta,'isin':self.info['isin'],'status':'CONFIRMED'},
       archive_invalid=report,invalid_receipts=['immutable-original-receipt.json'])
  self.store.set_setting(self.key,self.saved)
  self.full=pd.DataFrame(dict(open=100.,high=102.,low=99.,close=101.,volume=2.),
       index=pd.date_range('2026-11-13 11:30',periods=3,freq='5min'))
  self.full.attrs={**self.meta,'timeframe':'5m'}
  self.store.save_series(self.info,self.full.drop(pd.Timestamp(self.at)),'5m')

 def cleanup(self):
  self.store.read_cache.close()
  for suffix in ('','-wal','-shm'):Path(str(self.path)+suffix).unlink(missing_ok=True)

 def repair(self,frame=None,meta=None):
  candidate=self.full if frame is None else frame
  with patch.object(cp_experience,'local_recovery',return_value=(None,False)):
   return dq.repair(self.info,self.store,self.meta['symbol'],'5m',self.meta if meta is None else meta,
          lambda *a,**kw:candidate.copy(),now=self.now,max_ranges=1)

 def test_real_source_repair_removes_only_resolved_rejection_and_preserves_raw_receipt(self):
  result=self.repair()
  self.assertEqual(result['recovery_completed_ranges'],[[self.at,self.at]])
  saved=self.store.setting(self.key)
  self.assertNotIn('archive_invalid',saved)
  self.assertEqual(saved['invalid_receipts'],self.saved['invalid_receipts'])
  raw=g._load_native(self.store,self.info,'5m')
  self.assertNotIn('archive_invalid',raw.attrs)
  self.assertIn(pd.Timestamp(self.at),raw.index)
  self.assertEqual(len(raw),3)
  self.assertNotEqual(result['state'],'READY') # The live tail is still stale.

 def test_another_unrecovered_date_stays_invalid(self):
  other='2026-11-13 11:50:00';saved=copy.deepcopy(self.saved)
  saved['archive_invalid']['rejected_timestamps'].append(other)
  saved['archive_invalid']['ranges'].append([other,other]);self.store.set_setting(self.key,saved)
  self.repair();after=self.store.setting(self.key)
  self.assertEqual(after['archive_invalid']['state'],'INVALID')
  self.assertEqual(after['archive_invalid']['rejected_timestamps'],[other])
  self.assertEqual(after['archive_invalid']['ranges'],[[other,other]])

 def test_bad_replacement_cannot_clear_old_rejection(self):
  bad=self.full.copy();bad.loc[pd.Timestamp(self.at),'high']=1.
  self.repair(bad);self.assertEqual(self.store.setting(self.key),self.saved)
  self.assertNotIn(pd.Timestamp(self.at),g._load_native(self.store,self.info,'5m').index)

 def test_wrong_listing_replacement_cannot_clear_old_rejection(self):
  bad=self.full.copy();bad.attrs={**bad.attrs,'currency':'USD'}
  self.repair(bad);self.assertEqual(self.store.setting(self.key),self.saved)

 def test_late_failure_rolls_back_recovered_row_and_quarantine_metadata_together(self):
  original=cp_experience.record
  def record(*args,**kwargs):
   if 'SUCCESS' in args:raise ValueError('FIXTURE_AFTER_VALIDATED_RECOVERY')
   return original(*args,**kwargs)
  with patch.object(cp_experience,'record',side_effect=record):self.repair()
  self.assertEqual(self.store.setting(self.key),self.saved)
  self.assertNotIn(pd.Timestamp(self.at),g._load_native(self.store,self.info,'5m').index)

 def test_ordinary_archive_repair_accepts_full_frame_metadata_without_losing_identity(self):
  meta={**self.meta,'timeframe':'5m','classify_absence':True,'listing_proof':self.saved['listing_proof']}
  before=copy.deepcopy(meta);result=self.repair(meta=meta)
  self.assertEqual(result['recovery_completed_ranges'],[[self.at,self.at]])
  self.assertNotIn('archive_invalid',self.store.setting(self.key))
  self.assertEqual(meta,before)

if __name__=='__main__':unittest.main()
