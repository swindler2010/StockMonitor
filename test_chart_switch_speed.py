import copy,sys,unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock,patch
import numpy as np
import pandas as pd
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
import analytics,cp_controls as cp,chart_worker as cw
from performance_cache import ReadCache,InputRevisionChanged
from benchmark_ui import BenchmarkMixin
from snapshot_worker import Value,inputs

class ChartSwitchTests(unittest.TestCase):
 def setUp(self):
  self.cache=ReadCache(':memory:');self.addCleanup(self.cache.close)
  self.cache.db.execute('CREATE TABLE input_revisions(scope TEXT PRIMARY KEY,revision INTEGER)')
  self.store=NS(read_cache=self.cache)
  self.values=dict(watchlist=[dict(isin='A',name='Alpha')],drafts={},feed_errors={},symbol='Alpha',timeframe='4H',period='месяц',ai_config={},instrument_revisions={'instrument:A':0})
 def revision(self,scope):
  self.cache.db.execute('INSERT INTO input_revisions VALUES (?,1) ON CONFLICT(scope) DO UPDATE SET revision=revision+1',(scope,));self.cache.db.commit()
 def loader(self,store,values):
  store.read_cache.depend('market:A:15m');store.read_cache.depend('setting:owner:A')
  return {'frame':pd.DataFrame({'close':[100.]}),'trading_engine':None,'_core_pending':True}
 def test_selected_capture_matches_prior_contract_without_other_instruments(self):
  app=NS(**{k:Value(v) if k in ('symbol','timeframe','period') else v for k,v in self.values.items()});app.store=self.store
  app.watchlist.append(dict(isin='B',name='Beta'));app.drafts={'B':{'source':'other'}};app.feed_errors={'B':'error'}
  expected=inputs(app);expected.update(watchlist=[self.values['watchlist'][0]],drafts={},feed_errors={},instrument_revisions={'instrument:A':0})
  self.assertEqual(cw.capture(app),expected)
 def test_visual_cache_reuses_period_but_revisions_and_minute_invalidate(self):
  values={**self.values,'_chart_phase':'display'}
  with patch.object(cw,'_prepare',side_effect=self.loader) as loader,patch.object(cw,'display_epoch',return_value='minute1') as epoch:
   a=cw.prepare(self.store,values);a['frame'].iloc[0,0]=0
   b=cw.prepare(self.store,{**values,'period':'Max'});self.assertEqual(b['frame'].iloc[0,0],100);self.assertEqual(loader.call_count,1)
   self.revision('market:B:15m');cw.prepare(self.store,values);self.assertEqual(loader.call_count,1)
   for scope in ('market:A:15m','setting:owner:A','instrument:A'):
    self.revision(scope);cw.prepare(self.store,values)
   self.assertEqual(loader.call_count,4)
   epoch.return_value='minute2';cw.prepare(self.store,values);self.assertEqual(loader.call_count,5)
 def test_visual_cache_separates_tf_identity_and_drafts(self):
  values={**self.values,'_chart_phase':'display'}
  with patch.object(cw,'_prepare',side_effect=self.loader) as loader:
   cw.prepare(self.store,values)
   cw.prepare(self.store,{**values,'timeframe':'15m'})
   cw.prepare(self.store,{**values,'drafts':{'A':{'history_venue_lock':'GETTEX'}}})
   cw.prepare(self.store,{**values,'watchlist':[dict(isin='A',name='Alpha',history_currency='USD')]})
   self.assertEqual(loader.call_count,4)
 def test_cached_visual_still_records_inputs_and_rejects_changed_completion(self):
  with patch.object(cw,'_prepare',side_effect=self.loader):
   values={**self.values,'_chart_phase':'display'};cw.prepare(self.store,values)
   with self.cache.track_inputs() as token:cw.prepare(self.store,values)
   self.revision('market:A:15m')
   with self.assertRaises(InputRevisionChanged):self.cache.assert_current(token)
 def test_core_never_uses_visual_cache_or_calculates_a_chart(self):
  with patch('chart_worker.SnapshotContext') as ctx:
   ctx.return_value.trading_analysis.return_value={'status':'DATA BLOCKED','signal':'WAIT'}
   for _ in range(2):out=cw.prepare(self.store,{**self.values,'_chart_phase':'engine'})
   self.assertEqual(ctx.return_value.trading_analysis.call_count,2);ctx.return_value.frame_for.assert_not_called()
   self.assertEqual(out['trading_engine']['status'],'DATA BLOCKED')
 def test_partial_without_existing_owner_context_stays_blocked_without_core(self):
  frame=pd.DataFrame({'close':[100.]});frame.attrs['partial_chart']={'display_only':True}
  bound=dict(frame=frame,trading_engine={'status':'DATA BLOCKED','signal':'WAIT'},context={})
  with patch('chart_worker.SnapshotContext') as ctx,patch('partial_chart.select_display',return_value=frame),patch('partial_chart.bind',return_value=bound),patch('cp_controls.initialization_policy_for',return_value=None),patch('cp_controls.reviewed_entry_identity',return_value=None):
   store=NS(read_cache=self.cache,setting=Mock(return_value={}))
   out=cw.prepare(store,{**self.values,'_chart_phase':'display'})
   self.assertFalse(out.get('_core_pending'));self.assertEqual(out['trading_engine']['status'],'DATA BLOCKED');ctx.return_value.trading_analysis.assert_not_called()
 def view(self):
  return NS(closing=False,store=self.store,root=NS(),_render_prepared_chart=Mock(),_chart_timing=Mock(),_apply_benchmark_context=Mock(),_chart_jobs=Mock(),compute_chart=Mock(),decision=Mock(),explain=Mock())
 def display(self):
  return dict(frame=pd.DataFrame(),_input_revisions={'market:A:15m':0},_core_pending=True,trading_engine=None,context={})
 def test_display_arrives_first_then_current_core_without_second_render(self):
  view=self.view();display=self.display()
  with patch.object(cw,'capture',return_value=self.values):
   BenchmarkMixin._apply_chart(view,(self.values,display))
   view._render_prepared_chart.assert_called_once();view._chart_jobs.request.assert_called_once()
   self.assertIsNone(view._accepted_trading_chart[1]['trading_engine'])
   core=dict(_chart_engine=True,trading_engine={'signal':'WAIT','status':'READY'},context={},_input_revisions={'setting:owner:A':0})
   BenchmarkMixin._apply_chart(view,(self.values,core))
   self.assertFalse(view._accepted_trading_chart[1]['_core_pending']);view._render_prepared_chart.assert_called_once()
   self.assertEqual(view._accepted_trading_chart[1]['_input_revisions'],{'market:A:15m':0,'setting:owner:A':0})
 def test_late_core_cannot_bind_to_another_tf_or_changed_display(self):
  view=self.view();display=self.display();view._accepted_trading_chart=(self.values,display)
  core=dict(_chart_engine=True,trading_engine={'signal':'BUY','status':'READY'},context={},_input_revisions={'setting:owner:A':0})
  with patch.object(cw,'capture',return_value={**self.values,'timeframe':'1D'}):BenchmarkMixin._apply_chart(view,(self.values,core))
  self.assertIsNone(view._accepted_trading_chart[1]['trading_engine'])
  self.revision('market:A:15m')
  with patch.object(cw,'capture',return_value=self.values):BenchmarkMixin._apply_chart(view,(self.values,core))
  view.decision.set.assert_not_called();self.assertTrue(view._accepted_trading_chart[1]['_core_pending'])
 def test_queue_53_and_200_selection_bursts_apply_only_latest_without_writer(self):
  for size in (53,200):
   with self.subTest(size=size):
    jobs=[];applied=[];q=cw.LatestRequest(lambda *a:jobs.append(a),applied.append,self.fail)
    for i in range(size):q.request(lambda i=i:i)
    self.assertEqual(len(jobs),1);jobs[0][1](jobs[0][0]())
    self.assertFalse(applied);self.assertEqual(len(jobs),2);jobs[1][1](jobs[1][0]())
    self.assertEqual(applied,[size-1])
 def test_queue_staged_core_superseded_never_publishes_old_result(self):
  jobs=[];applied=[]
  def apply(value):
   applied.append(value)
   if value=='display':q.request(lambda:'old core')
  q=cw.LatestRequest(lambda *a:jobs.append(a),apply,self.fail)
  q.request(lambda:'display');jobs[0][1]('display');q.request(lambda:'new display')
  jobs[1][1]('old core');jobs[2][1]('new display')
  self.assertEqual(applied,['display','new display'])

class NumericAndProofTests(unittest.TestCase):
 def frame(self):
  close=100+np.arange(120)*.01+np.sin(np.arange(120)/4)
  from timeframes import schedule
  days=schedule(dict(venue='GETTEX',timezone='Europe/Berlin'),'2026-01-01','2026-10-01').index[:120]
  f=pd.DataFrame(dict(open=close-.1,high=close+1,low=close-1,close=close,volume=np.arange(120)+100,source='gettex_chart'),index=days)
  f.attrs=dict(timeframe='1D',source='gettex_chart',venue='GETTEX',symbol='A.GTX',currency='EUR',timezone='Europe/Berlin',data_quality={'state':'READY','evidence':['saved']*1000})
  return f
 def test_numeric_values_and_metadata_preserved_independently(self):
  f=self.frame();saved=copy.deepcopy(f.attrs);out=analytics.indicators(f)
  close=pd.Series(f.close.to_numpy(),index=f.index)
  pd.testing.assert_series_equal(out.ema10,close.ewm(span=10,adjust=False,min_periods=10).mean(),check_names=False)
  pd.testing.assert_series_equal(out.ema30,close.ewm(span=30,adjust=False,min_periods=30).mean(),check_names=False)
  self.assertEqual(out.attrs,saved);out.attrs['data_quality']['evidence'][0]='changed';self.assertEqual(f.attrs,saved)
  en,result=analytics._analyse_features(f,now=pd.Timestamp('2026-11-04',tz='UTC'))
  self.assertTrue(result['sufficient_history']);pd.testing.assert_frame_equal(pd.DataFrame(en),pd.DataFrame(analytics.indicators(f)),check_exact=True)
 def test_quality_and_warmup_guards_preserved(self):
  for state in ('INVALID','UNCONFIRMED','GAP','STALE'):
   f=self.frame();f.attrs['data_quality']['state']=state
   out,result=analytics._analyse_features(f,now=pd.Timestamp('2026-11-04',tz='UTC'))
   self.assertFalse(result['sufficient_history']);self.assertEqual(result['decision'],'WAIT')
   if state=='INVALID':self.assertTrue(out.ema10.isna().all())
  _,result=analytics._analyse_features(self.frame().tail(20),now=pd.Timestamp('2026-11-04',tz='UTC'));self.assertFalse(result['sufficient_history'])
 def test_approved_range_union_exact_overlap_boundaries_and_open_bar(self):
  meta={k:self.frame().attrs[k] for k in cp.IDENTITY};grid=pd.date_range('2026-10-28 07:30',periods=63,freq='15min')
  ranges=[[str(grid[a]),str(grid[b])] for a,b in [(0,2),(1,4),(10,11),(20,19),(40,44),(60,62)]]
  ranges.extend([[str(grid[5]),None],[None,str(grid[30])]])
  meta['tv_accepted_absence']=dict(version=cp.TV_VERSION,base='15m',identity=dict(meta),price_model=cp.TV_PRICE_MODEL,ranges=ranges)
  now=pd.Timestamp('2026-10-28 20:40',tz='UTC')
  import data_quality as dq
  closed=dq.expected_index(meta,'15m',grid.min(),grid.max(),now).intersection(grid)
  expected=closed[[any(pd.Timestamp(a)<=t<=pd.Timestamp(b) for a,b in ranges) for t in closed]]
  actual=cp.tv_absent_slots(meta,grid,'15m',now);pd.testing.assert_index_equal(actual,expected)
  self.assertNotIn(grid[-1],actual)
  for key,value in [('currency','USD'),('venue','XETRA')]:
   bad=copy.deepcopy(meta);bad[key]=value;self.assertTrue(cp.tv_absent_slots(bad,grid,'15m',now).empty)

if __name__=='__main__':unittest.main()
