import sys, unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
import numpy as np
import pandas as pd
import matplotlib
matplotlib.use('Agg')
from matplotlib.figure import Figure
from matplotlib.backends.backend_agg import FigureCanvasAgg
from chart_modes import MODES,draw_prices
from significant_levels import zones
from dividends import total_return,parse_receipts,model_returns
from unittest.mock import Mock,patch
import tempfile
from logos016 import fetch,placeholder
from extras016 import Extras016

def history(n=400):
    price=100+10*np.sin(np.arange(n)/10)
    return pd.DataFrame(dict(open=price,high=price+2,low=price-2,close=price+1,volume=100),index=pd.date_range('2025-01-01',periods=n))

class ExtrasTests(unittest.TestCase):
    def test_fullscreen_restore_waits_for_final_geometry(self):
        obj=Extras016();obj.root=Mock();obj.chart_body=Mock();obj.chart_side=Mock();obj.chart_outer=Mock();obj.canvas=Mock()
        obj.chart_outer.pack_slaves.return_value=[]
        obj.chart_body.sashpos.return_value=1000
        obj.chart_body.winfo_width.return_value=1250
        obj.toggle_chart_fullscreen()
        obj.chart_body.sashpos.reset_mock()
        obj.toggle_chart_fullscreen()
        obj.chart_body.sashpos.assert_not_called()
        resize=obj.chart_body.bind.call_args.args[1]
        old_timer=obj._sash_restore_timer
        resize()
        obj.root.after_cancel.assert_called_with(old_timer)
        obj.chart_body.winfo_width.return_value=1900
        obj.root.after.call_args.args[1]()
        obj.chart_body.sashpos.assert_not_called()
        obj.chart_body.winfo_width.return_value=1250
        obj.root.after.call_args.args[1]()
        obj.chart_body.sashpos.assert_called_once_with(0,1000)
        self.assertFalse(obj.chart_fullscreen)
        obj.chart_body.unbind.assert_called_once()

    def test_logo_failure_cached_and_placeholder(self):
        with tempfile.TemporaryDirectory() as directory,patch('yfinance.Ticker',side_effect=RuntimeError('offline')) as request:
            self.assertIsNone(fetch('TEST',directory))
            self.assertIsNone(fetch('TEST',directory))
            request.assert_called_once()
        self.assertEqual(placeholder('Apple').size,(24,24))

    def test_weekly_dividend_last_bucket(self):
        obj=Extras016();obj.timeframe=Mock();obj.timeframe.get.return_value='1W'
        obj.dividends_for=lambda _:dict(events=[dict(date='2026-01-08',amount=1,currency='EUR')])
        fig=Figure();obj.ax_price=fig.subplots()
        frame=history(2);frame.index=pd.to_datetime(['2025-12-29','2026-01-05'])
        obj.draw_dividends({},frame)
        self.assertEqual(len(obj.dividend_markers),1)
        self.assertEqual(obj.dividend_markers[0][0].get_position()[0],1)

    def test_receipts_validation(self):
        self.assertEqual(parse_receipts('2026-01-01 12,50 EUR')[0]['amount'],12.5)
        for text in ('2026-01-01 nan EUR','2026-01-01 -1 EUR','bad 12 EUR'):
            with self.assertRaises(ValueError):parse_receipts(text)

    def test_model_uses_pre_ex_date_holdings(self):
        row={'pnl':10,'identity':{'currency':'EUR','timezone':'Europe/Berlin'},'fills':[
            {'at':'2026-01-01T10:00:00Z','signal':'BUY','quantity':3},
            {'at':'2026-01-02T10:00:00Z','signal':'ADD','quantity':7}]}
        data={'complete':True,'start':'2025-01-01','end':'2099-01-01','events':[{'date':'2026-01-02','amount':2,'currency':'EUR'}]}
        self.assertEqual(model_returns(row,data)['dividends'],6)
        data['start']='2026-01-02'
        self.assertIsNone(model_returns(row,data)['dividends'])

    def test_daily_levels_request_same_identity(self):
        obj=Extras016();obj.store=Mock();obj.store.load_series.return_value=history()
        f=history();f.attrs=dict(source='proxy',venue='GER',currency='EUR',symbol='APC.DE')
        obj.level_zones({'isin':'test'},f)
        obj.store.load_series.assert_called_once_with('test','1D','proxy','APC.DE','GER','EUR')
        obj.store.load_official.assert_not_called()

    def test_fullscreen_restore_without_refresh(self):
        obj=Extras016();obj.root=Mock();obj.root.attributes.return_value=False
        obj.root.after_idle.side_effect=lambda f:f()
        obj.chart_body=Mock();obj.chart_body.sashpos.return_value=720
        obj.chart_body.winfo_width.return_value=1000
        obj.chart_side=Mock();obj.canvas=Mock();obj.chart_outer=Mock()
        controls=Mock();controls.winfo_manager.return_value='pack';controls.pack_info.return_value={'side':'top'}
        obj.chart_outer.pack_slaves.return_value=[controls]
        obj.toggle_chart_fullscreen();self.assertTrue(obj.chart_fullscreen)
        obj.escape_chart();self.assertFalse(obj.chart_fullscreen)
        obj.root.after.call_args.args[1]()
        controls.pack.assert_called_with(side='top');obj.chart_body.sashpos.assert_called_with(0,720)

    def test_chart_modes_render_with_overlays(self):
        for mode in MODES:
            with self.subTest(mode=mode):
                fig=Figure();canvas=FigureCanvasAgg(fig);ax=fig.subplots()
                draw_prices(ax,history(30),mode)
                ax.axhspan(95,96);ax.plot([3,8],[95,105]);ax.text(12,100,'D')
                canvas.draw()
                self.assertLess(ax.get_xlim()[0],0)
                self.assertGreater(ax.get_xlim()[1],29)
                self.assertEqual(len(ax.lines),2 if 'Line'==mode or 'Close line' in mode else 1)

    def test_levels_bounded_and_historical(self):
        result=zones(history())
        self.assertTrue(result)
        self.assertTrue(any(z['historical'] for z in result))
        for side in ('support','resistance'):
            self.assertLessEqual(sum(z['side']==side for z in result),3)
        self.assertTrue(all(z['low']<=z['high'] for z in result))

    def test_broken_support_removed(self):
        f=history();f.loc[f.index[-12:],['open','high','low','close']]=[50,52,48,51]
        self.assertFalse(any(z['side']=='support' and z['low']>60 for z in zones(f)))

    def test_unknown_dividends_not_zero(self):
        self.assertIsNone(total_return(10,[],lambda _:2,'EUR')['total_return'])
        event=[dict(date='2026-01-01',amount=2,currency='USD')]
        self.assertIsNone(total_return(10,event,lambda _:2,'EUR',True)['dividends'])
        event[0]['currency']='EUR'
        self.assertIsNone(total_return(10,event,lambda _:None,'EUR',True)['dividends'])
        self.assertEqual(total_return(10,event,lambda _:3,'EUR',True)['total_return'],16)
        self.assertEqual(total_return(10,[],lambda _:3,'EUR',True)['dividends'],0)

if __name__=='__main__':unittest.main()
