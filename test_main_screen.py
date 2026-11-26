"""Isolated real Tk layout: no live database, workers, quotes or PAPER orders."""
import sys
import json
import sqlite3
import tempfile
from contextlib import closing
import tkinter as tk
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).parent / 'stock_monitor'))
from main_screen import build, theme, cached_quotes, right_axes, group_match
from themes import THEMES
from timeframes import LABELS
from trading_view import bind_engine
from benchmark_ui import BenchmarkMixin


class LayoutBase:
    def build_ui(self):
        self._main_menu = tk.Menu(self.root)
        for title in ('Файл','Вид','Данные','Инструменты','CandlePilot','Торговля','Настройки','Справка'):
            menu = tk.Menu(self._main_menu, tearoff=False)
            menu.add_command(label='Test callback', command=self.show_card)
            self._main_menu.add_cascade(label=title, menu=menu)
        build(self, {'Max': None, 'год': 12}, '#0b1220', '#e5e7eb')


class LayoutApp(BenchmarkMixin, LayoutBase):
    """Exercise the actual final MRO's benchmark extension, not just the builder."""


class MainScreenTests(unittest.TestCase):
    def setUp(self):
        self.root = tk.Tk()
        self.root.title('Stock Monitor isolated layout test')
        self.root.geometry('1422x912+50+50')
        self.addCleanup(self.root.destroy)
        infos = [dict(isin='A', name='Apple', symbol='APC', currency='EUR'),
                 dict(isin='N', name='NVIDIA', symbol='NVD', currency='EUR')]
        self.app = LayoutApp()
        self.app.__dict__.update(dict(root=self.root, watchlist=infos, closing=False,
            store=SimpleNamespace(setting=Mock(side_effect=lambda key, default=None: default)),
            symbol=tk.StringVar(self.root, 'Apple'), timeframe=tk.StringVar(self.root, '1H'),
            period=tk.StringVar(self.root, 'Max'), source_status=tk.StringVar(self.root, 'GETTEX / EUR'),
            status=tk.StringVar(self.root, 'TEST'), ai_status=tk.StringVar(self.root, 'Диагностика')))
        for name in ('show_watchlist', 'show_indicators', 'show_drawing', 'show_news', 'show_portfolio',
                     'show_settings', 'save_user_changes', 'show_card', 'add_stock_dialog', 'refresh_chart',
                     'change_period', 'change_chart_mode', 'start_measurement', 'reset_view',
                     'toggle_chart_fullscreen', 'refresh_all_async', 'escape_chart', 'show_ai',
                     'on_motion', 'on_leave', 'on_press', 'on_release', 'on_scroll',
                     '_update_candlepilot', 'tooltip', 'job'):
            setattr(self.app, name, Mock())
        self.app.build_ui()
        self.root.update()
        self.screen = self.app.main_screen

    def test_all_tf_and_stock_selection_reuse_existing_callbacks_without_settings_writes(self):
        self.assertEqual(set(self.screen.tf_buttons), set(LABELS))
        for tf, button in self.screen.tf_buttons.items():
            self.app.refresh_chart.reset_mock()
            changed = tf != self.app.timeframe.get()
            button.invoke()
            self.root.update()
            self.assertEqual(self.app.timeframe.get(), tf)
            self.assertEqual(self.app.refresh_chart.call_count, int(changed))
            bind_engine(self.app,dict(status='READY',signal='WAIT',confidence=50))
            self.assertIn('('+tf+')',self.screen.signal_caption.get())
        self.screen.tree.selection_set('N')
        self.root.update()
        self.assertEqual(self.app.symbol.get(), 'NVIDIA')
        self.assertEqual(self.app.instrument.get(), 'NVIDIA')
        self.assertEqual(self.app.metrics['price'].get(), '—')
        self.app.refresh_chart.assert_called()
        self.assertEqual(self.app.store.setting.call_args_list[0].args[0], 'theme')
        # Read-only store double deliberately provides no set_setting/account API.

    def test_filter_order_new_stock_and_unknown_data_do_not_invent_readiness(self):
        self.screen.query.set('NVD')
        self.root.update()
        self.assertEqual(self.screen.tree.get_children(), ('N',))
        self.app.watchlist.append(dict(isin='B', name='Broadcom', symbol='AVGO'))
        self.screen.query.set('')
        self.root.update()
        self.assertEqual(self.screen.tree.get_children(), ('A', 'N', 'B'))
        self.assertEqual(self.app.metrics['price'].get(), '—')
        bind_engine(self.app, dict(signal='WAIT', status='READY', confidence=62, timeframe_context={'4H': {'facts': {'regime': 'BULLISH'}}}))
        self.assertEqual(float(self.screen.progress['value']), 62)
        self.assertIn('Рост', self.screen.context_vars['4H'].get())
        bind_engine(self.app, None)
        self.assertEqual(float(self.screen.progress['value']), 0)
        self.assertEqual(self.screen.context_vars['4H'].get(), '4H\n—')

    def test_delayed_news_cannot_cross_selected_identity(self):
        self.app.metrics['price'].set('100.00 EUR')
        self.screen.show_news('A',{'articles':[{'title':'Old Apple news'}]})
        self.app.symbol.set('NVIDIA')
        self.root.update()
        self.assertEqual(self.app.metrics['price'].get(), '—')
        self.assertFalse(self.screen.news_tree.get_children())
        before = self.screen.news_text.get()
        self.screen.show_news('A', {'articles': [{'title': 'wrong stock'}]})
        self.assertEqual(self.screen.news_text.get(), before)
        self.screen.show_news('N', {'articles': [{'title': 'NVIDIA news', 'provider': 'Test'}]})
        self.assertIn('NVIDIA news', str(self.screen.news_tree.item(self.screen.news_tree.get_children()[0], 'values')))

    def test_menu_clones_preserve_callbacks_and_signal_percent_is_not_invented(self):
        button = self.screen.menu_buttons[0]
        menu = self.root.nametowidget(button['menu'])
        self.assertEqual(menu.winfo_parent(), str(button))
        self.app.show_card.reset_mock()
        menu.invoke(0)
        self.app.show_card.assert_called_once()
        bind_engine(self.app, dict(status='READY',signal='WAIT',confidence=62))
        self.assertEqual(self.screen.strength.get(), '62%')
        bind_engine(self.app, None)
        self.assertEqual(self.screen.strength.get(), '—')
        self.assertRegex(self.screen.clock_text.get(), r'\d{2}\.\d{2}\.\d{4} \d{2}:\d{2}:\d{2}')

    def test_price_scale_is_right_and_collections_never_change_the_universe(self):
        axes = self.app.ax_price
        axes.set_xlim(2,5)
        axes.set_ylim(10,20)
        right_axes([axes, self.app.ax_vol])
        self.assertEqual(axes.yaxis.get_ticks_position(), 'right')
        self.assertEqual(axes.yaxis.get_label_position(), 'right')
        self.assertEqual(axes.get_xlim(), (2,5))
        self.assertEqual(axes.get_ylim(), (10,20))
        original = list(self.app.watchlist)
        self.screen.group_buttons['semis'].invoke()
        self.root.update()
        self.assertEqual(self.screen.tree.get_children(), ('N',))
        self.assertEqual(self.app.watchlist, original)
        self.assertFalse(group_match(dict(isin='INDEX:DAX',name='DAX',kind='benchmark'), 'all'))

    def test_favorite_persists_only_a_ui_key_and_does_not_select_or_trade_stock(self):
        self.screen._favorites_loaded=True
        self.app.store.set_setting=Mock()
        self.app.job.side_effect=lambda work,done,*args,**kwargs:done(work())
        selected=self.app.symbol.get();universe=list(self.app.watchlist)
        self.screen.toggle_favorite('N')
        self.root.update()
        self.app.store.set_setting.assert_called_once_with('ui_favorites',['N'])
        self.assertEqual(self.screen.tree.item('N','values')[2], '★')
        self.assertEqual(self.app.symbol.get(),selected)
        self.assertEqual(self.app.watchlist,universe)

    def test_camera_exports_rendered_chart_without_changing_view_or_settings(self):
        archive=Path(__file__).resolve().parent.parent.parent/'СМ-Архив'
        with tempfile.NamedTemporaryFile(suffix='.png',dir=archive if archive.is_dir() else None,delete=False) as temporary:path=Path(temporary.name)
        self.addCleanup(path.unlink)
        self.app.ax_price.set_xlim(2,5);self.app.canvas.draw()
        self.app.job.side_effect=lambda work,done,*args,**kwargs:done(work())
        with patch('tkinter.filedialog.asksaveasfilename',return_value=str(path)):
            self.screen.export_chart()
        self.assertTrue(path.read_bytes().startswith(b'\x89PNG\r\n\x1a\n'))
        self.assertEqual(self.app.ax_price.get_xlim(),(2,5))

    def test_level_checkbox_uses_existing_cas_for_only_the_display_setting(self):
        current=dict(settings={'Уровни анализа':True},revision='expected',unsaved_local_changes=False,write_in_progress=False)
        self.app.store.path='not-opened-by-this-test'
        self.app.job.side_effect=lambda work,done,*args,**kwargs:done(work())
        self.screen.levels_visible.set(False)
        with patch('indicator_controls.snapshot',return_value=current),patch('indicator_controls.persist',return_value={'saved':True}) as persist:
            self.screen.toggle_levels()
        self.assertEqual(persist.call_args.args[:3],('not-opened-by-this-test','expected',{'Уровни анализа':False}))
        self.app.refresh_chart.assert_called_once()

    def test_fullscreen_minimum_size_and_theme_preserve_canvas_and_viewport(self):
        canvas = self.app.canvas
        axes = self.app.ax_price
        axes.set_xlim(2, 5)
        self.screen.fullscreen(True)
        self.root.update()
        self.assertFalse(self.screen.sidebar.winfo_ismapped())
        self.screen.fullscreen(False)
        self.root.geometry('1000x680')
        self.root.update()
        self.assertTrue(self.screen.sidebar.winfo_ismapped())
        self.assertGreater(canvas.get_tk_widget().winfo_width(), 300)
        self.assertGreater(canvas.get_tk_widget().winfo_height(), 200)
        for palette in THEMES.values():
            theme(self.app, palette)
            self.root.update()
        self.assertIs(self.app.canvas, canvas)
        self.assertIs(self.app.ax_price, axes)
        self.assertEqual(axes.get_xlim(), (2, 5))
        self.assertTrue(self.screen.tf_buttons['1H'].winfo_ismapped())
        self.assertTrue(self.app.period_box.winfo_ismapped())


class SidebarQuoteTests(unittest.TestCase):
    def test_archive_quote_uses_exact_receipt_identity_and_never_updates_the_database(self):
        archive = Path(__file__).resolve().parent.parent.parent / 'СМ-Архив'
        with tempfile.NamedTemporaryFile(suffix='.sqlite3', dir=archive if archive.is_dir() else None, delete=False) as temporary:
            path = Path(temporary.name)
        self.addCleanup(path.unlink)
        with closing(sqlite3.connect(path)) as db:
            db.execute('CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT)')
            db.execute('CREATE TABLE series(isin,source,venue,currency,symbol,base_tf,ts,close,PRIMARY KEY(isin,source,venue,currency,symbol,base_tf,ts))')
            identity = dict(source='gettex_chart', venue='GETTEX', currency='EUR', symbol='TEST.GTX')
            db.execute('INSERT INTO settings VALUES (?,?)', ('gettex_chart:A:15m', json.dumps({'identity': identity})))
            for row in [('A','gettex_chart','GETTEX','EUR','TEST.GTX','15m','2026-11-20T10:00',100),
                        ('A','gettex_chart','GETTEX','EUR','TEST.GTX','15m','2026-11-20T10:15',101),
                        ('A','gettex_chart','OTHER','EUR','TEST.GTX','15m','2026-11-20T10:30',999),
                        ('A','gettex_chart','GETTEX','EUR','OTHER.GTX','15m','2026-11-20T10:30',888)]:
                db.execute('INSERT INTO series VALUES (?,?,?,?,?,?,?,?)', row)
            db.commit()
        before = path.read_bytes()
        self.assertEqual(cached_quotes(path, ('A','unknown')), {'A': ('101.00','+1.00')})
        self.assertEqual(path.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
