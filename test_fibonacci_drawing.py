import copy
from contextlib import closing
from pathlib import Path
import sqlite3
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).parent/'stock_monitor'))
import numpy as np
import pandas as pd
from matplotlib.figure import Figure
from drawing_fibonacci import fan, RATIOS, ZONES, ScreenFibArc
from drawing_tools import build, start, click
from drawing_renderer import draw
from drawing_actions import travel


class FibonacciTests(unittest.TestCase):
    def setUp(self):
        self.frame = pd.DataFrame({'close': np.linspace(100, 140, 40)}, index=pd.date_range('2026-11-12 08:00', periods=40, freq='15min'))
        self.frame.attrs.update(source='gettex', currency='EUR', venue='GETTEX', symbol='ABC')
        self.points = [dict(at=str(self.frame.index[i]), price=y) for i, y in ((0, 100), (10, 120))]
        self.info = dict(isin='fixture-A', name='Fixture', drawings=[], position={'quantity': 3})
        self.app = SimpleNamespace(current_df=self.frame, chart_meta={**self.frame.attrs, 'timeframe':'15m'}, drafts={},
                                   refresh_chart=Mock(), status=SimpleNamespace(set=Mock()))
        self.app.instrument_cfg = lambda: self.info
        self.app.user_state = lambda info: self.app.drafts.get(info['isin'], info)
        self.app.ax_price = Figure().subplots(); self.app.ax_price.set(xlim=(0, 40), ylim=(90, 160))

    def test_fan_ratios_up_down_and_invalid_calibration_are_explicit(self):
        up = {r: end for r, _, end in fan((0,100),(10,120),30)}
        down = {r: end for r, _, end in fan((0,120),(10,100),30)}
        self.assertEqual(up[.5], (30,130)); self.assertAlmostEqual(up[.618][1], 137.08)
        self.assertEqual(down[.5], (30,90)); self.assertEqual(len(up), 6)
        for point in ((0,120), (-1,120), (10,100)):
            with self.assertRaises(ValueError): fan((0,100), point, 30)

    def test_three_tools_render_without_changing_viewport_or_market_data(self):
        before = self.frame.copy(deep=True); limits = (self.app.ax_price.get_xlim(), self.app.ax_price.get_ylim())
        objects = [build(kind, self.points) for kind in ('fib_fan','fib_arcs','fib_timezones')]
        draw(self.app, objects, self.frame, '#00c5ff'); self.app.ax_price.figure.draw_without_rendering()
        self.assertEqual({index for _, index in self.app.drawing_artists}, {0,1,2})
        self.assertEqual((self.app.ax_price.get_xlim(), self.app.ax_price.get_ylim()), limits)
        pd.testing.assert_frame_equal(self.frame, before)
        self.assertEqual(len([p for p in self.app.ax_price.patches if isinstance(p,ScreenFibArc)]), 6)

    def test_arc_radius_and_semicircle_follow_data_through_pan_resize_zoom(self):
        ax = self.app.ax_price
        arc = ScreenFibArc(ax,(0,100),(10,120),.618)
        for bounds, size in [((0,40,90,160),(8,4)), ((5,20,95,130),(12,6))]:
            ax.figure.set_size_inches(*size); ax.set(xlim=bounds[:2], ylim=bounds[2:])
            a,b = ax.transData.transform([(0,100),(10,120)])
            # Center of the arc path is at angle zero: exactly B + .618*(A-B).
            center_point = arc.get_patch_transform().transform((1,0))
            np.testing.assert_allclose(center_point, b+.618*(a-b), atol=1e-9)
            radius = np.linalg.norm(a-b)*.618
            ends = arc.get_patch_transform().transform([(0,-1),(0,1)])
            np.testing.assert_allclose(np.linalg.norm(ends-b,axis=1),[radius,radius],atol=1e-9)

    def test_time_zones_use_fibonacci_bar_offsets_and_ignore_price_difference(self):
        points = [self.points[0], {**self.points[1], 'price':100}]
        draw(self.app, [build('fib_timezones', points)], self.frame, '#00c5ff')
        xs = [line.get_xdata()[0] for line in self.app.ax_price.lines]
        self.assertEqual(xs, [10*n for n in ZONES]); self.assertEqual(len(xs),11)
        self.assertNotIn('candles', build('fib_timezones', points))

    def test_log_scale_is_rejected_for_price_geometry_without_hiding_time_zones(self):
        self.app.ax_price.set_yscale('log')
        draw(self.app,[build('fib_arcs',self.points),build('fib_fan',self.points)],self.frame,'#00c5ff')
        self.assertFalse(self.app.ax_price.lines); self.assertFalse(self.app.ax_price.patches)
        self.assertTrue(any('линейная' in t.get_text() for t in self.app.ax_price.texts))
        draw(self.app,[build('fib_timezones',self.points)],self.frame,'#00c5ff')
        self.assertEqual(len(self.app.ax_price.lines),11)

    def test_capture_context_undo_and_hidden_locked_listing_guards_are_shared(self):
        start(self.app,'fib_arcs'); click(self.app,SimpleNamespace(xdata=0,ydata=100))
        self.app.chart_meta={**self.app.chart_meta,'source':'other'}
        click(self.app,SimpleNamespace(xdata=10,ydata=120)); self.assertFalse(self.app.drafts)
        self.app.chart_meta={**self.frame.attrs,'timeframe':'15m'}
        start(self.app,'fib_fan')
        for x,y in ((0,100),(10,120)): click(self.app,SimpleNamespace(xdata=x,ydata=y))
        objects=self.app.drafts['fixture-A']['drawings']; self.assertEqual(len(objects),1)
        draw(self.app,[{**objects[0],'venue':'OTHER'},{**objects[0],'visible':False},{**objects[0],'locked':True}],self.frame,'#00c5ff')
        self.assertEqual(len(self.app.ax_price.lines),7);self.assertFalse(self.app.drawing_artists)
        self.assertTrue(travel(self.app,'fixture-A','undo'));self.assertFalse(self.app.drafts['fixture-A']['drawings'])

    def test_anchor_direction_and_translated_descriptions(self):
        for kind in ('fib_fan','fib_arcs','fib_timezones'):
            with self.assertRaises(ValueError):build(kind,list(reversed(self.points)))
        with patch('i18n._language','de'):
            start(self.app,'fib_arcs');self.assertIn('Bögen',self.app.status.set.call_args.args[0])
        with patch('i18n._language','en'):
            start(self.app,'fib_timezones');self.assertIn('Time zones',self.app.status.set.call_args.args[0])

    def test_real_save_reopen_preserves_all_three_objects_and_existing_position(self):
        from storage import Store
        archive=Path(__file__).resolve().parents[2]/'СМ-Архив'
        file=tempfile.NamedTemporaryFile(prefix='Fibonacci drawing test ',suffix='.sqlite3',dir=archive if archive.is_dir() else None,delete=False)
        path=Path(file.name);file.close();self.addCleanup(lambda:path.unlink(missing_ok=True))
        with closing(sqlite3.connect(path)) as db:
            db.execute('CREATE TABLE instruments(isin TEXT PRIMARY KEY,payload TEXT)');db.execute('CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT)')
        objects=[build(kind,self.points) for kind in ('fib_fan','fib_arcs','fib_timezones')]
        store=Store.__new__(Store);store.path=path;store.pending_user_settings={};store.save_user_state([{**self.info,'drawings':objects}])
        reopened=Store.__new__(Store);reopened.path=path;saved=reopened.instruments()[0]
        self.assertEqual(saved['drawings'],objects);self.assertEqual(saved['position'],self.info['position'])


class FibonacciDialogTests(unittest.TestCase):
    def setUp(self):
        from test_drawing_dialog import DrawingDialogTests
        DrawingDialogTests.setUp(self)

    def test_group_menu_and_new_tools_are_localized_without_changing_canonical_group(self):
        from drawing_dialog import show
        for code, expected in [('de','Fibonacci-Fächer'),('en','Fibonacci fan')]:
            self.d.win.destroy()
            with patch('i18n._language',code):
                show(self.app);self.d=self.app._drawing_dialog
                self.d.group_box.set('Фибоначчи');self.root.update()
                self.assertEqual(self.d.tool_group.get(),'Фибоначчи')
                self.assertEqual(self.d.group_box._display.get(),'Fibonacci')
                self.assertEqual(self.d.tool_buttons['fib_fan'].cget('text').replace('\n',' '),expected)
                self.assertFalse(self.drafts)

if __name__=='__main__':unittest.main()
