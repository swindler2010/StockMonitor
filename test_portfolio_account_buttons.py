"""Account navigation uses one reader and never changes the trading mode."""
from concurrent.futures import Future
from pathlib import Path
import sys
import tkinter as tk
from tkinter import ttk
from types import SimpleNamespace as NS
import unittest
from unittest.mock import Mock,patch
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
from portfolio_design import window
from paper_entry_modes import STRATEGY_CODES
import paper_view


def packet(book):
    mode=next(k for k,v in STRATEGY_CODES.items() if v==book)
    return dict(status='ACTIVE',account=book,equity='10000',cash='10000',fees='0',
                realized='0',unrealized='0',policy={'starting_cash':'10000'},positions=[],
                automation=dict(independent=True,strategy_mode=mode,enabled=False),journal={})


class AccountButtonTests(unittest.TestCase):
    def setUp(self):
        self.root=tk.Tk();self.root.withdraw();ttk.Style(self.root).theme_use('clam')
        self.errors=[];self.root.report_callback_exception=lambda k,v,t:self.errors.append(str(v))
        self.addCleanup(lambda:self.assertEqual(self.errors,[]))
        self.addCleanup(lambda:(self.root.update_idletasks(),self.root.destroy()))
        self.jobs=[]
        def job(work,done,failed=None,**options):
            self.jobs.append((work,done,failed,options));return True
        self.app=NS(root=self.root,store=NS(path=Path('unused.sqlite3'),setting=Mock(side_effect=lambda k,d=None:d)),
                    watchlist=[],symbol=tk.StringVar(self.root,''),job=job,closing=False)
        self.win=tk.Toplevel(self.root);self.win.geometry('1240x850+20000+20000')
        self.tabs=ttk.Notebook(self.win);self.tabs.pack(fill='both',expand=True)
        window(self.app,self.win,self.tabs)
        self.real=ttk.Frame(self.tabs);self.paper=ttk.Frame(self.tabs)
        self.tabs.add(self.real,text='V1');self.tabs.add(self.paper,text='Demo')
        self.choices=patch('paper_orders.order_choices',return_value={}).start()
        self.submit=patch('paper_orders.submit').start()
        paper_view.attach(self.app,self.paper)
        self.read=patch('paper_account.status',side_effect=lambda path,**kw:packet(kw['portfolio'])).start()
        self.addCleanup(patch.stopall)
        self.root.update_idletasks()

    def finish(self,index):
        work,done,failed,_=self.jobs[index];result=work();done(result);self.root.update_idletasks();return result

    def choose(self,book):self.win._portfolio_account_buttons[book].invoke()

    def widgets(self):
        def walk(w):return [w]+[a for child in w.winfo_children() for a in walk(child)]
        return walk(self.paper)

    def test_four_buttons_bind_existing_accounts_with_one_surface_and_no_writes(self):
        self.assertEqual(list(self.win._portfolio_account_buttons),['V1','V2','V3','V4','V5'])
        self.assertEqual(self.jobs,[])
        self.assertEqual(ttk.Style(self.win).layout(self.tabs.cget('style')+'.Tab'),[('null',{'sticky':'nswe'})])
        surface=self.paper._paper_design
        for n,book in enumerate(('V2','V3','V4','V5')):
            self.choose(book);self.assertEqual(len(self.jobs),n+1)
            self.finish(n)
            self.assertTrue(surface.title.get().endswith(book))
            self.assertEqual(self.read.call_args.kwargs['portfolio'],book)
            self.assertEqual(self.jobs[n][3]['lane'],'read')
            self.assertIs(self.paper._paper_design,surface)
        self.assertEqual(len(self.tabs.tabs()),2)
        self.assertEqual(self.app.store.setting.mock_calls[0].args,('theme','Тёмная'))

    def test_late_read_is_rejected_and_only_latest_button_is_reread(self):
        self.choose('V2');self.choose('V3');self.choose('V5')
        self.assertEqual(len(self.jobs),1)
        self.finish(0)
        self.assertEqual(len(self.jobs),2)
        self.assertTrue(self.paper._paper_design.title.get().endswith('V5'))
        self.finish(1)
        self.assertEqual(self.read.call_args.kwargs['portfolio'],'V5')
        self.assertTrue(self.paper._paper_design.title.get().endswith('V5'))

    def test_v1_does_not_schedule_paper_reads_and_unknown_routes_are_rejected(self):
        self.choose('V2');self.choose('V1');self.finish(0)
        self.assertEqual(len(self.jobs),1)
        self.assertEqual(self.tabs.select(),str(self.real))
        self.choose('V2');self.assertEqual(len(self.jobs),2)
        with self.assertRaises(ValueError):self.paper._paper_select_portfolio('V1')
        with self.assertRaises(ValueError):self.win._portfolio_choose_account('UNKNOWN')
        self.assertEqual(len(self.jobs),2)

    def test_wrong_account_response_never_replaces_requested_balance_or_enables_orders(self):
        self.choose('V2')
        self.jobs[0][1](packet('V4'))
        self.assertFalse(self.paper._paper_design.title.get().endswith('V4'))
        buttons=[w for w in self.widgets() if isinstance(w,ttk.Button) and w.cget('text') in ('BUY','SELL')]
        self.assertTrue(all('disabled' in w.state() for w in buttons))

    def test_owner_order_is_bound_to_clicked_account_and_navigation_does_not_execute(self):
        future=Future()
        self.choices.return_value={'US0378331005':'Apple'}
        self.submit.return_value=future
        submit=self.submit
        self.choose('V4');self.finish(0)
        submit.assert_not_called()
        next(w for w in self.widgets() if isinstance(w,ttk.Button) and w.cget('text')=='BUY').invoke()
        self.assertEqual(submit.call_args.args[1]['_paper_portfolio'],'V4')
        self.assertEqual(submit.call_args.args[2],'MANUAL')
        self.choose('V5')
        future.set_result(dict(status='REJECTED',code='PAPER_STALE_QUOTE'))
        self.root.after(160,self.root.quit);self.root.mainloop()
        self.assertEqual(submit.call_count,1)
        self.assertTrue(self.paper._paper_design.title.get().endswith('V5'))

if __name__=='__main__':unittest.main()
