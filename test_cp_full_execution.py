"""Full-reference route only; never opens the user's DB or submits trades."""
import copy,json,sys,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch
import pandas as pd
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
import calculation_window as cw
from storage import Store
from performance_cache import capture_closed_inputs

class FullRoute(unittest.TestCase):
 def test_audited_full_context_uses_its_own_benchmark_for_signal(self):
  now=pd.Timestamp('2026-10-27T10:01Z')
  identity=dict(source='proxy',symbol='SAP.DE',venue='GER',currency='EUR',timezone='Europe/Berlin')
  ctx=NS(user_state=lambda i:{},source_mode=lambda i:'proxy',frame_for=lambda i,t:pd.DataFrame())
  def row(i,f,t,n):
   return dict(timeframe=t,identity=identity,analysis_ready=False,blockers=['RECURSIVE_INITIALIZATION_SENSITIVE'],
    full_reference=dict(analysis_ready=True,features={},context='full_closed_reference_v1'))
  full=dict(context='full_closed_reference_v1',quality='READY',state='OUTPERFORMING')
  with patch.object(cw,'bounded_features',side_effect=row),patch.object(cw,'bounded_benchmark',return_value=full),patch('trading_engine.evaluate',return_value=dict(status='READY',signal='BUY')):
   result=cw.assess_all(ctx,dict(isin='DE0007164600'),now,benchmark=dict(quality='UNCONFIRMED',state='UNAVAILABLE'))
  candidate=next(c for c in result['candidates'] if c['timeframe']=='5m')
  self.assertEqual(candidate['context'],'full_closed_reference_v1')
  self.assertTrue(candidate['analysis_ready']);self.assertTrue(candidate['signal_ready'])
  self.assertFalse(candidate['trade_ready'])
  self.assertEqual(candidate['signal_benchmark']['context'],'full_closed_reference_v1')

class FullTransaction(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.store=Store(self.tmp.name)
  self.now=pd.Timestamp('2026-10-27T10:01Z');self.exp=(self.now+pd.Timedelta(minutes=1)).isoformat()
  self.isin='DE0007164600';self.identity=dict(source='proxy',symbol='SAP.DE',venue='GER',currency='EUR',timezone='Europe/Berlin')
  self.info=dict(isin=self.isin,name='SAP',eur_listings=[dict(self.identity,isin=self.isin,status='CONFIRMED')]);self.store.save_instrument(self.info)
  benchmark=dict(isin='INDEX:DAX',name='DAX',kind='benchmark');self.store.save_instrument(benchmark)
  self.key='cp_eligibility:'+self.isin;self.required=['5m','4H','1D'];specs=[];scopes=['instrument:'+self.isin,'instrument:INDEX:DAX']
  for tf,base in [('5m','5m'),('4H','1H'),('1D','1D')]:
   f=pd.DataFrame(dict(open=100.,high=101.,low=99.,close=100.,volume=100.),index=pd.date_range('2026-10-26',periods=40,freq='5min'))
   f.attrs=dict(self.identity,timeframe=base);self.store.save_series(self.info,f,base)
   specs.append(dict(isin=self.isin,identity=self.identity,base_tf=base,timeframe=tf,through=self.now.isoformat(),**{'from':'2026-10-26'}))
   scopes.append('market:'+self.isin+':'+base)
  self.bmeta=dict(self.identity,symbol='^GDAXI');f.attrs=dict(self.bmeta,timeframe='1D');self.store.save_series(benchmark,f,'1D')
  bspec=dict(isin='INDEX:DAX',identity=self.bmeta,base_tf='1D',timeframe='1D',through=self.now.isoformat(),**{'from':'2026-10-26'})
  specs.append(bspec);scopes+=['market:INDEX:DAX:1D']
  self.q='data_quality:'+self.isin+':SAP.DE:1H';self.store.set_setting(self.q,dict(state='READY',identity=self.identity));scopes+=['setting:'+self.q]
  token=self.store.read_cache.token_for(scopes)
  with self.store.connect() as db:
   db.execute('BEGIN');guard=capture_closed_inputs(db,specs,token)
  market=dict(context='full_closed_reference_v1',quality='READY',state='OUTPERFORMING',benchmark_id='INDEX:DAX',_closed_input=bspec,valid_until=self.exp)
  self.record=dict(context='bounded_200_exact_reference_v1',execution_version=1,isin=self.isin,checked_at=self.now.isoformat(),expires_at=self.exp,
   analysis_scope=dict(policy=cw.SCOPE_POLICY,start='2021-10-27',timezone='Europe/Berlin'),calendar_revision=cw.data_quality.XFRA_SESSION_REVISION,_input_revisions=token,
   benchmark=dict(state='UNAVAILABLE',quality='UNCONFIRMED'),full_reference_benchmark=market,
   timeframes={tf:dict(identity=self.identity,analysis_ready=False,blockers=['RECURSIVE_INITIALIZATION_SENSITIVE'],valid_until=self.exp,
    full_reference=dict(context='full_closed_reference_v1',analysis_ready=True,quality={'state':'READY'},scope_initialization={'stable':True},count=40,last_close_at=(self.now-pd.Timedelta(minutes=1)).isoformat())) for tf in self.required},
   candidates=[dict(context='full_closed_reference_v1',strategy='CORE:1',timeframe='5m',required_tf=self.required,
    analysis_ready=True,signal_ready=True,trade_ready=True,execution_identity=self.identity,result=dict(status='READY',signal='BUY'),
    full_execution_version=1,signal_benchmark=copy.deepcopy(market),closed_inputs=guard,expires_at=self.exp)])
  self.original=copy.deepcopy(self.record);self.save()
 def tearDown(self):self.store.read_cache.close();self.tmp.cleanup()
 def save(self):self.store.set_setting(self.key,self.record)
 def check(self,now=None,side='BUY'):
  with self.store.connect() as db:
   db.execute('BEGIN IMMEDIATE')
   return cw.execution_eligibility(db,self.info,'5m',self.identity,now or self.now,side)
 def test_full_reference_contract_is_separate_and_survives_reopen(self):
  result=self.check();self.assertEqual(result['context'],'full_closed_reference_v1')
  self.assertFalse(self.record['timeframes']['5m']['analysis_ready'])
  self.store.read_cache.close();self.store=Store(self.tmp.name)
  self.assertEqual(self.check(),result)
  with self.store.connect() as db:self.assertEqual(db.execute("select count(*) from sqlite_master where name='paper_fills'").fetchone()[0],0)
 def test_old_analysis_receipt_cannot_be_reused_as_permission(self):
  del self.record['candidates'][0]['full_execution_version'];self.save()
  with self.assertRaisesRegex(ValueError,'PROOF_MISSING'):self.check()
 def test_actual_market_revision_dq_and_identity_revoke(self):
  for command,args in [
   ("UPDATE series SET close=100.5 WHERE isin=? AND base_tf='1H'",(self.isin,)),
   ('UPDATE settings SET value=? WHERE key=?',(json.dumps(dict(state='UNCONFIRMED',identity=self.identity)),self.q)),
   ("UPDATE instruments SET payload=? WHERE isin=?",(json.dumps(dict(self.info,history_symbol='SAP.F')),self.isin))]:
   with self.subTest(command=command),self.store.connect() as db:
    db.execute('BEGIN IMMEDIATE');db.execute(command,args)
    with self.assertRaisesRegex(ValueError,'NOT_CURRENT'):cw.execution_eligibility(db,self.info,'5m',self.identity,self.now)
    db.rollback()
 def test_expiry_scope_and_source_guards(self):
  with self.assertRaisesRegex(ValueError,'NOT_CURRENT'):self.check(now=pd.Timestamp(self.exp))
  for field,value in [('source','gettex'),('currency','USD'),('venue','FRA')]:
   with self.subTest(field=field),self.store.connect() as db:
    with self.assertRaises(ValueError):cw.execution_eligibility(db,self.info,'5m',dict(self.identity,**{field:value}),self.now)
  self.record['analysis_scope']['start']='2021-10-26';self.save()
  with self.assertRaisesRegex(ValueError,'NOT_CURRENT'):self.check()
 def test_benchmark_cannot_be_borrowed_from_bounded_context(self):
  for value in [dict(self.record['full_reference_benchmark'],quality='UNCONFIRMED'),dict(self.record['full_reference_benchmark'],context='bounded_200_exact_reference_v1')]:
   self.record['full_reference_benchmark']=value;self.save()
   with self.assertRaisesRegex(ValueError,'BENCHMARK_BLOCKED'):self.check()
 def test_every_full_dependency_and_warmup_remain_mandatory(self):
  for tf in self.required:
   for field,value in [('quality',{'state':'GAP'}),('scope_initialization',{'stable':False}),('count',34),('analysis_ready',False),('last_close_at',(self.now+pd.Timedelta(minutes=1)).isoformat())]:
    self.record=copy.deepcopy(self.original);self.record['timeframes'][tf]['full_reference'][field]=value;self.save()
    with self.subTest(tf=tf,field=field),self.assertRaises(ValueError):self.check()
  self.record=copy.deepcopy(self.original);self.record['candidates'][0]['closed_inputs']['series']=[];self.save()
  with self.assertRaisesRegex(ValueError,'DEPENDENCY_MISSING'):self.check()
 def test_side_strategy_and_paper_allowlist_unchanged(self):
  with self.assertRaisesRegex(ValueError,'SIGNAL_MISMATCH'):self.check(side='SELL')
  self.record['candidates'][0]['strategy']='CORE:999';self.save()
  with self.assertRaisesRegex(ValueError,'STRATEGY_MISMATCH'):self.check()
  with self.store.connect() as db:
   with self.assertRaisesRegex(ValueError,'POLICY_BLOCKED'):cw.execution_eligibility(db,dict(self.info,isin='OTHER'),'5m',self.identity,self.now)
 def test_attach_still_requires_existing_executor_and_same_signal(self):
  ctx=NS(store=self.store,timeframe='old',user_state=lambda i:{})
  for status,signal,expected in [('READY','BUY',True),('BLOCKED','BUY',False),('READY','SELL',False)]:
   r=copy.deepcopy(self.original);contract=dict(status=status,blockers=[] if status=='READY' else ['1H:WINDOW_UNCONFIRMED'],trading_engine=dict(status='READY',signal=signal),windows={'5m':dict(identity=self.identity)})
   with patch.object(cw,'build',return_value=contract):cw.attach_execution(ctx,self.info,r,self.now)
   self.assertEqual(r['candidates'][0]['trade_ready'],expected);self.assertEqual(ctx.timeframe,'old')
 def test_historical_current_is_not_execution_current(self):
  # Append outside the closed range: historical analysis remains valid, while
  # execution must wait for a fresh assessment of the new material revision.
  with self.store.connect() as db:
   db.execute('BEGIN IMMEDIATE');db.execute("UPDATE input_revisions SET revision=revision+1 WHERE scope=?",('market:'+self.isin+':5m',))
   self.assertEqual(cw.candidate_state(db,self.record,self.record['candidates'][0],self.now),'CURRENT')
   with self.assertRaisesRegex(ValueError,'NOT_CURRENT'):cw.execution_eligibility(db,self.info,'5m',self.identity,self.now)
   db.rollback()
 def test_health_does_not_show_stale_execution_as_ready(self):
  import candlepilot_health as health
  from datetime import datetime
  with self.store.connect() as db:
   db.execute("UPDATE input_revisions SET revision=revision+1 WHERE scope=?",('market:'+self.isin+':5m',))
  with patch.object(health,'datetime',wraps=datetime) as clock:
   clock.now.return_value=self.now.to_pydatetime()
   result=health.collect(self.store.path)
  row=next(r for r in result['eligibility_rows'] if r['isin']==self.isin and r['tf']=='5m')
  self.assertTrue(row['analysis_ready']);self.assertFalse(row['trade_ready'])
  self.assertIn('SMART_ELIGIBILITY_NOT_CURRENT',row['candidates'][0]['trade_blockers'])

class ActualCoreRoute(unittest.TestCase):
 def test_actual_indicators_core_and_existing_execution_gates(self):
  """Controlled OHLCV fixture, not market-data evidence or a paper order."""
  import numpy as np
  import data_quality as dq
  import benchmarks
  from analytics import resample,analyse_multiframe
  from snapshot_worker import Value
  now=pd.Timestamp('2026-10-27T10:01Z');meta=dict(source='proxy',symbol='SAP.DE',venue='GER',currency='EUR',timezone='Europe/Berlin')
  info=dict(isin='DE0007164600',name='SAP',position={'quantity':0},source_mode='proxy',eur_history_auto=True,eur_listings=[dict(meta,isin='DE0007164600',status='CONFIRMED')])
  def candles(tf,n):
   clock=now+pd.Timedelta(minutes=15) if tf=='5m' else now
   idx=dq.expected_index(meta,tf,pd.Timestamp('2023-01-01'),pd.Timestamp('2026-10-27 12:00'),clock)
   if tf=='5m':idx=idx[idx+pd.Timedelta(minutes=5)<=now.tz_convert(meta['timezone']).tz_localize(None)]
   idx=idx[-n:]
   a=np.arange(len(idx));price=100+a*.015+.25*np.sin(a*.15);price[-1]+=1.5
   f=pd.DataFrame(dict(open=price-.1,high=price+.3,low=price-.3,close=price,volume=100.,source='proxy'),index=idx);f.iloc[-1,f.columns.get_loc('volume')]=300
   f.attrs=dict(meta,timeframe=tf);return f
  with tempfile.TemporaryDirectory() as folder:
   store=Store(folder);self.addCleanup(store.read_cache.close);store.save_instrument(info)
   raw={t:candles(t,n) for t,n in [('5m',1000),('1H',6500),('1D',950)]}
   for t,f in raw.items():store.save_series(info,f,t)
   # Actual existing aggregation establishes every 4H parent from its children.
   frames={t:cw.scope_raw(f,t,now) for t,f in raw.items()}
   for t,f in frames.items():f.attrs['data_quality']=dq.audit(f,t,now=now)
   frames['4H']=resample(frames['1H'],'1H','4H',now)
   binfo=benchmarks.CATALOG['INDEX:DAX'];store.save_instrument(binfo)
   bf=raw['1D'].copy();bf.loc[:,'close']=100.;bf.loc[:,'open']=100.;bf.loc[:,'high']=101.;bf.loc[:,'low']=99.
   bf.attrs=dict(benchmarks.identity(binfo),timeframe='1D');store.save_series(binfo,bf,'1D')
   def frame(i,t):
    base=frames.get(t,pd.DataFrame());store.read_cache.depend('market:'+info['isin']+':'+base.attrs.get('aggregation_base',t));return base
   ctx=NS(store=store,timeframe=Value('5m'),five_year_scope=True,user_state=lambda i:dict(i),source_mode=lambda i:'proxy',frame_for=frame)
   try:
    with store.read_cache.track_inputs(isolated=True) as token:
     store.read_cache.depend('instrument:'+info['isin']);result=cw.assess_all(ctx,info,now)
     candidate=next(c for c in result['candidates'] if c['timeframe']=='5m')
     self.assertTrue(candidate['analysis_ready'],candidate)
     self.assertEqual(candidate['context'],'full_closed_reference_v1')
     self.assertFalse(result['timeframes']['5m']['analysis_ready'])
     self.assertTrue(candidate['signal_ready'],candidate)
     ctx.trading_analysis=lambda i:analyse_multiframe({t:frames[t] for t in ('5m','4H','1D')},'5m',info,result['full_reference_benchmark'],now=now)
     cw.attach_execution(ctx,info,result,now)
     self.assertTrue(candidate['trade_ready'],candidate.get('trade_blockers'))
     result['_input_revisions']=dict(token)
    store.set_setting('cp_eligibility:'+info['isin'],result)
    with store.connect() as db:
     db.execute('BEGIN IMMEDIATE');receipt=cw.execution_eligibility(db,info,'5m',meta,now,candidate['result']['signal'])
     self.assertEqual(receipt['context'],'full_closed_reference_v1');db.rollback()
    # No order, and none of the live account/state was opened.
   finally:store.read_cache.close()

if __name__=='__main__':unittest.main()
