"""Snapshot uses the existing exact cache, never cached admission or revisions."""
import sqlite3,sys,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import pandas as pd
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
import data_quality as dq
import snapshot_worker as sw
from performance_cache import ReadCache,InputRevisionChanged


class SnapshotReuse(unittest.TestCase):
 def setUp(self):
  archive=Path(__file__).resolve().parents[2]/'СМ-Архив'
  with tempfile.NamedTemporaryFile(prefix='snapshot-reuse-',suffix='.sqlite3',dir=archive,delete=False) as f:self.path=Path(f.name)
  self.writer=sqlite3.connect(self.path)
  self.writer.execute('CREATE TABLE input_revisions(scope TEXT PRIMARY KEY,revision INTEGER)')
  self.writer.execute("INSERT INTO input_revisions VALUES('market:TEST:15m',1)");self.writer.commit()
  self.cache=ReadCache(self.path)
  self.addCleanup(self.path.unlink);self.addCleanup(self.writer.close);self.addCleanup(self.cache.close)
  self.store=SimpleNamespace(read_cache=self.cache)
  self.now=pd.Timestamp('2026-11-10T12:00Z')
  self.frame=pd.DataFrame(dict(open=[10.]*40,high=[11.]*40,low=[9.]*40,close=[10.]*40,volume=[1.]*40),index=pd.date_range('2026-11-09 07:30',periods=40,freq='15min'))
  self.frame.attrs=dict(source='gettex_chart',symbol='TEST.GTX',venue='GETTEX',currency='EUR',timezone='Europe/Berlin',timeframe='15m')
  self.values=dict(watchlist=[dict(isin='TEST',name='Test')],symbol='Test',timeframe='15m',period='Max',drafts={},feed_errors={},ai_config={'provider':'local'})
  self.reports=[];self.mutate=None
 def run_snapshot(self):
  def stock(ctx,info):
   self.cache.depend('market:TEST:15m')
   frame=self.frame.copy();frame.attrs.update(symbol=info['isin']+'.GTX')
   report=dq.audit(frame,'15m',now=self.now,check_stale=False)
   return dict(isin=info['isin'],name=info['name'],timeframe='15m',candles=[],data_quality=report)
  def engine(ctx,info):
   frame=self.frame.copy();frame.attrs.update(symbol=info['isin']+'.GTX')
   if self.mutate:self.mutate(frame)
   report=dq.audit(frame,'15m',now=self.now,check_stale=False)
   self.reports.append(report)
   return dict(benchmark_context={},status='READY' if report['state']=='READY' else 'DATA BLOCKED',signal='WAIT')
  with patch.object(sw.Desktop014,'snapshot_stock',stock),patch.object(sw.SnapshotContext,'trading_analysis',engine):
   return sw.calculate(self.store,self.values,None)
 def test_snapshot_reuses_identical_checks_and_retains_results(self):
  with patch('data_quality.expected_index',wraps=dq.expected_index) as build:
   result=self.run_snapshot();self.assertEqual(build.call_count,2)
  self.assertEqual(result['stocks'][0]['data_quality']['state'],'READY')
  self.assertEqual(result['stocks'][0]['trading_engine']['signal'],'WAIT')
  self.assertEqual(self.reports[0],result['stocks'][0]['data_quality'])
 def test_changed_ohlcv_and_identity_remain_blocked(self):
  for change in ('ohlcv','identity'):
   with self.subTest(change=change):
    self.cache.analytics_items.clear()
    def mutate(frame):
     if change=='ohlcv':frame.iloc[-1,frame.columns.get_loc('close')]=99.
     else:frame.attrs['currency']=''
    self.mutate=mutate
    with patch('data_quality.expected_index',wraps=dq.expected_index) as build:
     result=self.run_snapshot();self.assertEqual(build.call_count,4)
    self.assertEqual(result['stocks'][0]['trading_engine']['status'],'DATA BLOCKED')
 def test_actual_revision_change_still_rejects_snapshot(self):
  def changed(_):
   self.writer.execute("UPDATE input_revisions SET revision=revision+1 WHERE scope='market:TEST:15m'");self.writer.commit()
  self.mutate=changed
  with self.assertRaises(InputRevisionChanged):self.run_snapshot()
 def test_53_and_200_instruments_stay_separate_and_cache_bounded(self):
  for count in (53,200):
   with self.subTest(count=count):
    self.cache.analytics_items.clear();self.reports=[]
    self.values['watchlist']=[dict(isin=str(i),name='Test' if i==0 else str(i)) for i in range(count)]
    with patch('data_quality.expected_index',wraps=dq.expected_index) as build:
     result=self.run_snapshot();self.assertEqual(build.call_count,2*count)
    self.assertEqual(len(result['stocks']),count)
    self.assertEqual([r['identity']['symbol'] for r in self.reports],[str(i)+'.GTX' for i in range(count)])
    self.assertLessEqual(len(self.cache.analytics_items),self.cache.analytics_limit)
 def test_reuse_context_is_released_after_success_and_failure(self):
  import calculation_reuse
  self.run_snapshot();self.assertIsNone(calculation_reuse._active.get())
  self.mutate=lambda _:(_ for _ in ()).throw(RuntimeError('controlled failure'))
  with self.assertRaisesRegex(RuntimeError,'controlled failure'):self.run_snapshot()
  self.assertIsNone(calculation_reuse._active.get())


if __name__=='__main__':unittest.main()
