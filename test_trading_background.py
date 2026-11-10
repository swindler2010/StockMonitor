"""Stage 2A targeted tests: no Tk window, feeds or working database."""
import copy
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
import pandas as pd
import analytics
import benchmarks
from storage import Store
from snapshot_worker import SnapshotContext,Value,inputs,calculate
from chart_worker import prepare,capture,LatestRequest
from benchmark_ui import BenchmarkMixin
from features import Features
from compute_process import Client


class BackgroundTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.store=Store(self.temp.name)
        self.info=dict(isin='US0378331005',name='Apple',history_symbol='AAPL',history_currency='USD',history_venue='NMS',source_mode='proxy',benchmark_id='INDEX:NDX',position={'quantity':0})
        self.values=dict(watchlist=[self.info],drafts={},feed_errors={},symbol='Apple',timeframe='1H',period='год',ai_config={})
    def tearDown(self):
        self.store.read_cache.close();self.temp.cleanup()
    def context(self):
        c=SnapshotContext();c.store=self.store
        for k,v in self.values.items():setattr(c,k,Value(v) if k in ('symbol','timeframe','period') else copy.deepcopy(v))
        return c
    @staticmethod
    def snapshot_stocks(context,stock_consumer=None,stock_builder=None):
        stocks=[]
        for info in context.watchlist:
            stock=stock_builder(info) if stock_builder else {'name':info['name']}
            if stock_consumer is not None:stock_consumer(stock,info)
            stocks.append(stock)
        return {'stocks':stocks}
    def frame(self,info,tf):
        f=pd.DataFrame(dict(open=[100.]*40,high=[102.]*40,low=[99.]*40,close=[101.]*40,volume=[100.]*40,source='proxy'),index=pd.bdate_range('2025-01-02',periods=40))
        f.attrs=dict(timeframe=tf,source='proxy',symbol='AAPL',venue='NMS',currency='USD',timezone='America/New_York',data_quality={'state':'READY'})
        return f
    def test_chart_worker_calls_core_off_calling_thread_with_profile_and_benchmark(self):
        calls=[];outputs=[];errors=[];original=analytics.analyse_multiframe
        def core(frames,tf,info,benchmark,now=None):
            calls.append((threading.get_ident(),set(frames),tf,benchmark));return original(frames,tf,info,benchmark,now)
        market={'quality':'READY','state':'OUTPERFORMING','difference_pp':2}
        def work():
            try:outputs.append(prepare(self.store,self.values))
            except Exception as error:errors.append(error)
        with patch.object(SnapshotContext,'frame_for',side_effect=self.frame),patch.object(SnapshotContext,'market_context',return_value={}),patch('chart_worker.Extras016.level_zones',return_value=[]),patch('benchmarks.load',return_value=self.frame(self.info,'1D')),patch('benchmarks.relative_performance',return_value=market),patch('analytics.analyse_multiframe',side_effect=core),patch('history_service.download',side_effect=AssertionError('Unexpected network')):
            worker=threading.Thread(target=work);worker.start();worker.join(15)
        self.assertFalse(worker.is_alive());self.assertEqual(errors,[]);self.assertEqual(len(calls),1)
        self.assertNotEqual(calls[0][0],threading.get_ident());self.assertEqual(calls[0][1],{'1H','4H','1D'})
        self.assertEqual(calls[0][3]['benchmark_id'],'INDEX:NDX');self.assertEqual(calls[0][3]['state'],'OUTPERFORMING')
        self.assertIn('trading_engine',outputs[0])
    def test_snapshot_worker_uses_core_for_each_instrument_not_selected_benchmark(self):
        other={**self.info,'isin':'US67066G1040','name':'NVIDIA','history_symbol':'NVDA','benchmark_id':'INDEX:SOX'}
        self.values['watchlist'].append(other)
        with patch('snapshot_worker.Desktop014.snapshot',side_effect=self.snapshot_stocks),patch.object(SnapshotContext,'market_context',return_value={}),patch.object(SnapshotContext,'frame_for',side_effect=self.frame),patch('benchmarks.load',return_value=pd.DataFrame()),patch('analytics.analyse_multiframe',wraps=analytics.analyse_multiframe) as core:
            result=calculate(self.store,self.values,0)
        self.assertEqual(core.call_count,2)
        self.assertEqual([s['trading_engine']['benchmark_context']['benchmark_id'] for s in result['stocks']],['INDEX:NDX','INDEX:SOX'])
    def test_daily_profile_exact(self):
        c=self.context();c.timeframe=Value('1D');c.frame_for=self.frame
        with patch('analytics.analyse_multiframe',wraps=analytics.analyse_multiframe) as core:
            c.trading_analysis(self.info)
        self.assertEqual(set(core.call_args.args[0]),{'4H','1D','1W'})
    def test_missing_tf_blocks(self):
        c=self.context();c.frame_for=lambda info,tf:pd.DataFrame() if tf=='4H' else self.frame(info,tf)
        result=c.trading_analysis(self.info)
        self.assertEqual(result['signal'],'WAIT');self.assertEqual(result['status'],'DATA BLOCKED')
        self.assertTrue(any('4H' in s for s in result['blockers']))
    def test_generation_cache_and_invalidation(self):
        c=self.context();c.frame_for=self.frame
        with patch('analytics.analyse_multiframe',wraps=analytics.analyse_multiframe) as core:
            a=c.trading_analysis(self.info);b=c.trading_analysis(self.info)
            self.assertEqual(core.call_count,1);self.assertEqual(a,b)
            self.store.set_setting('test_generation',1)
            c.trading_analysis(self.info);self.assertEqual(core.call_count,1)
            self.store.save_instrument(self.info)
            c.trading_analysis(self.info);self.assertEqual(core.call_count,2)
    def test_chart_and_snapshot_share_core_cache_in_same_worker_context(self):
        with patch.object(SnapshotContext,'frame_for',side_effect=self.frame),patch.object(SnapshotContext,'market_context',side_effect=AssertionError('Duplicate benchmark calculation')),patch('chart_worker.Extras016.level_zones',return_value=[]),patch('snapshot_worker.Desktop014.snapshot',side_effect=self.snapshot_stocks),patch('analytics.analyse_multiframe',wraps=analytics.analyse_multiframe) as core,patch('benchmarks.relative_performance',return_value={'state':'UNAVAILABLE'}) as relative:
            prepare(self.store,self.values);calculate(self.store,self.values,0)
            self.assertEqual(core.call_count,1)
            self.assertEqual(relative.call_count,1)
    def test_commit_during_calculation_never_returns_result(self):
        c=self.context();c.frame_for=self.frame
        original=analytics.analyse_multiframe
        def race(*args,**kwargs):
            result=original(*args,**kwargs);self.store.save_instrument(self.info);return result
        with patch('analytics.analyse_multiframe',side_effect=race):
            with self.assertRaisesRegex(RuntimeError,'Stale'):c.trading_analysis(self.info)
    def test_chart_apply_rejects_data_and_selection_changes(self):
        for change in ('database','symbol','timeframe'):
            ui=self.context();ui.closing=False;ui._render_prepared_chart=Mock()
            values=capture(ui)
            if change=='database':self.store.set_setting('changed',1)
            elif change=='symbol':ui.symbol=Value('Other')
            else:ui.timeframe=Value('1D')
            BenchmarkMixin._apply_chart(ui,(values,{}))
            ui._render_prepared_chart.assert_not_called()
    def test_snapshot_accept_rejects_data_change(self):
        ui=self.context();ui.closing=False;ui.update_ai_view=Mock();ui.bridge=SimpleNamespace(publish=Mock())
        captured=inputs(ui);self.store.set_setting('changed',1)
        self.assertFalse(Features.accept_background_snapshot(ui,{'diagnostics':[]},captured))
        ui.bridge.publish.assert_not_called()
    def test_benchmark_draft_rejects_old_result_and_invalidates_cache(self):
        c=self.context();c.frame_for=self.frame;c.closing=False;c._render_prepared_chart=Mock()
        with patch('analytics.analyse_multiframe',wraps=analytics.analyse_multiframe) as core:
            c.trading_analysis(self.info);captured=capture(c)
            c.drafts[self.info['isin']]={**self.info,'benchmark_id':'INDEX:SOX'}
            BenchmarkMixin._apply_chart(c,(captured,{}));c._render_prepared_chart.assert_not_called()
            result=c.trading_analysis(self.info)
            self.assertEqual(core.call_count,2)
            self.assertEqual(result['benchmark_context']['benchmark_id'],'INDEX:SOX')
    def test_latest_request_rejects_superseded_completion(self):
        pending=[];applied=[];jobs=LatestRequest(lambda work,ok,fail:pending.append(ok),applied.append,self.fail)
        jobs.request(lambda:1);jobs.request(lambda:2);pending[0]('old');pending[1]('new')
        self.assertEqual(applied,['new'])
    def test_real_compute_process_returns_blocked_without_network_or_tk(self):
        client=Client(self.store.path)
        try:
            result=client.calculate('chart',self.values)
            self.assertNotEqual(client.process.pid,os.getpid())
            self.assertEqual(result['trading_engine']['signal'],'WAIT')
            self.assertEqual(result['trading_engine']['status'],'DATA BLOCKED')
        finally:
            client.close()
            if client.process:
                client.process.wait(timeout=10);client.process.stdin.close();client.process.stdout.close()

if __name__=='__main__':unittest.main()
