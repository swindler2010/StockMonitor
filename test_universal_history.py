"""Search/Add orchestration regression tests; widget doubles, no native UI claims."""
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
from features import Features
from history_service import recover
from timeframes import LABELS,base_for

class Value:
    def __init__(self,value=''):self.value=value
    def get(self):return self.value
    def set(self,value):self.value=value

class Widget:
    widgets=[]
    def __init__(self,*args,**kwargs):
        self.options=kwargs;self.bindings={};self.rows={};self.chosen=();self.exists=True
        self.widgets.append(self)
    def pack(self,**kwargs):pass
    def title(self,*args):pass
    def geometry(self,*args):pass
    def configure(self,**kwargs):self.options.update(kwargs)
    def bind(self,event,callback):self.bindings[event]=callback
    def winfo_exists(self):return self.exists
    def destroy(self):self.exists=False
    def selection(self):return self.chosen
    def get_children(self):return list(self.rows)
    def delete(self,item):self.rows.pop(item)
    def insert(self,*args,**kwargs):self.rows[kwargs['iid']]=kwargs;return kwargs['iid']

class SearchAddTests(unittest.TestCase):
    def setUp(self):
        Widget.widgets=[];self.jobs=[]
        self.app=SimpleNamespace(root=None,watchlist=[],symbol_box={},symbol=Value(),
            source_status=Value(),refresh_chart=Mock(),start_feed=Mock(),attach_logo=Mock(),
            job=lambda work,done,failed=None:self.jobs.append((work,done,failed)))
        self.patches=[patch('features.tk.Toplevel',Widget),patch('features.tk.StringVar',Value)]
        self.patches += [patch('features.ttk.'+name,Widget) for name in ('Frame','Label','Entry','Treeview','Button')]
        for item in self.patches:item.start();self.addCleanup(item.stop)
        self.errors=patch('features.messagebox.showerror').start();self.addCleanup(patch.stopall)
        self.open_dialog()
    def open_dialog(self):
        Widget.widgets=[]
        Features.add_stock_dialog(self.app)
        self.entries=[w.options['textvariable'] for w in Widget.widgets if 'textvariable' in w.options and 'wraplength' not in w.options]
        self.tree=next(w for w in Widget.widgets if 'selectmode' in w.options)
        self.find=next(w for w in Widget.widgets if w.options.get('text')=='Найти')
        self.add=next(w for w in Widget.widgets if w.options.get('text')=='Добавить')
    def identity(self,symbol,isin,currency='USD'):
        return dict(status='CONFIRMED',isin=isin,symbol=symbol,venue='NMS',currency=currency,source='proxy',evidence={'test':True})
    def found(self,items):
        with patch('features.market.search',return_value=items):
            self.entries[0].set('query');self.find.options['command']()
            work,done,_=self.jobs.pop();done(work())
    def select(self,index):
        self.tree.chosen=(str(index),);self.tree.bindings['<<TreeviewSelect>>'](None)
        return self.jobs.pop()
    def test_arbitrary_instrument_starts_same_feed_before_chart(self):
        for symbol,isin in [('ABC','US1234567890'),('XYZ.DE','DE1234567890'),('QRS.L','GB1234567890')]:
            self.app.watchlist=[];order=[]
            self.app.start_feed.side_effect=lambda item:order.append(('feed',item.copy()))
            self.app.refresh_chart.side_effect=lambda:order.append(('chart',None))
            self.found([{'symbol':symbol,'longname':symbol,'exchange':'NMS'}])
            _,done,_=self.select(0)
            self.assertEqual(self.add.options['state'],'disabled')
            done(self.identity(symbol,isin))
            self.add.options['command']()
            self.assertEqual([kind for kind,_ in order],['feed','chart'])
            item=order[0][1];self.assertEqual(item['history_symbol'],symbol)
            self.assertEqual(item['isin'],isin);self.assertEqual(item['currency'],'USD')
            self.assertIn('LOADING',self.app.source_status.get())
            # Reopen the same normal dialog for the next arbitrary result.
            if symbol!='QRS.L':self.open_dialog()
    def test_switch_clears_identity_and_ignores_old_resolver(self):
        self.found([{'symbol':'A'},{'symbol':'B'}])
        _,old,_=self.select(0);self.entries[2].set('US1234567890');self.entries[6].set('OLD')
        _,new,_=self.select(1)
        self.assertEqual(self.entries[2].get(),'');self.assertEqual(self.entries[6].get(),'')
        old(self.identity('A','US1234567890'))
        self.assertEqual(self.add.options['state'],'disabled')
        new(self.identity('B','DE1234567890','EUR'))
        self.assertEqual(self.entries[6].get(),'B')
    def test_same_symbol_reselected_rejects_earlier_generation(self):
        self.found([{'symbol':'A'},{'symbol':'B'}]);_,old,_=self.select(0)
        self.select(1);_,current,_=self.select(0)
        old(self.identity('A','US1234567890'))
        self.assertEqual(self.entries[2].get(),'')
        current(self.identity('A','GB1234567890','GBP'))
        self.assertEqual(self.entries[2].get(),'GB1234567890')
    def test_unconfirmed_currency_blocks_add_without_eur_fallback(self):
        self.found([{'symbol':'A'}]);_,done,_=self.select(0)
        done({'status':'BLOCKED','error':'Currency unavailable'});self.add.options['command']()
        self.assertEqual(self.entries[5].get(),'');self.app.start_feed.assert_not_called()
    def test_missing_isin_blocks_add(self):
        self.found([{'symbol':'A'}]);_,done,_=self.select(0)
        done({'status':'BLOCKED','error':'ISIN unavailable'});self.add.options['command']()
        self.app.start_feed.assert_not_called()

    def test_changed_confirmed_fields_cannot_launch_history(self):
        self.found([{'symbol':'A'}]);_,done,_=self.select(0)
        done(self.identity('A','US1234567890'));self.entries[4].set('OTHER')
        self.add.options['command']();self.app.start_feed.assert_not_called()

    def test_new_search_invalidates_confirmed_identity(self):
        self.found([{'symbol':'A'}]);_,done,_=self.select(0)
        done(self.identity('A','US1234567890'))
        self.entries[0].set('other');self.find.options['command']()
        self.assertEqual(self.add.options['state'],'disabled')
        self.add.options['command']();self.app.start_feed.assert_not_called()

    def test_stale_search_result_does_not_replace_latest_results(self):
        self.entries[0].set('first');self.find.options['command']();_,first,_=self.jobs.pop()
        self.entries[0].set('second');self.find.options['command']();_,second,_=self.jobs.pop()
        second([{'symbol':'B'}]);first([{'symbol':'A'}])
        self.assertTrue(self.tree.rows['0']['text'].startswith('B •'))

    def test_requested_isin_must_match_resolved_identity(self):
        self.found([{'symbol':'A'}]);self.entries[0].set('US1912161007')
        _,done,_=self.select(0);done(self.identity('A','US17275R1023'))
        self.assertEqual(self.add.options['state'],'disabled')
        self.add.options['command']();self.app.start_feed.assert_not_called()

class PipelineTests(unittest.TestCase):
    def test_all_supported_tf_share_four_bases_and_failures_do_not_skip_them(self):
        self.assertEqual({base_for(tf) for tf in LABELS},{'1D','5m','15m','1H'})
        store=Mock();store.setting.return_value={}
        with patch('history_backfill.sync',side_effect=ValueError('external source unavailable')) as sync:
            with self.assertRaisesRegex(ValueError,'external source unavailable'):
                recover({'isin':'US1234567890','history_symbol':'ANY'},store)
        self.assertEqual([call.args[3] for call in sync.call_args_list],['1D','5m','15m','1H'])
        self.assertEqual({call.args[2] for call in sync.call_args_list},{'ANY'})

if __name__=='__main__':unittest.main()
