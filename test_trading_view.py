"""Stage 2B binding tests with value/widget doubles; no Windows window."""
import copy
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,MagicMock,patch
import pandas as pd
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
from trading_view import format_engine,bind_engine,accepted_chart
from desktop014 import Desktop014
from benchmark_ui import BenchmarkMixin


class Value:
    def __init__(self,value=''):self.value=value
    def set(self,value):self.value=value
    def get(self):return self.value


class BindingTests(unittest.TestCase):
    def engine(self,signal='BUY'):
        return dict(signal=signal,status='READY',confidence=71,score=10,
            bullish_reasons=[{'text':'EMA растут'}],bearish_reasons=[{'text':'Сопротивление рядом'}],
            blockers=[],entry_blockers=['Объём слабый'],
            timeframe_context={'1H':{'facts':{'regime':'BULLISH'},'data_quality':{'state':'READY'}}},
            benchmark_context={'benchmark_name':'Nasdaq-100','state':'OUTPERFORMING','quality':'READY','difference_pp':2.5})
    def view(self):return SimpleNamespace(decision=Value(),explain=Value())
    def test_buy_from_prepared_result(self):
        v=self.view();bind_engine(v,self.engine());self.assertEqual(v.decision.get(),'BUY');self.assertIn('71/100',v.explain.get())
    def test_sell_from_prepared_result(self):
        v=self.view();bind_engine(v,self.engine('SELL'));self.assertEqual(v.decision.get(),'SELL')
    def test_hold_and_wait_distinct(self):
        self.assertEqual(format_engine(self.engine('HOLD'))[0],'HOLD')
        self.assertEqual(format_engine(self.engine('WAIT'))[0],'WAIT')
    def test_blocked_never_displays_buy_or_sell(self):
        for signal in ('BUY','SELL'):
            e=self.engine(signal);e['status']='DATA BLOCKED';e['blockers']=['4H: INVALID']
            title,details=format_engine(e)
            self.assertEqual(title,'WAIT / DATA BLOCKED');self.assertIn('4H: INVALID',details)
    def test_reasons_and_context_are_displayed_without_mutation(self):
        e=self.engine();before=copy.deepcopy(e);_,text=format_engine(e)
        for expected in ('EMA растут','Сопротивление рядом','Объём слабый','1H: BULLISH / READY','Nasdaq-100','OUTPERFORMING','+2.50'):
            self.assertIn(expected,text)
        self.assertEqual(e,before)
    def test_missing_or_legacy_engine_is_safe(self):
        for e in (None,{},'legacy',{'decision':'BUY'},{'signal':'BUY'}, {'signal':'ADD','status':'READY'}):
            title,text=format_engine(e);self.assertEqual(title,'WAIT • нет результата');self.assertIn('не подтверждён',text)
    def test_binding_does_not_recalculate(self):
        with patch('analytics.analyse_multiframe',side_effect=AssertionError('UI calculation')),patch('analytics.analyse',side_effect=AssertionError('Legacy UI calculation')),patch('trading_engine.evaluate',side_effect=AssertionError('UI policy')):
            bind_engine(self.view(),self.engine())
    def test_existing_chart_render_binds_prepared_engine_even_without_candles(self):
        v=self.view();v.canvas=Mock();v.fig=MagicMock();v.hover_overlay=None
        v.current_quality_signature=Mock(return_value=());v._cancel_motion=Mock()
        v.store=Mock();v.store.setting.return_value={}
        v.symbol=Value('Apple');v.timeframe=Value('1H');v.period=Value('год')
        v.instrument_cfg=Mock(return_value={'isin':'A','name':'Apple'})
        v.user_state=Mock(return_value={});v.source_mode=Mock(return_value='proxy')
        v.source_choice=Value();v.render_key=None;v.frame_for=Mock(return_value=pd.DataFrame())
        v.instrument=Value();v.metrics={};v.source_status=Value();v.status=Value();v.feed_errors={}
        v._prepared_chart={'trading_engine':self.engine('SELL'),'analysis':('ignored',{'decision':'BUY','reason':'OLD RECOMMENDATION'})}
        with patch('analytics.analyse_multiframe',side_effect=AssertionError('UI calculation')):
            Desktop014.refresh_chart(v)
        self.assertEqual(v.decision.get(),'SELL');self.assertNotIn('OLD RECOMMENDATION',v.explain.get())
    def test_stale_payload_does_not_change_display(self):
        v=self.view();v.closing=False;v._render_prepared_chart=Mock()
        with patch('chart_worker.capture',return_value={'data_generation':2}):
            BenchmarkMixin._apply_chart(v,({'data_generation':1},{'trading_engine':self.engine()}))
        v._render_prepared_chart.assert_not_called();self.assertEqual(v.decision.get(),'')
    def test_card_reuses_only_current_accepted_chart(self):
        values={'watchlist':[{'isin':'A','name':'Apple'}],'symbol':'Apple','data_generation':1}
        prepared={'trading_engine':self.engine()};v=self.view();v._accepted_trading_chart=(values,prepared)
        with patch('chart_worker.capture',return_value=values):
            self.assertIs(accepted_chart(v,'A'),prepared);self.assertIsNone(accepted_chart(v,'B'))
        with patch('chart_worker.capture',return_value={**values,'data_generation':2}):self.assertIsNone(accepted_chart(v,'A'))

if __name__=='__main__':unittest.main()
