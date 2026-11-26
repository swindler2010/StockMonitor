import copy,json,sys,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch
import pandas as pd
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
from storage import Store
from performance_cache import frame_dependency,capture_closed_inputs,validate_closed_inputs,InputRevisionChanged,frozen_store
from snapshot_worker import AnalysisCycle,SnapshotContext,Value,analysis_inputs,run_cycle
from calculation_window import candidate_state,execution_eligibility
import data_quality as dq

class ClosedInputs(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.store=Store(self.tmp.name)
  self.info=dict(isin='TEST',name='test');self.store.save_watchlist([self.info])
  self.now=pd.Timestamp('2026-10-26T20:30Z')
  self.meta=dict(source='proxy',symbol='TEST.F',venue='FRA',currency='EUR',timezone='Europe/Berlin')
  self.frame=pd.DataFrame(dict(open=[10.,11.,12.],high=[11.,12.,13.],low=[9.,10.,11.],close=[10.,11.,12.],volume=[1.,2.,3.]),index=pd.to_datetime(['2026-10-22','2026-10-23','2026-10-26']))
  self.frame.attrs={**self.meta,'timeframe':'1D'};self.store.save_series(self.info,self.frame,'1D')
  self.q='data_quality:TEST:TEST.F:1D';self.store.set_setting(self.q,dict(state='READY',identity=self.meta,checked_at='old'))
  self.store.set_setting('data_quality:TEST:TEST:15m',dict(state='GAP'))
  self.spec=frame_dependency('TEST',self.frame,'1D',self.now)
  scopes=['instrument:TEST','market:TEST:1D','market:TEST:15m','setting:'+self.q,'setting:data_quality:TEST:TEST:15m']
  self.token=self.store.read_cache.token_for(scopes)
  with self.store.connect() as db:self.guard=capture_closed_inputs(db,[self.spec],self.token)
 def tearDown(self):self.store.read_cache.close();self.tmp.cleanup()
 def check(self):
  with self.store.connect() as db:
   db.execute('BEGIN');return validate_closed_inputs(db,self.guard)
 def test_append_forming_and_other_tf_do_not_revoke_closed_reference(self):
  new=self.frame.copy();new.iloc[-1,new.columns.get_loc('close')]=12.5
  self.store.save_series(self.info,new,'1D')
  later=new.tail(1).copy();later.index=pd.to_datetime(['2026-10-27']);self.store.save_series(self.info,later,'1D')
  self.store.set_setting('data_quality:TEST:TEST:15m',dict(state='INVALID'))
  self.assertNotEqual(self.check()['market:TEST:1D'],self.token['market:TEST:1D'])
  self.assertEqual(self.guard['series'][0]['count'],2)
 def test_correction_deletion_and_late_insert_are_material(self):
  for action in ('correction','deletion','insert'):
   with self.subTest(action=action),self.store.connect() as db:
    db.execute('BEGIN')
    if action=='correction':db.execute("UPDATE series SET close=99 WHERE ts='2026-10-23 00:00:00'")
    elif action=='deletion':db.execute("DELETE FROM series WHERE ts='2026-10-23 00:00:00'")
    else:db.execute("INSERT INTO series SELECT isin,source,venue,currency,symbol,base_tf,'2026-10-21 00:00:00',open,high,low,close,volume FROM series LIMIT 1")
    with self.assertRaises(InputRevisionChanged):validate_closed_inputs(db,self.guard)
    db.rollback()
 def test_identity_and_quality_revoke_but_check_timestamp_does_not(self):
  self.store.set_setting(self.q,dict(state='READY',identity=self.meta,checked_at='new'));self.check()
  for state in ('INVALID','GAP','UNCONFIRMED','SOURCE_UNAVAILABLE','RECOVERING'):
   with self.subTest(state=state),self.store.connect() as db:
    db.execute('BEGIN');db.execute('UPDATE settings SET value=? WHERE key=?',(json.dumps(dict(state=state,identity=self.meta)),self.q))
    with self.assertRaises(InputRevisionChanged):validate_closed_inputs(db,self.guard)
    db.rollback()
  for column,value in [('venue','GER'),('currency','USD'),('source','unknown'),('symbol','OTHER.F')]:
   with self.subTest(column=column),self.store.connect() as db:
    db.execute('BEGIN');db.execute('UPDATE series SET '+column+'=?',(value,))
    with self.assertRaises(InputRevisionChanged):validate_closed_inputs(db,self.guard)
    db.rollback()
  self.store.save_instrument(dict(self.info,position={'quantity':1}))
  with self.assertRaises(InputRevisionChanged):self.check()
 def test_frozen_reads_allow_live_writer_and_remain_coherent(self):
  with frozen_store(self.store) as snapshot:
   before=snapshot.load_series('TEST','1D','proxy','TEST.F','FRA','EUR')
   modified=self.frame.copy();modified.loc[modified.index[0],'close']=10.5;self.store.save_series(self.info,modified,'1D')
   after=snapshot.load_series('TEST','1D','proxy','TEST.F','FRA','EUR')
   pd.testing.assert_frame_equal(before,after)
  self.assertEqual(self.store.load_series('TEST','1D','proxy','TEST.F','FRA','EUR').close.iloc[0],10.5)
 def test_candidate_scope_expiry_and_execution_stay_separate(self):
  rec=dict(isin='TEST',execution_version=1,calendar_revision=dq.XFRA_SESSION_REVISION,_input_revisions=self.token,
           analysis_scope={'policy':'rolling_five_calendar_years_v1','start':'2021-10-26','timezone':'Europe/Berlin'})
  c=dict(context='full_closed_reference_v1',closed_inputs=self.guard,expires_at='2026-10-26T21:00Z')
  self.store.set_setting('data_quality:TEST:TEST:15m',{'state':'INVALID'})
  with self.store.connect() as db:
   self.assertEqual(candidate_state(db,rec,c,self.now),'CURRENT')
   self.assertEqual(candidate_state(db,rec,c,pd.Timestamp('2026-10-26T21:00Z')),'EXPIRED')
   bad=copy.deepcopy(c);bad['closed_inputs']['revisions'].pop('instrument:TEST')
   self.assertEqual(candidate_state(db,rec,bad,self.now),'STALE')
  from paper_account import ALLOWED
  isin=next(iter(ALLOWED));record=copy.deepcopy(rec);record.update(isin=isin,reference_binding_version=1,expires_at='2026-10-27T00:00Z',candidates=[dict(c,timeframe='5m',analysis_ready=True,signal_ready=True,trade_ready=True)])
  self.store.save_instrument(dict(isin=isin,name='allowed'));record['_input_revisions']=self.store.read_cache.token_for(['instrument:'+isin])
  self.store.set_setting('cp_eligibility:'+isin,record)
  with self.store.connect() as db,self.assertRaises(ValueError):execution_eligibility(db,{'isin':isin},'5m',self.meta,self.now)
 def test_closed_frame_uses_fixed_capture_time(self):
  ctx=SnapshotContext();ctx.analysis_at=self.now;ctx.closed_specs={}
  out=ctx.closed_frame(self.info,self.frame,'1D')
  self.assertEqual(len(out),2);self.assertEqual(len(ctx.closed_specs),1)
  self.assertEqual(len(self.frame),3)
 def test_serialized_proof_and_checkpoint_survive_reopen(self):
  self.store.set_setting('test_proof',self.guard)
  store2=Store(self.tmp.name)
  try:
   with store2.connect() as db:validate_closed_inputs(db,store2.setting('test_proof'))
   class Client:
    def calculate(self,*args):return {'analysis_snapshot':{}}
   run_cycle(Client(),store2,{},None);run_cycle(Client(),store2,{},None)
   self.assertEqual(store2.setting('analysis_cycle')['sequence'],2)
   self.assertEqual(store2.setting('analysis_cycle')['state'],'COMPUTED')
  finally:store2.read_cache.close()
 def test_health_keeps_historical_analysis_separate_and_revokes_correction(self):
  from candlepilot_health import collect
  snap=dict(ready=True,_input_revisions=self.token,_closed_inputs=self.guard,
    analysis_snapshot=dict(captured_at=self.now.isoformat()),stocks=[dict(isin='TEST',timeframe='1D',trading_engine={'status':'READY','signal':'BUY'})])
  current=collect(self.store.path,snap)['core']['TEST']
  self.assertEqual(current['state'],'HISTORICAL');self.assertFalse(current['execution_permission'])
  new=self.frame.copy();new.loc[new.index[1],'close']=99;self.store.save_series(self.info,new,'1D')
  stale=collect(self.store.path,snap)['core']['TEST']
  self.assertEqual(stale['state'],'STALE');self.assertFalse(stale['closed_inputs_unchanged'])
 def test_one_changed_instrument_does_not_discard_other_instruments(self):
  from snapshot_worker import validate_snapshot
  other=dict(isin='OTHER',name='other');self.store.save_instrument(other);self.store.save_series(other,self.frame,'1D')
  spec=frame_dependency('OTHER',self.frame,'1D',self.now)
  with self.store.connect() as db:other_guard=capture_closed_inputs(db,[spec],self.store.read_cache.token_for(['instrument:OTHER','market:OTHER:1D']))
  good=dict(isin='OTHER',name='other',timeframe='1D',candles=[{'close':10}],_closed_inputs=other_guard,trading_engine={'status':'READY','signal':'BUY'})
  bad=dict(isin='TEST',name='test',timeframe='1D',candles=[{'close':10}],_closed_inputs=self.guard,trading_engine={'status':'READY','signal':'BUY'})
  self.store.set_setting(self.q,dict(state='INVALID',identity=self.meta))
  state=dict(stocks=[bad,good],analysis_snapshot={},_calculation_timing={},selected={'isin':'TEST'})
  result=validate_snapshot(self.store,state)
  self.assertEqual(result['stocks'][0]['trading_engine']['signal'],'WAIT');self.assertEqual(result['stocks'][0]['candles'],[])
  self.assertEqual(result['stocks'][1],good);self.assertEqual(result['analysis_snapshot']['validated_instruments'],1)
  with self.store.connect() as db:validate_closed_inputs(db,result['_closed_inputs'])

class Scheduling(unittest.TestCase):
 def test_view_change_retains_historical_scope_and_does_not_relabel_market(self):
  from snapshot_worker import bind_view_selection
  old={'selected':{'isin':'A','timeframe':'1W'},'analysis_snapshot':{'execution_permission':False},'market_context':{'benchmark_id':'DAX'}}
  new=bind_view_selection(old,{'isin':'B','timeframe':'5m'})
  self.assertEqual(new['analysis_selection'],old['selected']);self.assertEqual(new['market_context']['state'],'UNAVAILABLE')
  self.assertEqual(bind_view_selection(new,old['selected'])['market_context'],old['market_context'])
  self.assertEqual(old['selected']['isin'],'A')
 def test_ten_minute_nonoverlap_one_overdue_and_no_backlog(self):
  c=AnalysisCycle();self.assertTrue(c.due(0))
  for t in range(1,2000):self.assertFalse(c.due(t))
  self.assertTrue(c.pending);c.finished();self.assertTrue(c.due(2000));c.finished()
  self.assertFalse(c.due(2599));self.assertTrue(c.due(2600));c.finished()
  self.assertFalse(c.due(2601));self.assertTrue(AnalysisCycle().due(2601))
 def test_chart_changes_do_not_cancel_but_real_settings_do(self):
  a=dict(symbol='one',timeframe='1D',period='Max',feed_errors={},watchlist=[{'isin':'one'}],drafts={},ai_config={})
  b={**a,'symbol':'two','timeframe':'5m','period':'month','feed_errors':{'x':'outage'}}
  self.assertEqual(analysis_inputs(a),analysis_inputs(b))
  self.assertNotEqual(analysis_inputs(a),analysis_inputs({**b,'drafts':{'one':{'source_mode':'gettex'}}}))
 def test_publication_preserves_asof_and_current_selection_but_rejects_revisions(self):
  from features import Features
  from snapshot_worker import inputs
  with tempfile.TemporaryDirectory() as folder:
   store=Store(folder);watch=[{'isin':'A','name':'first'},{'isin':'B','name':'second'}];store.save_watchlist(watch);published=[]
   app=NS(store=store,watchlist=watch,drafts={},feed_errors={},symbol=Value('first'),timeframe=Value('1W'),period=Value('Max'),ai_config={},closing=False,update_ai_view=lambda:None,bridge=NS(publish=published.append))
   values=inputs(app);state=dict(stocks=[],selected={'isin':'A','timeframe':'1W','period':'Max'},analysis_snapshot={'captured_at':'2026-10-26T20:00Z'},_input_revisions=values['instrument_revisions'])
   app.symbol=Value('second');app.timeframe=Value('5m')
   self.assertTrue(Features.accept_background_snapshot(app,state,values));self.assertEqual(state['selected']['isin'],'B')
   self.assertEqual(state['analysis_selection']['isin'],'A');self.assertEqual(len(published),1)
   store.save_instrument({'isin':'A','name':'first','currency':'USD'})
   self.assertFalse(Features.accept_background_snapshot(app,state,values));self.assertEqual(len(published),1)
   store.read_cache.close()

if __name__=='__main__':unittest.main()
