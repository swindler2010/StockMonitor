"""Exact manual parent corroboration queues source-only recovery; never TV OHLCV import."""
import copy,importlib.util,json,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import pandas as pd
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
import cp_reconciliation as rv,revision_watcher as rw
import test_cp_opening_session_tail as fixtures

class PrimaryWindow(unittest.TestCase):
 fixture=fixtures.OpeningSessionTail.fixture
 clean=staticmethod(fixtures.OpeningSessionTail.clean)

 def test_complete_exact_parent_confirmation_recovers_missing_quantities_through_queue_and_rejects_changed_evidence(self):
  t=fixtures.stamp;first,end=t('08:00'),t('08:15')
  ticks=[(t('08:06'),100.,2.),(t('08:11'),102.,3.),(t('08:12'),101.,5.)]
  native=[(first,99.,99.,99.,99.,1.),(t('08:05'),99.,99.,99.,99.,1.)]
  s,info,m,child,before,official,proof,now=self.fixture(native,ticks,first,end,t('08:10'),t('08:10'),'PRESENT');isin=info['isin']
  spec=rv._spec(isin,m,'15m',first,first);root=Path(__file__).parent.parent.parent/'СМ-Архив'
  with s.connect() as d:
   captured=rv._input(d,spec);source=rw._official_window(d,isin,first,end,now,allow_zero_rows=True)
  packet=dict(records=[dict(spec=spec,native=captured)],native_window=before,native_window_digest=rv.digest(before),source=source,source_digest=rv.digest(source))
  paths=[]
  for suffix,content in (('.json',json.dumps(packet).encode()),('.png',b'fixture screenshot evidence')):
   f=tempfile.NamedTemporaryFile(dir=root,prefix='CP primary isolated ',suffix=suffix,delete=False);f.write(content);f.close();paths.append(Path(f.name));self.addCleanup(paths[-1].unlink,missing_ok=True)
  reference,image=paths;fields=['timestamp','open','high','low','close','volume']
  def confirm(**kw):return rv.confirm_primary_window(s,spec,str(reference),'GETTEX:FIX',[str(image)],kw.get('fields',fields),kw.get('confirmed',True),'Fixture: exact independent parent and all fields visually confirmed')
  with self.assertRaisesRegex(ValueError,'EXACT_PRIMARY'):confirm(confirmed=False)
  with self.assertRaisesRegex(ValueError,'EXACT_PRIMARY'):confirm(fields=fields[:-1])
  with s.connect() as d:
   d.execute("UPDATE series SET volume=volume+1 WHERE base_tf='5m' AND ts=?",(t('08:05'),));d.commit()
  with self.assertRaisesRegex(ValueError,'PRIMARY_NATIVE_INPUT_CHANGED'):confirm()
  with s.connect() as d:d.execute("UPDATE series SET volume=volume-1 WHERE base_tf='5m' AND ts=?",(t('08:05'),))
  receipt=confirm();self.assertEqual(receipt['candles_written'],0)
  with s.connect() as d:
   for b in before:self.assertEqual(rw._native(d,isin,m,b,first,end),before[b])
   j=rv.read(d,rv.key(spec));i=rv._input(d,j);plan=rw.retime_plan(d,j,i,now);self.assertIsNotNone(plan);self.assertEqual(plan['mode'],'VERIFIED_PRIMARY_WINDOW');self.assertTrue(plan['relocations'][0]['superseded'])
   bad=copy.deepcopy(proof);bad['reference']['GETTEX:20261113'].pop('posttrade.20261113.07.15.mund.csv.gz');rv.write(d,'interval_evidence:'+isin,bad)
   with self.assertRaisesRegex(ValueError,'RETIME_'):rw.publish_retime(d,j,i,plan,now)
   d.rollback()
   d.execute("UPDATE series SET high=high+1 WHERE base_tf='15m'")
   with self.assertRaisesRegex(ValueError,'RETIME_'):rw.publish_retime(d,j,i,plan,now)
   d.rollback()
   other=rv.read(d,rv.key(child));rv.write(d,rv.key(child),{**other,'observation':{**other['observation'],'result':'ABSENT'}})
   with self.assertRaisesRegex(ValueError,'RETIME_OTHER_CURRENT_TV'):rw.retime_plan(d,j,rv._input(d,j),now)
   d.rollback()
   rv.write(d,rw.RETIME_POLICY,dict(approved=False,version=rw.RETIME_VERSION));self.assertIsNone(rw.retime_plan(d,j,rv._input(d,j),now));d.rollback()
  image.write_bytes(b'changed screenshot')
  with s.connect() as d:self.assertIsNone(rw.retime_plan(d,j,rv._input(d,j),now))
  image.write_bytes(b'fixture screenshot evidence')
  with patch('gettex_history.attach_known_interruptions',side_effect=AssertionError('No downloads')):
   rv.turn(s,info,now,native_empty_only=True);rv.turn(s,info,now+pd.Timedelta(seconds=1),native_empty_only=True)
  j=s.setting(rv.key(spec));self.assertEqual(j['status'],'RESOLVED_NATIVE');r=s.setting(rw.RETIME_PREFIX+isin+':'+j['id']);self.assertEqual(r['superseded'],1);self.assertEqual(r['relocated'],0)
  with s.connect() as d:
   self.assertTrue(rw.retime_current(d,r))
   for b in before:self.assertEqual(rw._native(d,isin,m,b,first,end),official[b])
  self.assertIsNone(s.setting('cp_manual_risk:'+isin))
  # Explicit rollback helper is exercised only on this isolated fixture.
  loader=importlib.util.spec_from_file_location('primary_rollback',root/'CP native window targeted rollback.py');rollback=importlib.util.module_from_spec(loader);loader.loader.exec_module(rollback)
  with s.connect() as d:
   d.execute('BEGIN IMMEDIATE');d.execute("UPDATE series SET high=high+1 WHERE base_tf='5m'")
   with self.assertRaisesRegex(ValueError,'ROLLBACK_CURRENT_INPUTS_CHANGED'):rollback.undo(d,rw.RETIME_PREFIX+isin+':'+j['id'])
   d.rollback();d.execute('BEGIN IMMEDIATE');rollback.undo(d,rw.RETIME_PREFIX+isin+':'+j['id'])
   for b in before:self.assertEqual(rw._native(d,isin,m,b,first,end),before[b])

if __name__=='__main__':unittest.main()
