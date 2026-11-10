import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

sys.path.insert(0,str(Path(__file__).resolve().parent/'stock_monitor'))
import benchmarks as bm
from storage import Store


def candles(info, closes, dates=('2026-09-28','2026-09-29','2026-09-30')):
    frame=pd.DataFrame({'open':closes,'high':[x+1 for x in closes],
        'low':[x-1 for x in closes],'close':closes,'volume':[100.]*len(closes)},
        index=pd.DatetimeIndex(dates[:len(closes)]))
    frame.attrs=bm.identity(info)
    frame.attrs['return_basis']=info.get('return_basis','price')
    return frame


class BenchmarkTests(unittest.TestCase):
    def test_catalog_is_explicit_direct_indices_and_excludes_etfs(self):
        expected={'SP500':'^GSPC','NDX':'^NDX','SOX':'^SOX','DAX':'^GDAXI','STOXX600':'^STOXX','VIX':'VIX'}
        self.assertEqual({k.rsplit(':',1)[-1]:v['history_symbol'] for k,v in bm.CATALOG.items()},expected)
        self.assertEqual(bm.CATALOG['INDEX:SOX']['history_venue'],'NIM')
        self.assertEqual(bm.CATALOG['INDEX:STOXX600']['currency'],'EUR')
        self.assertEqual(bm.CATALOG['INDEX:VIX']['history_source'],'cboe')
        self.assertFalse(any('ETF' in x['name'] for x in bm.CATALOG.values()))
        self.assertEqual(bm.quality.CALENDARS['PAR'],'XPAR')
        self.assertEqual(bm.quality.TIMEZONES['XPAR'],'Europe/Paris')

    def test_cboe_vix_calendar_includes_verified_gth_holiday_sessions(self):
        cal=bm.quality.calendar('CBOE_VIX',2023,2023).schedule
        self.assertIn(pd.Timestamp('2023-05-29'),cal.index)
        self.assertIn(pd.Timestamp('2023-07-04'),cal.index)
        self.assertNotIn(pd.Timestamp('2023-04-07'),cal.index)

    def test_index_missing_volume_is_explicitly_not_applicable(self):
        info=bm.CATALOG['INDEX:DAX'];frame=candles(info,[100,101]);frame['volume']=float('nan')
        with patch.object(bm.history_service,'download',return_value=frame):
            checked=bm.download(info)
        self.assertTrue((checked.volume==0).all())
        self.assertEqual(checked.attrs['volume_policy'],'not applicable: index is not a traded security')

    def test_default_context_uses_underlying_not_european_us_listing(self):
        self.assertEqual(bm.default_benchmark({'isin':'US0378331005','history_symbol':'APC.F'}),'INDEX:NDX')
        self.assertEqual(bm.default_benchmark({'isin':'US67066G1040','history_symbol':'NVDA.DE'}),'INDEX:SOX')
        self.assertEqual(bm.default_benchmark({'isin':'DE0007164600','history_symbol':'SAP.DE'}),'INDEX:DAX')
        self.assertEqual(bm.default_benchmark({'isin':'FR0000120321','history_symbol':'OR.PA'}),'INDEX:STOXX600')

    def test_relative_performance_uses_returns_and_same_closed_local_dates(self):
        stock=bm.CATALOG['INDEX:SP500'].copy();stock.update(isin='stock',history_source='proxy',history_symbol='AAPL',history_venue='NMS')
        index=bm.CATALOG['INDEX:NDX']
        a=candles(stock,[100,110,120]);b=candles(index,[1000,1050,1100],('2026-09-28','2026-09-29','2026-09-30'))
        with patch.object(bm.quality,'audit',return_value={'state':'READY','issues':[]}),\
             patch.object(bm.quality,'expected_index',side_effect=lambda meta,tf,first,last,now:pd.date_range(first,last,freq='D')):
            result=bm.relative_performance(a,b,'Последние',now=pd.Timestamp('2026-10-01',tz='UTC'))
        self.assertEqual(result['state'],'OUTPERFORMING')
        self.assertAlmostEqual(result['stock_return_pct'],20.0)
        self.assertAlmostEqual(result['benchmark_return_pct'],10.0)
        self.assertAlmostEqual(result['difference_pp'],10.0)
        self.assertEqual(result['comparable_sessions'],3)
        self.assertEqual(result['stock_currency'],'USD')

    def test_cross_market_returns_do_not_compare_absolute_currency_prices(self):
        de=bm.CATALOG['INDEX:DAX'].copy();de.update(isin='de-stock',kind='stock')
        eu=bm.CATALOG['INDEX:STOXX600']
        a=candles(de,[10,11,12]);b=candles(eu,[10000,10500,11000])
        a.attrs['currency']='EUR';b.attrs['currency']='EUR'
        with patch.object(bm.quality,'audit',return_value={'state':'READY','issues':[]}),\
             patch.object(bm.quality,'expected_index',side_effect=lambda meta,tf,first,last,now:pd.date_range(first,last,freq='D')):
            result=bm.relative_performance(a,b,'Max',now=pd.Timestamp('2026-10-01',tz='UTC'))
        self.assertEqual(result['state'],'OUTPERFORMING')
        self.assertAlmostEqual(result['stock_return_pct'],20.0)
        self.assertAlmostEqual(result['benchmark_return_pct'],10.0)

    def test_unavailable_vix_and_missing_identity_never_fall_back(self):
        vix=bm.CATALOG['INDEX:VIX'];stock=bm.CATALOG['INDEX:SP500']
        a=candles(stock,[10,11]);b=candles(vix,[20,21])
        with patch.object(bm.quality,'audit',return_value={'state':'READY','issues':[]}),\
             patch.object(bm.quality,'expected_index',side_effect=lambda meta,tf,first,last,now:pd.date_range(first,last,freq='D')):
            self.assertIn('not an equity',bm.relative_performance(a,b,'Max',now=pd.Timestamp('2026-10-01',tz='UTC'))['reason'])
        a.attrs.pop('currency')
        self.assertIn('INVALID',bm.relative_performance(a,candles(stock,[10,11]),'Max')['reason'])

    def test_sync_persists_verified_series_and_respects_incremental_watermark(self):
        info=bm.CATALOG['INDEX:SP500']
        with tempfile.TemporaryDirectory() as folder:
            store=Store(folder)
            try:
                first=candles(info,[100,101],('2026-09-29','2026-09-30'));calls=[]
                def provider(item,start):
                    calls.append(start)
                    return first if start is None else candles(item,[102],('2026-10-01',))
                ready={'state':'READY','issues':[],'ranges':[]}
                with patch.object(bm.quality,'audit',return_value=ready):
                    s1=bm.sync(store,info,now=pd.Timestamp('2026-10-01T22:00Z'),downloader=provider)
                    self.assertEqual(s1['count'],2)
                    s2=bm.sync(store,info,now=pd.Timestamp('2026-10-02T22:00Z'),downloader=provider)
                    self.assertEqual(s2['count'],3)
                    bm.sync(store,info,now=pd.Timestamp('2026-10-02T22:30Z'),downloader=provider)
                self.assertEqual(len(calls),2)
                self.assertEqual(calls[0],None)
                self.assertEqual(str(pd.Timestamp(calls[1]).date()),'2026-09-30')
                self.assertEqual(str(pd.Timestamp(calls[1]).tzinfo),'America/New_York')
                self.assertEqual(len(bm.load(store,info)),3)
                with store.connect() as db:self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0],'ok')
            finally:store.read_cache.close()

    def test_bad_identity_is_rejected_without_replacing_cached_rows(self):
        info=bm.CATALOG['INDEX:SP500']
        with tempfile.TemporaryDirectory() as folder:
            store=Store(folder)
            try:
                original=candles(info,[100],('2026-09-30',));store.save_series(info,original,'1D')
                state={'status':'COMPLETE','attempted_at':'2026-09-01T00:00:00+00:00'}
                store.set_setting('benchmark_history:'+info['isin'],state)
                wrong=candles(info,[999],('2026-10-01',));wrong.attrs['currency']='EUR'
                result=bm.sync(store,info,now=pd.Timestamp('2026-10-02T22:00Z'),downloader=lambda *_:wrong)
                self.assertEqual(result['quality'],'INVALID')
                self.assertEqual(float(bm.load(store,info).close.iloc[0]),100.0)
            finally:store.read_cache.close()


class BenchmarkPresentationTests(unittest.TestCase):
    def test_daily_archive_endpoints_preserve_dst_early_close_and_missing_session(self):
        from timeframes import endpoints
        frame=pd.DataFrame(index=pd.to_datetime(['2026-11-27','2026-03-06','2026-03-09','2026-03-08','2026-03-06']))
        frame.attrs={**bm.identity(bm.CATALOG['INDEX:SP500']),'timeframe':'1D'}
        actual=endpoints(frame)
        expected=pd.to_datetime(['2026-11-27T18:00Z','2026-03-06T21:00Z','2026-03-09T20:00Z',None,'2026-03-06T21:00Z'],utc=True)
        pd.testing.assert_index_equal(actual,expected.as_unit('ns'))

    def test_snapshot_warming_cache_preserves_benchmark_chart(self):
        from snapshot_worker import SnapshotContext
        from benchmark_ui import BenchmarkMixin
        from desktop014 import Desktop014
        class UI(BenchmarkMixin,Desktop014):pass
        with tempfile.TemporaryDirectory() as directory:
            store=Store(Path(directory));info=bm.CATALOG['INDEX:SP500']
            try:
                store.save_series(info,candles(info,[100,101,102]),'1D')
                worker=SnapshotContext();worker.store=store;worker.drafts={}
                ui=UI();ui.store=store;ui.drafts={}
                with patch('benchmark_ui.quality.audit',return_value={'state':'READY'}):
                    background=worker.frame_for(info,'1D')
                    visible=ui.frame_for(info,'1D')
                self.assertEqual(len(background),3)
                self.assertEqual(visible.close.tolist(),[100,101,102])
                self.assertEqual(visible.attrs['symbol'],'^GSPC')
            finally:store.read_cache.close()

    def test_vix_labels_do_not_claim_yahoo_dollar_price_or_trade_signal(self):
        from benchmark_ui import BenchmarkMixin
        from types import SimpleNamespace
        from unittest.mock import Mock
        class Value:
            def __init__(self,value=''):self.value=value
            def get(self):return self.value
            def set(self,value):self.value=value
        ui=SimpleNamespace(ax_price=Mock(),canvas=Mock(),timeframe=Value('1D'),
            metrics={k:Value('15.31 USD') for k in ('price','ema10','ema30','support','resistance','stop','add','position','updated')},
            decision=Value('SELL'),explain=Value())
        BenchmarkMixin.apply_benchmark_labels(ui,bm.CATALOG['INDEX:VIX'],
            {'quality':'READY','status':'INCOMPLETE','archive_quality':'GAP'})
        title=ui.ax_price.set_title.call_args.args[0]
        self.assertIn('Cboe',title);self.assertNotIn('Yahoo',title)
        self.assertEqual(ui.metrics['price'].get(),'15.31 volatility points')
        self.assertEqual(ui.metrics['stop'].get(),'—')
        self.assertEqual(ui.decision.get(),'ИНДЕКС')
        self.assertIn('GAP',ui.explain.get())
        self.assertIn('USD',ui.explain.get())

class BackgroundSnapshotTests(unittest.TestCase):
    def test_weekly_monthly_endpoints_match_calendar_slices_for_twenty_years(self):
        from timeframes import endpoints,schedule
        for tf,freq in (('1W','W-FRI'),('1M','MS')):
            frame=pd.DataFrame(index=pd.date_range('2006-10-01','2026-10-01',freq=freq))
            frame.attrs={**bm.identity(bm.CATALOG['INDEX:SP500']),'timeframe':tf}
            sched=schedule(frame.attrs,frame.index.min()-pd.Timedelta(days=7),frame.index.max()+pd.Timedelta(days=40))
            expected=[]
            for stamp in frame.index:
                begin=stamp if tf=='1M' else stamp-pd.Timedelta(days=6)
                end=stamp+pd.offsets.MonthBegin(1)-pd.Timedelta(days=1) if tf=='1M' else stamp
                part=sched.loc[str(begin.date()):str(end.date())]
                expected.append(part.close.iloc[-1] if len(part) else pd.NaT)
            pd.testing.assert_index_equal(endpoints(frame).as_unit('ns'),pd.DatetimeIndex(expected).as_unit('ns'))

    def test_ui_reader_does_not_wait_for_background_write_and_backup_is_consistent(self):
        import sqlite3
        from contextlib import closing
        if sqlite3.sqlite_version_info < (3,51,3):self.skipTest('WAL requires patched SQLite')
        with tempfile.TemporaryDirectory() as folder:
            store=Store(folder);store.set_setting('saved',1)
            with store.connect() as writer:
                writer.execute("UPDATE settings SET value='2' WHERE key='saved'")
                # This read must complete while the write transaction is still held.
                with closing(sqlite3.connect(store.path,timeout=.1)) as reader:
                    self.assertEqual(reader.execute('PRAGMA journal_mode').fetchone()[0],'wal')
                    self.assertEqual(reader.execute("SELECT value FROM settings WHERE key='saved'").fetchone()[0],'1')
            self.assertEqual(store.setting('saved'),2)
            backup=Path(folder)/'backup.sqlite3';store.backup(backup)
            with closing(sqlite3.connect(backup)) as check:
                self.assertEqual(check.execute('PRAGMA integrity_check').fetchone()[0],'ok')
                self.assertEqual(check.execute("SELECT value FROM settings WHERE key='saved'").fetchone()[0],'2')
            store.read_cache.close()

    def test_stale_selection_benchmark_and_shutdown_never_apply(self):
        from features import Features
        from types import SimpleNamespace
        from unittest.mock import Mock
        ui=SimpleNamespace(closing=False,bridge=Mock(),update_ai_view=Mock())
        captured={'symbol':'Apple','timeframe':'1D','drafts':{'benchmark_id':'INDEX:NDX'}}
        state={'diagnostics':['current result']}
        for current in ({**captured,'symbol':'SAP'}, {**captured,'timeframe':'1W'},
                        {**captured,'drafts':{'benchmark_id':'INDEX:SP500'}}):
            with patch('snapshot_worker.inputs',return_value=current):
                self.assertFalse(Features.accept_background_snapshot(ui,state,captured))
        ui.closing=True
        with patch('snapshot_worker.inputs',return_value=captured):
            self.assertFalse(Features.accept_background_snapshot(ui,state,captured))
        ui.bridge.publish.assert_not_called();ui.update_ai_view.assert_not_called()
        ui.closing=False
        with patch('snapshot_worker.inputs',return_value=captured):
            self.assertTrue(Features.accept_background_snapshot(ui,state,captured))
        self.assertEqual(ui.local_report,'current result')
        ui.bridge.publish.assert_called_once_with(state)

    def test_diagnostics_timer_never_calculates_snapshot_on_ui_thread(self):
        from features import Features
        from types import SimpleNamespace
        from unittest.mock import Mock
        ui=SimpleNamespace(closing=False,root=Mock(),ai_tick=Mock(),snapshot=Mock(side_effect=AssertionError('UI computation')))
        with patch('snapshot_worker.inputs',return_value={}):Features.ai_tick(ui)
        ui.snapshot.assert_not_called();ui.root.after.assert_called_once()

if __name__=='__main__':unittest.main()
