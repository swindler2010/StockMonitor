"""Accounting boundaries and unchanged broker/market/PAPER records."""
import copy
import json
import sqlite3
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
import real_portfolio as real


def packet():
    observed=(datetime.now(timezone.utc)-timedelta(hours=1)).isoformat()
    return dict(observed_at=observed,
        holdings=dict(portfolioId='owner',holdings=[dict(isin='US0378331005',name='Apple',
            fifoPrice=10,valuation=24,valuationCurrency='EUR',
            currentQuote=dict(currency='EUR',midPrice=12,timestamp=observed),
            position=dict(filled=2,blocked=0,pending=0))]),
        cash=dict(portfolioId='owner',cash=dict(cashBalance=-4,buyingPowerWithoutCredit=0,buyingPower=20),
                  credit=dict(loaned=4,availableCreditLine=24)),
        overview=dict(portfolioId='owner',valuation=dict(total=20,securities=24,crypto=0),timestamps={},performance=[]),
        transactions=[dict(id='cancelled-1',status='CANCELLED',kind='security',security=dict(side='BUY',quantity='90'))],
        transactions_complete=True)


class RealAccountTests(unittest.TestCase):
    def setUp(self):
        self.db=sqlite3.connect(':memory:')
        self.db.execute('CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT)')
        self.db.execute('CREATE TABLE instruments(isin TEXT PRIMARY KEY,payload TEXT)')
        self.db.execute('CREATE TABLE paper_portfolio_fills(id TEXT,payload TEXT)')
        self.db.execute('CREATE TABLE candles(isin TEXT,close REAL)')
        self.db.execute('INSERT INTO paper_portfolio_fills VALUES (?,?)',('legacy','unchanged'))
        self.db.execute('INSERT INTO candles VALUES (?,?)',('US0378331005',12))
        self.db.execute('INSERT INTO settings VALUES (?,?)',('paper_auto_1h','{"enabled":true}'))
        self.packet=packet()
        self.state=real.save_import(self.db,self.packet)

    def tearDown(self):self.db.close()

    def event(self,kind='BUY',**extra):
        return dict(id='real-event-0001',at=(datetime.now(timezone.utc)-timedelta(minutes=10)).isoformat(),
            kind=kind,isin='US0378331005',quantity='1',price='11',fee='1',confirmed_execution=True,**extra)

    def test_real_negative_cash_and_cancelled_history_are_not_paper_fills(self):
        s=real.read(self.db)
        self.assertEqual(s['cash'],'-4.00');self.assertEqual(s['positions'][0]['quantity'],'2')
        self.assertFalse(s['execution_permission']);self.assertFalse(s['broker_orders'])
        self.assertEqual(s['broker_transactions'][0]['status'],'CANCELLED')
        self.assertEqual(self.db.execute('SELECT * FROM paper_portfolio_fills').fetchall(),[('legacy','unchanged')])
        self.assertEqual(self.db.execute('SELECT close FROM candles').fetchone()[0],12)
        self.assertEqual(self.db.execute('SELECT value FROM settings WHERE key=?',('paper_auto_1h',)).fetchone()[0],'{"enabled":true}')

    def test_import_repeated_exactly_does_not_reset_or_duplicate(self):
        self.assertEqual(real.save_import(self.db,self.packet),self.state)
        self.assertEqual(real.read(self.db)['revision'],1)

    def test_no_missing_or_nonfinite_cash_is_assumed_zero(self):
        for bad in (None,float('nan'),float('inf'),True):
            p=copy.deepcopy(self.packet);p['cash']['cash']['cashBalance']=bad
            with self.subTest(bad=bad),self.assertRaises(ValueError):real.prepare(p)

    def test_identity_currency_future_and_duplicate_fail_closed(self):
        variants=[]
        p=copy.deepcopy(self.packet);p['cash']['portfolioId']='someone-else';variants.append(p)
        p=copy.deepcopy(self.packet);p['holdings']['holdings'][0]['currentQuote']['currency']='USD';variants.append(p)
        p=copy.deepcopy(self.packet);p['holdings']['holdings'][0]['currentQuote']['timestamp']=(datetime.now(timezone.utc)+timedelta(days=1)).isoformat();variants.append(p)
        p=copy.deepcopy(self.packet);p['holdings']['holdings']*=2;variants.append(p)
        p=copy.deepcopy(self.packet);p['transactions']*=2;variants.append(p)
        p=copy.deepcopy(self.packet);p['transactions_complete']=False;variants.append(p)
        for p in variants:
            with self.subTest(p=p),self.assertRaises(ValueError):real.prepare(p)

    def test_reconciliation_difference_is_not_hidden(self):
        p=copy.deepcopy(self.packet);p['overview']['valuation']['total']=30
        with self.assertRaisesRegex(ValueError,'MISMATCH'):real.prepare(p)

    def test_unknown_cost_is_unknown_not_zero(self):
        p=copy.deepcopy(self.packet);p['holdings']['holdings'][0]['fifoPrice']=None
        self.assertIsNone(real.prepare(p)['positions'][0]['cost'])

    def test_actual_buy_sell_cash_basis_and_fees(self):
        s=real.operation(self.db,self.event(),1)
        self.assertEqual((s['cash'],s['positions'][0]['quantity'],s['positions'][0]['cost']),('-16.00','3','32.00'))
        event=self.event('SELL');event.update(id='real-event-0002',price='15')
        s=real.operation(self.db,event,2)
        self.assertEqual((s['cash'],s['positions'][0]['quantity'],s['positions'][0]['cost']),('-2.00','2','21.33'))
        self.assertEqual(s['estimated_realized_since_import'],'3.33');self.assertIsNone(s['realized_since_import'])
        self.assertEqual(s['fees_since_import'],'2.00')
        self.assertIsNone(s['positions'][0]['market_value'])

    def test_duplicate_record_and_conflicting_id(self):
        event=self.event();s=real.operation(self.db,event,1)
        self.assertEqual(real.operation(self.db,event,1),s)
        event['price']='12'
        with self.assertRaisesRegex(ValueError,'ID_CONFLICT'):real.operation(self.db,event,2)

    def test_concurrent_change_and_broker_reimport_do_not_erase_local_records(self):
        real.operation(self.db,self.event(),1)
        event=self.event();event['id']='real-event-0002'
        with self.assertRaisesRegex(ValueError,'ACCOUNT_CHANGED'):real.operation(self.db,event,1)
        p=copy.deepcopy(self.packet);p['observed_at']=datetime.now(timezone.utc).isoformat()
        with self.assertRaisesRegex(ValueError,'RECONCILIATION'):real.save_import(self.db,p,2)

    def test_not_an_order_and_no_sale_beyond_held_quantity(self):
        event=self.event();event['confirmed_execution']=False
        with self.assertRaisesRegex(ValueError,'CONFIRMATION'):real.operation(self.db,event,1)
        event=self.event('SELL');event['quantity']='3'
        with self.assertRaisesRegex(ValueError,'INSUFFICIENT'):real.operation(self.db,event,1)

    def test_deposit_and_fee_are_exact_cash_flows(self):
        event=self.event('DEPOSIT');event.update(amount='10',fee='0')
        s=real.operation(self.db,event,1);self.assertEqual(s['cash'],'6.00')
        event=self.event('FEE');event.update(id='real-event-0002',amount='2',fee='0')
        s=real.operation(self.db,event,2)
        self.assertEqual(s['cash'],'4.00');self.assertEqual(s['fees_since_import'],'2.00')

    def test_recommendation_uses_actual_held_quantity_and_no_execution_permission(self):
        from contextlib import contextmanager
        from types import SimpleNamespace
        from unittest.mock import Mock
        identity=dict(source='gettex_chart',symbol='APC.GTX',venue='GETTEX',currency='EUR',timezone='Europe/Berlin')
        self.db.execute('INSERT INTO settings VALUES (?,?)',('gettex_chart:US0378331005:1D',json.dumps({'identity':identity})))
        @contextmanager
        def connect():yield self.db
        store=SimpleNamespace(connect=connect,read_cache=SimpleNamespace(depend=lambda key:None))
        values={'watchlist':[{'isin':'US0378331005'}],'real_revision':1}
        engine={'status':'READY','signal':'HOLD','reason':'held real position'}
        with patch('gettex_chart.confirmed_state',return_value=True),patch('trading_context.calculate',return_value={'engine':engine,'proof':{}}) as calculate:
            result=real.calculate(store,values)
        self.assertEqual(calculate.call_args.args[4],{'quantity':'2'})
        self.assertEqual(result['real_quantity'],'2');self.assertFalse(result['execution_permission'])
        self.assertEqual(result['signal'],'HOLD')

    def test_compact_mcp_never_inlines_the_broker_journal(self):
        from mcp_projection import compact_identity
        result=compact_identity({'real_account':self.state})
        self.assertEqual(result['real_account']['account'],'V1')
        self.assertEqual(result['real_account']['broker_transactions'],
            dict(status='PAGED',total_count=1,section='runtime',path='/real_account/broker_transactions'))
        self.assertFalse(result['real_account']['execution_permission'])

    def test_wrong_real_revision_never_reaches_core_calculation(self):
        from contextlib import contextmanager
        from types import SimpleNamespace
        @contextmanager
        def connect():yield self.db
        store=SimpleNamespace(connect=connect,read_cache=SimpleNamespace(depend=lambda key:None))
        with patch('trading_context.calculate') as calculate:
            with self.assertRaisesRegex(ValueError,'REAL_ACCOUNT_CHANGED'):
                real.calculate(store,{'watchlist':[{'isin':'US0378331005'}],'real_revision':99})
            calculate.assert_not_called()

    def test_lightweight_health_does_not_read_or_serialize_real_account(self):
        from bridge import Bridge
        from types import SimpleNamespace as NS
        import threading
        bridge=Bridge.__new__(Bridge)
        bridge.directory=Path('unused');bridge.lock=threading.Lock();bridge.published_at=None
        bridge.diagnostic_pipeline={};bridge.instance_id='test';bridge.server=NS(server_port=1)
        with patch('real_portfolio.status',side_effect=AssertionError('account query')) as read:
            health=bridge.identity(include_diagnostics=False)
        read.assert_not_called()
        self.assertEqual(health['real_account']['path'],'/real_account')

    def test_account_operations_and_levels_survive_reopen_with_revision_guards(self):
        import tempfile
        archive=Path(__file__).resolve().parents[2]/'СМ-Архив'
        with tempfile.NamedTemporaryFile(prefix='SM real V1 test ',suffix='.sqlite3',dir=archive if archive.exists() else None,delete=False) as f:
            path=Path(f.name)
        try:
            db=sqlite3.connect(path)
            db.execute('CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT)')
            db.execute('CREATE TABLE instruments(isin TEXT PRIMARY KEY,payload TEXT)')
            real.save_import(db,self.packet);db.commit();db.close()
            self.assertEqual(real.write_levels(path,'US0378331005','9','20',1),2)
            self.assertEqual(real.status(path)['positions'][0]['levels'],{'stop':'9','target':'20'})
            self.assertEqual(real.write_operation(path,self.event(),2),3)
            reopened=real.status(path)
            self.assertEqual(reopened['cash'],'-16.00');self.assertEqual(reopened['positions'][0]['quantity'],'3')
            self.assertFalse(reopened['broker_orders'])
            self.assertEqual(real.footer_status(path),dict(account='V1',mode='REAL_ACCOUNTING',equity=None,daily_pnl=None))
            with self.assertRaisesRegex(ValueError,'ACCOUNT_CHANGED'):
                real.write_levels(path,'US0378331005','8','21',2)
            self.assertEqual(real.status(path)['revision'],3)
        finally:
            path.unlink(missing_ok=True)

    def test_main_footer_uses_v1_and_never_labels_lifetime_return_as_daily(self):
        from main_screen import Screen
        from types import SimpleNamespace as NS
        from unittest.mock import Mock
        surface=NS(portfolio_text=Mock(),daily_text=Mock())
        Screen.bind_account(surface,dict(account='V1',mode='REAL_ACCOUNTING',equity='20.00',daily_pnl=None))
        self.assertEqual(surface.portfolio_text.set.call_args.args[0],'V1: 20.00 EUR')
        self.assertIn('—',surface.daily_text.set.call_args.args[0])


class RealViewTests(unittest.TestCase):
    def setUp(self):
        import tkinter as tk
        from tkinter import ttk
        from types import SimpleNamespace as NS
        from unittest.mock import Mock
        self.root=tk.Tk();self.root.withdraw();self.addCleanup(self.root.destroy)
        self.errors=[];self.root.report_callback_exception=lambda k,v,t:self.errors.append(str(v))
        self.addCleanup(lambda:self.assertEqual(self.errors,[]))
        state=real.prepare(packet());state.update(status='AVAILABLE',revision=1,
            instruments=[{'isin':'US0378331005','name':'Apple'},{'isin':'US67066G1040','name':'NVIDIA'}])
        state['positions'][0].update(registered=True,unrealized='4.00')
        state['broker_transactions']=[dict(id=str(i),status='CANCELLED',lastEventAt=str(i),kind='security') for i in range(922)]
        self.state=state;self.jobs=[]
        def job(work,done,failed=None,**options):self.jobs.append((work,done,failed,options));return True
        self.app=NS(root=self.root,store=NS(path='unused',read_cache=NS(assert_current=Mock())),
            closing=False,job=job,compute_chart=NS(calculate=Mock()))
        self.parent=ttk.Frame(self.root)
        self.parent.pack()
        self.read=patch('real_portfolio.status',side_effect=lambda path:copy.deepcopy(self.state)).start()
        self.capture=patch('analytical_read.capture',return_value={'watchlist':[{'isin':'US0378331005'}]}).start()
        self.addCleanup(patch.stopall)
        from real_portfolio_view import attach
        attach(self.app,self.parent)
        self.finish(0)

    def widgets(self):
        def walk(widget):return [widget]+[v for child in widget.winfo_children() for v in walk(child)]
        return walk(self.parent)

    def finish(self,index):
        work,done,failed,_=self.jobs[index]
        try:done(work())
        except Exception as error:
            if failed:failed(str(error))
            else:raise
        self.root.update_idletasks()

    def test_real_cash_and_journal_are_shown_without_replaying_or_writing(self):
        from tkinter import ttk
        trees=[w for w in self.widgets() if isinstance(w,ttk.Treeview)]
        self.assertEqual([len(t.get_children()) for t in trees],[1,50])
        self.assertEqual(trees[0].set('US0378331005','quantity'),'2')
        self.assertEqual(self.state['cash'],'-4.00')
        self.assertEqual(len(self.jobs),1)
        self.assertEqual(self.jobs[0][3]['lane'],'read')

    def test_late_advice_is_discarded_after_instrument_switch(self):
        import tkinter as tk
        from tkinter import ttk
        from i18n import tr
        button=next(w for w in self.widgets() if isinstance(w,ttk.Button) and w.cget('text')==tr('Проверить рекомендацию'))
        button.invoke();self.assertEqual(len(self.jobs),2)
        selector=next(w for w in button.master.winfo_children() if isinstance(w,ttk.Combobox))
        selector.set('NVIDIA · US67066G1040');selector.event_generate('<<ComboboxSelected>>')
        self.jobs[1][1]({'signal':'UNEXPECTED_ADVICE'})
        self.assertFalse(any('UNEXPECTED_ADVICE' in w.get('1.0','end') for w in self.widgets() if isinstance(w,tk.Text)))

    def test_repeated_advice_click_does_not_discard_first_result(self):
        import tkinter as tk
        from tkinter import ttk
        from i18n import tr
        button=next(w for w in self.widgets() if isinstance(w,ttk.Button) and w.cget('text')==tr('Проверить рекомендацию'))
        button.invoke()
        button.invoke()
        self.assertEqual(len(self.jobs),2)
        now=datetime.now(timezone.utc)
        result=dict(signal='WAIT',core='READY',reason='V1_DAILY_SETUP_WAIT',real_quantity='2',
                    identity={'currency':'EUR'},long_term={},generated_at=now.isoformat(),
                    valid_until=(now+timedelta(minutes=5)).isoformat(),_input_revisions={})
        self.jobs[1][1](result)
        self.assertFalse(button.instate(['disabled']))
        self.assertTrue(any(tr('Пока подождать') in w.get('1.0','end')
                            for w in self.widgets() if isinstance(w,tk.Text)))

    def test_journal_paging_keeps_all_rows_and_never_submits_a_trade(self):
        from tkinter import ttk
        tree=next(w for w in self.widgets() if isinstance(w,ttk.Treeview) and 'status' in w['columns'])
        first=tree.get_children()
        next(w for w in self.widgets() if isinstance(w,ttk.Button) and w.cget('text')=='▶').invoke()
        self.assertEqual(len(tree.get_children()),50)
        self.assertNotEqual(tree.get_children(),first)
        self.assertEqual(len(self.jobs),1)
        self.app.compute_chart.calculate.assert_not_called()


if __name__=='__main__':unittest.main()
