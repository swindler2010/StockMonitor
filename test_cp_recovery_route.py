"""New end-to-end recovery route check using saved real SAP/FRA/EUR evidence.
Only the temporary Store is mutated. Provider boundary replays the saved receipt.
"""
import sys,json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import pandas as pd
ROOT=Path(__file__).parent
sys.path.insert(0,str(ROOT/'stock_monitor'))
import history_service as hs
import data_quality as dq
import calculation_window
from storage import Store
from snapshot_worker import SnapshotContext
from analytics import indicators
from performance_cache import frame_dependency,capture_closed_inputs,validate_closed_inputs,InputRevisionChanged

class RealSavedRecoveryRoute(unittest.TestCase):
 def setUp(self):
  self.fixture=json.loads((ROOT/'history64-evidence/cp-autonomy27/route-real-fixture.json').read_text(encoding='utf8'))
  self.info=self.fixture['info'];self.event=self.fixture['event'];self.now=pd.Timestamp(self.fixture['captured_at'])
  self.meta=dict(source=self.event['source'],symbol=self.event['symbol'],venue=self.event['venue'],currency=self.event['currency'],timezone='Europe/Berlin')
  whole=pd.DataFrame(self.fixture['rows'],columns=['ts',*dq.FIELDS]);whole.index=pd.to_datetime(whole.pop('ts'))
  self.stamp=pd.Timestamp(self.event['ts']);previous=whole.loc[whole.index<self.stamp].iloc[-1:].copy()
  # One real prior bar and the following five saved real bars. Unknown overnight
  # gaps remain present, not filled or labelled NO_TRADES.
  current=whole.loc[(whole.index>=self.stamp)&(whole.index<=self.stamp+pd.Timedelta(hours=5))].copy()
  self.full=pd.concat([previous,current]);self.full.attrs=self.meta.copy()
  self.reply=self.full.loc[[self.stamp]].copy();self.reply.attrs=self.meta.copy()
  self.tmp=tempfile.TemporaryDirectory();self.store=Store(self.tmp.name)
  self.store.save_instrument(self.info);self.store.set_setting('history_identity:'+self.info['isin'],self.meta)
  self.store.save_series(self.info,self.full.drop(self.stamp),'1H')
  self.calls=[];self.context=self.make_context()
 def tearDown(self):
  self.store.read_cache.close();self.tmp.cleanup()
 def make_context(self):
  ctx=SnapshotContext();ctx.store=self.store;ctx.drafts={}
  ctx.source_mode=lambda info:'proxy' # isolated view of the exact recovery listing
  ctx.analysis_at=self.now;ctx.closed_specs={}
  return ctx
 def fetch(self,symbol,interval,start,end=None,**kwargs):
  self.assertEqual((symbol,interval),(self.meta['symbol'],'60m'))
  self.calls.append([symbol,str(start),str(end)])
  a=pd.Timestamp(start).tz_convert(self.meta['timezone']).tz_localize(None)
  b=pd.Timestamp(end).tz_convert(self.meta['timezone']).tz_localize(None)
  out=self.reply.loc[(self.reply.index>=a)&(self.reply.index<b)].copy();out.attrs=self.reply.attrs.copy()
  return out
 def rows(self):
  return self.store.load_series(self.info['isin'],'1H',self.meta['source'],self.meta['symbol'],self.meta['venue'],self.meta['currency'])
 def test_real_saved_route_persistence_dependencies_and_resume(self):
  before=self.rows();views={tf:self.context.frame_for(self.info,tf) for tf in ('2H','3H','4H')}
  for tf,view in views.items():self.assertTrue(self.stamp not in view.index or not bool(view.loc[self.stamp,'bar_complete']),tf)
  spec=frame_dependency(self.info['isin'],views['2H'],'2H',self.now)
  token=self.store.read_cache.token_for(['instrument:'+self.info['isin'],'market:'+self.info['isin']+':1H'])
  with self.store.connect() as db:guard=capture_closed_inputs(db,[spec],token)
  with patch.object(hs,'download',side_effect=self.fetch):
   result=hs.inspect_saved_archive(self.info,self.store,self.now)
  self.assertEqual(result['base'],'1H');self.assertEqual(len(self.calls),1)
  after=self.rows()
  pd.testing.assert_frame_equal(after[list(dq.FIELDS)],self.full[list(dq.FIELDS)],check_dtype=False,check_freq=False)
  pd.testing.assert_frame_equal(after.loc[before.index,list(dq.FIELDS)],before[list(dq.FIELDS)],check_dtype=False)
  self.assertFalse(self.store.read_cache.current(token))
  with self.store.connect() as db:
   with self.assertRaises(InputRevisionChanged):validate_closed_inputs(db,guard)
   self.assertEqual(db.execute('select count(*) from candle_recoveries').fetchone()[0],1)
  derived={}
  for tf in ('2H','3H','4H'):
   view=self.context.frame_for(self.info,tf);self.assertTrue(bool(view.loc[self.stamp,'bar_complete']),tf)
   count=int(tf[0]);raw=self.full.loc[(self.full.index>=self.stamp)&(self.full.index<self.stamp+pd.Timedelta(hours=count))]
   expected=[raw.open.iloc[0],raw.high.max(),raw.low.min(),raw.close.iloc[-1],raw.volume.sum()]
   self.assertEqual(list(view.loc[self.stamp,list(dq.FIELDS)]),expected,tf)
   self.assertEqual(view.attrs['venue'],'FRA');self.assertEqual(view.attrs['currency'],'EUR')
   enriched=indicators(view);self.assertEqual(enriched.loc[self.stamp,'open'],expected[0])
   derived[tf]={'bar_complete':True,'ohlcv':[float(x) for x in expected]}
  fresh_token=self.store.read_cache.token_for(token)
  with self.store.connect() as db:
   fresh=capture_closed_inputs(db,[frame_dependency(self.info['isin'],view,tf,self.now)],fresh_token)
   validate_closed_inputs(db,fresh)
  admission=calculation_window.inspect(self.info,view,'4H',self.now)
  self.assertEqual(admission['status'],'BLOCKED')
  self.assertIn('INSUFFICIENT_CLOSED_BARS',admission['blockers'])
  self.store.read_cache.close();self.store=Store(self.tmp.name);self.context=self.make_context()
  pd.testing.assert_frame_equal(self.rows()[list(dq.FIELDS)],after[list(dq.FIELDS)],check_dtype=False)
  with patch.object(hs,'download',side_effect=self.fetch):
   resumed=hs.inspect_saved_archive(self.info,self.store,self.now+pd.Timedelta(seconds=1))
   calls=len(self.calls)
   waiting=hs.inspect_saved_archive(self.info,self.store,self.now+pd.Timedelta(seconds=2))
  self.assertEqual(len(self.calls),calls);self.assertLessEqual(calls,2)
  with self.store.connect() as db:self.assertEqual(db.execute('select count(*) from candle_recoveries').fetchone()[0],1)
  self.assertEqual(len(self.rows()),len(self.full))
  result={'status':'PASS','kind':'isolated replay of actual stored recovery','identity':self.meta,'isin':self.info['isin'],'recovered_at':str(self.stamp),'original_unchanged':True,'recovered_unique':1,'derived':derived,'old_guard_rejected':True,'fresh_guard_valid':True,'admission':admission['status'],'blockers':admission['blockers'],'resume':resumed,'waiting':waiting,'provider_calls':self.calls}
  (ROOT/'history64-evidence/cp-autonomy27/route-integration-result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf8')
 def test_real_route_rejects_wrong_currency_and_corrupt_provider_reply(self):
  original=self.rows().copy()
  for kind in ('currency','ohlcv'):
   with self.subTest(kind=kind):
    broken=self.reply.copy();broken.attrs=self.meta.copy()
    if kind=='currency':broken.attrs['currency']='USD'
    else:broken.loc[:, 'high']=0.
    with patch.object(hs,'download',return_value=broken):
     result=hs.inspect_saved_archive(self.info,self.store,self.now)
    self.assertNotIn(self.stamp,self.rows().index)
    pd.testing.assert_frame_equal(self.rows()[list(dq.FIELDS)],original[list(dq.FIELDS)],check_dtype=False)
    with self.store.connect() as db:
     exists=db.execute("select 1 from sqlite_master where type='table' and name='candle_recoveries'").fetchone()
     self.assertEqual(db.execute('select count(*) from candle_recoveries').fetchone()[0] if exists else 0,0)
    report=self.store.setting(dq.key(self.info,self.meta['symbol'],'1H'),{})
    self.assertNotEqual(report['state'],'READY')
    # Independent adverse replay, not a production retry/cooldown override.
    self.store.read_cache.close();self.tmp.cleanup();self.tmp=tempfile.TemporaryDirectory();self.store=Store(self.tmp.name)
    self.store.save_instrument(self.info);self.store.set_setting('history_identity:'+self.info['isin'],self.meta)
    self.store.save_series(self.info,original,'1H')

if __name__=='__main__':unittest.main()

