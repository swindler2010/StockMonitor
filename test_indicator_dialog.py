"""Actual redesigned dialog plus the existing apply callback; no live database."""
import copy
import sys
import tkinter as tk
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch

sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
from desktop014 import Desktop014
from indicator_controls import DEFAULT_IND
from indicator_dialog import styles
from themes import THEMES


class IndicatorDialogTests(unittest.TestCase):
    def setUp(self):
        self.root=tk.Tk();self.root.withdraw();self.addCleanup(self.root.destroy)
        self.saved=copy.deepcopy(DEFAULT_IND)
        self.writes=[]
        def setting(key,default=None):
            return copy.deepcopy(self.saved) if key=='global_indicators' else default
        def save(key,value):self.writes.append((key,copy.deepcopy(value)));self.saved.update(value)
        self.app=SimpleNamespace(root=self.root,store=SimpleNamespace(setting=setting,set_setting=save),refresh_chart=Mock())
        Desktop014.show_indicators(self.app)
        self.dialog=self.app._indicator_dialog
        self.root.update()

    def test_existing_apply_receives_visible_parameters_and_preserves_unedited_values(self):
        self.dialog.values['SMA'].set(True)
        self.dialog.values['sma'].set('21')
        self.dialog.apply_button.invoke();self.root.update()
        expected={**DEFAULT_IND,'SMA':True,'sma':21}
        self.assertEqual(self.writes,[('global_indicators',expected)])
        self.app.refresh_chart.assert_called_once()
        self.assertFalse(self.dialog.win.winfo_exists())

    def test_invalid_and_concurrent_edits_still_reject_and_defaults_are_only_a_draft(self):
        with patch('desktop014.messagebox.showerror') as error:
            self.dialog.values['macd_fast'].set('40')
            self.dialog.apply_button.invoke()
            self.assertTrue(error.called);self.assertFalse(self.writes)
            self.dialog.defaults_button.invoke()
            self.assertFalse(self.writes)
            self.app._indicator_write_busy=True
            self.dialog.apply_button.invoke()
            self.assertFalse(self.writes)
            self.app._indicator_write_busy=False
            self.saved['ema_fast']=11
            self.dialog.apply_button.invoke()
            self.assertFalse(self.writes)
        self.app.refresh_chart.assert_not_called()

    def test_small_window_keeps_footer_visible_and_reveals_last_field_without_saving(self):
        self.dialog.win.geometry('500x500');self.root.update()
        self.dialog.fields['Уровни анализа'].focus_force()
        self.dialog.reveal(self.dialog.fields['Уровни анализа']);self.root.update()
        self.assertGreater(self.dialog.canvas.yview()[0],0)
        self.assertTrue(self.dialog.apply_button.winfo_ismapped())
        self.assertLess(self.dialog.actions.winfo_y()+self.dialog.actions.winfo_height(),self.dialog.win.winfo_height())
        self.dialog.win.event_generate('<Escape>');self.root.update()
        self.assertFalse(self.writes)

    def test_styles_are_scoped_and_reused_for_each_supported_theme(self):
        for palette in THEMES.values():
            self.assertEqual(styles(self.root,palette),styles(self.root,palette))
        self.assertEqual(len(self.root._indicator_dialog_styles),3)
        self.assertFalse(self.writes)


if __name__=='__main__':unittest.main()
