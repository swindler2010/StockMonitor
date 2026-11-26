"""Protect numeric-only work from copying the archive or losing safety metadata."""
import copy
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import numpy as np
import pandas as pd
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
import analytics as a
import cp_controls as cp
import partial_chart as pc
import chart_indicators as ci
from chart_modes import draw_prices
from matplotlib.figure import Figure
from matplotlib.backends.backend_agg import FigureCanvasAgg
from desktop014 import DEFAULT_IND
import test_chart_switch_speed as fixture

class Evidence:
    copies=0
    def __deepcopy__(self,memo):
        type(self).copies+=1
        return self

def sample():
    f=fixture.NumericAndProofTests().frame()
    f.attrs['archive_probe']=Evidence()
    return f

class NumericMetadataTests(unittest.TestCase):
    def test_analysis_copies_archive_once_at_public_boundary(self):
        f=sample();Evidence.copies=0
        out,result=a._analyse_features(f,now=pd.Timestamp('2026-11-04T12:00Z'))
        self.assertTrue(result['sufficient_history'])
        self.assertEqual(Evidence.copies,1)
        self.assertEqual(out.attrs,f.attrs)

    def test_default_close_series_retains_independent_metadata(self):
        f=sample();Evidence.copies=0;out=cp.analytical_close_series(f)
        self.assertEqual(Evidence.copies,1)
        self.assertEqual(out.attrs,f.attrs)
        out.attrs['data_quality']['evidence'][0]='changed'
        self.assertEqual(f.attrs['data_quality']['evidence'][0],'saved')

    def test_price_render_does_not_copy_archive(self):
        f=sample();fig=Figure();ax=fig.subplots();Evidence.copies=0
        draw_prices(ax,f,'Candles')
        self.assertEqual(Evidence.copies,0)

    def test_partial_levels_do_not_copy_archive(self):
        f=sample();f['chart_segment']=1;f.attrs['partial_indicators']={'blocked_reason':None}
        Evidence.copies=0
        result,zones=pc.display_levels(f,pd.Timestamp('2026-11-04T12:00Z'))
        self.assertTrue(result['display_levels_available'])
        self.assertEqual(Evidence.copies,0)
        recent=pd.DataFrame(f,copy=False).iloc[:-1].tail(20)
        self.assertEqual(result['support'],float(recent.low.min()))
        self.assertEqual(result['resistance'],float(recent.high.max()))

    def test_partial_drawing_does_not_copy_archive(self):
        from test_partial_indicators import frame
        f=ci.partial_indicators(frame());f.attrs['archive_probe']=Evidence()
        fig=Figure();axes=fig.subplots(5);app=SimpleNamespace(ax_price=axes[0],panels=list(zip(('Объём','RSI','MACD','ATR'),axes[1:])))
        Evidence.copies=0
        ci.draw_partial(app,f,f,DEFAULT_IND,'gray','blue')
        self.assertEqual(Evidence.copies,0)

    def test_values_only_cannot_bypass_risk_gate(self):
        f=sample();f.attrs['accepted_risk']={'price_estimates':[{'timestamp':'2026-01-05'}]}
        with self.assertRaisesRegex(ValueError,'ESTIMATE_CONTEXT_UNCONFIRMED'):
            cp.analytical_close_series(f,values_only=True)

    def test_authorized_estimate_keeps_same_checks_and_numeric_result(self):
        f=sample();f=f.drop(f.index[40]);stamp=f.index[39]+pd.Timedelta(hours=12)
        left,right=f.index[39:41];ends=a.bar_ends(f)
        previous,following=float(f.close.loc[left]),float(f.close.loc[right])
        f.attrs['accepted_risk']={'context':cp.RISK_CONTEXT,'approved':True,
            'price_model':cp.PRICE_MODEL,'identity':{k:f.attrs[k] for k in cp.IDENTITY},
            'timeframe':'1D','checked_at':'2026-11-04T12:00Z',
            'price_estimates':[dict(timestamp=str(stamp),previous_at=str(left),next_at=str(right),
                previous_close=previous,next_close=following,value=previous/2+following/2,
                available_after=ends[40].isoformat())]}
        expected=cp.analytical_close_series(f)
        actual=cp.analytical_close_series(f,values_only=True)
        pd.testing.assert_series_equal(actual,expected,check_exact=True)
        self.assertEqual(len(actual),len(f)+1)
        for field,value in [('currency','USD'),('source','proxy')]:
            bad=f.copy();bad.attrs[field]=value
            with self.assertRaisesRegex(ValueError,'ESTIMATE_CONTEXT_UNCONFIRMED'):
                cp.analytical_close_series(bad,values_only=True)
        bad=f.copy();bad.loc[right,'close']+=1
        with self.assertRaisesRegex(ValueError,'ESTIMATE_INPUT_CHANGED'):
            cp.analytical_close_series(bad,values_only=True)

    def test_live_price_never_changes_closed_signal_inputs(self):
        f=sample();f.attrs.pop('archive_probe');f.attrs['bar_ends']={str(t):pd.Timestamp(t,tz='Europe/Berlin').tz_convert('UTC').isoformat() for t in f.index}
        now=pd.Timestamp(f.attrs['bar_ends'][str(f.index[-2])])
        _,original=a._analyse_features(f,now=now)
        changed=f.copy();changed.iloc[-1,changed.columns.get_loc('close')]=1e6;changed.iloc[-1,changed.columns.get_loc('high')]=1e6
        enriched,result=a._analyse_features(changed,now=now)
        self.assertEqual(result,original)
        self.assertEqual(result['excluded_open_bars'],1)
        self.assertNotEqual(float(enriched.ema30.iloc[-1]),float(a.indicators(f).ema30.iloc[-1]))

    def test_history_and_output_metadata_remain_independent(self):
        f=sample();before=pd.DataFrame(f,copy=True);saved=copy.deepcopy(f.attrs)
        out,result=a._analyse_features(f,now=pd.Timestamp('2026-11-04T12:00Z'))
        pd.testing.assert_frame_equal(pd.DataFrame(f),before,check_exact=True)
        self.assertEqual(f.attrs,saved)
        out.attrs['data_quality']['evidence'][0]='changed'
        self.assertEqual(f.attrs,saved)

    def test_levels_keep_last_closed_segment_and_exact_end(self):
        f=sample();f['chart_segment']=1;f.loc[f.index[-10:],'chart_segment']=2
        f.attrs['partial_indicators']={'blocked_reason':None}
        f.attrs['bar_ends']={str(t):(t.tz_localize('Europe/Berlin')+pd.Timedelta(hours=23)).isoformat() for t in f.index}
        got,_=pc.display_levels(f,pd.Timestamp('2026-11-04T12:00Z'))
        self.assertTrue(got['display_levels_historical'])
        self.assertEqual(got['display_levels_at'],f.attrs['bar_ends'][str(f.index[-11])])
        f.attrs['partial_indicators']['blocked_reason']='IDENTITY_NOT_PROVEN'
        self.assertEqual(pc.display_levels(f),( {},[]))

if __name__=='__main__':unittest.main()
