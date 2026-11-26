"""Previously reviewed zero-timestamp pattern, independently proved without TV."""
import json,sys,tempfile,unittest,copy
from pathlib import Path
from unittest.mock import patch
import pandas as pd
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
import cp_reconciliation as rv,revision_watcher as rw
import test_cp_opening_session_tail as fixtures
from test_cp_tv_legacy_reference import FlatStore

class AutomaticZero(unittest.TestCase):
 fixture=fixtures.OpeningSessionTail.fixture
 clean=staticmethod(fixtures.OpeningSessionTail.clean)
 def test_normal_due_queue_proves_zero_shift_without_any_external_observation(self):
  t=fixtures.stamp;ticks=[(t('07:31'),100.,3.),(t('07:42'),101.,0.),(t('08:01'),102.,5.)]
  native=[(t('07:30'),100.,100.,100.,100.,3.),(t('07:45'),101.,101.,101.,101.,0.),(t('08:00'),102.,102.,102.,102.,5.)]
  s,info,m,j,before,official,proof,now=self.fixture(native,ticks,t('07:30'),t('08:15'),t('07:50'),t('07:55'),None);isin=info['isin']
  with s.connect() as d:
   j=rv.read(d,rv.key(j));j.update(status='AWAITING_SOURCE',finding=dict(state='SOURCE_TIMEBASE_UNCONFIRMED'),native_empty_algorithm=99,next_check=(now-pd.Timedelta(minutes=1)).isoformat());rv.write(d,rv.key(j),j);d.commit()
   self.assertFalse(j.get('observation'));i=rv._input(d,j);p=rw.retime_plan(d,j,i,now);self.assertIsNotNone(p);self.assertTrue(p['automatic_zero_timebase']);self.assertEqual(p['zero_timestamp_mapping'],{t('07:45'):t('07:40')})
   d.execute("UPDATE series SET volume=volume+1 WHERE base_tf='5m' AND ts=?",(t('07:30'),));self.assertIsNone(rw.retime_plan(d,j,rv._input(d,j),now));d.rollback()
   d.execute("UPDATE series SET high=high+1 WHERE base_tf='15m' AND ts=?",(t('07:45'),));self.assertIsNone(rw.retime_plan(d,j,rv._input(d,j),now));d.rollback()
   other,_=rv._enqueue(d,rv._spec(isin,m,'5m',t('07:40'),t('07:40')),dict(kind='conflict'),now);bound=rv._input(d,other);other['observation']=dict(result='ABSENT',identity_confirmed=True,at=now.isoformat(),input_fingerprint=bound['fingerprint'],fingerprint_version=rv.FINGERPRINT_VERSION);rv.write(d,rv.key(other),other);self.assertIsNone(rw.retime_plan(d,j,rv._input(d,j),now));d.rollback()
   bad=copy.deepcopy(proof);bad['reference']['GETTEX:20261113'].pop('posttrade.20261113.07.00.mund.csv.gz');rv.write(d,'interval_evidence:'+isin,bad)
   with self.assertRaisesRegex(ValueError,'RETIME_INPUT_CHANGED'):rw.publish_retime(d,j,i,p,now)
   d.rollback()
  with patch('gettex_history.attach_known_interruptions',side_effect=AssertionError('No download')):rv.turn(s,info,now,native_empty_only=True)
  current=s.setting(rv.key(j));self.assertEqual(current['status'],'RESOLVED_NATIVE');self.assertFalse(current.get('observation'));self.assertEqual(current['automatic_zero_algorithm'],1)
  r=s.setting(rw.RETIME_PREFIX+isin+':'+j['id'])
  with s.connect() as d:
   self.assertTrue(rw.retime_current(d,r))
   for b in before:self.assertEqual(rw._native(d,isin,m,b,r['first'],r['end']),[row for row in official[b] if r['first']<=row[0]<r['end']])
   self.assertFalse(rv._native_empty_pending(d,isin,8))
  self.assertIsNone(s.setting('cp_manual_risk:'+isin))

 def test_53_and_200_automatic_jobs_get_one_due_pass_without_renewing_deadlines(self):
  root=Path(__file__).parent.parent.parent/'СМ-Архив'
  for count in (53,200):
   with self.subTest(count=count):
    f=tempfile.NamedTemporaryFile(dir=root,prefix='CP automatic zero isolated ',suffix='.sqlite3',delete=False);p=Path(f.name);f.close();s=FlatStore(p);self.addCleanup(self.clean,s,p);now=pd.Timestamp.now(tz='UTC');m=dict(source='gettex_chart',symbol='FIX.GTX',venue='GETTEX',currency='EUR',timezone='Europe/Berlin');isin='US0378331005'
    with s.connect() as d:
     for n in range(count):
      at=str(pd.Timestamp(fixtures.stamp('08:00'))+pd.Timedelta(minutes=n*5));j=rv._spec(isin,m,'5m',at,at);j.update(status='RESOLVED_NATIVE' if n%4==2 else 'AWAITING_SOURCE',created_at=(now-pd.Timedelta(days=1)).isoformat(),next_check=(now+pd.Timedelta(hours=1) if n%4==1 else now-pd.Timedelta(minutes=1)).isoformat(),native_empty_algorithm=99,automatic_zero_algorithm=1 if n%4==3 else 0,finding=dict(state='SOURCE_TIMEBASE_UNCONFIRMED'));rv.write(d,rv.key(j),j)
     page=rv._native_empty_pending(d,isin,8);self.assertEqual(len(page),8)
     for key,raw in page:
      j=json.loads(raw);self.assertFalse(j.get('observation'));self.assertEqual(j['automatic_zero_algorithm'],0);self.assertLess(pd.Timestamp(j['next_check']),now);rv.write(d,key,{**j,'automatic_zero_algorithm':1})
     self.assertFalse({k for k,v in page}&{k for k,v in rv._native_empty_pending(d,isin,8)})
     for key,raw in page:self.assertEqual(rv.read(d,key)['next_check'],json.loads(raw)['next_check'])
     self.assertEqual(d.execute('select count(*) from candles').fetchone()[0],0)

if __name__=='__main__':unittest.main()
