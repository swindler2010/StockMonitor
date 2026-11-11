"""New nine-TF admission regressions; isolated fixtures, no orders/network."""
import copy,json,sys,unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch,Mock
import pandas as pd
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
import calculation_window as cw
import test_cp_full_execution as fixture
from performance_cache import capture_closed_inputs

class IndependentEntry(unittest.TestCase):
 def setUp(self):
  fixture.FullTransaction.setUp(self)
  old=self.record['candidates'][0]
  with self.store.connect() as db:
   db.execute("INSERT INTO series(isin,symbol,venue,currency,source,base_tf,ts,open,high,low,close,volume) SELECT isin,symbol,venue,currency,source,'15m',ts,open,high,low,close,volume FROM series WHERE isin=? AND base_tf='5m'",(self.isin,))
  self.record['timeframes']['15m']=self.record['timeframes'].pop('5m')
  old.update(timeframe='15m',required_tf=['15m','4H','1D'],input_binding_version=2)
  specs=copy.deepcopy([s['spec'] for s in old['closed_inputs']['series']])
  specs[0].update(timeframe='15m',base_tf='15m')
  scopes=[k for k in self.record['_input_revisions'] if not k.endswith(':5m')]+['market:'+self.isin+':15m','setting:cp_manual_risk:'+self.isin]
  token=self.store.read_cache.token_for(scopes)
  with self.store.connect() as db:
   db.execute('BEGIN');old['closed_inputs']=capture_closed_inputs(db,specs,token)
  old.update(execution_revisions=token,execution_expires_at=self.exp)
  self.record.update(input_binding_version=2)
  self.record['_input_revisions'].update(token)
  self.original=copy.deepcopy(self.record);self.save()
 tearDown=fixture.FullTransaction.tearDown
 save=fixture.FullTransaction.save
 def check(self,now=None):
  with self.store.connect() as db:
   db.execute('BEGIN IMMEDIATE')
   return cw.execution_eligibility(db,self.info,'15m',self.identity,now or self.now,'BUY')
 def test_unrelated_5m_revision_does_not_revoke_15m(self):
  with self.store.connect() as db:db.execute("UPDATE series SET volume=volume+1 WHERE isin=? AND base_tf='5m'",(self.isin,))
  with self.store.connect() as db:self.assertEqual(cw.eligibility_state(db,self.record,self.now),'STALE')
  self.assertEqual(self.check()['timeframe'],'15m')
 def test_optional_expiry_does_not_extend_entry_expiry(self):
  self.record['expires_at']=(self.now-pd.Timedelta(seconds=1)).isoformat();self.save()
  self.assertEqual(self.check()['timeframe'],'15m')
  with self.assertRaisesRegex(ValueError,'NOT_CURRENT'):self.check(pd.Timestamp(self.exp))
 def test_required_15m_and_1h_revision_and_identity_revoke(self):
  for tf in ('15m','1H','1D'):
   with self.subTest(tf=tf),self.store.connect() as db:
    db.execute('BEGIN IMMEDIATE');db.execute('UPDATE series SET volume=volume+1 WHERE isin=? AND base_tf=?',(self.isin,tf))
    with self.assertRaisesRegex(ValueError,'NOT_CURRENT|INPUT_CHANGED'):cw.execution_eligibility(db,self.info,'15m',self.identity,self.now)
    db.rollback()
  with self.store.connect() as db:
   for field,value in [('venue','FRA'),('currency','USD'),('source','gettex')]:
    with self.subTest(field=field),self.assertRaises(ValueError):cw.execution_eligibility(db,self.info,'15m',dict(self.identity,**{field:value}),self.now)
 def test_derived_15m_retains_real_5m_dependency(self):
  c=self.record['candidates'][0];specs=[copy.deepcopy(s['spec']) for s in c['closed_inputs']['series']];specs[0]['base_tf']='5m'
  token={**c['execution_revisions'],**self.store.read_cache.token_for(['market:'+self.isin+':5m'])}
  with self.store.connect() as db:db.execute('BEGIN');c['closed_inputs']=capture_closed_inputs(db,specs,token)
  c['execution_revisions']=token;self.save()
  with self.store.connect() as db:db.execute("UPDATE series SET close=100.1 WHERE isin=? AND base_tf='5m'",(self.isin,))
  with self.assertRaisesRegex(ValueError,'NOT_CURRENT'):self.check()
 def test_actual_new_entry_bar_revokes_execution_even_if_history_matches(self):
  with self.store.connect() as db:db.execute("UPDATE input_revisions SET revision=revision+1 WHERE scope=?",('market:'+self.isin+':15m',))
  with self.store.connect() as db:self.assertEqual(cw.candidate_state(db,self.record,self.record['candidates'][0],self.now),'CURRENT')
  with self.assertRaisesRegex(ValueError,'INPUT_CHANGED'):self.check()
 def test_quote_age_and_grant_changes_still_block(self):
  self.record['candidates'][0]['execution_expires_at']=self.now.isoformat();self.save()
  with self.assertRaisesRegex(ValueError,'EXECUTION_EXPIRED'):self.check()
  self.record=copy.deepcopy(self.original);self.save();self.store.set_setting('cp_manual_risk:'+self.isin,{'accepted':[]})
  with self.assertRaisesRegex(ValueError,'NOT_CURRENT'):self.check()
 def test_bounded_candidate_uses_same_independent_proof(self):
  c=self.record['candidates'][0];c['context']='bounded_200_exact_reference_v1'
  self.record['benchmark']=self.record['full_reference_benchmark']
  for row in self.record['timeframes'].values():row['analysis_ready']=True
  self.save()
  with self.store.connect() as db:db.execute("UPDATE input_revisions SET revision=revision+1 WHERE scope=?",('market:'+self.isin+':5m',))
  self.assertEqual(self.check()['context'],'bounded_200_exact_reference_v1')
 def test_owner_analysis_context_cannot_be_automatic_permission(self):
  c=self.record['candidates'][0];c['owner_reviewed_analysis']=True
  with patch.object(cw,'build') as build:cw.attach_execution(NS(store=self.store),self.info,self.record,self.now)
  build.assert_not_called();self.assertFalse(c['trade_ready'])
  self.assertIn('OWNER_REVIEWED',c['trade_blockers'][0])
 def test_attach_tracks_execution_only_1h_and_price_deadline(self):
  c=self.record['candidates'][0];c['context']='bounded_200_exact_reference_v1'
  ctx=NS(store=self.store,timeframe='old',user_state=lambda i:{})
  def contract(*args,**kwargs):
   self.store.read_cache.depend('market:'+self.isin+':1H')
   return dict(status='READY',blockers=[],trading_engine={'signal':'BUY'},windows={'15m':dict(identity=self.identity,last_close_at=(self.now-pd.Timedelta(minutes=14,seconds=30)).isoformat())})
  with patch.object(cw,'build',side_effect=contract):cw.attach_execution(ctx,self.info,self.record,self.now)
  self.assertEqual(ctx.timeframe,'old');self.assertTrue(c['trade_ready'])
  self.assertIn('market:'+self.isin+':1H',c['execution_revisions']);self.assertNotIn('market:'+self.isin+':5m',c['execution_revisions'])
  self.assertEqual(pd.Timestamp(c['execution_expires_at']),self.now+pd.Timedelta(seconds=30))

class PinnedSource(unittest.TestCase):
 def call(self,empty15=False):
  from desktop014 import Desktop014
  meta=dict(source='gettex_chart',symbol='AAPL.GTX',venue='GETTEX',currency='EUR',timezone='Europe/Berlin')
  info={'isin':'US0378331005'};frames=[]
  def load(s,i,tf,*args):
   frames.append(tf)
   f=pd.DataFrame() if tf=='15m' and empty15 else pd.DataFrame(dict(open=[100.],high=[101.],low=[99.],close=[100.],volume=[5.]),index=pd.to_datetime(['2026-10-30 10:00']))
   if not f.empty:f.attrs=meta.copy()
   return f
  store=NS(load_official=Mock(side_effect=AssertionError('unselected official read')),setting=lambda k,d=None:d)
  ctx=NS(store=store,user_state=lambda i:{'history_venue_lock':'GETTEX'})
  with patch('gettex_chart.load',side_effect=load),patch('data_quality.audit',return_value={'state':'READY','issues':[]}),patch('desktop014.resample',side_effect=lambda f,*a,**kw:f):
   out=Desktop014._frame_uncached(ctx,info,'15m',required_identity=meta)
  return frames,out
 def test_confirmed_15m_does_not_read_optional_witness_5m(self):
  frames,out=self.call();self.assertEqual(frames,['15m']);self.assertEqual(out.attrs['source'],'gettex_chart')
 def test_missing_15m_keeps_real_5m_fallback(self):
  frames,out=self.call(True);self.assertEqual(frames,['15m','5m']);self.assertFalse(out.empty)

class OwnerProjection(unittest.TestCase):
 def setUp(self):
  from contextlib import nullcontext
  self.now=pd.Timestamp('2026-11-02T00:10Z');self.info={'isin':'US0378331005'}
  self.policy=json.loads((Path(__file__).parent/'history64-evidence/apple-admission30/policy.json').read_text(encoding='utf8'))
  self.policy['analysis_timeframes']=['5m','10m','15m','30m','1H','2H','3H','4H','1D','1W','1M']
  self.ctx=NS(store=NS(setting=lambda k,d=None:{'initialization_policy':self.policy}),timeframe=NS(get=lambda:'5m'))
  self.ctx.trading_analysis=Mock(return_value=dict(status='READY',signal='WAIT',blockers=[],benchmark_context={}))
 def test_projection_uses_existing_core_without_automatic_permission(self):
  with patch.object(cw,'_candidate_proof',return_value={'verified_fixture':True}):
   out=cw._owner_analysis_candidate(self.ctx,self.info,'1M',self.now)
  self.ctx.trading_analysis.assert_called_once_with(self.info,analysis_only=True)
  self.assertTrue(out['analysis_ready']);self.assertFalse(out['trade_ready']);self.assertFalse(out['signal_ready'])
  self.assertEqual(out['identity'],self.policy['identity']);self.assertEqual(out['input_binding_version'],2)
 def test_real_core_blockers_not_overridden(self):
  self.ctx.trading_analysis.return_value=dict(status='DATA BLOCKED',signal='WAIT',blockers=['UNCONFIRMED','INSUFFICIENT_WARMUP'])
  with patch.object(cw,'_candidate_proof',return_value={}):out=cw._owner_analysis_candidate(self.ctx,self.info,'15m',self.now)
  self.assertFalse(out['analysis_ready']);self.assertIn('UNCONFIRMED',out['blockers'])
 def test_no_extension_to_other_stock_or_optional_tf(self):
  for info,tf in [(self.info,'5m'),(self.info,'10m'),({'isin':'DE0007164600'},'15m')]:
   self.assertIsNone(cw._owner_analysis_candidate(self.ctx,info,tf,self.now))
  self.ctx.trading_analysis.assert_not_called()

class ManualDependencies(unittest.TestCase):
 setUpBase=IndependentEntry.setUp
 tearDown=IndependentEntry.tearDown
 save=IndependentEntry.save
 def setUp(self):
  self.setUpBase()
  import cp_controls as cp
  from paper_account import ACCOUNT
  self.allowed=dict(id='old15',kind='archive',tf='15m',identity=self.identity)
  self.optional=dict(id='new5',kind='archive',tf='5m',identity=self.identity)
  self.store.set_setting('cp_manual_risk:'+self.isin,dict(account=ACCOUNT,accepted={'old15':dict(problem=self.allowed,actor='owner')}))
  specs=[s['spec'] for s in self.record['candidates'][0]['closed_inputs']['series']]
  token=self.store.read_cache.token_for(self.record['candidates'][0]['closed_inputs']['revisions'])
  with self.store.connect() as db:db.execute('BEGIN');self.proof=capture_closed_inputs(db,specs,token)
 def gate(self,problems,proof=None):
  import cp_controls as cp
  with self.store.connect() as db,patch.object(cp,'problems',return_value=problems):
   db.execute('BEGIN');return cp.manual_gate(db,self.isin,self.identity,'15m',self.proof if proof is None else proof)
 def test_manual_15m_ignores_only_unrelated_5m_report(self):
  out=self.gate([self.allowed,self.optional]);self.assertEqual(out['problem_ids'],['old15'])
  self.assertEqual(out['required_timeframes'],['15m','1D','1H','4H'])
  self.assertFalse(out['automatic_signal'])
 def test_real_15m_gap_and_unknown_identity_remain_blocked(self):
  for delta in ({'tf':'15m'},{'identity':{}},{'tf':'4H'}):
   with self.subTest(delta=delta),self.assertRaisesRegex(ValueError,'UNACCEPTED'):self.gate([self.allowed,dict(self.optional,**delta)])
 def test_derived_from_5m_must_review_5m_report(self):
  specs=copy.deepcopy([s['spec'] for s in self.proof['series']]);specs[0]['base_tf']='5m'
  with self.store.connect() as db:db.execute('BEGIN');proof=capture_closed_inputs(db,specs,self.proof['revisions'])
  with self.assertRaisesRegex(ValueError,'UNACCEPTED'):self.gate([self.allowed,self.optional],proof)
 def test_forged_incomplete_proof_and_revised_inputs_block(self):
  bad=copy.deepcopy(self.proof);bad['series']=[s for s in bad['series'] if s['spec']['timeframe']!='4H']
  with self.assertRaisesRegex(ValueError,'DEPENDENCY_SCOPE'):self.gate([self.allowed],bad)
  with self.store.connect() as db:db.execute("UPDATE series SET close=100.1 WHERE isin=? AND base_tf='15m'",(self.isin,))
  with self.assertRaisesRegex(ValueError,'DEPENDENCY_CHANGED'):self.gate([self.allowed])
 def test_revoked_owner_permission_still_blocks(self):
  self.store.set_setting('cp_manual_risk:'+self.isin,{'accepted':{}})
  with self.assertRaisesRegex(ValueError,'NOT_GRANTED'):self.gate([self.allowed])

class ReceiptTelemetry(unittest.TestCase):
 setUp=IndependentEntry.setUp
 tearDown=IndependentEntry.tearDown
 save=IndependentEntry.save
 def capture(self):
  self.key='gettex_chart:'+self.isin+':15m';self.bkey='benchmark_history:INDEX:DAX'
  self.value=dict(state='UNCHANGED',identity=self.identity,quality='READY',received_at='old',planned_at='old',requested_to='old',receipt='old.json',sha256='old',listing_proof=dict(self.identity,status='CONFIRMED',verified_at='old',evidence='proof'))
  self.store.set_setting(self.key,self.value);self.store.set_setting(self.bkey,dict(quality='READY',attempted_at='old'))
  scopes=[*self.record['candidates'][0]['closed_inputs']['revisions'],'setting:'+self.key,'setting:'+self.bkey]
  with self.store.connect() as db:db.execute('BEGIN');return capture_closed_inputs(db,[s['spec'] for s in self.record['candidates'][0]['closed_inputs']['series']],self.store.read_cache.token_for(scopes))
 def validate(self,proof):
  from performance_cache import validate_closed_inputs
  with self.store.connect() as db:db.execute('BEGIN');return validate_closed_inputs(db,proof)
 def test_unchanged_candles_survive_new_http_receipt_and_benchmark_poll(self):
  proof=self.capture();v=copy.deepcopy(self.value)
  for k in ('received_at','planned_at','requested_to','receipt','sha256'):v[k]='new'
  v['listing_proof']['verified_at']='new';self.store.set_setting(self.key,v)
  self.store.set_setting(self.bkey,dict(quality='READY',attempted_at='new'))
  self.validate(proof)
 def test_meaningful_identity_dq_proof_and_native_history_still_revoke(self):
  from performance_cache import InputRevisionChanged
  proof=self.capture()
  for delta in ({'quality':'INVALID'},{'identity':dict(self.identity,currency='USD')},{'listing_proof':dict(self.value['listing_proof'],status='UNCONFIRMED')},{'native_monthly':{'receipt_sha256':'changed'}},{'archive_invalid':{'state':'INVALID'}}):
   self.store.set_setting(self.key,{**self.value,**delta})
   with self.subTest(delta=delta),self.assertRaises(InputRevisionChanged):self.validate(proof)
   self.store.set_setting(self.key,self.value)
  self.store.set_setting(self.bkey,dict(quality='GAP',attempted_at='new'))
  with self.assertRaises(InputRevisionChanged):self.validate(proof)
 def test_existing_proofs_rebound_once_without_starving_queue(self):
  other=dict(isin='OTHER',name='Other')
  self.store.set_setting('cp_eligibility:OTHER',dict(self.record,isin='OTHER',checked_at='2026-01-01',candidates=[],closed_input_semantics=2))
  with patch.object(cw,'eligibility_state',return_value='CURRENT'):
   self.assertEqual(cw.eligibility_due(self.store,[other,self.info],self.now),[self.info])
   self.record['closed_input_semantics']=2;self.save()
   self.assertEqual(cw.eligibility_due(self.store,[other,self.info],self.now),[])
 def test_rebind_drains_53_and_200_jobs_without_duplicate_or_starvation(self):
  for size in (53,200):
   with self.subTest(size=size):
    infos=[dict(isin='SYNTHETIC-'+str(i),name=str(i)) for i in range(size)]
    records={i['isin']:dict(input_binding_version=2,checked_at=str(n).zfill(4),candidates=[{'analysis_ready':n==size-1}]) for n,i in enumerate(infos)}
    for isin,value in records.items():self.store.set_setting('cp_eligibility:'+isin,value)
    seen=[]
    with patch.object(cw,'eligibility_state',return_value='CURRENT'):
     for unused in range(size):
      due=cw.eligibility_due(self.store,infos,self.now);self.assertEqual(len(due),1)
      isin=due[0]['isin'];self.assertNotIn(isin,seen);seen.append(isin)
      records[isin]['closed_input_semantics']=2
      self.store.set_setting('cp_eligibility:'+isin,records[isin])
     self.assertEqual(cw.eligibility_due(self.store,infos,self.now),[])
    self.assertEqual(seen[0],infos[-1]['isin'])
    self.assertEqual(seen[1:],[i['isin'] for i in infos[:-1]])

if __name__=='__main__':unittest.main()
