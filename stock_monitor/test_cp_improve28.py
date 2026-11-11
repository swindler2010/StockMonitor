import copy,json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import pandas as pd
import gettex_chart as chart
from storage import Store
from partial_chart import project_saved,bind
from test_gettex_chart import ISIN,META,NOW,FakeClient,payload

class PartialArchive(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory(prefix='cp-improve28-');self.addCleanup(self.tmp.cleanup)
  self.s=Store(self.tmp.name);self.addCleanup(self.s.read_cache.close)
  self.info={'isin':ISIN,'name':'Apple','kind':'stock'};self.s.save_watchlist([self.info])
  for base in ('5m','1D'):self.s.set_setting(chart.state_key(ISIN,base),{'retry_after':(NOW+pd.Timedelta(days=1)).isoformat()})
 def data(self):
  rows=[]
  for n,t in enumerate(pd.date_range('2026-10-27 07:30','2026-10-27 22:45',freq='15min',tz='Europe/Berlin')):
   rows.append(dict(TIME=str(int(t.timestamp())),OPEN='100',HIGH='102',LOW='99',CLOSE='101',VOLUME=str(20+n)))
  rows[5]['HIGH']='80'
  return payload(rows=rows)
 def test_valid_segments_saved_without_erasing_invalid_and_read_back(self):
  p=self.data();r=chart.turn(self.info,self.s,NOW,FakeClient(p))
  self.assertEqual(r['state'],'PARTIAL');self.assertEqual(r['added'],61)
  f=chart.load(self.s,self.info,'15m');self.assertEqual(f.attrs['data_quality']['state'],'INVALID')
  self.assertEqual(f.attrs['partial_chart']['raw_count'],62)
  self.assertEqual(f.attrs['partial_chart']['excluded_rows'],1)
  self.assertNotIn(pd.Timestamp('2026-10-27 08:45'),f.index)
  self.assertEqual(json.loads(Path(r['receipt']).read_text())['data'],p['data'])
  with self.s.connect() as db:self.assertEqual(db.execute('select count(*) from candle_revision_history').fetchone()[0],0)
  reopened=Store(self.tmp.name);self.addCleanup(reopened.read_cache.close)
  self.assertEqual(len(chart.load(reopened,self.info,'15m')),61)
 def test_every_dependent_tf_excludes_damaged_bucket_and_blocks_engine(self):
  chart.turn(self.info,self.s,NOW,FakeClient(self.data()));f=chart.load(self.s,self.info,'15m')
  for tf in ('15m','30m','1H','2H','3H','4H'):
   with self.subTest(tf=tf):
    projected=project_saved(f,'15m',tf,NOW);self.assertIsNotNone(projected)
    self.assertEqual(projected.attrs['data_quality']['state'],'INVALID')
    self.assertTrue(projected.attrs['partial_chart']['display_only'])
    self.assertEqual(bind({},projected)['trading_engine']['status'],'DATA BLOCKED')
    self.assertEqual(bind({},projected)['analysis'][1]['decision'],'WAIT')
  self.assertIsNone(project_saved(f,'15m','1D',NOW))
 def test_incremental_clean_tail_does_not_clear_unresolved_bad_row(self):
  chart.turn(self.info,self.s,NOW,FakeClient(self.data()))
  r=chart.turn(self.info,self.s,NOW+pd.Timedelta(minutes=16),FakeClient(payload(rows=self.data()['data'][-1:])))
  self.assertEqual(r['state'],'PARTIAL')
  p=self.data();p['data'][5]['HIGH']='102'
  state=self.s.setting(chart.state_key(ISIN,'15m'));state.pop('last',None)
  self.s.set_setting(chart.state_key(ISIN,'15m'),state) # isolated historical recovery response
  r=chart.turn(self.info,self.s,NOW+pd.Timedelta(minutes=32),FakeClient(p))
  self.assertEqual(r['added'],1);self.assertNotIn('archive_invalid',r)
 def test_identity_global_damage_and_race_cannot_partially_commit(self):
  p=self.data()
  for change in ({'currency':'USD'},{'venue':'GER'}):
   with self.assertRaises(ValueError):chart.decode(p,dict(META,**change),ISIN,'15m',NOW-pd.Timedelta(days=90),NOW,NOW)
  self.s.set_setting(chart.state_key(ISIN,'15m'),{})
  self.s.set_setting('gettex_chart_next_request',NOW.isoformat())
  r=chart.turn(self.info,self.s,NOW,FakeClient(p,callback=lambda:self.s.save_watchlist([dict(self.info,name='Changed')])) )
  self.assertEqual(r['state'],'ERROR');self.assertIn('INPUT_CHANGED',r['reason'])
  with self.s.connect() as db:self.assertEqual(db.execute('select count(*) from series').fetchone()[0],0)
 def test_partial_week_and_month_cannot_include_damaged_day(self):
  import data_quality as quality
  from partial_chart import split_archive
  idx=quality.expected_index(META,'1D',pd.Timestamp('2026-07-01'),pd.Timestamp('2026-10-27'),NOW)
  raw=pd.DataFrame([[100,102,99,101,20]]*len(idx),index=idx,columns=chart.FIELDS);raw.attrs=dict(META,timeframe='1D')
  raw.loc[pd.Timestamp('2026-08-12'),'high']=1
  safe=split_archive(raw,META,NOW);self.assertIsNotNone(safe)
  safe.attrs['archive_invalid']=dict(safe.attrs['data_quality'],rejected_timestamps=['2026-08-12 00:00:00'])
  week=project_saved(safe,'1D','1W',NOW);month=project_saved(safe,'1D','1M',NOW)
  self.assertNotIn(pd.Timestamp('2026-08-14'),week.index);self.assertNotIn(pd.Timestamp('2026-08-01'),month.index)
  self.assertIn(pd.Timestamp('2026-07-01'),month.index)
  self.assertEqual(month.attrs['data_quality']['state'],'INVALID')
 def test_bad_new_observation_does_not_delete_existing_candle_but_quarantines_display(self):
  good=self.data();good['data'][5]['HIGH']='102'
  chart.turn(self.info,self.s,NOW,FakeClient(good))
  state=self.s.setting(chart.state_key(ISIN,'15m'));state.pop('last',None);self.s.set_setting(chart.state_key(ISIN,'15m'),state)
  r=chart.turn(self.info,self.s,NOW+pd.Timedelta(minutes=16),FakeClient(self.data()))
  self.assertEqual(r['state'],'PARTIAL');self.assertEqual(r['changed'],0);self.assertEqual(r['added'],0)
  raw=chart.load(self.s,self.info,'15m');self.assertEqual(len(raw),62)
  out=project_saved(raw,'15m','15m',NOW);self.assertEqual(len(out),61)
  self.assertEqual(raw.loc[pd.Timestamp('2026-10-27 08:45'),'high'],102)

class IncrementalDays(unittest.TestCase):
 def setUp(self):
  import timeframes as tf
  self.tf=tf;tf._session_buckets.cache_clear();tf._day_values.cache_clear();tf._period_grid.cache_clear()
  dates=['2026-10-23','2026-10-26','2026-10-27']
  idx=pd.DatetimeIndex([t for d in dates for t in pd.date_range(d+' 07:30',d+' 22:45',freq='15min')])
  self.f=pd.DataFrame([[100+n,102+n,99+n,101+n,20+n] for n in range(len(idx))],index=idx,columns=chart.FIELDS)
  self.f.attrs=dict(META,timeframe='15m',classify_absence=True)
 def test_only_changed_session_rebuilt_and_numerically_equal_to_uncached(self):
  import importlib.util
  path=Path(__file__).resolve().parents[1]/'history64-evidence/cp-improve28/before/code/timeframes.py'
  spec=importlib.util.spec_from_file_location('prior_timeframes',path);old=importlib.util.module_from_spec(spec);spec.loader.exec_module(old)
  for target in ('30m','1H','2H','3H','4H'):
   self.tf._session_buckets.cache_clear()
   a=self.tf.aggregate(self.f,'15m',target,NOW);stats=self.tf._session_buckets.cache_info()
   b=self.tf.aggregate(self.f,'15m',target,NOW);hit=self.tf._session_buckets.cache_info()
   self.assertEqual(hit.misses,stats.misses);self.assertEqual(hit.hits-stats.hits,3)
   changed=self.f.copy();changed.loc[changed.index[-1],'volume']+=7
   out=self.tf.aggregate(changed,'15m',target,NOW)
   self.assertEqual(self.tf._session_buckets.cache_info().misses-hit.misses,1)
   ref=old.aggregate(changed,'15m',target,NOW)
   pd.testing.assert_frame_equal(out,ref)
   self.assertEqual(out.attrs['data_quality']['state'],ref.attrs['data_quality']['state'])
 def test_daily_day_cache_does_not_change_pandas_arithmetic_or_parent_proof(self):
  from analytics import resample
  first=resample(self.f,'15m','1D',NOW);stats=self.tf._day_values.cache_info()
  changed=self.f.copy();changed.loc[changed.index[-1],'volume']+=7
  out=resample(changed,'15m','1D',NOW)
  self.assertEqual(self.tf._day_values.cache_info().misses-stats.misses,1)
  ref=changed.resample('1D').agg(dict(open='first',high='max',low='min',close='last',volume='sum')).dropna()
  pd.testing.assert_frame_equal(out[list(chart.FIELDS)],ref,check_freq=False,check_dtype=False)
  self.assertTrue(out.bar_complete.all());self.assertEqual(out.attrs['daily_provenance']['raw_state'],'STALE')
 def test_deletion_identity_and_empty_evidence_invalidate_without_false_ready(self):
  self.tf.aggregate(self.f,'15m','1H',NOW);before=self.tf._session_buckets.cache_info()
  hole=self.f.drop(self.f.index[5]);out=self.tf.aggregate(hole,'15m','1H',NOW)
  self.assertNotEqual(out.attrs['data_quality']['state'],'READY')
  self.assertGreater(self.tf._session_buckets.cache_info().misses,before.misses)
  wrong=self.f.copy();wrong.attrs['timezone']='America/New_York'
  self.assertTrue(self.tf.aggregate(wrong,'15m','1H',NOW).empty)
  bad=self.f.copy();bad.iloc[5,1]=1
  self.assertEqual(self.tf.aggregate(bad,'15m','1H',NOW).attrs['data_quality']['state'],'INVALID')
 def test_freshness_rechecked_even_when_all_numeric_days_reused(self):
  a=self.tf.aggregate(self.f,'15m','1H',pd.Timestamp('2026-10-27T22:20:00Z'))
  b=self.tf.aggregate(self.f,'15m','1H',NOW)
  self.assertEqual(a.attrs['data_quality']['state'],'READY');self.assertEqual(b.attrs['data_quality']['state'],'STALE')
 def test_proven_empty_change_invalidates_only_affected_day(self):
  t=self.f.index[5];hole=self.f.drop(t)
  first=self.tf.aggregate(hole,'15m','1H',NOW);count=len(first)
  hole.attrs['absence_evidence']=dict(identity=META,basis='complete_published_trade_files',
   reference='new isolated proof',checked_at=NOW.isoformat(),no_trades=[[str(t),str(t+pd.Timedelta(minutes=15))]],trade_intervals=[])
  second=self.tf.aggregate(hole,'15m','1H',NOW)
  self.assertEqual(len(second),count+1)
  hole.attrs['absence_evidence']['identity']=dict(META,currency='USD')
  revoked=self.tf.aggregate(hole,'15m','1H',NOW);self.assertEqual(len(revoked),count)
 def test_daily_named_index_and_fractional_ohlcv_serialization_preserved(self):
  f=self.f.astype(float);f.index.name='ts';f['volume']=f.volume/7;f['open']=f.open+0.01
  out=self.tf.daily_ohlcv(f)
  ref=f.resample('1D').agg(dict(open='first',high='max',low='min',close='last',volume='sum')).dropna()
  self.assertEqual(out.to_csv(),ref.to_csv())

class VisiblePriority(unittest.TestCase):
 setUp=PartialArchive.setUp
 def test_preferred_base_two_turn_bound_and_persistent_fairness(self):
  for base in chart.BASES:self.s.set_setting(chart.state_key(ISIN,base),{})
  calls=[]
  class Client:
   def fetch(_,isin,base,start,end):
    calls.append(base)
    if base=='1D':return {'status':'OK','request':{'ric':'AAPL.GTX','samples':'D'},'data':[dict(_DATE_END='2026-10-27',OPEN_PRC=100,HIGH_1=102,LOW_1=99,CLOSE_PRC=101,ACVOL_1=20)]},META
    return payload(base),META
  import candle_schedule
  with patch.object(candle_schedule,'decision',return_value={'due':True}),patch.object(chart,'prepare_daily',return_value={}):
   for n in range(9):
    reopened=Store(self.tmp.name)
    try:r=chart.turn(self.info,reopened,NOW+pd.Timedelta(minutes=n*2),Client(),preferred_tf='5m')
    finally:reopened.read_cache.close()
    self.assertNotEqual(r['state'],'ERROR',r)
  self.assertEqual(calls[:3],['5m','5m','15m']);self.assertIn('1D',calls)
  for n in (0,3,6):self.assertNotEqual(calls[n:n+3],['5m']*3)
 def test_front_source_published_earlier_all_maintenance_still_runs_once(self):
  from desktop014 import Desktop014
  from contextlib import nullcontext
  from types import SimpleNamespace
  import queue,time,history_service,gettex_history,revision_watcher
  import importlib.util
  p=Path(__file__).resolve().parents[1]/'history64-evidence/cp-improve28/before/code/desktop014.py'
  spec=importlib.util.spec_from_file_location('prior_desktop',p);old=importlib.util.module_from_spec(spec);spec.loader.exec_module(old)
  times={}
  for label,call in [('before',old.Desktop014._run_feed),('after',Desktop014._run_feed)]:
   seen=[];start=time.perf_counter()
   def step(name,result=None):
    def run(*a,**kw):seen.append(name);time.sleep(.015);return result
    return run
   def chart_call(*a,**kw):seen.append('chart');times[label]=time.perf_counter()-start;return {'state':'UNCHANGED'}
   ctx=SimpleNamespace(store=self.s,closing=False,feed_slots=nullcontext(),events=queue.Queue(),
    _download_gettex_trades=step('official',pd.DataFrame()),_recover_gettex_range=lambda *a:pd.DataFrame(),
    _http_bytes=lambda *a:b'',_parse_gettex_csv=lambda *a:pd.DataFrame(),feed_done=lambda *a:None)
   with (patch.object(history_service,'recover',side_effect=step('proxy',(None,[]))),patch.object(history_service,'inspect_saved_archive',side_effect=step('archive')),
    patch.object(gettex_history,'acquire_missing_evidence',side_effect=step('evidence')),patch.object(gettex_history,'classify_cached_session',side_effect=step('classify')),
    patch.object(revision_watcher,'check',side_effect=step('revision')),patch.object(chart,'turn',side_effect=chart_call)):
    call(ctx,dict(self.info,_cp_focus=dict(isin=ISIN,source='gettex_chart',timeframe='5m')))
   for name in ('official','proxy','archive','evidence','classify','revision','chart'):self.assertEqual(seen.count(name),1)
  self.assertLess(times['after'],times['before'])
  path=Path(__file__).resolve().parents[1]/'history64-evidence/cp-improve28/focus-timing.json'
  path.write_text(json.dumps({'seconds':times,'scenario':'controlled stage-order test, six 15ms dependencies; not live provider latency'},indent=2))
 def test_chart_revision_not_other_instrument_refreshes_within_existing_tick(self):
  from desktop014 import Desktop014
  from snapshot_worker import Value
  from types import SimpleNamespace
  ctx=SimpleNamespace(instrument_cfg=lambda:self.info,store=self.s,source_mode=lambda i:'proxy',timeframe=Value('15m'),
   _cp_chart_inputs=dict(isin=ISIN,timeframe='15m',scopes=['market:'+ISIN+':15m']))
  with patch('desktop014.time.time',return_value=1000):
   a=Desktop014.current_quality_signature(ctx)
   f=pd.DataFrame([[100,102,99,101,20]],columns=chart.FIELDS,index=pd.to_datetime(['2026-10-27 07:30']));f.attrs=META
   self.s.save_series(self.info,f,'15m');b=Desktop014.current_quality_signature(ctx)
   self.assertNotEqual(a,b)
   self.s.save_series({'isin':'US5949181045'},f,'15m');c=Desktop014.current_quality_signature(ctx)
   self.assertEqual(b,c)

if __name__=='__main__':unittest.main()
