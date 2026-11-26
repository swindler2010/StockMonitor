import copy,json,sys,tempfile,unittest
from pathlib import Path
import pandas as pd
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
import data_quality as dq
from analytics import resample
import calculation_window as cw
from storage import Store

class Systemic(unittest.TestCase):
 def setUp(self):
  self.now=pd.Timestamp('2026-10-26T18:00Z')
  self.meta=dict(source='proxy',symbol='TEST.DE',venue='GER',currency='EUR',timezone='Europe/Berlin')
  self.info={'isin':'TEST','eur_listings':[dict(self.meta,isin='TEST',status='CONFIRMED')]}
  idx=dq.expected_index(self.meta,'1D',pd.Timestamp('2020-09-29'),pd.Timestamp('2026-10-26'),self.now)
  self.daily=pd.DataFrame(dict(open=10.,high=11.,low=9.,close=10.,volume=100.,source='proxy'),index=idx)
  self.daily.attrs={**self.meta,'timeframe':'1D','data_quality':{'state':'READY'}}
 def test_leading_retention_period_excluded_only_in_analytic_context(self):
  for tf in ('1W','1M'):
   f=resample(self.daily,'1D',tf,self.now);before=f.copy();attrs=copy.deepcopy(f.attrs)
   self.assertEqual(f.attrs['data_quality']['state'],'INVALID')
   r=cw.bounded_features(self.info,f,tf,self.now)
   self.assertTrue(r['analysis_ready'],r);self.assertEqual(r['excluded_leading_partial'],str(f.index[0]))
   pd.testing.assert_frame_equal(f,before);self.assertEqual(f.attrs,attrs)
 def test_internal_missing_session_cannot_use_leading_edge_waiver(self):
  f=resample(self.daily.drop(self.daily.index[-12]),'1D','1W',self.now)
  self.assertNotIn('leading_partial',f.attrs['daily_provenance']);self.assertFalse(cw.bounded_features(self.info,f,'1W',self.now)['analysis_ready'])
 def test_bad_raw_upstream_and_identity_still_block(self):
  for state in ('INVALID','SOURCE_UNAVAILABLE','UNCONFIRMED','RECOVERING'):
   f=self.daily.copy();f.attrs['data_quality']={'state':state};f=resample(f,'1D','1M',self.now)
   self.assertNotIn('leading_partial',f.attrs['daily_provenance']);self.assertFalse(cw.bounded_features(self.info,f,'1M',self.now)['analysis_ready'])
  f=resample(self.daily,'1D','1M',self.now);f.attrs['currency']='USD'
  self.assertFalse(cw.bounded_features(self.info,f,'1M',self.now)['analysis_ready'])
 def test_forged_leading_proof_cannot_hide_internal_bad_aggregate(self):
  f=resample(self.daily,'1D','1W',self.now);f.loc[f.index[-10],'bar_complete']=False
  self.assertFalse(cw.bounded_features(self.info,f,'1W',self.now)['analysis_ready'])
 def test_audit_does_not_copy_unrelated_large_metadata(self):
  class NoCopy:
   def __deepcopy__(self,memo):raise AssertionError('deepcopy of metadata')
  f=self.daily.copy();sentinel=NoCopy();f.attrs['large_evidence_payload']=sentinel
  r=dq.audit(f,'1D',now=self.now);self.assertEqual(r['state'],'READY');self.assertIs(f.attrs['large_evidence_payload'],sentinel)
  f.loc[f.index[-2],'high']=1
  self.assertEqual(dq.audit(f,'1D',now=self.now)['state'],'INVALID')
 def test_full_reference_remains_separate_when_recursive_window_differs(self):
  import numpy as np
  from types import SimpleNamespace as NS
  from unittest.mock import patch
  d=self.daily.copy();price=100+np.arange(len(d))*.02+np.sin(np.arange(len(d))*.11)
  for k,offset in [('open',-.1),('close',0),('high',1),('low',-1)]:d[k]=price+offset
  frames={t:resample(d,'1D',t,self.now) for t in ('1D','1W','1M')}
  ctx=NS(user_state=lambda i:{},source_mode=lambda i:'proxy',frame_for=lambda i,t:frames.get(t,pd.DataFrame()))
  with patch.object(cw,'bounded_benchmark',return_value={'quality':'READY','state':'OUTPERFORMING'}):r=cw.assess_all(ctx,self.info,self.now)
  self.assertFalse(r['timeframes']['1D']['analysis_ready']);self.assertEqual(r['timeframes']['1D']['blockers'],['RECURSIVE_INITIALIZATION_SENSITIVE'])
  self.assertTrue(r['timeframes']['1D']['full_reference']['analysis_ready'])
  for t in ('1W','1M'):
   c=next(c for c in r['candidates'] if c['timeframe']==t)
   self.assertTrue(c['analysis_ready'],c);self.assertEqual(c['context'],'full_closed_reference_v1');self.assertFalse(c['signal_ready']);self.assertFalse(c['trade_ready'])
  d.loc[d.index[-10],'high']=0;frames['1D']=d
  with patch.object(cw,'bounded_benchmark',return_value={}):blocked=cw.assess_all(ctx,self.info,self.now)
  self.assertFalse(any(c['analysis_ready'] for c in blocked['candidates']))

class QueueFairness(unittest.TestCase):
 def test_oldest_due_is_not_starved_by_continuously_due_priority_names(self):
  from paper_account import ALLOWED
  for count in (49,53,200):
   with self.subTest(count=count),tempfile.TemporaryDirectory() as directory:
    store=Store(directory);now=pd.Timestamp('2026-10-26T18:00Z');names=list(sorted(ALLOWED))+['other'+str(n) for n in range(count-len(ALLOWED))];infos=[{'isin':i,'name':i} for i in names];store.save_watchlist(infos)
    def record(info,at):return dict(execution_version=1,isin=info['isin'],checked_at=at.isoformat(),expires_at=(now-pd.Timedelta(seconds=1)).isoformat(),_input_revisions=store.read_cache.token_for(['instrument:'+info['isin']]))
    for i in infos:store.set_setting('cp_eligibility:'+i['isin'],record(i,now-pd.Timedelta(hours=1)))
    seen=[]
    for n in range(count):
     i=cw.eligibility_due(store,infos,now)[0];seen.append(i['isin']);store.set_setting('cp_eligibility:'+i['isin'],record(i,now+pd.Timedelta(seconds=n)))
    self.assertEqual(len(set(seen)),count);store.read_cache.close()

class FullContextGuards(unittest.TestCase):
 def test_full_context_cannot_inherit_execution_permission(self):
  from test_smart_execution import NewExecutionContract
  fixture=NewExecutionContract();fixture.setUp()
  try:
   fixture.record['candidates'][0]['context']='full_closed_reference_v1';fixture.save()
   with self.assertRaisesRegex(ValueError,'SMART_CONTEXT_MISMATCH'):fixture.guard()
   # No order or paper trade is submitted by this test.
  finally:fixture.tearDown()
 def test_full_benchmark_uses_full_reference_and_preserves_unknown(self):
  from test_smart_execution import BenchmarkIntegration
  from unittest.mock import patch
  fixture=BenchmarkIntegration();fixture.setUp()
  with patch('benchmarks.load',return_value=fixture.f),patch('calculation_window.bar_ends',return_value=pd.date_range('2025-01-01',periods=210,tz='UTC')),patch('benchmarks.relative_performance',return_value={'state':'UNAVAILABLE','quality':'UNCONFIRMED'}) as calc:
   out=cw.bounded_benchmark(fixture.ctx,fixture.info,{},fixture.f,fixture.now,full_reference=True)
  self.assertEqual(calc.call_count,1);self.assertEqual(len(calc.call_args.args[0]),210)
  self.assertEqual(out['context'],'full_closed_reference_v1');self.assertFalse(cw.benchmark_ready(out))

class BenchmarkTail(unittest.TestCase):
 def test_tail_is_finalized_without_future_request_and_unchanged_rewrite(self):
  import benchmarks as bm
  from test_benchmarks import candles
  with tempfile.TemporaryDirectory() as folder:
   store=Store(folder);info=bm.CATALOG['INDEX:NDX'];old=candles(info,[100],['2026-10-26']);store.save_series(info,old,'1D')
   calls=[]
   def provider(i,start):
    calls.append(start);self.assertEqual(start,pd.Timestamp('2026-10-26T00:00',tz='America/New_York'));self.assertLess(start,pd.Timestamp('2026-10-26T18:00Z'))
    return candles(i,[101],['2026-10-26'])
   bm.sync(store,info,now=pd.Timestamp('2026-10-26T18:00Z'),downloader=provider)
   self.assertEqual(bm.load(store,info).close.iloc[0],101);token=store.read_cache.token_for(['market:INDEX:NDX:1D'])
   bm.sync(store,info,now=pd.Timestamp('2026-10-26T18:30Z'),downloader=provider);self.assertEqual(len(calls),1)
   bm.sync(store,info,now=pd.Timestamp('2026-10-26T21:00Z'),downloader=provider);self.assertEqual(len(calls),2)
   self.assertEqual(token,store.read_cache.token_for(['market:INDEX:NDX:1D']));store.read_cache.close()
 def test_future_cache_never_causes_future_network_request(self):
  import benchmarks as bm
  from test_benchmarks import candles
  from unittest.mock import Mock
  with tempfile.TemporaryDirectory() as folder:
   store=Store(folder);info=bm.CATALOG['INDEX:DAX'];f=candles(info,[100],['2026-10-27']);store.save_series(info,f,'1D');provider=Mock()
   result=bm.sync(store,info,now=pd.Timestamp('2026-10-26T18:00Z'),downloader=provider)
   self.assertEqual(result['quality'],'INVALID');provider.assert_not_called();self.assertEqual(len(bm.load(store,info)),1);store.read_cache.close()

class CalendarValidity(unittest.TestCase):
 def test_pending_publication_grace_expires_before_next_session(self):
  meta={'venue':'GER','timezone':'Europe/Berlin'}
  self.assertEqual(cw.context_deadline(meta,'1D',pd.Timestamp('2026-10-26T16:40Z')),pd.Timestamp('2026-10-26T17:00Z'))
  self.assertEqual(cw.context_deadline(meta,'1W',pd.Timestamp('2026-10-30T16:40Z')),pd.Timestamp('2026-10-30T17:00Z'))
 def test_closed_incomplete_aggregate_blocked_even_during_grace(self):
  fixture=Systemic();fixture.setUp();now=pd.Timestamp('2026-10-23T15:35Z')
  raw=fixture.daily.loc[:'2026-10-22'].copy();f=resample(raw,'1D','1W',now)
  r=cw.bounded_features(fixture.info,f,'1W',now)
  self.assertFalse(r['analysis_ready']);self.assertIn('CLOSED_AGGREGATE_INCOMPLETE',r['blockers'])
 def test_deadline_follows_actual_next_closed_bar(self):
  cases=[('GER','5m','2026-10-26T08:01Z','2026-10-26T08:05Z'),
   ('GER','1D','2026-10-26T18:00Z','2026-10-27T16:30Z'),
   ('GER','1W','2026-10-26T18:00Z','2026-10-30T16:30Z'),
   ('GER','1M','2026-10-26T18:00Z','2026-10-30T16:30Z'),
   ('GER','5m','2026-10-23T18:00Z','2026-10-26T08:05Z'),
   ('GETTEX','5m','2026-10-26T20:01Z','2026-10-26T20:05Z')]
  for venue,tf,at,expected in cases:
   with self.subTest(venue=venue,tf=tf,at=at):
    self.assertEqual(cw.context_deadline({'venue':venue,'timezone':'Europe/Berlin'},tf,pd.Timestamp(at)),pd.Timestamp(expected))
  now=pd.Timestamp('2026-10-26T18:00Z');self.assertEqual(cw.context_deadline({'venue':'UNKNOWN'},'1D',now),now+pd.Timedelta(seconds=60))
 def test_calendar_validity_cannot_override_revision_or_expiry(self):
  with tempfile.TemporaryDirectory() as directory:
   store=Store(directory);store.save_watchlist([{'isin':'TEST','name':'Test'}]);now=pd.Timestamp('2026-10-26T18:00Z')
   rec=dict(execution_version=1,isin='TEST',expires_at=cw.context_deadline({'venue':'GER','timezone':'Europe/Berlin'},'1D',now).isoformat(),_input_revisions=store.read_cache.token_for(['instrument:TEST']))
   with store.connect() as db:
    self.assertEqual(cw.eligibility_state(db,rec,now+pd.Timedelta(hours=1)),'CURRENT')
    self.assertEqual(cw.eligibility_state(db,rec,pd.Timestamp(rec['expires_at'])),'EXPIRED')
    db.execute("UPDATE input_revisions SET revision=revision+1 WHERE scope='instrument:TEST'")
    self.assertEqual(cw.eligibility_state(db,rec,now),'STALE')
   store.read_cache.close()
 def test_health_shows_full_context_without_bounded_blocker_or_trade(self):
  import candlepilot_health as health
  with tempfile.TemporaryDirectory() as directory:
   store=Store(directory);store.save_watchlist([{'isin':'TEST','name':'Test'}])
   row=dict(context='bounded_200_exact_reference_v1',chart_ready=True,analysis_ready=False,count=200,blockers=['RECURSIVE_INITIALIZATION_SENSITIVE'],full_reference=dict(count=1000,first='a',last='b'))
   record=dict(execution_version=1,isin='TEST',expires_at='2027-01-01T00:00Z',_input_revisions=store.read_cache.token_for(['instrument:TEST']),timeframes={'1W':row},candidates=[dict(context='full_closed_reference_v1',timeframe='1W',analysis_ready=True,signal_ready=False,trade_ready=False,blockers=[],result={'blockers':[]})])
   store.set_setting('cp_eligibility:TEST',record)
   out=health.collect(Path(directory)/'stock_monitor.sqlite3');chosen=next(r for r in out['eligibility_rows'] if r['tf']=='1W')
   self.assertTrue(chosen['analysis_ready']);self.assertEqual(chosen['count'],1000);self.assertEqual(chosen['blockers'],[])
   self.assertEqual(chosen['bounded_blockers'],['RECURSIVE_INITIALIZATION_SENSITIVE']);self.assertFalse(chosen['trade_ready']);store.read_cache.close()

if __name__=='__main__':unittest.main()
