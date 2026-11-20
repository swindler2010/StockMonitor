"""UI polling uses semantic revisions, not full historical DQ payloads."""
import sys,time,unittest,json
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
from desktop014 import Desktop014
from test_cp_reviewed_risk import Memory

class PollBudgetTests(unittest.TestCase):
 def setUp(self):
  self.store=Memory();self.addCleanup(self.store.read_cache.close)
  self.info={'isin':'TEST'};self.key='data_quality:TEST:SAVED:1H'
  self.store.set_setting('history_identity:TEST',{'symbol':'SAVED'})
  self.report={'state':'UNCONFIRMED','ranges':[['2026-01-01','2026-01-02']]*20000,'checked_at':'before'}
  self.store.set_setting(self.key,self.report)
  self.app=NS(instrument_cfg=lambda:self.info,store=self.store,source_mode=lambda _: 'proxy',timeframe=NS(get=lambda:'1H'),
    _cp_chart_inputs={'isin':'TEST','timeframe':'1H','scopes':['market:TEST:15m']})
 def signature(self):return Desktop014.current_quality_signature(self.app)
 def test_53_200_polls_never_decode_dq_and_keep_relevant_invalidation(self):
  original=self.store.setting;reads=[]
  def setting(key,default=None):
   reads.append(key)
   if key.startswith('data_quality:'):self.fail('full DQ decoded on UI poll')
   return original(key,default)
  with patch.object(self.store,'setting',side_effect=setting),patch('desktop014.time.time',return_value=120):
   before=self.signature()
   for n in (53,200):
    for _ in range(n):self.assertEqual(self.signature(),before)
   self.assertEqual(reads,['history_identity:TEST'])
   q={**self.report,'checked_at':'after','raw_retry_after':99};self.store.set_setting(self.key,q)
   self.assertEqual(before,self.signature())
   self.store.set_setting('unrelated',{'value':1});self.assertEqual(before,self.signature())
   self.store.set_setting(self.key,{**q,'state':'INVALID'});changed=self.signature();self.assertNotEqual(before,changed)
   self.store.db.execute("INSERT INTO candles VALUES ('OTHER','gettex','15m','2026-01-01',1,2,1,2,1)");self.store.db.commit()
   self.assertEqual(changed,self.signature())
   self.store.db.execute("INSERT INTO candles VALUES ('TEST','gettex','15m','2026-01-01',1,2,1,2,1)");self.store.db.commit()
   self.assertNotEqual(changed,self.signature())
   self.store.set_setting('history_identity:TEST',{'symbol':'NEW'})
   self.signature();self.assertEqual(reads,['history_identity:TEST','history_identity:TEST'])
 def test_minute_boundary_and_selection_remain_significant(self):
  with patch('desktop014.time.time',return_value=120):before=self.signature()
  with patch('desktop014.time.time',return_value=180):self.assertNotEqual(before,self.signature())
  with patch('desktop014.time.time',return_value=120):
   self.info={'isin':'OTHER'};self.assertNotEqual(before,self.signature())

class FreshnessBudgetTests(unittest.TestCase):
 def fixture(self):
  from freshness_runtime import FreshnessRuntime
  jobs=[];timers={};clock=[100.];published=[]
  def after(delay,callback):
   key=len(timers)+1;timers[key]=(delay,callback);return key
  root=NS(after=after,after_cancel=lambda key:timers.pop(key,None))
  def job(*args,**kwargs):jobs.append(args);return True
  app=NS(closing=False,watchlist=[{'isin':'TEST'}],root=root,job=job,store=NS(directory=Path('.')),
    _freshness_listener=published.append,bridge=NS())
  return FreshnessRuntime(app),app,jobs,timers,clock,published
 def test_53_200_feed_events_publish_asof_once_and_bound_followup(self):
  r,app,jobs,timers,clock,published=self.fixture()
  with patch('freshness_runtime.perf_counter',side_effect=lambda:clock[0]):
   r.request('FEED_COMPLETED:TEST')
   for n in (53,200):
    for _ in range(n):r.request('FEED_COMPLETED:TEST')
   self.assertEqual(len(jobs),1)
   value={'checked_at':'as-of-original','_automatic_read_seconds':1};jobs[0][1](value)
   self.assertEqual(published,[value]);self.assertTrue(value['_newer_inputs_pending'])
   self.assertEqual(len(timers),1);self.assertEqual(next(iter(timers.values()))[0],60000)
   for _ in range(200):r.request('FEED_COMPLETED:TEST')
   self.assertEqual(len(jobs),1);self.assertEqual(len(timers),1)
   r.request('PANEL_REFRESH');self.assertEqual(len(jobs),2);self.assertFalse(timers)
   app.watchlist.append({'isin':'OTHER'});jobs[1][1]({'checked_at':'obsolete'})
   self.assertEqual(len(published),1);self.assertEqual(next(iter(timers.values()))[0],0)
   app.closing=True;self.assertFalse(r.request());self.assertEqual(len(jobs),2)
 def test_failed_read_is_unavailable_and_does_not_spin_on_feeds(self):
  r,app,jobs,timers,clock,published=self.fixture()
  with patch('freshness_runtime.perf_counter',side_effect=lambda:clock[0]):
   r.request('FEED_COMPLETED:TEST');jobs[0][2]('private error')
   self.assertIsNone(r.latest);self.assertEqual(app.bridge.freshness_activity['state'],'UNAVAILABLE')
   for _ in range(200):r.request('FEED_COMPLETED:TEST')
   self.assertEqual(len(jobs),1);self.assertEqual(len(timers),1);self.assertFalse(published)

class LineageReadBudgetTests(unittest.TestCase):
 def test_one_wal_view_reuses_proof_next_read_rechecks_and_race_rejects(self):
  import tempfile,pandas as pd
  from test_cp_tv_legacy_reference import FlatStore
  from analytical_read import calculate_consistent
  from history_service import attach_parent_provenance,parent_provenance_key
  from performance_cache import InputRevisionChanged
  import revision_watcher as rw
  archive=Path(__file__).parent.parent.parent/'СМ-Архив'
  with tempfile.NamedTemporaryFile(dir=archive,suffix='.sqlite3',delete=False) as file:path=Path(file.name)
  store=None
  try:
   store=FlatStore(path);isin='TEST';key='receipt:TEST';stamp='2026-11-13 10:00:00'
   meta=dict(source='gettex_chart',symbol='TEST.GTX',venue='GETTEX',currency='EUR',timezone='Europe/Berlin')
   receipt=dict(first=stamp,end='2026-11-13 10:15:00',source={'proof':{'identity':meta}})
   store.set_setting(key,receipt);store.set_setting(rw.RETIME_POLICY,{'approved':True})
   store.set_setting('interval_evidence:'+isin,dict(identity=meta,checked_at='2026-11-14T09:00Z'))
   store.set_setting(parent_provenance_key(isin,meta,'15m'),{stamp:{'official_timebase':key}})
   frame=pd.DataFrame([[100,101,99,100,1]],index=pd.to_datetime([stamp]),columns=['open','high','low','close','volume']);frame.attrs=meta
   def calculate(captured,values):
    frames=[attach_parent_provenance(captured,frame.copy(),isin,'15m') for _ in range(values['count'])]
    if values.get('race'):store.set_setting(rw.RETIME_POLICY,{'approved':False})
    return {'invalid':frames[-1].attrs['invalid_parent_provenance']}
   with patch('revision_watcher.retime_current',return_value=True) as check:
    for count in (53,200):
     before=check.call_count
     result=calculate_consistent(store,{'count':count},calculate)
     self.assertEqual(check.call_count,before+1);self.assertEqual(result['invalid'],[])
     self.assertIn('setting:'+rw.RETIME_POLICY,result['_input_revisions'])
     self.assertIn('setting:gettex_chart:TEST:15m',result['_input_revisions'])
    with self.assertRaises(InputRevisionChanged):calculate_consistent(store,{'count':2,'race':True},calculate)
   with patch('revision_watcher.retime_current',return_value=False) as check:
    self.assertEqual(calculate_consistent(store,{'count':2},calculate)['invalid'],[stamp]);self.assertEqual(check.call_count,1)
    # Ordinary writer/read calls never reuse the transaction-only receipt.
    for _ in range(2):attach_parent_provenance(store,frame.copy(),isin,'15m')
    self.assertEqual(check.call_count,3)
  finally:
   if store:store.read_cache.close()
   for suffix in ('','-wal','-shm'):Path(str(path)+suffix).unlink(missing_ok=True)

if __name__=='__main__':unittest.main()
