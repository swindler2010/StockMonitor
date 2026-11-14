"""A late change revokes its dependent stocks, not a whole independent snapshot."""
import copy,sys,time,unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
from features import Features
from snapshot_worker import inputs,Value


class Cache:
 def __init__(self,token):self.token=token;self.late_change=None
 def token_for(self,scopes):return {k:self.token.get(k,0) for k in scopes}
 def current(self,token):
  if self.late_change:self.token[self.late_change]+=1;self.late_change=None
  return token==self.token_for(token)


class PublicationIsolation(unittest.TestCase):
 def prepare(self,count=2,shared=False):
  token={};stocks=[];watch=[]
  for n in range(count):
   isin='S'+str(n);watch.append(dict(isin=isin,name=isin));scopes=['instrument:'+isin,'market:'+isin+':15m']
   if shared and n<2:scopes.append('market:BENCH:1D')
   revisions={k:1 for k in scopes};token.update(revisions)
   guard=dict(version=1,revisions=revisions,strict={},settings={},settings_start={},series=[])
   stocks.append(dict(isin=isin,name=isin,timeframe='1H',candles=[dict(close=10,source='gettex')],count=100,
    data_quality=dict(state='READY'),trading_engine=dict(status='READY',signal='BUY'),_closed_inputs=guard))
  cache=Cache(copy.deepcopy(token));self.published=[];self.rejected=[]
  self.app=NS(store=NS(read_cache=cache),watchlist=watch,drafts={},feed_errors={},ai_config={},closing=False,
   symbol=Value('S0'),timeframe=Value('1H'),period=Value('Max'),update_ai_view=lambda:None,
   bridge=NS(publish=self.published.append,revision_rejected=lambda *x:self.rejected.append(x)))
  self.values=inputs(self.app)
  self.state=dict(stocks=stocks,selected=dict(isin='S0',timeframe='1H',period='Max'),
   analysis_snapshot=dict(captured_at='2026-11-11T12:00Z',execution_permission=False),
   _input_revisions=token,_calculation_timing={},market_context=dict(state='READY'),
   _revision_trace={'A':dict(scope='market:S0:15m'),'B':dict(scope='market:S1:15m')})
  return cache
 def publish(self):return Features.accept_background_snapshot(self.app,self.state,self.values)
 def test_late_single_instrument_change_preserves_independent_result(self):
  cache=self.prepare();cache.token['market:S0:15m']+=1
  self.assertTrue(self.publish());self.assertEqual(len(self.published),1)
  bad,good=self.state['stocks']
  self.assertEqual(bad['candles'],[]);self.assertEqual(bad['trading_engine']['status'],'DATA BLOCKED')
  self.assertEqual(bad['trading_engine']['signal'],'WAIT');self.assertEqual(good['trading_engine']['signal'],'BUY')
  self.assertNotIn('market:S0:15m',self.state['_input_revisions'])
  self.assertEqual(self.state['market_context']['state'],'UNAVAILABLE')
  self.assertNotIn('A',self.state['_revision_trace']);self.assertIn('B',self.state['_revision_trace'])
  self.assertFalse(self.state['analysis_snapshot']['execution_permission'])
 def test_shared_benchmark_revokes_all_dependents_only(self):
  cache=self.prepare(3,shared=True);cache.token['market:BENCH:1D']+=1;self.assertTrue(self.publish())
  self.assertEqual([s['trading_engine']['status'] for s in self.state['stocks']],['DATA BLOCKED','DATA BLOCKED','READY'])
  self.assertEqual(self.state['analysis_snapshot']['validated_instruments'],1)
 def test_all_affected_is_published_as_blocked_not_ready(self):
  cache=self.prepare(shared=True);cache.token['market:BENCH:1D']+=1;self.assertTrue(self.publish())
  self.assertEqual(self.state['analysis_snapshot']['validated_instruments'],0)
  self.assertTrue(all(s['count']==0 and s['trading_engine']['signal']=='WAIT' for s in self.state['stocks']))
 def test_legacy_missing_or_unowned_scope_remains_rejected(self):
  for variant in ('legacy','missing_guard','unknown_scope','unchanged_unknown_scope','missing_binding'):
   cache=self.prepare();cache.token['market:S0:15m']+=1
   if variant=='legacy':self.state.pop('analysis_snapshot')
   elif variant=='missing_guard':self.state['stocks'][1].pop('_closed_inputs')
   elif variant=='missing_binding':self.state['stocks'][0]['_closed_inputs']['revisions']={}
   else:self.state['_input_revisions']['setting:global_unknown']=1;cache.token['setting:global_unknown']=1 if variant=='unchanged_unknown_scope' else 2
   with self.subTest(variant=variant):self.assertFalse(self.publish());self.assertEqual(self.published,[])
 def test_drafts_identity_and_second_publication_race_still_reject(self):
  for variant in ('draft','identity','late_race'):
   cache=self.prepare();cache.token['market:S0:15m']+=1
   if variant=='draft':self.app.drafts={'S0':{'source_mode':'unknown'}}
   elif variant=='identity':cache.token['instrument:S1']+=1
   else:cache.late_change='market:S1:15m'
   with self.subTest(variant=variant):self.assertFalse(self.publish());self.assertEqual(self.published,[])
 def test_existing_invalidated_stock_is_not_restored(self):
  cache=self.prepare(3);self.state['stocks'][2]=dict(isin='S2',name='S2',timeframe='1H',count=0,
   candles=[],input_state='INVALIDATED',data_quality={'state':'STALE'},trading_engine={'status':'DATA BLOCKED','signal':'WAIT'})
  for k in list(self.state['_input_revisions']):
   if ':S2' in k:self.state['_input_revisions'].pop(k)
  self.state['analysis_snapshot']['invalidated_inputs']={'S2':'previous worker change'}
  cache.token['market:S0:15m']+=1;self.assertTrue(self.publish())
  self.assertEqual(self.state['stocks'][2]['input_state'],'INVALIDATED')
  self.assertEqual(self.state['analysis_snapshot']['validated_instruments'],1)
 def test_53_and_200_stocks_need_only_metadata_not_candle_calculations(self):
  for count in (53,200):
   cache=self.prepare(count);cache.token['market:S0:15m']+=1;started=time.perf_counter()
   with patch('performance_cache._closed_rows',side_effect=AssertionError('No candle IO in Tk publication')):
    self.assertTrue(self.publish())
   self.assertEqual(self.state['analysis_snapshot']['validated_instruments'],count-1)
   self.assertEqual(sum(s['trading_engine']['status']=='READY' for s in self.state['stocks']),count-1)
   print('metadata_publication',count,round(time.perf_counter()-started,4),'seconds')


class RealProofIsolation(unittest.TestCase):
 def test_corrected_candle_between_worker_and_ui_keeps_other_real_proof(self):
  from contextlib import contextmanager
  import test_closed_receipt_metadata as fx
  from performance_cache import capture_closed_inputs,validate_closed_inputs
  from snapshot_worker import validate_snapshot,retain_current_publication
  fx.ClosedReceiptMetadata.setUp(self)
  self.db.execute("INSERT INTO series SELECT 'OTHER',source,venue,currency,symbol,base_tf,ts,open,high,low,close,volume FROM series")
  self.db.executemany('INSERT INTO input_revisions VALUES(?,1)',[('instrument:OTHER',),('market:OTHER:15m',)])
  spec=copy.deepcopy(self.guard['series'][0]['spec']);spec['isin']='OTHER'
  other=capture_closed_inputs(self.db,[spec],{'instrument:OTHER':1,'market:OTHER:15m':1});self.db.commit()
  @contextmanager
  def connect():
   try:yield self.db
   finally:self.db.rollback()
  stocks=[dict(isin=isin,name=isin,timeframe='1H',count=1,candles=[{'close':10}],_closed_inputs=g,
               trading_engine=dict(status='READY',signal='BUY')) for isin,g in [('TEST',self.guard),('OTHER',other)]]
  state=dict(stocks=stocks,selected={'isin':'TEST'},analysis_snapshot={},_calculation_timing={})
  validate_snapshot(NS(connect=connect),state)
  self.db.execute("UPDATE series SET close=10.1 WHERE isin='TEST'")
  self.db.execute("UPDATE input_revisions SET revision=revision+1 WHERE scope='market:TEST:15m'");self.db.commit()
  current={k:self.db.execute('SELECT revision FROM input_revisions WHERE scope=?',(k,)).fetchone()[0] for k in state['_input_revisions']}
  self.assertTrue(retain_current_publication(state,current))
  self.assertEqual(state['stocks'][0]['trading_engine']['signal'],'WAIT')
  self.assertEqual(state['stocks'][1]['_closed_inputs'],other)
  self.assertNotIn('market:TEST:15m',state['_input_revisions'])
  validate_closed_inputs(self.db,state['_closed_inputs'])


if __name__=='__main__':unittest.main()
