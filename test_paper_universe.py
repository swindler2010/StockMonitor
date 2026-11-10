"""New all-registered-stock contract. Isolated fixture orders only; no network."""
import sys,pathlib,json,copy,tempfile,unittest,sqlite3
from types import SimpleNamespace as NS
from unittest.mock import patch
sys.path.insert(0,str(pathlib.Path(__file__).parent/'stock_monitor'))
import pandas as pd
from contextlib import closing
from storage import Store
import paper_account as paper
import calculation_window as cw
from trading_engine import profile

class Universe(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.s=Store(self.tmp.name);self.addCleanup(self.s.read_cache.close)
  self.info={'isin':'US02079K1079','name':'Alphabet','kind':'stock'};self.s.save_instrument(self.info)
  self.now=pd.Timestamp('2026-11-02T12:01:00Z');self.db=sqlite3.connect(self.s.path);self.addCleanup(self.db.close);paper.initialize(self.db);self.db.commit()
  self.bind(self.info)
 def bind(self,info):
  self.info=info;i=info['isin'];self.ident=dict(source='gettex',symbol=i,venue='GETTEX',currency='EUR',timezone='Europe/Berlin')
  self.token=self.s.read_cache.token_for(['instrument:'+i,'market:'+i+':15m'])
  self.record=dict(context='bounded_200_exact_reference_v1',isin=i,execution_version=1,
    checked_at=self.now.isoformat(),expires_at=(self.now+pd.Timedelta(minutes=1)).isoformat(),
    analysis_scope={'policy':cw.SCOPE_POLICY,'start':'2021-11-02','timezone':'Europe/Berlin'},
    _input_revisions=self.token,benchmark={'state':'OUTPERFORMING','quality':'READY'},
    timeframes={t:dict(analysis_ready=True,identity=self.ident) for t in ('15m',*profile('15m'))},
    candidates=[dict(strategy='CORE:1',timeframe='15m',required_tf=list(dict.fromkeys(('15m',*profile('15m')))),
    analysis_ready=True,signal_ready=True,trade_ready=True,execution_identity=self.ident,result={'signal':'BUY'})])
  self.s.set_setting('cp_eligibility:'+i,self.record)
  smart=cw.execution_eligibility(self.db,info,'15m',self.ident,self.now,'BUY')
  self.result=dict(isin=i,timeframe='15m',identity=self.ident,_input_revisions=self.token,
    data_quality={'state':'READY','identity':self.ident,'checked_at':self.now.isoformat()},
    bars=[dict(open=100.,high=101.,low=99.,close=100.,volume=10.,closed=True,open_at='2026-11-02T11:45:00Z',close_at='2026-11-02T12:00:00Z')],
    execution_context=dict(status='READY',isin=i,checked_at=self.now.isoformat(),trading_engine={'status':'READY'},
      windows={t:dict(status='READY',identity=self.ident) for t in ('15m','1H','4H')},smart_eligibility=smart))
  self.args=dict(request_id='isolated-new-universe',isin=i,side='BUY',quantity=1,timeframe='15m',_deadline=self.now.timestamp()+60)
 def execute(self):
  with self.db:
   self.db.execute('BEGIN IMMEDIATE');return paper.execute(self.db,self.info,self.result,self.args,self.now.timestamp())
 def test_new_stock_executes_once_through_real_executor_and_preserves_account(self):
  old_policy=paper.POLICY.copy();a=self.execute();b=self.execute()
  self.assertEqual(a['status'],'COMPLETED');self.assertTrue(b['idempotent'])
  self.assertEqual(len(paper.ledger(self.db)['fills']),1);self.assertEqual(paper.ledger(self.db)['cash'],'9898.95')
  paper.initialize(self.db);self.assertEqual(paper.ledger(self.db)['cash'],'9898.95');self.assertEqual(paper.POLICY,old_policy)
 def test_future_registration_and_restart_are_automatic(self):
  i={'isin':'US5949181045','name':'Future test registration'}
  self.assertNotIn(i['isin'],paper.ledger(self.db)['allowed_isins']);self.s.save_instrument(i)
  self.bind(i);self.assertEqual(self.execute()['status'],'COMPLETED')
  with closing(sqlite3.connect(self.s.path)) as second:self.assertIn(i['isin'],paper.ledger(second)['allowed_isins'])
  self.assertEqual(paper.ledger(self.db)['universe']['version'],paper.UNIVERSE_VERSION)
 def test_benchmark_and_unregistered_do_not_gain_execution(self):
  self.assertFalse(paper.instrument_allowed({'isin':'US5949181045','kind':'benchmark'}))
  self.db.execute('delete from instruments where isin=?',(self.info['isin'],));self.db.commit()
  self.result['_input_revisions']=self.s.read_cache.token_for(self.token)
  with self.assertRaisesRegex(ValueError,'PAPER_INSTRUMENT_BLOCKED'):self.execute()
  self.assertEqual(paper.ledger(self.db)['fills'],[])
 def test_new_stock_invalid_stale_currency_identity_still_block(self):
  original=copy.deepcopy(self.result)
  cases=[('data_quality','state','UNCONFIRMED','PAPER_DATA_BLOCKED'),('identity','currency','USD','PAPER_EUR'),
    ('identity','venue','FRA','PAPER_IDENTITY'),('data_quality','checked_at','2026-11-01T12:00:00Z','PAPER_STALE_DQ')]
  for obj,key,value,error in cases:
   self.result=copy.deepcopy(original);self.result[obj][key]=value
   with self.assertRaisesRegex(ValueError,error):self.execute()
  self.result=copy.deepcopy(original);self.result['bars'][0]['close_at']='2026-11-02T11:30:00Z'
  with self.assertRaisesRegex(ValueError,'PAPER_STALE_QUOTE'):self.execute()
  self.result=copy.deepcopy(original);self.result['bars'][0]['high']=1
  with self.assertRaisesRegex(ValueError,'PAPER_BAD_OHLCV'):self.execute()
  self.assertEqual(paper.ledger(self.db)['fills'],[])
 def test_new_stock_changed_revisions_and_core_remain_blocking(self):
  self.result['execution_context']['trading_engine']['status']='DATA_BLOCKED'
  with self.assertRaisesRegex(ValueError,'PAPER_ENGINE'):self.execute()
  self.result['execution_context']['trading_engine']['status']='READY';self.s.save_instrument({**self.info,'name':'Changed'})
  with self.assertRaisesRegex(ValueError,'PAPER_STALE_INPUTS'):self.execute()
 def test_existing_real_account_and_fill_are_not_migrated_or_reset(self):
  backup=pathlib.Path(__file__).parent/'history64-evidence/cp-tv-universe02/before.sqlite3'
  with closing(sqlite3.connect(backup.as_uri()+'?mode=ro',uri=True)) as src:
   for table in ('paper_accounts','paper_fills'):
    self.db.execute('delete from '+table)
    for row in src.execute('select * from '+table):self.db.execute('insert into '+table+' values ('+','.join('?' for _ in row)+')',row)
   before={t:self.db.execute('select * from '+t).fetchall() for t in ('paper_accounts','paper_fills')}
  paper.initialize(self.db);state=paper.ledger(self.db)
  self.assertEqual(len(state['fills']),1);self.assertEqual(state['positions'][0]['isin'],'US0378331005');self.assertEqual(state['positions'][0]['quantity'],'1')
  self.assertEqual(before,{t:self.db.execute('select * from '+t).fetchall() for t in before})
 def test_attachment_for_new_stock_uses_existing_risk_gates(self):
  c=dict(timeframe='15m',signal_ready=True,result={'signal':'BUY'})
  contract=dict(status='BLOCKED',blockers=['REAL_GAP'],trading_engine={'signal':'WAIT'},windows={'15m':{'identity':self.ident}})
  with patch.object(cw,'build',return_value=contract) as call:
   cw.attach_execution(NS(timeframe=None,user_state=lambda i:{}),self.info,{'candidates':[c]},self.now)
  self.assertTrue(call.called);self.assertFalse(c['trade_ready']);self.assertIn('REAL_GAP',c['trade_blockers']);self.assertNotIn('PAPER_INSTRUMENT_BLOCKED',c['trade_blockers'])
 def test_no_automatic_historical_risk_created_for_new_stock(self):
  import cp_controls as ctl
  self.assertEqual(ctl.automatic_turn(self.s,self.info,self.now)['state'],'NOT_APPLICABLE')
  self.assertEqual(self.s.setting('cp_manual_risk:'+self.info['isin'],{}),{})
 def test_bridge_new_registered_stock_reaches_existing_job_and_failure_receipt(self):
  from features import Features
  app=NS(watchlist=[self.info],user_dirty=False,job=lambda work,done,failed,**kw:failed('fixture unavailable'))
  with patch('analytical_read.capture',return_value={'watchlist':[self.info]}):
   future=Features.bridge_dispatch(app,'paper_trade',self.args)
  self.assertEqual(future.result(),{'status':'BLOCKED','code':'PAPER_DATA_UNAVAILABLE'})
  self.assertEqual(paper.ledger(self.db)['fills'],[])
 def test_read_analytics_builds_guarded_context_for_new_stock(self):
  import analytical_read as ar
  f=pd.DataFrame(columns=['open','high','low','close','volume'],index=pd.DatetimeIndex([]));f.attrs=self.ident
  ctx=NS(instrument_cfg=lambda:self.info,user_state=lambda i:{},frame_for=lambda i,t:f)
  contract={'status':'BLOCKED','blockers':['NO_CANDLES']}
  with patch.object(ar,'SnapshotContext',return_value=ctx),patch.object(cw,'build',return_value=contract) as build:
   result=ar.calculate(self.s,{'request':dict(isin=self.info['isin'],timeframe='15m')})
  self.assertTrue(build.called);self.assertEqual(result['execution_context'],contract);self.assertEqual(result['status'],'NO_DATA')
 def test_existing_panel_reports_current_and_future_universe_without_ready_claim(self):
  from paper_view import summary
  a=summary(paper.ledger(self.db));self.s.save_instrument({'isin':'US5949181045','name':'new'})
  b=summary(paper.ledger(self.db));self.assertNotEqual(a,b)
  self.assertIn('1',a);self.assertIn('2',b);self.assertNotIn('TRADE READY',b)
 def test_new_stock_mtf_alignment_keeps_exact_entry_identity(self):
  from snapshot_worker import SnapshotContext
  from desktop014 import Desktop014
  ctx=NS(store=self.s,closed_frame=lambda i,f,t:f,frame_for=lambda i,t:None)
  entry=pd.DataFrame();entry.attrs=self.ident;result=pd.DataFrame({'close':[100.]})
  with patch.object(Desktop014,'_frame_uncached',return_value=result) as pin:
   actual=SnapshotContext.frame_for_entry(ctx,{**self.info,'eur_history_auto':True},'1D',entry)
  self.assertIs(actual,result);self.assertEqual(pin.call_args.kwargs['required_identity'],self.ident)
  usd=entry.copy();usd.attrs={**self.ident,'currency':'USD'}
  with patch.object(Desktop014,'_frame_uncached',side_effect=AssertionError('must not align USD')):
   self.assertIsNone(SnapshotContext.frame_for_entry(ctx,{**self.info,'eur_history_auto':True},'1D',usd))

if __name__=='__main__':unittest.main()
