"""Joint disjoint diagnoses and proved neighbour changes, never stale fact reuse."""
import copy,json,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import pandas as pd
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
import cp_reconciliation as rv,revision_watcher as rw
import test_cp_opening_session_tail as fixtures
from test_cp_tv_legacy_reference import FlatStore

class JointReviewContext(unittest.TestCase):
 fixture=fixtures.OpeningSessionTail.fixture
 clean=staticmethod(fixtures.OpeningSessionTail.clean)

 def test_conserved_joint_reviews_keep_exact_disjoint_primary_confirmation_and_final_guards(self):
  t=fixtures.stamp
  ticks=[(t('13:58'),100.,3.),(t('14:10'),99.,0.),(t('14:25'),98.,3.),(t('14:40'),97.,0.),(t('15:06'),96.,103.),(t('15:11'),96.2,10.),(t('15:12'),96.15,3.)]
  native5=[(t('14:00'),100.,100.,100.,100.,3.),(t('14:15'),99.,99.,99.,99.,0.),(t('14:30'),98.,98.,98.,98.,3.),(t('14:40'),97.,97.,97.,97.,0.),(t('15:05'),95.95,95.95,95.95,95.95,103.),(t('15:10'),96.2,96.2,96.2,96.2,13.)]
  s,info,m,_,before,official,proof,now=self.fixture(native5,ticks,t('13:45'),t('15:15'),t('14:05'),t('14:05'),None);isin=info['isin'];root=Path(__file__).parent.parent.parent/'СМ-Архив'
  spec=rv._spec(isin,m,'15m',t('15:00'),t('15:00'))
  with s.connect() as d:
   captured=rv._input(d,spec);source=rw._official_window(d,isin,spec['first'],spec['end'],now,allow_zero_rows=True);native={b:rw._native(d,isin,m,b,spec['first'],spec['end']) for b in before}
  packet=dict(records=[dict(spec=spec,native=captured)],native_window=native,native_window_digest=rv.digest(native),source=source,source_digest=rv.digest(source));paths=[]
  for suffix,data in (('.json',json.dumps(packet).encode()),('.png',b'isolated exact TV parent fixture')):
   f=tempfile.NamedTemporaryFile(dir=root,prefix='CP joint context isolated ',suffix=suffix,delete=False);f.write(data);f.close();paths.append(Path(f.name));self.addCleanup(paths[-1].unlink,missing_ok=True)
  rv.confirm_primary_window(s,spec,paths[0],'GETTEX:FIX',[str(paths[1])],['timestamp','open','high','low','close','volume'],True,'isolated exact primary parent')
  primary=s.setting(rv.key(spec));original_observation=copy.deepcopy(primary['observation']);jobs=[]
  for clock in ('14:15','14:30'):
   js=rv._spec(isin,m,'15m',t(clock),t(clock))
   with s.connect() as d:
    j,_=rv._enqueue(d,js,dict(kind='related_fixture'),now);i=rv._input(d,j);old=dict(j);j.update(input_fingerprint=i['fingerprint'],fingerprint_version=rv.FINGERPRINT_VERSION);rv._save_job(d,j,old)
   rv.observe(s,j['id'],isin,rv.digest(s.setting(rv.key(j))),'DIFFERENT','GETTEX:FIX',True);jobs.append(s.setting(rv.key(j)))
  job=jobs[-1]
  with s.connect() as d:
   i=rv._input(d,job);plan=rw.retime_plan(d,job,i,now);self.assertIsNotNone(plan);self.assertEqual(plan['mode'],'VERIFIED_NATIVE_TRADE_PARTITION');self.assertIn(jobs[0]['id'],plan['co_review_inputs']);self.assertEqual(plan['end'],t('14:45'))
   saved=rv.read(d,rv.key(jobs[0]));rv.write(d,rv.key(saved),{**saved,'observation':{**saved['observation'],'result':'ABSENT'}})
   with self.assertRaisesRegex(ValueError,'RETIME_'):rw.publish_retime(d,job,i,plan,now)
   d.rollback();d.execute("UPDATE series SET volume=volume+1 WHERE base_tf='5m' AND ts=?",(t('14:30'),))
   with self.assertRaisesRegex(ValueError,'RETIME_'):rw.publish_retime(d,job,i,plan,now)
   d.rollback();rv.write(d,rw.RETIME_POLICY,dict(approved=False,version=rw.RETIME_VERSION));self.assertIsNone(rw.retime_plan(d,job,rv._input(d,job),now));d.rollback()
  # Freeze between the two publications to exercise the still-unchanged
  # primary subject. The ordinary turn below completes its queued follow-up.
  with s.connect() as d:
   d.execute('BEGIN IMMEDIATE');rw.publish_retime(d,job,rv._input(d,job),plan,now)
  receipt=s.setting(rw.RETIME_PREFIX+isin+':'+job['id']);self.assertIsNotNone(receipt)
  with s.connect() as d:
   j=rv.read(d,rv.key(spec));i=rv._input(d,j);self.assertEqual(i.get('review_context_fingerprint'),original_observation['input_fingerprint']);self.assertTrue(rv._observation_current(j,i));self.assertIsNotNone(rw.retime_plan(d,j,i,now));self.assertEqual(j['status'],'RECHECK_QUEUED')
   # Neither a fresh timestamp nor a neighbouring receipt excuses any actual
   # change to the subject, neighbour, original policy or primary files.
   for base,ts in [('15m',t('14:30')),('5m',t('15:05'))]:
    d.execute('UPDATE series SET high=high+1 WHERE base_tf=? AND ts=?',(base,ts));changed=rv._input(d,j)
    if base=='15m':self.assertFalse(rv._observation_current(j,changed))
    self.assertIsNone(rw.retime_plan(d,j,changed,now));d.rollback()
   bad=copy.deepcopy(proof);bad['reference']['GETTEX:20261113'].pop(next(iter(bad['reference']['GETTEX:20261113'])));rv.write(d,'interval_evidence:'+isin,bad);self.assertFalse(rv._observation_current(j,rv._input(d,j)));d.rollback()
  with patch('gettex_history.attach_known_interruptions',side_effect=AssertionError('No network')):
   rv.turn(s,info,now+pd.Timedelta(seconds=1),native_empty_only=True);rv.turn(s,info,now+pd.Timedelta(seconds=2),native_empty_only=True)
  self.assertEqual(s.setting(rv.key(spec))['status'],'RESOLVED_NATIVE');self.assertEqual(s.setting(rv.key(spec))['observation'],original_observation)
  final=s.setting(rw.RETIME_PREFIX+isin+':'+spec['id'])
  with s.connect() as d:self.assertTrue(rw.retime_current(d,final));self.assertEqual(rw._native(d,isin,m,'15m',spec['first'],spec['end']),source['rows']['15m'])
  self.assertIsNone(s.setting('cp_manual_risk:'+isin))

 def test_53_and_200_adjacent_difference_followups_are_bounded_and_preserve_other_identity(self):
  root=Path(__file__).parent.parent.parent/'СМ-Архив'
  for count in (53,200):
   with self.subTest(count=count):
    f=tempfile.NamedTemporaryFile(dir=root,prefix='CP joint queue isolated ',suffix='.sqlite3',delete=False);path=Path(f.name);f.close();s=FlatStore(path);self.addCleanup(self.clean,s,path);now=pd.Timestamp.now(tz='UTC');meta=dict(source='gettex_chart',venue='GETTEX',currency='EUR',symbol='FIX.GTX',timezone='Europe/Berlin');old=['2026-11-13 14:30:00',100.,100.,100.,100.,1.]
    receipt=dict(isin='A',identity=meta,job_id='repair',first=old[0],end='2026-11-13 14:45:00',before={'15m':[old]},source=dict(rows={'15m':[(old[0],99.,99.,99.,99.,1.)]}),mode='VERIFIED_NATIVE_TRADE_PARTITION')
    with s.connect() as d:
     for n in range(count):
      ts=str(pd.Timestamp('2026-11-13 15:00')+pd.Timedelta(minutes=15*n));j=rv._spec('A',meta,'15m',ts,ts);j.update(status='AWAITING_SOURCE',input_fingerprint='old',observation=dict(result='DIFFERENT',at=now.isoformat()),native_neighbors=dict(before=old,after=None));rv.write(d,rv.key(j),j)
     other=rv._spec('A',{**meta,'symbol':'OTHER.GTX'},'15m','2026-11-13 15:00:00','2026-11-13 15:00:00');other.update(status='AWAITING_SOURCE',input_fingerprint='old',observation=dict(result='DIFFERENT'),native_neighbors=dict(before=old,after=None));rv.write(d,rv.key(other),other)
     with patch.object(rv,'_input',return_value=dict(fingerprint='changed')):result=rv._queue_repaired_scopes(d,'receipt',receipt,now)
     self.assertEqual(len(result['queued']),16);self.assertFalse(result['complete']);self.assertEqual(rv.read(d,rv.key(other)),other)
     for row, in d.execute("select value from settings where json_extract(value,'$.status')='RECHECK_QUEUED'"):
      j=json.loads(row);self.assertEqual(j['observation']['result'],'DIFFERENT');self.assertEqual(j['input_fingerprint'],'old');self.assertEqual(j['next_check'],now.isoformat())

if __name__=='__main__':unittest.main()
