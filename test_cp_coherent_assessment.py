"""All-TF calculation reads a consistent WAL state; publication remains strict."""
import copy,json,sqlite3,unittest
from unittest.mock import patch
import test_admission_expiry as fx
import calculation_window as cw
from performance_cache import capture_closed_inputs


class CoherentAssessment(unittest.TestCase):
 def setUp(self):
  self.f=fx.Publication(methodName='runTest');self.f.setUp();self.addCleanup(self.f.tearDown)
  self.store,self.info=self.f.s,self.f.i;self.change=None;self.seen=[]
  self.meta=dict(source='gettex_chart',symbol='TEST.GTX',venue='GETTEX',currency='EUR',timezone='Europe/Berlin')
  with self.store.connect() as db:
   db.execute('PRAGMA journal_mode=WAL')
   db.execute('INSERT INTO series VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',('TEST','gettex_chart','GETTEX','EUR','TEST.GTX','15m','2026-11-10 10:00:00',10,11,9,10,100))
  self.store.set_setting('assessment-test',{'version':1})
 def assess(self,ctx,info,now):
  s=ctx.store;s.read_cache.depend('instrument:TEST');s.read_cache.depend('market:TEST:15m')
  s.setting('assessment-test');s.setting('cp_demo_initialization_policy')
  spec=dict(isin='TEST',identity=self.meta,base_tf='15m',timeframe='1H',through='2026-11-10T09:15Z')
  ctx.closed_specs={'one':spec}
  with s.connect() as db:
   self.seen.append(db.execute('SELECT close FROM series ORDER BY ts').fetchall())
   guard=capture_closed_inputs(db,[spec],s.read_cache.dependencies())
  if self.change:self.change()
  s.read_cache.depend('market:TEST:15m')
  with s.connect() as db:self.seen.append(db.execute('SELECT close FROM series ORDER BY ts').fetchall())
  result=self.f.assessment(ctx,info,now)
  result['candidates']=[dict(timeframe='1H',context='full_closed_reference_v1',input_binding_version=2,
   analysis_ready=True,signal_ready=False,trade_ready=False,closed_inputs=guard,expires_at=(now+fx.pd.Timedelta(minutes=10)).isoformat())]
  return result
 def run_calculation(self):
  with patch.object(cw,'assess_all',side_effect=self.assess),patch.object(cw,'attach_execution'):
   return cw.calculate_eligibility(self.store,self.info)
 def append(self):
  with self.store.connect() as db:
   db.execute("INSERT INTO series SELECT isin,source,venue,currency,symbol,base_tf,'2026-11-10 10:15:00',open,high,low,close,volume FROM series WHERE ts='2026-11-10 10:00:00'")
 def test_append_during_calculation_keeps_coherent_closed_inputs(self):
  self.change=self.append;result=self.run_calculation()
  self.assertNotIn('error',result);self.assertEqual(self.seen,[[ (10.0,) ],[ (10.0,) ]])
  self.assertFalse(result['candidates'][0]['trade_ready'])
  with self.store.connect() as db:
   self.assertEqual(db.execute('SELECT count(*) FROM series').fetchone()[0],2)
   self.assertEqual(cw.candidate_state(db,result,result['candidates'][0],self.f.now),'CURRENT')
  self.assertTrue(self.store.read_cache.current(result['_input_revisions']))
 def test_used_ohlcv_correction_is_rejected_after_coherent_read(self):
  def correct():
   with self.store.connect() as db:db.execute('UPDATE series SET close=10.2')
  self.change=correct;result=self.run_calculation()
  self.assertIn('Closed candle changed',result['error']);self.assertEqual(self.seen[0],self.seen[1])
 def test_identity_settings_and_unowned_dependencies_remain_strict(self):
  for variant in ('identity','setting','unowned'):
   if variant=='identity':self.change=lambda:self.store.save_instrument({**self.info,'name':'changed'})
   elif variant=='setting':self.change=lambda:self.store.set_setting('assessment-test',{'version':2})
   else:
    self.store.read_cache.depend('market:UNKNOWN:5m')
    old_assess=self.assess
    def unowned(ctx,info,now):
     ctx.store.read_cache.depend('market:UNKNOWN:5m')
     return old_assess(ctx,info,now)
    def change():
     with self.store.connect() as db:db.execute("INSERT INTO input_revisions VALUES('market:UNKNOWN:5m',1)")
    self.change=change
   with self.subTest(variant=variant):
    if variant=='unowned':
     with patch.object(cw,'assess_all',side_effect=unowned),patch.object(cw,'attach_execution'):
      result=cw.calculate_eligibility(self.store,self.info)
    else:result=self.run_calculation()
    self.assertIn('error',result)
   self.store.save_instrument(self.info);self.store.set_setting('assessment-test',{'version':1})
 def test_transaction_is_read_only_and_original_store_still_writable(self):
  old=self.assess
  def readonly(ctx,info,now):
   with ctx.store.connect() as db:
    with self.assertRaises(sqlite3.OperationalError):db.execute("INSERT INTO settings VALUES('forbidden','{}')")
   return old(ctx,info,now)
  with patch.object(cw,'assess_all',side_effect=readonly),patch.object(cw,'attach_execution'):
   self.assertNotIn('error',cw.calculate_eligibility(self.store,self.info))
  self.store.set_setting('after',{'writable':True});self.assertTrue(self.store.setting('after')['writable'])
 def test_late_change_after_calculation_still_rejects_publication(self):
  result=self.run_calculation();self.store.save_instrument({**self.info,'name':'later'})
  with self.assertRaisesRegex(ValueError,'ELIGIBILITY_INPUT_CHANGED'):cw.publish_eligibility(self.store,self.info,result)
  self.assertIsNone(self.store.setting('cp_eligibility:TEST'))
 def test_execution_tokens_and_deadlines_are_not_rebased(self):
  def attach(ctx,info,result,now):
   result['candidates'][0].update(execution_revisions=ctx.store.read_cache.dependencies(),execution_expires_at=now.isoformat())
   self.append()
  with patch.object(cw,'assess_all',side_effect=self.assess),patch.object(cw,'attach_execution',side_effect=attach):
   result=cw.calculate_eligibility(self.store,self.info)
  self.assertNotIn('error',result);c=result['candidates'][0]
  self.assertNotEqual(c['execution_revisions']['market:TEST:15m'],result['_input_revisions']['market:TEST:15m'])
  self.assertLess(fx.pd.Timestamp(c['execution_expires_at']),fx.pd.Timestamp.now(tz='UTC'))
 def test_exception_releases_reader_and_never_publishes_ready(self):
  with patch.object(cw,'assess_all',side_effect=ValueError('invalid proof')):
   result=cw.calculate_eligibility(self.store,self.info)
  self.assertIn('invalid proof',result['error']);self.assertNotIn('candidates',result)
  self.store.set_setting('after',1)
  with self.store.connect() as db:self.assertEqual(db.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()[0],0)

 def grant_fixture(self):
  import cp_controls as ctl
  self.grant_key='cp_manual_risk:TEST'
  self.store.set_setting(ctl.TV_POLICY,dict(enabled=True,approved=True,version=ctl.TV_VERSION))
  self.grant_scopes=['market:TEST:15m','instrument:TEST','setting:'+ctl.TV_POLICY]
  with self.store.connect() as db:token=ctl._revisions(db,self.grant_scopes)
  self.grant=dict(accepted={'gap':dict(actor=ctl.TV_ACTOR,input_revisions=token,
   problem=dict(isin='TEST',identity=self.meta,tf='15m',state='UNCONFIRMED',range=['2026-10-28 12:15:00']*2),
   evidence=dict(status='OWNER_ACCEPTED_TV_ABSENCE',native_binding='same-reviewed-data'))})
  self.store.set_setting(self.grant_key,self.grant)
  original=self.assess
  def with_grant(ctx,info,now):
   ctx.store.setting(self.grant_key);ctx.store.setting(ctl.TV_POLICY)
   return original(ctx,info,now)
  self.assess=with_grant
 def renew_grant(self):
  import cp_controls as ctl
  value=copy.deepcopy(self.grant)
  with self.store.connect() as db:value['accepted']['gap']['input_revisions']=ctl._revisions(db,self.grant_scopes)
  self.store.set_setting(self.grant_key,value)
 def test_verified_grant_renewal_uses_closed_proof_and_publishes(self):
  self.grant_fixture()
  def renew():self.append();self.renew_grant()
  self.change=renew;result=self.run_calculation()
  self.assertNotIn('error',result)
  saved=cw.publish_eligibility(self.store,self.info,result)
  self.assertEqual(saved['checked_at'],result['checked_at'])
  with self.store.connect() as db:self.assertEqual(cw.candidate_state(db,saved,saved['candidates'][0],self.f.now),'CURRENT')
 def test_material_grant_change_is_rejected_and_queue_retry_remains_due(self):
  import cp_assessment as queue
  self.grant_fixture()
  item=queue.request(self.store,'TEST')
  changed=copy.deepcopy(self.grant);changed['accepted']['gap']['evidence']['native_binding']='DIFFERENT'
  self.change=lambda:self.store.set_setting(self.grant_key,changed)
  result=self.run_calculation()
  self.assertIn('Closed input changed: setting:'+self.grant_key,result.get('error',''))
  queue.finish(self.store,item,result)
  pending=self.store.setting('cp_assessment_request:TEST')
  self.assertEqual(pending['retry_at']-pending['last_attempt_at'],60)
  with self.store.connect() as db:
   self.assertIsNone(queue.choose(db,pending['retry_at']-1))
   self.assertEqual(queue.choose(db,pending['retry_at'])['isin'],'TEST')
 def test_grant_revoked_after_calculation_fails_final_publication(self):
  self.grant_fixture();result=self.run_calculation();self.assertNotIn('error',result)
  self.store.set_setting(self.grant_key,{'accepted':{}})
  with self.assertRaisesRegex(ValueError,'ELIGIBILITY_INPUT_CHANGED'):
   cw.publish_eligibility(self.store,self.info,result)
  self.assertIsNone(self.store.setting('cp_eligibility:TEST'))
 def test_unvalidated_renewal_stays_blocked(self):
  self.grant_fixture();self.change=self.append
  result=self.run_calculation()
  self.assertIn('Closed input changed: setting:'+self.grant_key,result.get('error',''))
 def test_53_200_dependencies_remain_captured_and_checked(self):
  old=self.assess
  for count in (53,200):
   def many(ctx,info,now):
    for n in range(count):ctx.store.setting('independent:'+str(n))
    return old(ctx,info,now)
   with patch.object(cw,'assess_all',side_effect=many),patch.object(cw,'attach_execution'):
    result=cw.calculate_eligibility(self.store,self.info)
   self.assertNotIn('error',result)
   self.assertTrue(all('setting:independent:'+str(n) in result['_input_revisions'] for n in range(count)))
   self.store.set_setting('independent:'+str(count-1),{'changed':True})
   with self.assertRaisesRegex(ValueError,'ELIGIBILITY_INPUT_CHANGED'):
    cw.publish_eligibility(self.store,self.info,result)


if __name__=='__main__':unittest.main()
