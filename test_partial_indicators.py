"""New segmented display path only. No network, DB, CORE or trade operations."""
import copy,sys,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import numpy as np
import pandas as pd
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
from analytics import indicators
from chart_indicators import partial_indicators,segmented_line,draw_partial,FIXED_COLUMNS
from partial_chart import bind

def frame(n=90,split=45,tf='15m',isin='US0378331005'):
    index=pd.date_range('2026-10-20 07:30',periods=n,freq='15min')
    price=100+np.arange(n)*.1+np.sin(np.arange(n))
    f=pd.DataFrame(dict(open=price,high=price+1,low=price-1,close=price+.2,volume=20+np.arange(n),bar_complete=True),index=index)
    f.attrs=dict(source='gettex_chart',venue='GETTEX',currency='EUR',symbol='APC.GTX',timezone='Europe/Berlin',isin=isin,timeframe=tf,
        data_quality={'state':'UNCONFIRMED'},partial_chart=dict(display_only=True,break_before=[str(index[split])] if split else [],segments=2 if split else 1))
    return f

def live(f):
    f.loc[f.index[-1],'bar_complete']=False
    f.attrs['live_candle']=dict(timestamp=str(f.index[-1]),display_only=True,analysis_mode='PRELIMINARY_ONLY',status='WAITING_DATA',
        identity={k:f.attrs[k] for k in ('source','venue','currency','symbol','timezone')})
    return f

class PartialIndicators(unittest.TestCase):
    def test_native_listing_proof_carries_isin_without_top_level_alias(self):
        f=frame();isin=f.attrs.pop('isin')
        f.attrs['listing_proof']={k:f.attrs[k] for k in ('source','venue','currency','symbol','timezone')}
        f.attrs['listing_proof'].update(isin=isin,status='CONFIRMED')
        self.assertTrue(pd.notna(partial_indicators(f).ema30.iloc[-1]))
        f.attrs['listing_proof']['currency']='USD'
        self.assertEqual(partial_indicators(f).attrs['partial_indicators']['blocked_reason'],'LISTING_PROOF_MISMATCH')

    def test_all_eleven_timeframes_and_instruments_use_same_real_segments(self):
        for tf in ('5m','10m','15m','30m','1H','2H','3H','4H','1D','1W','1M'):
            for isin in ('US0378331005','DE0007164600','NEW_INSTRUMENT'):
                with self.subTest(tf=tf,isin=isin):
                    f=frame(tf=tf,isin=isin);got=partial_indicators(f)
                    for segment in (f.iloc[:45],f.iloc[45:]):
                        clean=pd.DataFrame(segment,copy=True);expected=indicators(clean)
                        np.testing.assert_allclose(got.loc[segment.index,list(FIXED_COLUMNS)],expected[list(FIXED_COLUMNS)],equal_nan=True)
                    self.assertEqual(got.attrs['partial_indicators']['segments'],2)

    def test_invalid_ohlcv_resets_without_rewriting_it(self):
        f=frame(split=0);f.loc[f.index[45],'high']=1
        got=partial_indicators(f)
        self.assertEqual(got.loc[f.index[45],'high'],1)
        self.assertTrue(got.iloc[45:55].ema10.isna().all())
        self.assertTrue(pd.notna(got.ema10.iloc[55]))
        self.assertEqual(got.attrs['partial_indicators']['excluded_indicator_rows'],1)

    def test_provenance_and_rejected_rows_reset(self):
        for field,value in [('invalid_parent_provenance',{str(frame().index[40]):'INVALID'}),('archive_invalid',{'rejected_timestamps':[str(frame().index[40])]})]:
            f=frame(split=0);f.attrs[field]=value;got=partial_indicators(f)
            self.assertTrue(got.iloc[40:50].ema10.isna().all())
            self.assertTrue(pd.notna(got.ema10.iloc[50]))

    def test_identity_source_currency_fail_closed(self):
        for change in ('USD','mixed','missing','report'):
            f=frame()
            if change=='USD':f.attrs['currency']='USD'
            if change=='mixed':f['venue']='GETTEX';f.loc[f.index[0],'venue']='XETRA'
            if change=='missing':f.attrs.pop('source')
            if change=='report':f.attrs['data_quality']['identity']={'source':'proxy'}
            got=partial_indicators(f);self.assertTrue(got[list(FIXED_COLUMNS)].isna().all().all());self.assertTrue(got.attrs['partial_indicators']['blocked_reason'])

    def test_duplicate_unsorted_timestamps_fail_closed(self):
        for f in (frame().iloc[::-1],pd.concat([frame(),frame().tail(1)])):
            f.attrs=frame().attrs
            self.assertEqual(partial_indicators(f).attrs['partial_indicators']['blocked_reason'],'INVALID_TIMESTAMPS')

    def test_each_indicator_has_own_warmup(self):
        got=partial_indicators(frame(40,split=0))
        for column,start in dict(ema10=9,ema30=29,rsi=14,atr=13,macd=25,macd_signal=33,volume_mean20=20).items():
            self.assertTrue(got[column].iloc[:start].isna().all(),column);self.assertTrue(pd.notna(got[column].iloc[start]),column)

    def test_live_updates_only_preview_not_closed_values(self):
        f=live(frame());before=partial_indicators(f);changed=f.copy();changed.loc[changed.index[-1],['close','high']]=[150,151]
        after=partial_indicators(changed)
        pd.testing.assert_frame_equal(before.iloc[:-1],after.iloc[:-1])
        self.assertNotEqual(before.ema30.iloc[-1],after.ema30.iloc[-1]);self.assertTrue(after.attrs['partial_indicators']['live_provisional'])
        self.assertFalse(after.bar_complete.iloc[-1])

    def test_live_with_wrong_identity_and_interior_incomplete_excluded(self):
        f=live(frame(split=0));f.attrs['live_candle']['identity']['source']='proxy';f.loc[f.index[40],'bar_complete']=False
        got=partial_indicators(f);self.assertTrue(got.iloc[-1][list(FIXED_COLUMNS)].isna().all());self.assertTrue(got.iloc[40:50].ema10.isna().all())

    def test_accepted_absence_does_not_invent_or_split(self):
        f=frame(split=0).drop(frame().index[42]);f.attrs['tv_accepted_absence']={'note':'already removed from projected expected grid'}
        got=partial_indicators(f);expected=indicators(pd.DataFrame(f,copy=True))
        np.testing.assert_allclose(got.ema30,expected.ema30,equal_nan=True);self.assertEqual(len(got),89);self.assertEqual(got.attrs['partial_indicators']['segments'],1)

    def test_does_not_use_interpolated_accepted_risk_price(self):
        f=frame();original=partial_indicators(f);f.attrs['accepted_risk']={'interpolated':True,'prices':{str(f.index[46]):1e9}}
        with patch('cp_controls.analytical_close_series',side_effect=lambda x,**kw: (self.assertNotIn('accepted_risk',x.attrs),x.close)[1]):
            got=partial_indicators(f)
        np.testing.assert_allclose(got.ema30,original.ema30,equal_nan=True)

    def test_custom_chart_periods_and_fixed_core_are_separate(self):
        f=frame();got=partial_indicators(f,{'ema_fast':3,'ema_slow':5,'rsi':3,'volume':3,'macd_fast':2,'macd_slow':4,'macd_signal':2})
        self.assertTrue(pd.notna(got.chart_ema_fast.iloc[47]));self.assertTrue(pd.isna(got.ema10.iloc[47]))
        self.assertTrue(pd.notna(got.chart_macd_signal.iloc[49]));self.assertTrue(pd.isna(got.macd_signal.iloc[49]))

    def test_plot_break_keeps_first_real_value(self):
        got=partial_indicators(frame(),{'ema_fast':1});x,y=segmented_line(got,got.chart_ema_fast)
        self.assertEqual(len(x),91);self.assertTrue(np.isnan(x[45]));self.assertEqual(y[46],got.close.iloc[45])

    def test_empty_short_and_all_invalid_are_safe(self):
        for f in (frame().iloc[:0],frame(3,split=0),frame().assign(volume=-1)):
            got=partial_indicators(f);self.assertTrue(got.ema30.isna().all());self.assertEqual(len(got),len(f))

    def test_bind_preserves_dq_wait_core_and_original_inputs(self):
        f=live(frame());original=copy.deepcopy(f);root=copy.deepcopy(f.attrs)
        result=bind({'trading_engine':{'signal':'BUY','status':'READY'}},f)
        self.assertEqual(result['trading_engine']['status'],'DATA BLOCKED');self.assertEqual(result['trading_engine']['signal'],'WAIT')
        self.assertEqual(result['context']['state'],'UNAVAILABLE');self.assertFalse(result['analysis'][1]['ohlcv_valid'])
        self.assertEqual(result['frame'].attrs['data_quality'],root['data_quality'])
        pd.testing.assert_frame_equal(f,original);self.assertEqual(f.attrs,root)
        self.assertTrue(result['analysis'][1]['display_indicators_available']);self.assertTrue(pd.notna(result['analysis'][0].ema30.iloc[-1]))

    def test_chart_worker_prepares_settings_without_core(self):
        import chart_worker
        f=frame();store=SimpleNamespace(setting=lambda *a:{'ema_fast':3})
        context=SimpleNamespace(instrument_cfg=lambda:{'isin':'TEST'},user_state=lambda i:{},frame_for=lambda *a:f)
        with patch.object(chart_worker,'SnapshotContext',return_value=context),patch('partial_chart.select_display',return_value=f):
            result=chart_worker.prepare(store,{'timeframe':'15m'})
        self.assertTrue(pd.notna(result['analysis'][0].chart_ema_fast.iloc[2]));self.assertFalse(result['analysis'][1]['ohlcv_valid'])

    def test_renderer_uses_worker_columns_and_breaks(self):
        import matplotlib
        matplotlib.use('Agg')
        from matplotlib.figure import Figure
        from desktop014 import DEFAULT_IND
        fig=Figure();axes=fig.subplots(6,1);app=SimpleNamespace(ax_price=axes[0],panels=list(zip(('Объём','RSI','MACD','ATR','Относительный объём'),axes[1:])))
        f=partial_indicators(live(frame()));settings={**DEFAULT_IND,'Средний объём':True,'VWAP':True}
        with patch('analytics.indicators',side_effect=AssertionError('Must not calculate on Tk')):
            draw_partial(app,f,f,settings,'gray','blue')
        self.assertTrue(len(axes[0].lines)>=2);self.assertTrue(np.isnan(axes[0].lines[0].get_xdata()).any())
        self.assertGreater(len(axes[3].lines),1)

    def test_first_warmed_rsi_is_visible_as_point(self):
        from matplotlib.figure import Figure
        from desktop014 import DEFAULT_IND
        fig=Figure();price,rsi=fig.subplots(2,1)
        app=SimpleNamespace(ax_price=price,panels=[('RSI',rsi)])
        f=partial_indicators(live(frame(15,split=0)))
        draw_partial(app,f,f,DEFAULT_IND,'gray','blue')
        points=[x for x in rsi.lines if x.get_marker()=='.']
        self.assertEqual(len(points),1);self.assertEqual(points[0].get_xdata().tolist(),[14])

if __name__=='__main__':unittest.main()
