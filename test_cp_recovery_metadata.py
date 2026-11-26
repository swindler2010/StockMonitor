"""One published assessment must survive repair bookkeeping, never real inputs."""
import copy,unittest
from unittest.mock import patch
import test_admission_expiry as fixture
import calculation_window as cw
from performance_cache import _setting_value,_digest

class RecoveryMetadata(unittest.TestCase):
 def test_published_current_survives_progress_but_rejects_material_change(self):
  f=fixture.Publication(methodName='runTest');f.setUp();self.addCleanup(f.tearDown)
  key='data_quality:TEST:TEST.GTX:15m';scope='setting:'+key
  report=dict(state='UNCONFIRMED',identity=dict(source='gettex_chart',symbol='TEST.GTX',venue='GETTEX',currency='EUR'),
   base_tf='15m',issues=['Unconfirmed interval'],ranges=[['2026-11-12 22:45:00']*2],missing_count=1,
   checked_at=f.now.isoformat(),repairable=True)
  f.s.set_setting(key,report)
  def assess(ctx,info,now):
   ctx.store.setting(key);ctx.closed_specs={};r=f.assessment(ctx,info,now);r['expires_at']=(now+fixture.pd.Timedelta(minutes=5)).isoformat()
   for c in r['candidates']:c.update(analysis_ready=False,signal_ready=False,trade_ready=False)
   return r
  with patch.object(cw,'assess_all',side_effect=assess),patch.object(cw,'attach_execution'):
   result=cw.calculate_eligibility(f.s,f.i)
  self.assertNotIn('error',result)
  saved=cw.publish_eligibility(f.s,f.i,result)
  def state():
   with f.s.connect() as db:return cw.eligibility_state(db,saved,f.now)
  self.assertEqual(state(),'CURRENT');revision=saved['_input_revisions'][scope]
  with f.s.connect() as db:before=_digest(_setting_value(db,key))
  progress=dict(report,checked_at=(f.now+fixture.pd.Timedelta(seconds=5)).isoformat(),
   next_retry=(f.now+fixture.pd.Timedelta(minutes=5)).isoformat(),recovery_cursor='2026-10-28 12:45:00',
   recovery_ranges=[['2026-11-09 16:15:00']*2],recovery_completed_ranges=[],recovery_failed_ranges=[],
   local_recovery_reused=0,recovery_provider_attempted=True,recovery_wait='PROVIDER')
  # The real repair persists plain audit, then progress. Neither changes facts.
  for value in (progress,report,dict(progress,recovery_cursor='2026-10-27 12:45:00')):
   f.s.set_setting(key,value)
   self.assertEqual(state(),'CURRENT')
   self.assertEqual(f.s.setting(key),value) # cooldown and diagnostics still persisted
   with f.s.connect() as db:
    self.assertEqual(db.execute('select revision from input_revisions where scope=?',(scope,)).fetchone()[0],revision)
    self.assertEqual(_digest(_setting_value(db,key)),before)
  self.assertEqual(saved['_input_revisions'],result['_input_revisions'])
  self.assertEqual(saved['expires_at'],result['expires_at'])
  with f.s.connect() as db:self.assertEqual(cw.eligibility_state(db,saved,fixture.pd.Timestamp(saved['expires_at'])),'EXPIRED')
  for field,value in [('state','INVALID'),('ranges',[['2026-11-13 10:00:00']*2]),('stale_count',1),
                      ('identity',dict(report['identity'],currency='USD')),('unknown_quality_fact',True)]:
   with self.subTest(field=field):
    # Get a new ordinary receipt before each real change, never reset counters.
    f.s.set_setting(key,report)
    with patch.object(cw,'assess_all',side_effect=assess),patch.object(cw,'attach_execution'):
     saved=cw.publish_eligibility(f.s,f.i,cw.calculate_eligibility(f.s,f.i))
    f.s.set_setting(key,dict(report,**{field:value}))
    self.assertEqual(state(),'STALE')
    with self.assertRaisesRegex(ValueError,'ELIGIBILITY_INPUT_CHANGED'):cw.publish_eligibility(f.s,f.i,saved)
  self.assertFalse(any(c['trade_ready'] for c in saved['candidates']))

if __name__=='__main__':unittest.main()
