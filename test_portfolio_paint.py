"""Portfolio repaint regression; isolated Tk widgets, no data writes or orders."""
import gc
from pathlib import Path
import sys
import tkinter as tk
from tkinter import ttk
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock,patch
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
from portfolio_design import Surface
from reference_design import Card
from themes import THEMES


class PortfolioPaintTests(unittest.TestCase):
    def setUp(self):
        self.root=tk.Tk();self.root.geometry('1240x850+20000+20000')
        ttk.Style(self.root).theme_use('clam')
        self.errors=[]
        self.root.report_callback_exception=lambda kind,value,tb:self.errors.append(str(value))
        self.addCleanup(lambda:self.assertEqual(self.errors,[]))
        self.addCleanup(lambda:(self.root.update_idletasks(),self.root.destroy()))
        self.app=NS(root=self.root,store=NS(setting=lambda k,d=None:d),
                    watchlist=[],symbol=tk.StringVar(self.root,''),job=Mock(),closing=False)

    def host(self):
        host=ttk.Frame(self.root);host.pack(fill='both',expand=True)
        return host

    def test_large_artwork_does_not_enlarge_cards_or_change_shared_dialog_style(self):
        from indicator_dialog import styles
        p=styles(self.root,THEMES['Тёмная']);style=ttk.Style(self.root)
        shared=style.layout(p+'.Info.TFrame')
        surface=Surface(self.app,self.host())
        old=Card(self.root,'Capital',p);new=surface.card(self.root,'Capital')
        old.pack();new.pack();self.root.update_idletasks()
        self.assertEqual((old.winfo_reqwidth(),old.winfo_reqheight()),
                         (new.winfo_reqwidth(),new.winfo_reqheight()))
        self.assertEqual(style.layout(p+'.Info.TFrame'),shared)
        photo=self.root._portfolio_card_images[surface.card_style]
        self.assertGreater(photo.width(),400)
        self.assertGreater(photo.height(),100)
        self.assertEqual(self.root.tk.call(str(photo),'get',256,64),(10,33,54))
        self.assertTrue(self.root.tk.call(str(photo),'transparency','get',0,0))

    def test_closing_one_surface_retains_other_card_images_and_values(self):
        host=self.host();one=Surface(self.app,host)
        first=self.root._portfolio_card_images[one.card_style]
        win=tk.Toplevel(self.root);win.geometry('450x240+20000+20000')
        second_host=ttk.Frame(win);second_host.pack(fill='both',expand=True)
        other=Surface(self.app,second_host)
        self.assertIs(first,self.root._portfolio_card_images[other.card_style])
        host.destroy();gc.collect()
        other.bind(dict(status='ACTIVE',account='V1',equity='10000',cash='9000',fees='2',policy={'starting_cash':'10000'}))
        self.root.update_idletasks()
        self.assertIn('9 000',other.values['cash'].get())
        self.assertIn(str(first),self.root.tk.call('image','names'))
        win.destroy()

    def test_actual_demo_keeps_order_controls_and_quantity_across_repaints(self):
        import paper_view
        host=self.host()
        with patch.object(paper_view,'_auto_refresh'),patch('paper_orders.submit') as submit:
            paper_view.attach(self.app,host)
            def walk(w):return [w]+[v for child in w.winfo_children() for v in walk(child)]
            widgets=walk(host)
            quantity=next(w for w in widgets if isinstance(w,ttk.Entry) and not isinstance(w,ttk.Combobox) and str(w.cget('state'))!='disabled')
            quantity.delete(0,'end');quantity.insert(0,'7')
            controls=[w for w in widgets if isinstance(w,ttk.Button) and w.cget('text') in ('BUY','SELL')]
            commands=[w.cget('command') for w in controls]
            for width in (900,1240,1024):
                self.root.geometry(f'{width}x850+20000+20000');self.root.update_idletasks()
            self.assertEqual(quantity.get(),'7')
            self.assertEqual([w.cget('command') for w in controls],commands)
            self.assertEqual(len(controls),2)
            cards=[w for w in widgets if isinstance(w,Card)]
            self.assertGreaterEqual(len(cards),6)
            self.assertTrue(all(w.cget('style')==host._paper_design.card_style for w in cards))
            submit.assert_not_called();self.app.job.assert_not_called()

if __name__=='__main__':unittest.main()
