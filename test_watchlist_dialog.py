import sys
import tkinter as tk
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
from watchlist_dialog import Dialog,matches,counts
from watchlist_order import display_details
from main_screen import theme
from themes import THEMES


class WatchlistDialogTests(unittest.TestCase):
    def setUp(self):
        self.root=tk.Tk();self.root.withdraw();self.addCleanup(self.root.destroy)
        infos=[dict(isin='A',name='Apple',symbol='APC',currency='EUR'),dict(isin='N',name='NVIDIA',symbol='NVD',currency='EUR')]
        rows={'A':dict(tier=0,label='Деморежим 1H: WAIT',timeframes=['1H'],venues=['GETTEX']),
              'N':dict(tier=2,label='Анализ доступен: 4H',timeframes=['4H'],venues=['GETTEX'])}
        self.app=SimpleNamespace(root=self.root,store=SimpleNamespace(setting=lambda key,default=None:default),
            watchlist=infos,symbol=tk.StringVar(self.root,'Apple'))
        self.app.stock_order=SimpleNamespace(rows=rows,ordered=lambda:list(self.app.watchlist),refresh=Mock())
        for name in ('show_indicators','show_news','show_portfolio','show_settings','add_stock_dialog','refresh_chart','attach_logo','tooltip'):
            setattr(self.app,name,Mock())
        theme(self.app,THEMES['Тёмная'])
        self.dialog=Dialog(self.app);self.root.update()

    def test_filters_and_sort_only_change_display_and_select_reuses_existing_chart_callback(self):
        d=self.dialog;self.assertEqual(d.tree.get_children(),('A','N'))
        d.query.set('NVD');self.root.update()
        self.assertEqual(d.tree.get_children(),('N',));self.assertEqual(self.app.symbol.get(),'Apple')
        d.tree.selection_set('N');self.root.update();d.select()
        self.assertEqual(self.app.symbol.get(),'NVIDIA');self.app.refresh_chart.assert_called_once()
        self.assertEqual([i['isin'] for i in self.app.watchlist],['A','N'])

    def test_refresh_handles_new_and_removed_stocks_and_closed_window_callback(self):
        d=self.dialog
        artwork=tk.PhotoImage(master=self.root,width=2,height=2)
        self.app.main_screen=SimpleNamespace(logo_images={'A':artwork})
        d.render()
        self.assertEqual(d.tree.item('A','image'),(str(artwork),))
        self.app.watchlist.append(dict(isin='B',name='Broadcom',symbol='AVGO',currency='EUR'))
        d.render();self.assertTrue(d.tree.exists('B'))
        d.query.set('Apple');self.root.update()
        self.app.watchlist=[self.app.watchlist[0]];d.render()
        self.assertFalse(d.tree.exists('N'));self.assertFalse(d.tree.exists('B'))
        d.close();d.render();self.app.refresh_chart.assert_not_called()

    def test_unknown_status_is_not_ready_and_fields_come_only_from_valid_metadata(self):
        d=self.dialog
        self.assertEqual(tuple(v.get() for v in d.stats),('2','1','0','1','0'))
        record=dict(checked_at='2026-11-20T20:00:00+00:00',timeframes={'1H':dict(chart_ready=True,identity=dict(venue='GETTEX',source='gettex_chart'))})
        self.assertEqual(display_details(record,'STALE',[])['timeframes'],[])
        self.assertEqual(display_details(record,'CURRENT',[])['timeframes'],['1H'])
        self.assertEqual(display_details(record,'STALE',[])['venues'],['GETTEX'])
        self.assertFalse(matches(dict(isin='X',name='Unknown'),{},state='Готовы'))
        self.assertEqual(counts([dict(isin='X')],{}),(1,0,1,0,0))


if __name__=='__main__':unittest.main()
