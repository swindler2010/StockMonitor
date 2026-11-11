import sys,unittest,copy,json,importlib.util
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch,Mock
import pandas as pd
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
import calculation_window as cw
import paper_account as paper
spec=importlib.util.spec_from_file_location('old_fixture',Path(__file__).parent/'history64-evidence/test_paper_five_20oct.py');fixture=importlib.util.module_from_spec(spec);spec.loader.exec_module(fixture)

class NewExecutionContract(unittest.TestCase):
 def setUp(self):
  fixture.PaperTests.setUp(self)
  self.db.execute('CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT)')
  self.args['timeframe']='5m'
  self.record=dict(context='bounded_200_exact_reference_v1',execution_version=1,isin=self.isin,
   checked_at='2026-10-20T08:01:00+00:00',expires_at='2026-10-20T08:02:00+00:00',_input_revisions=dict(self.token),
   benchmark={'state':'OUTPERFORMING','quality':'READY'},
   timeframes={t:dict(analysis_ready=True,identity=dict(self.identity)) for t in ('5m','4H','1D')},
   candidates=[dict(strategy='CORE:1',timeframe='5m',required_tf=['5m','4H','1D'],analysis_ready=True,signal_ready=True,trade_ready=True,execution_identity=dict(self.identity),result={'signal':'BUY'})])
  self.save()
  self.result['execution_context']['smart_eligibility']=self.guard()
 def tearDown(self): self.db.close()
 def save(self):
  self.db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)',('cp_eligibility:'+self.isin,json.dumps(self.record)));self.db.commit()
 def guard(self,**kwargs):return cw.execution_eligibility(self.db,self.info,'5m',self.identity,pd.Timestamp(self.now,unit='s',tz='UTC'),**kwargs)
 def trade(self):return fixture.PaperTests.trade(self)
 def test_executor_uses_new_contract_and_audits_it(self):
  r=self.trade();self.assertEqual(r['fill']['smart_eligibility']['strategy'],'CORE:1')
  self.assertTrue(self.trade()['idempotent'])
 def test_missing_receipt_cannot_execute(self):
  self.db.execute('DELETE FROM settings');self.db.commit()
  with self.assertRaisesRegex(ValueError,'NOT_CURRENT'):self.trade()
  self.assertEqual(paper.ledger(self.db)['fills'],[])
 def test_benchmark_revision_revokes_in_same_transaction(self):
  self.record['_input_revisions']['market:INDEX:DAX:1D']=0;self.save()
  self.db.execute("INSERT INTO input_revisions VALUES ('market:INDEX:DAX:1D',1)");self.db.commit()
  with self.assertRaisesRegex(ValueError,'NOT_CURRENT'):self.trade()
 def test_expired_contract_revoked(self):
  self.record['expires_at']='2026-10-20T08:00:59Z';self.save()
  with self.assertRaisesRegex(ValueError,'NOT_CURRENT'):self.trade()
 def test_wrong_strategy_tf_identity_signal_and_unknown_benchmark(self):
  original=copy.deepcopy(self.record)
  mutations=[('strategy','CORE:999'),('required_tf',['5m']),('execution_identity',{**self.identity,'currency':'USD'}),('result',{'signal':'SELL'})]
  for k,v in mutations:
   self.record=copy.deepcopy(original);self.record['candidates'][0][k]=v;self.save()
   with self.assertRaises(ValueError):self.trade()
  self.record=copy.deepcopy(original);self.record['benchmark']['quality']='UNCONFIRMED';self.save()
  with self.assertRaisesRegex(ValueError,'BLOCKED'):self.trade()
 def test_changed_receipt_not_silently_substituted(self):
  self.record['checked_at']='2026-10-20T08:00:59Z';self.save()
  with self.assertRaisesRegex(ValueError,'CONTEXT_CHANGED'):self.trade()
 def test_old_schema_is_not_execution_permission(self):
  del self.record['execution_version'];self.save()
  with self.assertRaisesRegex(ValueError,'NOT_CURRENT'):self.guard()

class BenchmarkIntegration(unittest.TestCase):
 def setUp(self):
  self.now=pd.Timestamp('2026-10-23T21:30Z');self.info={'isin':'DE0007236101','history_symbol':'SIE.DE'}
  self.f=pd.DataFrame({'close':range(1,211)},index=pd.bdate_range('2026-01-01',periods=210));self.f.attrs={'timeframe':'1D','timezone':'Europe/Berlin','venue':'GER'}
  self.ctx=NS(store=NS(read_cache=NS(depend=Mock())))
 def call(self,markets):
  with patch('benchmarks.load',return_value=self.f),patch('calculation_window.bar_ends',return_value=pd.date_range('2025-01-01',periods=210,tz='UTC')),patch('benchmarks.relative_performance',side_effect=markets) as calc:
   out=cw.bounded_benchmark(self.ctx,self.info,{},self.f,self.now)
   self.assertEqual(len(calc.call_args_list[0].args[0]),200);self.assertEqual(len(calc.call_args_list[1].args[0]),210)
   self.ctx.store.read_cache.depend.assert_called_with('instrument:INDEX:DAX')
   return out
 def test_actual_market_states_accepted_without_rewriting_core(self):
  r=self.call([{'state':'OUTPERFORMING','quality':'READY'}]*2);self.assertTrue(cw.benchmark_ready(r))
 def test_window_changes_vote_blocks(self):
  r=self.call([{'state':'OUTPERFORMING','quality':'READY'},{'state':'UNDERPERFORMING','quality':'READY'}]);self.assertFalse(cw.benchmark_ready(r));self.assertEqual(r['reason'],'BENCHMARK_WINDOW_VOTE_CHANGED')
 def test_unproven_reference_blocks(self):
  r=self.call([{'state':'NEUTRAL','quality':'READY'},{'state':'UNAVAILABLE'}]);self.assertFalse(cw.benchmark_ready(r))
 def test_attach_preserves_full_execution_gate_and_tf(self):
  ctx=NS(timeframe='old',user_state=lambda i:{})
  c={'timeframe':'15m','signal_ready':True,'result':{'signal':'BUY'}};r={'candidates':[c]}
  contract={'status':'BLOCKED','blockers':['WINDOW_GAP'],'trading_engine':{'signal':'BUY'},'windows':{'15m':{'identity':{}}}}
  with patch('calculation_window.build',return_value=contract) as build:
   cw.attach_execution(ctx,self.info,r,self.now)
   self.assertFalse(c['trade_ready']);self.assertIn('WINDOW_GAP',c['trade_blockers']);self.assertFalse(build.call_args.kwargs['require_smart'])
  self.assertEqual(ctx.timeframe,'old')
 def test_matching_existing_execution_contract_allows_candidate(self):
  identity={'source':'gettex','venue':'GETTEX','currency':'EUR'}
  c={'timeframe':'5m','signal_ready':True,'result':{'signal':'BUY'}}
  with patch('calculation_window.build',return_value={'status':'READY','blockers':[],'trading_engine':{'signal':'BUY'},'windows':{'5m':{'identity':identity}}}):
   cw.attach_execution(NS(timeframe=None,user_state=lambda i:{}),self.info,{'candidates':[c]},self.now)
  self.assertTrue(c['trade_ready']);self.assertEqual(c['execution_identity'],identity)
 def test_full_and_bounded_signal_disagreement_blocks(self):
  c={'timeframe':'5m','signal_ready':True,'result':{'signal':'BUY'}}
  with patch('calculation_window.build',return_value={'status':'READY','blockers':[],'trading_engine':{'signal':'SELL'},'windows':{'5m':{'identity':{}}}}):
   cw.attach_execution(NS(timeframe=None,user_state=lambda i:{}),self.info,{'candidates':[c]},self.now)
  self.assertFalse(c['trade_ready']);self.assertIn('FULL_BOUNDED_SIGNAL_MISMATCH',c['trade_blockers'])

class AutomaticWiring(unittest.TestCase):
 def test_runtime_assessment_invokes_benchmark_and_accepts_existing_states(self):
  identity=dict(source='gettex',symbol='TEST',venue='GETTEX',currency='EUR',timezone='Europe/Berlin')
  ctx=NS(user_state=lambda i:{},source_mode=lambda i:'gettex',frame_for=lambda i,t:pd.DataFrame())
  market={'state':'OUTPERFORMING','quality':'READY'}
  def row(i,f,t,n):return dict(timeframe=t,analysis_ready=True,identity=identity,features={},blockers=[])
  with patch('calculation_window.bounded_features',side_effect=row),patch('calculation_window.bounded_benchmark',return_value=market) as bm,patch('trading_engine.evaluate',return_value={'status':'READY','signal':'BUY'}):
   r=cw.assess_all(ctx,{'isin':'TEST'},pd.Timestamp.now(tz='UTC'))
  bm.assert_called_once();self.assertTrue(all(c['signal_ready'] for c in r['candidates']))
  self.assertTrue(all(not c['trade_ready'] for c in r['candidates']))
 def test_unproven_recursive_daily_window_cannot_gain_benchmark_signal(self):
  ctx=NS(user_state=lambda i:{},source_mode=lambda i:'gettex',frame_for=lambda i,t:pd.DataFrame())
  with patch('calculation_window.bounded_features',return_value={'analysis_ready':False,'blockers':['INDICATOR_WARMUP_OR_OHLCV']}),patch('calculation_window.bounded_benchmark',side_effect=AssertionError('unproven')):
   r=cw.assess_all(ctx,{'isin':'TEST'},pd.Timestamp.now(tz='UTC'))
  self.assertFalse(any(c['signal_ready'] for c in r['candidates']))
 def test_automatic_turn_persists_new_contract_without_network(self):
  import tempfile
  from storage import Store
  with tempfile.TemporaryDirectory() as d:
   store=Store(d);info={'isin':'TEST','name':'Test'};store.save_instrument(info)
   def assessment(c,i,n):return dict(isin=i['isin'],checked_at=n.isoformat(),expires_at=(n+pd.Timedelta(minutes=1)).isoformat(),timeframes={},candidates=[])
   with patch('calculation_window.assess_all',side_effect=assessment):r=cw.eligibility_turn(store,info)
   self.assertNotIn('error',r);self.assertEqual(r['execution_version'],1)
   with store.connect() as db:self.assertEqual(cw.eligibility_state(db,r,pd.Timestamp.now(tz='UTC')),'CURRENT')
   store.read_cache.close()

if __name__=='__main__':unittest.main()
