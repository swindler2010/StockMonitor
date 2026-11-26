import sys, unittest
from pathlib import Path
from unittest.mock import patch
import pandas as pd
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
import timeframes as tf
import data_quality as q
from analytics import resample, bar_ends, analyse

class Tests(unittest.TestCase):
    def frame(self, first, last=None, base='5m', venue='NMS', now='2026-10-04T00:00Z'):
        meta={'source':'proxy','symbol':'TEST','venue':venue,'currency':'EUR' if venue=='GER' else 'USD',
              'timezone':'Europe/Berlin' if venue=='GER' else 'America/New_York','timeframe':base}
        idx=q.expected_index(meta,base,first,last or first+' 23:59',pd.Timestamp(now))
        f=pd.DataFrame({'open':100.,'high':103.,'low':99.,'close':102.,'volume':10.},index=idx)
        f.attrs=meta
        return f
    def test_ohlcv(self):
        f=self.frame('2026-10-02');f.iloc[0]=[100,105,98,101,11];f.iloc[1]=[101,107,99,106,13]
        out=resample(f,'5m','10m',now=pd.Timestamp('2026-10-04T00:00Z'))
        self.assertEqual(out.iloc[0][['open','high','low','close','volume']].tolist(),[100,107,98,106,24])
        self.assertEqual(len(out),39);self.assertEqual(out.attrs['data_quality']['state'],'READY')
    def test_two_three_hour_sessions(self):
        f=self.frame('2026-10-01','2026-10-02 16:00',base='15m')
        for target, count in [('2H',8),('3H',6)]:
            out=resample(f,'15m',target,now=pd.Timestamp('2026-10-04T00:00Z'))
            self.assertEqual(len(out),count)
            self.assertEqual(out.index[0].strftime('%H:%M'),'09:30')
            self.assertEqual(bar_ends(out)[-1].tz_convert('America/New_York').strftime('%H:%M'),'16:00')
            self.assertEqual(out.attrs['data_quality']['state'],'READY')
    def test_early_close_and_holiday(self):
        f=self.frame('2025-11-26','2025-11-28 16:00',base='15m')
        self.assertNotIn(pd.Timestamp('2025-11-27'),f.index.normalize())
        for target in ('2H','3H'):
            out=resample(f,'15m',target)
            self.assertEqual(bar_ends(out)[-1].tz_convert('America/New_York').strftime('%H:%M'),'13:00')
            self.assertEqual(out.attrs['data_quality']['state'],'STALE')
            self.assertTrue(out.bar_complete.all())
    def test_us_dst(self):
        out=resample(self.frame('2026-03-06','2026-03-09 16:00',base='15m'),'15m','2H')
        starts=out.index.tz_localize('America/New_York').tz_convert('UTC')
        self.assertEqual(starts[0].hour,14);self.assertEqual(starts[4].hour,13)
        self.assertEqual(out.index[4].hour,9)
    def test_europe_dst(self):
        out=resample(self.frame('2026-03-27','2026-03-30 17:30',base='15m',venue='GER'),'15m','3H')
        starts=out.index.tz_localize('Europe/Berlin').tz_convert('UTC')
        self.assertEqual(starts[0].hour,8);self.assertEqual(starts[3].hour,7)
        self.assertTrue(out.bar_complete.all())
    def test_partial_closed_session_invalid(self):
        f=self.frame('2026-10-02').drop(pd.Timestamp('2026-10-02 10:00'))
        out=resample(f,'5m','10m')
        self.assertEqual(out.attrs['data_quality']['state'],'INVALID')
        self.assertFalse(out.bar_complete.all())
    def test_forming_is_not_closed(self):
        now=pd.Timestamp('2026-10-02T15:40Z')
        f=self.frame('2026-10-02',base='15m').loc[:'2026-10-02 11:30']
        out=resample(f,'15m','2H',now=now)
        self.assertFalse(out.bar_complete.iloc[-1])
        _,report=analyse(out,{},now=now)
        self.assertEqual(report['closed_bar_count'],1)
        self.assertEqual(report['excluded_open_bars'],1)
    def test_indicators_recalculate(self):
        f=self.frame('2026-09-01','2026-10-02 16:00',base='15m')
        f['source']='proxy'
        f['close']=100+pd.Series(range(len(f)),index=f.index)*.001
        results=[]
        for target in ('2H','3H'):
            out=resample(f,'15m',target,now=pd.Timestamp('2026-10-04T00:00Z'))
            indicators,report=analyse(out,{},now=pd.Timestamp('2026-10-04T00:00Z'))
            self.assertTrue(report['sufficient_history'])
            results.append(float(indicators.ema30.iloc[-1]))
        self.assertNotEqual(*results)
    def test_selected_period_persists(self):
        import tempfile
        from storage import Store
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            for target in ('5m','10m','2H','3H'):
                store=Store(directory);store.set_setting('selected',{'timeframe':target});store.read_cache.close()
                restarted=Store(directory)
                self.assertEqual(restarted.setting('selected')['timeframe'],target)
                restarted.read_cache.close()
    def test_five_minute_watermark_survives_restart(self):
        import tempfile
        from storage import Store
        from history_backfill import sync
        now=pd.Timestamp('2026-10-04T00:00Z')
        frame=self.frame('2026-08-06','2026-10-02 16:00')
        calls=[]
        def download(symbol,interval,start):
            self.assertEqual(interval,'5m');calls.append(start)
            return frame if len(calls)==1 else pd.DataFrame()
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as directory:
            store=Store(directory)
            first=sync({'isin':'TEST'},store,'TEST','5m',download,now)
            self.assertEqual(first['status'],'SOURCE_LIMITED');store.read_cache.close()
            store=Store(directory)
            second=sync({'isin':'TEST'},store,'TEST','5m',download,now)
            self.assertEqual(second['status'],'SOURCE_LIMITED')
            self.assertGreater(calls[1],now-pd.Timedelta(days=3));store.read_cache.close()
    def test_session_break_is_not_crossed(self):
        from types import SimpleNamespace
        day=pd.Timestamp('2026-10-02')
        schedule=pd.DataFrame({'open':[pd.Timestamp('2026-10-02T13:30Z')],
            'close':[pd.Timestamp('2026-10-02T20:00Z')],
            'break_start':[pd.Timestamp('2026-10-02T16:00Z')],
            'break_end':[pd.Timestamp('2026-10-02T17:00Z')]},index=[day])
        with patch('data_quality.calendar',return_value=SimpleNamespace(schedule=schedule)):
            f=self.frame('2026-10-02',base='15m')
            out=resample(f,'15m','3H')
            self.assertEqual(out.index.strftime('%H:%M').tolist(),['09:30','13:00'])
            self.assertEqual(bar_ends(out).tz_convert('America/New_York').strftime('%H:%M').tolist(),['12:00','16:00'])
    def test_sliced_endpoints(self):
        out=resample(self.frame('2026-10-02',base='15m'),'15m','3H')
        self.assertEqual(len(bar_ends(out.tail(1))),1)
        self.assertEqual(bar_ends(out.tail(1))[0],bar_ends(out)[-1])
    def test_gettex_0730_2300_all_intraday_counts_and_clipped_close(self):
        meta={'source':'gettex','symbol':'TEST','venue':'GETTEX','currency':'EUR','timezone':'Europe/Berlin'}
        now=pd.Timestamp('2026-10-04T00:00Z')
        idx=q.expected_index(meta,'5m','2026-10-02','2026-10-02 23:00',now)
        raw=pd.DataFrame({'open':100.,'high':103.,'low':99.,'close':102.,'volume':10.},index=idx)
        raw.attrs=meta
        for target,count in {'5m':186,'10m':93,'15m':62,'30m':31,'1H':16,'2H':8,'3H':6,'4H':4}.items():
            with self.subTest(tf=target):
                out=resample(raw,'5m',target,now=now)
                self.assertEqual(len(out),count)
                self.assertEqual(out.index[0].strftime('%H:%M'),'07:30')
                self.assertEqual(bar_ends(out)[-1].tz_convert('Europe/Berlin').strftime('%H:%M'),'23:00')
                self.assertEqual(out.volume.sum(),raw.volume.sum())
                if 'bar_complete' in out:self.assertTrue(out.bar_complete.all())
    def test_gettex_dst_weekends_and_holidays(self):
        meta={'venue':'GETTEX','timezone':'Europe/Berlin'}
        now=pd.Timestamp('2026-12-30T00:00Z')
        for day,utc_hour in [('2026-03-27',6),('2026-03-30',5),('2026-10-23',5),('2026-10-26',6)]:
            idx=q.expected_index(meta,'5m',day,day+' 23:00',now)
            self.assertEqual(len(idx),186)
            self.assertEqual(idx[0].hour,7);self.assertEqual(idx[0].minute,30)
            self.assertEqual(idx.tz_localize('Europe/Berlin').tz_convert('UTC')[0].hour,utc_hour)
            self.assertNotIn(pd.Timestamp(day+' 06:00'),idx)
            self.assertNotIn(pd.Timestamp(day+' 23:00'),idx)
        for day in ('2026-10-03','2026-12-25'):
            self.assertEqual(len(q.expected_index(meta,'5m',day,day+' 23:00',now)),0)
    def test_all_periods_preserved(self):
        self.assertEqual(set(tf.LABELS),{'5m','10m','15m','30m','1H','2H','3H','4H','1D','1W','1M'})
        self.assertEqual(tf.base_for('10m'),'5m');self.assertEqual(tf.base_for('3H'),'1H')
        from mcp_server import TOOLS
        tool=next(t for t in TOOLS if t['name']=='stock_monitor_select')
        self.assertEqual(set(tool['inputSchema']['properties']['timeframe']['enum']),set(tf.LABELS))
    def test_journal_resolves_new_periods(self):
        from decision_results import recorded_frame
        frames={'5m':self.frame('2026-10-02'), '15m':self.frame('2026-10-02',base='15m')}
        class Store:
            def load_series(self,isin,base,*args):return frames[base].copy()
        for target in ('5m','10m','2H','3H'):
            identity={**frames['5m'].attrs,'timeframe':target}
            out=recorded_frame(Store(),{'isin':'TEST','price_identity':identity})
            self.assertFalse(out.empty);self.assertEqual(out.attrs['timeframe'],target)
    def test_explicit_identity_required(self):
        from history_service import recover
        class Store:
            def setting(self,*args):return {}
        with patch('history_service.download') as download:
            with self.assertRaises(ValueError):recover({'isin':'TEST'},Store())
            download.assert_not_called()
    def test_intraday_survives_daily_failure(self):
        import history_service as hs
        class Store:
            def setting(self,*args):return {}
            def set_setting(self,*args):pass
        calls=[]
        def sync(info,store,symbol,base,download):
            calls.append(base)
            if base=='1D':raise ValueError('Invalid daily archive')
            return {'status':'SOURCE_LIMITED','backfill_complete':True}
        with patch('history_backfill.sync',side_effect=sync):
            _,errors=hs.recover({'isin':'TEST','history_symbol':'TEST'},Store())
        self.assertEqual(calls,['1D','5m','15m','1H']);self.assertEqual(len(errors),1)

if __name__=='__main__':unittest.main()
