import sys
import tkinter as tk
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch

sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
from features import Features
from main_screen import theme
from themes import THEMES


class InstrumentDialogTests(unittest.TestCase):
    def setUp(self):
        self.root=tk.Tk();self.root.withdraw();self.addCleanup(self.root.destroy)
        self.jobs=[]
        self.app=SimpleNamespace(root=self.root,store=SimpleNamespace(setting=lambda key,default=None:default),
            watchlist=[],symbol=tk.StringVar(self.root),symbol_box={},source_status=tk.StringVar(self.root),
            refresh_chart=Mock(),register_confirmed_instrument=Mock(),attach_logo=Mock(),
            job=lambda work,done,failed:self.jobs.append((work,done,failed)))
        theme(self.app,THEMES['Тёмная']);Features.add_stock_dialog(self.app)
        self.dialog=self.app._instrument_dialog;self.root.update()

    def begin(self):
        d=self.dialog;d.query.set('NVIDIA');d.find_button.invoke()
        _,found,_=self.jobs.pop(0)
        found([dict(symbol='NVD.DE',longname='NVIDIA',exchDisp='GETTEX',quoteType='EQUITY')])
        d.results.selection_set('0');self.root.update()
        self.assertEqual(str(d.add_button['state']),'disabled')
        return self.jobs.pop(0)[1]

    def evidence(self):
        return dict(status='CONFIRMED',name='NVIDIA',isin='US67066G1040',symbol='NVD.DE',venue='GETTEX',currency='EUR',evidence={'fixture':True})

    def test_confirmed_row_fields_feed_the_original_add_callback(self):
        done=self.begin();done(self.evidence());self.root.update()
        self.assertEqual(self.dialog.results.item('0','values')[:4],('NVD.DE','US67066G1040','GETTEX','EUR'))
        self.dialog.add_button.invoke();self.root.update()
        self.app.register_confirmed_instrument.assert_called_once()
        item=self.app.register_confirmed_instrument.call_args.args[0]
        self.assertEqual((item['isin'],item['venue'],item['currency']),('US67066G1040','GETTEX','EUR'))
        self.assertEqual((item['position'],item['levels'],item['drawings']),({},{},[]))
        self.app.refresh_chart.assert_called_once()

    def test_clear_invalidates_pending_resolution_and_does_not_admit_or_register(self):
        done=self.begin();self.dialog.clear_button.invoke();done(self.evidence());self.root.update()
        self.assertEqual(str(self.dialog.add_button['state']),'disabled')
        self.assertFalse(self.dialog.results.get_children())
        self.assertTrue(all(not v.get() for v in self.dialog.fields.values()))
        self.app.register_confirmed_instrument.assert_not_called()

    def test_edited_identity_and_unconfirmed_results_cannot_use_add(self):
        done=self.begin();done(self.evidence());self.root.update()
        self.dialog.fields['venue'].set('NASDAQ')
        with patch('features.messagebox.showerror') as error:self.dialog.add_button.invoke();self.assertTrue(error.called)
        self.app.register_confirmed_instrument.assert_not_called()
        self.app.refresh_chart.assert_not_called()


if __name__=='__main__':unittest.main()
