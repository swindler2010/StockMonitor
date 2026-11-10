import sys,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import numpy as np
import pandas as pd
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
import data_quality as q
from intraday import normalize
from analytics import resample,bar_ends
from storage import Store
from history_backfill import sync,key

class IntradayTests(unittest.TestCase):
    now=pd.Timestamp('2026-10-04T00:00Z')
    meta={'source':'proxy','venue':'NMS','currency':'USD','symbol':'ANY','timezone':'America/New_York'}
    def frame(self,base='1H',first='2026-10-02',last='2026-10-02 16:00',meta=None,now=None):
        meta=dict(meta or self.meta)
        idx=q.expected_index(meta,base,first,last,now or self.now)
        f=pd.DataFrame({'open':10.,'high':12.,'low':9.,'close':11.,'volume':100.},index=idx)
        f.attrs=meta;return f
    def state(self,f,base='1H',now=None):return q.audit(f,base,now=now or self.now,check_stale=False)['state']
    def test_valid_source_prices_and_volume_preserved(self):
        f=self.frame();out=normalize(f,'1H',self.now)
        pd.testing.assert_frame_equal(f,out);self.assertEqual(self.state(out),'READY')
    def test_request_window_rejects_appended_latest_quote(self):
        f=self.frame();extra=f.iloc[[0]].copy();extra.index=pd.DatetimeIndex(['2026-10-03 16:00'])
        raw=pd.concat([f,extra]);raw.attrs=f.attrs
        out=normalize(raw,'1H',self.now,start=pd.Timestamp('2026-10-02T13:30Z'),end=pd.Timestamp('2026-10-02T20:30Z'))
        self.assertEqual(len(out),len(f));self.assertEqual(self.state(out),'READY')
        self.assertEqual(out.attrs['intraday_normalization']['excluded']['outside_request'],1)
    def test_close_observation_is_not_relabelled_as_bar(self):
        f=self.frame();extra=f.iloc[[0]].copy();extra.index=pd.DatetimeIndex(['2026-10-02 16:00'])
        raw=pd.concat([f,extra]);raw.attrs=f.attrs
        out=normalize(raw,'1H',self.now)
        pd.testing.assert_frame_equal(f,out);self.assertEqual(self.state(out),'READY')
    def test_impossible_prices_nan_or_volume_leave_visible_gap(self):
        for field,value in [('high',1),('volume',-1),('volume',np.nan),('open',np.inf)]:
            with self.subTest(field=field,value=value):
                f=self.frame();f.loc[f.index[2],field]=value
                out=normalize(f,'1H',self.now)
                self.assertEqual(len(out),len(f)-1);self.assertEqual(self.state(out),'GAP')
    def test_bad_edge_cannot_create_false_ready(self):
        for position in (0,-1):
            f=self.frame();f.loc[f.index[position],'volume']=np.nan
            out=normalize(f,'1H',self.now)
            self.assertEqual(self.state(out),'GAP')
            self.assertEqual(q.audit(out,'1H',now=self.now)['missing_count'],1)
    def test_all_bad_rows_are_unavailable(self):
        f=self.frame();f['open']=np.nan;out=normalize(f,'1H',self.now)
        self.assertTrue(out.empty);self.assertEqual(self.state(out),'SOURCE_UNAVAILABLE')
    def test_identical_duplicates_deduplicated_without_volume_inflation(self):
        f=self.frame();raw=pd.concat([f,f.iloc[[0]]]);raw.attrs=f.attrs
        out=normalize(raw,'1H',self.now);pd.testing.assert_frame_equal(f,out)
        self.assertEqual(self.state(out),'READY')
    def test_conflicting_duplicates_do_not_choose_price(self):
        f=self.frame();extra=f.iloc[[2]].copy();extra['close']=10.5
        raw=pd.concat([f,extra]);raw.attrs=f.attrs;out=normalize(raw,'1H',self.now)
        self.assertNotIn(f.index[2],out.index);self.assertEqual(self.state(out),'GAP')
    def test_off_grid_inside_session_blocks_even_with_complete_other_rows(self):
        f=self.frame();extra=f.iloc[[0]].copy();extra.index=pd.DatetimeIndex(['2026-10-02 10:00'])
        raw=pd.concat([f,extra]);raw.attrs=f.attrs;out=normalize(raw,'1H',self.now)
        self.assertEqual(self.state(out),'INVALID')
    def test_non_candle_live_quote_not_aggregated(self):
        f=self.frame();extra=f.iloc[[0]].copy();extra.iloc[0]=[10,10,10,10,0]
        extra.index=pd.DatetimeIndex(['2026-10-02 10:02:03'])
        raw=pd.concat([f,extra]);raw.attrs=f.attrs;out=normalize(raw,'1H',self.now)
        self.assertEqual(self.state(out),'READY');self.assertEqual(out.volume.sum(),f.volume.sum())
    def test_forming_and_delayed_source_rows_excluded(self):
        now=pd.Timestamp('2026-10-02T15:40Z');f=self.frame().loc[:'2026-10-02 11:30']
        out=normalize(f,'1H',now)
        self.assertEqual(out.index.strftime('%H:%M').tolist(),['09:30'])
        self.assertEqual(self.state(out,now=now),'READY')
    def test_numeric_strings_and_sorting(self):
        f=self.frame();raw=f.iloc[::-1].astype(str);raw.attrs=f.attrs
        out=normalize(raw,'1H',self.now);pd.testing.assert_frame_equal(f,out)
    def test_utc_to_local_keeps_us_dst_session_anchor(self):
        f=self.frame(first='2026-03-06',last='2026-03-09 16:00')
        raw=f.copy();raw.index=raw.index.tz_localize(self.meta['timezone']).tz_convert('UTC')
        out=normalize(raw,'1H',self.now);pd.testing.assert_frame_equal(f,out)
        utc=out.index.tz_localize(self.meta['timezone']).tz_convert('UTC')
        self.assertEqual((utc[0].hour,utc[7].hour),(14,13))
    def test_europe_dst_and_early_close(self):
        meta={**self.meta,'venue':'GER','currency':'EUR','timezone':'Europe/Berlin'}
        f=self.frame(first='2026-03-27',last='2026-03-30 17:30',meta=meta)
        out=normalize(f,'1H',self.now);self.assertEqual(self.state(out),'READY')
        utc=out.index.tz_localize(meta['timezone']).tz_convert('UTC')
        self.assertEqual((utc[0].hour,utc[9].hour),(8,7))
    def test_early_close_partial_hour_has_calendar_endpoint(self):
        f=self.frame(first='2025-11-28',last='2025-11-28 16:00')
        out=normalize(f,'1H',self.now);out.attrs['timeframe']='1H'
        self.assertEqual(len(out),4);self.assertEqual(bar_ends(out)[-1].tz_convert(self.meta['timezone']).strftime('%H:%M'),'13:00')
    def test_naive_dst_ambiguity_and_unknown_calendar_are_blocked(self):
        f=self.frame().iloc[:1].copy();f.index=pd.DatetimeIndex(['2026-11-01 01:30'])
        with self.assertRaises(q.QualityError):normalize(f,'1H',pd.Timestamp('2026-11-03T00:00Z'))
        f=self.frame();f.attrs['venue']='UNKNOWN'
        with self.assertRaises(q.QualityError):normalize(f,'1H',self.now)
    def test_source_identity_is_not_substituted(self):
        f=self.frame();f['currency']='EUR'
        with self.assertRaises(q.QualityError):normalize(f,'1H',self.now)
    def test_normalization_is_idempotent_and_keeps_gap_span(self):
        f=self.frame();f.loc[f.index[0],'volume']=np.nan
        first=normalize(f,'1H',self.now);second=normalize(first,'1H',self.now)
        pd.testing.assert_frame_equal(first,second);self.assertEqual(self.state(second),'GAP')
        self.assertEqual(second.attrs['intraday_normalization']['excluded']['invalid_ohlcv'],1)
    def test_derived_intraday_tf_have_exact_ohlcv_and_ends(self):
        for base,targets in [('5m',('10m',)),('15m',('30m','2H','3H')),('1H',('4H',))]:
            raw=normalize(self.frame(base),'%s'%base,self.now)
            for target in targets:
                out=resample(raw,base,target,now=self.now)
                self.assertEqual(self.state(out,target),'READY');self.assertTrue(out.bar_complete.all())
                self.assertEqual(out.volume.sum(),raw.volume.sum())
                self.assertEqual(bar_ends(out)[-1].tz_convert(self.meta['timezone']).strftime('%H:%M'),'16:00')
    def test_restart_retains_missing_edge_and_incremental_cannot_hide_it(self):
        f=self.frame('5m');f.loc[f.index[0],'volume']=np.nan
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent/'test-tmp') as folder:
            store=Store(folder)
            with patch('data_quality.repair',side_effect=lambda info,store,symbol,base,meta,download,now:q.audit(store.load_series(info['isin'],base,'proxy',symbol),base,now=now,check_stale=False)):
                sync({'isin':'ANY'},store,'ANY','5m',lambda *a,**k:f.copy(),self.now)
            store.read_cache.close();store=Store(folder)
            out=store.load_series('ANY','5m','proxy','ANY')
            self.assertEqual(self.state(out,'5m'),'GAP')
            with patch('data_quality.repair',side_effect=lambda info,store,symbol,base,meta,download,now:q.audit(store.load_series(info['isin'],base,'proxy',symbol),base,now=now,check_stale=False)):
                sync({'isin':'ANY'},store,'ANY','5m',lambda *a,**k:self.frame('5m').tail(1),self.now)
            self.assertEqual(self.state(store.load_series('ANY','5m','proxy','ANY'),'5m'),'GAP')
            store.read_cache.close()

    def test_incremental_refresh_keeps_bounded_recovery_cooldown(self):
        f=self.frame('5m',first='2026-08-06');f.loc[f.index[0],'volume']=np.nan
        repair_calls=[]
        def download(symbol,interval,start,end=None):
            if end is not None:repair_calls.append((start,end))
            return normalize(f.copy(),'5m',self.now,start=start,end=end or self.now)
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent/'test-tmp') as folder:
            store=Store(folder)
            sync({'isin':'ANY'},store,'ANY','5m',download,self.now)
            sync({'isin':'ANY'},store,'ANY','5m',download,self.now)
            self.assertEqual(len(repair_calls),1)
            self.assertEqual(store.setting(q.key({'isin':'ANY'},'ANY','5m'))['state'],'SOURCE_GAP')
            store.read_cache.close()

    def test_fall_dst_from_utc_and_weekend_observation(self):
        now=pd.Timestamp('2026-11-04T00:00Z')
        f=self.frame(first='2026-10-30',last='2026-11-02 16:00',now=now)
        raw=f.copy();raw.index=raw.index.tz_localize(self.meta['timezone']).tz_convert('UTC')
        extra=raw.iloc[:1].copy();extra.index=pd.DatetimeIndex(['2026-11-01T05:30Z'])
        raw=pd.concat([raw,extra]);raw.attrs=self.meta
        out=normalize(raw,'1H',now);pd.testing.assert_frame_equal(f,out)
        utc=out.index.tz_localize(self.meta['timezone']).tz_convert('UTC')
        self.assertEqual((utc[0].hour,utc[7].hour),(13,14))

    def test_lunch_rows_excluded_and_aggregation_cannot_cross_break(self):
        schedule=pd.DataFrame({'open':[pd.Timestamp('2026-10-02T13:30Z')],
            'close':[pd.Timestamp('2026-10-02T20:00Z')],
            'break_start':[pd.Timestamp('2026-10-02T16:00Z')],
            'break_end':[pd.Timestamp('2026-10-02T17:00Z')]},index=[pd.Timestamp('2026-10-02')])
        with patch('data_quality.calendar',return_value=SimpleNamespace(schedule=schedule)):
            f=self.frame('15m');extra=f.iloc[:1].copy();extra.index=pd.DatetimeIndex(['2026-10-02 12:15'])
            raw=pd.concat([f,extra]);raw.attrs=self.meta
            cleaned=normalize(raw,'15m',self.now);out=resample(cleaned,'15m','3H',now=self.now)
            self.assertEqual(self.state(out,'3H'),'READY')
            self.assertEqual(bar_ends(out).tz_convert(self.meta['timezone']).strftime('%H:%M').tolist(),['12:00','16:00'])

    def test_corrupt_leading_source_slot_cannot_be_excused_as_retention_boundary(self):
        f=self.frame('5m');f.loc[f.index[0],'volume']=np.nan
        raw=normalize(f,'5m',self.now);out=resample(raw,'5m','10m',now=self.now)
        self.assertNotEqual(out.attrs['data_quality']['state'],'READY')

if __name__=='__main__':unittest.main()
