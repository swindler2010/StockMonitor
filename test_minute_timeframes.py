"""Real 1m/3m charts, original trade contracts and exact trade-ledger revisions."""
import sys,tempfile,unittest,json,copy
from pathlib import Path
from unittest.mock import patch,Mock
from types import SimpleNamespace
import pandas as pd
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
from storage import Store
from analytics import resample,TIMEFRAMES
from timeframes import LABELS,ANALYSIS_LABELS,base_for
from performance_cache import InputRevisionChanged
from intraday import trade_minutes
import benchmarks as bm
import history_service as history
import data_quality as quality
ARCHIVE=Path(__file__).resolve().parents[2]/'СМ-Архив'
TEMP_ROOT=ARCHIVE if ARCHIVE.is_dir() else None

class MinuteChartTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='minute-chart-',dir=TEMP_ROOT)
        self.store=Store(self.temp.name);self.addCleanup(self.temp.cleanup);self.addCleanup(self.store.read_cache.close)
        self.info=dict(isin='US0378331005',name='Apple',venue='GETTEX',currency='EUR')
        self.store.save_instrument(self.info)
    def trades(self,count=6):
        return pd.DataFrame(dict(price=[100.+n for n in range(count)],volume=[float(n+1) for n in range(count)]),index=pd.date_range('2026-11-25 07:30:02',periods=count,freq='1min'))
    def test_thirteen_chart_tf_keep_original_eleven_analysis_and_execution_contract(self):
        from cp_controls import demo_initialization_policy,DEMO_ALL_INITIALIZATION,DEMO_ALL_POLICY
        from paper_account import ACCOUNT
        from trading_context import plan
        self.assertEqual(len(LABELS),13);self.assertEqual(len(ANALYSIS_LABELS),11)
        self.assertEqual(set(TIMEFRAMES),set(ANALYSIS_LABELS))
        for source in ('gettex','gettex_chart','proxy'):
            self.assertEqual(base_for('1m',source),'1m');self.assertEqual(base_for('3m',source),'1m')
        identity=dict(source='gettex_chart',symbol='AAPL.GTX',venue='GETTEX',currency='EUR',timezone='Europe/Berlin')
        policy=dict(version=DEMO_ALL_INITIALIZATION,isin=self.info['isin'],identity=identity,account=ACCOUNT,approved=True,authority=DEMO_ALL_POLICY,entry_timeframes=['15m'],analysis_timeframes=list(ANALYSIS_LABELS),older_verified_warmup=True)
        self.assertTrue(demo_initialization_policy(policy,self.info['isin'],identity))
        self.assertTrue(plan('1H')['execution_tf']==['15m'])
        with self.assertRaisesRegex(ValueError,'TRADING_TIMEFRAME'):plan('3m')
    def test_true_minutes_and_three_minute_ohlcv_keep_source_and_daily_unchanged(self):
        trades=self.trades();extra=trades.iloc[[0]].copy();extra.index=extra.index+pd.Timedelta(seconds=10);extra['price']=108.;extra['volume']=4.
        self.store.save_trades(self.info['isin'],pd.concat([trades,extra]).sort_index())
        minute=self.store.load_official(self.info['isin'],'1m')
        self.assertEqual(len(minute),6);self.assertEqual(minute.iloc[0][['open','high','low','close','volume']].tolist(),[100.,108.,100.,108.,5.])
        three=resample(minute,'1m','3m',now=pd.Timestamp('2026-11-25T17:00Z'))
        self.assertEqual(len(three),2);self.assertEqual(three.iloc[0][['open','high','low','close','volume']].tolist(),[100.,108.,100.,102.,10.])
        self.assertEqual(three.attrs['source'],'gettex');self.assertEqual(three.attrs['currency'],'EUR')
        self.assertEqual(three.attrs['aggregation_base'],'1m')
        self.assertTrue(self.store.load_official(self.info['isin'],'1D').empty)
    def test_five_minute_candle_cannot_be_split_into_minutes(self):
        f=pd.DataFrame({'open':[100.],'high':[101.],'low':[99.],'close':[100.],'volume':[5.]},index=pd.to_datetime(['2026-11-25 07:30']))
        for tf in ('1m','3m'):self.assertTrue(resample(f,'5m',tf).empty)
    def test_invalid_trade_values_rejected_and_original_trades_not_modified(self):
        f=self.trades();before=f.copy(deep=True);f.loc[f.index[0],'price']=-1
        with self.assertRaisesRegex(ValueError,'MINUTE_TRADE_VALUES'):trade_minutes(f)
        self.assertEqual(f.loc[f.index[0],'price'],-1);self.assertEqual(before.loc[before.index[0],'price'],100.)
    def test_raw_trade_change_invalidates_minute_cache_but_not_fifteen_minute_market_scope(self):
        self.store.save_trades(self.info['isin'],self.trades())
        with self.store.read_cache.track_inputs() as token:first=self.store.load_official(self.info['isin'],'1m')
        fifteen=self.store.read_cache.token_for(['market:'+self.info['isin']+':15m'])
        extra=self.trades(1);extra.index=extra.index+pd.Timedelta(seconds=25);extra['price']=120.
        self.store.save_trades(self.info['isin'],extra)
        self.assertIn('trades:'+self.info['isin'],token);self.assertFalse(self.store.read_cache.current(token))
        self.assertTrue(self.store.read_cache.current(fifteen))
        second=self.store.load_official(self.info['isin'],'1m');self.assertEqual(second.iloc[0]['close'],120.)
        self.assertEqual(first.iloc[0]['close'],100.)
    def test_actual_concurrent_trade_change_rejects_frozen_result(self):
        from analytical_read import calculate_consistent
        self.store.save_trades(self.info['isin'],self.trades())
        def calculator(captured,values):
            f=captured.load_official(self.info['isin'],'1m')
            extra=self.trades(1);extra.index=extra.index+pd.Timedelta(seconds=25);extra['price']=120.
            self.store.save_trades(self.info['isin'],extra)
            return {'frame':f}
        with self.assertRaises(InputRevisionChanged):calculate_consistent(self.store,{},calculator)
    def test_53_and_200_raw_inputs_reuse_scoped_cache_and_reject_only_changed_instrument(self):
        for count in (53,200):
            with self.subTest(count=count):
                isin='T'+str(count);self.store.save_trades(isin,self.trades(count))
                with self.store.read_cache.track_inputs() as token:self.assertEqual(len(self.store.load_official(isin,'1m')),count)
                self.store.save_trades('unrelated',self.trades(1));self.assertTrue(self.store.read_cache.current(token))
                self.store.save_trades(isin,self.trades(count));self.assertTrue(self.store.read_cache.current(token))
                changed=self.trades(1);changed.index=changed.index+pd.Timedelta(seconds=25);changed['price']=200.
                self.store.save_trades(isin,changed);self.assertFalse(self.store.read_cache.current(token))
    def test_existing_index_five_minute_cache_bootstraps_minute_on_next_same_writer_turn(self):
        info=bm.CATALOG['INDEX:SP500'];clock=pd.Timestamp('2026-11-25T16:00Z');f=self.trades()
        minute=trade_minutes(f);minute.index=minute.index+pd.Timedelta(hours=2);minute.attrs=bm.data_identity(info,'1m')
        self.store.set_setting(bm.history_key(info,'5m'),{'count':2,'attempted_at':clock.isoformat()})
        plan=bm.feed_plan(self.store,info,clock);self.assertEqual(plan['base'],'1m')
        receipt=bm.sync_intraday(self.store,info,clock,downloader=lambda *_:minute,plan=plan['plan'],base='1m')
        self.assertEqual(receipt['count'],6);self.assertEqual(receipt['base'],'1m')
        self.assertEqual(self.store.setting(bm.history_key(info,'5m'))['count'],2)
    def test_index_minute_named_range_keeps_stock_auxiliary_requests_bounded(self):
        meta=bm.data_identity(bm.CATALOG['INDEX:SP500'],'1m');stamps=[int(t.timestamp()) for t in pd.to_datetime(['2026-11-25T14:30Z','2026-11-25T14:31Z'])]
        response=Mock();response.json.return_value={'chart':{'error':None,'result':[{'meta':{'symbol':'^GSPC','currency':'USD','exchangeName':'SNP','exchangeTimezoneName':'America/New_York','instrumentType':'INDEX'},'timestamp':stamps,'indicators':{'quote':[{'open':[100.,101.],'high':[102.,103.],'low':[99.,100.],'close':[101.,102.],'volume':[None,None]}]}}]}}
        with patch.object(history.requests,'get',return_value=response) as get:
            f=history.download('^GSPC','1m',minute_chart=True);self.assertEqual(len(f),2)
            self.assertEqual(get.call_args.kwargs['params']['range'],'7d');self.assertNotIn('period1',get.call_args.kwargs['params'])
            called=get.call_count
            with self.assertRaisesRegex(ValueError,'AUX_1M_EXPLICIT'):history.download('^GSPC','1m')
            with self.assertRaisesRegex(ValueError,'MINUTE_CHART_REQUIRES'):history.download('APC.DE','1m',expected={**meta,'symbol':'APC.DE','currency':'EUR'},minute_chart=True)
            self.assertEqual(get.call_count,called)
    def test_public_chart_schema_exposes_minutes_without_new_paper_execution_interval(self):
        from mcp_server import TOOLS
        for name in ('stock_monitor_select','stock_monitor_read_analytics'):
            t=next(t for t in TOOLS if t['name']==name);self.assertTrue({'1m','3m'}<=set(t['inputSchema']['properties']['timeframe']['enum']))
        paper=next(t for t in TOOLS if t['name']=='stock_monitor_paper_trade');self.assertEqual(paper['inputSchema']['properties']['timeframe']['enum'],['15m'])
    def test_minute_chart_skips_unrequested_new_core_trade_strategy(self):
        from snapshot_worker import SnapshotContext,Value
        context=SnapshotContext();context.timeframe=Value('3m')
        self.assertIsNone(context._trading_analysis(self.info))

class RecordedMinuteIndex(unittest.TestCase):
    def test_recorded_real_yahoo_minutes_support_both_timeframes(self):
        p=ARCHIVE/'Minute candles source probe.json'
        if not p.exists():self.skipTest('Source receipt absent')
        observation=json.loads(p.read_text(encoding='utf8'))['observations'][0];data=observation['raw']['chart']['result'][0]
        from intraday import normalize
        info=bm.CATALOG['INDEX:SP500'];meta=bm.data_identity(info,'1m');q=data['indicators']['quote'][0];clock=pd.Timestamp(observation['checked_at'])
        f=pd.DataFrame({k:q[k] for k in quality.FIELDS},index=pd.to_datetime(data['timestamp'],unit='s',utc=True).tz_convert(meta['timezone']))
        f.volume=pd.to_numeric(f.volume,errors='coerce').fillna(0);f.attrs=meta;f=normalize(f,'1m',now=clock)
        with tempfile.TemporaryDirectory(prefix='minute-source-',dir=TEMP_ROOT) as folder:
            store=Store(folder)
            try:
                store.save_series(info,f,'1m')
                from benchmark_ui import BenchmarkMixin
                from benchmark_display import select_frame
                for tf in ('1m','3m'):
                    normal=BenchmarkMixin._frame_uncached(SimpleNamespace(store=store,analysis_at=clock),info,tf)
                    shown=select_frame(info,tf,normal,store,clock);self.assertGreater(len(shown),0)
                    self.assertEqual(shown.attrs['symbol'],'^GSPC');self.assertEqual(shown.attrs['timeframe'],tf)
                    self.assertNotIn('accepted_risk',shown.attrs)
            finally:store.read_cache.close()

if __name__=='__main__':unittest.main()
