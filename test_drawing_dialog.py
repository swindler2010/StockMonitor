import copy
import sys
import tkinter as tk
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
from drawing_dialog import show,TOOLS
from main_screen import theme
from themes import THEMES

class DrawingDialogTests(unittest.TestCase):
    def setUp(self):
        self.root=tk.Tk();self.root.withdraw();self.addCleanup(self.root.destroy)
        self.info=dict(isin='fixture-a',name='Fixture',drawings=[dict(type='hline',price=120,at='2026-11-12 08:00',currency='EUR',source_mode='GETTEX')])
        self.current=self.info;self.drafts={}
        self.app=SimpleNamespace(root=self.root,store=SimpleNamespace(setting=lambda key,default=None:default),
            instrument_cfg=lambda:self.current,user_state=lambda info:self.drafts.get(info['isin'],info),
            drafts=self.drafts,status=tk.StringVar(self.root),refresh_chart=Mock(),tooltip=Mock(),chart_meta={})
        theme(self.app,THEMES['Тёмная']);show(self.app);self.d=self.app._drawing_dialog;self.root.update()

    def select(self):self.d.objects.selection_set('0');self.root.update()

    def test_copy_delete_and_undo_use_original_draft_history(self):
        original=copy.deepcopy(self.info);self.select()
        self.assertGreaterEqual(self.d.objects.winfo_height(),60)
        self.d.actions['copy'].invoke();self.root.update()
        self.assertEqual(len(self.drafts['fixture-a']['drawings']),2)
        self.d.objects.selection_set('1');self.root.update();self.d.actions['delete'].invoke();self.root.update()
        self.assertEqual(len(self.drafts['fixture-a']['drawings']),1)
        self.d.history('undo');self.assertEqual(len(self.drafts['fixture-a']['drawings']),2)
        self.d.history('redo');self.assertEqual(len(self.drafts['fixture-a']['drawings']),1)
        self.assertEqual(self.info,original)

    def test_filtered_row_keeps_original_index_and_stale_stock_cannot_edit(self):
        self.info['drawings'].append(dict(type='marker',price=130,at='2026-11-12 09:00'))
        self.d.listed=copy.deepcopy(self.info['drawings']);self.d.query.set('130');self.root.update()
        self.assertEqual(self.d.objects.get_children(),('1',));self.d.objects.selection_set('1');self.root.update();self.current=dict(isin='fixture-b',name='Other',drawings=[])
        self.d.actions['copy'].invoke();self.d.tool_buttons['trend'].invoke()
        self.assertFalse(self.drafts);self.app.refresh_chart.assert_not_called()
        self.assertFalse(hasattr(self.app,'drawing_mode'));self.assertTrue(self.d.win.winfo_exists())

    def test_modal_rechecks_chart_identity_and_external_draft(self):
        self.select()
        def changed(*args,**kwargs):self.app.chart_meta={'source':'other'};return 121
        with patch('drawing_dialog.simpledialog.askfloat',side_effect=changed):self.d.change('price')
        self.assertFalse(self.drafts)
        self.app.chart_meta={};self.drafts['fixture-a']={**self.info,'drawings':[]}
        self.d.history('undo');self.assertEqual(self.drafts['fixture-a']['drawings'],[])
        self.app.refresh_chart.assert_not_called()

    def test_footer_actions_remain_inside_compact_window(self):
        self.d.win.geometry('900x620');self.root.update()
        for button in [*self.d.actions.values(),self.d.done_button]:
            bottom=button.winfo_rooty()-self.d.win.winfo_rooty()+button.winfo_height()
            self.assertLessEqual(bottom,self.d.win.winfo_height())
            self.assertGreater(button.winfo_height(),20)

    def test_group_switch_exposes_fibonacci_and_shapes_without_changing_drafts(self):
        self.d.tool_group.set('Фибоначчи');self.root.update()
        self.assertTrue(self.d.tool_buttons['fib'].winfo_ismapped())
        self.assertFalse(self.d.tool_buttons['hline'].winfo_ismapped())
        self.d.tool_group.set('Фигуры и аннотации');self.root.update()
        self.assertTrue(self.d.tool_buttons['circle'].winfo_ismapped())
        self.assertTrue(self.d.tool_buttons['text'].winfo_ismapped())
        self.assertFalse(self.d.tool_buttons['fib'].winfo_ismapped());self.assertFalse(self.drafts)

    def test_registered_tools_and_singleton_preserve_click_modes(self):
        same=show(self.app);self.assertIs(same,self.d.win)
        self.assertEqual(set(self.d.tool_buttons),{x[1] for x in TOOLS})
        self.d.tool_buttons['trend'].invoke();self.assertEqual(self.app.drawing_mode,'trend')
        self.assertIsNone(self.app.drawing_start);self.assertFalse(self.drafts)

if __name__=='__main__':unittest.main()
