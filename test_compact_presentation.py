"""Display contracts only: no account, market requests or stored value writes."""
import copy,sys,unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
from financial_format import format_value,format_date
from workspace_pages import overview_metrics,statement_value,metadata_summary,calendar_text

class CompactPresentationTests(unittest.TestCase):
    def test_large_signed_values_and_language_units_do_not_change_prices(self):
        for lang,expected in [('ru','3,87 трлн USD'),('de','3,87 Bio. USD'),('en','3.87 T USD')]:
            self.assertEqual(format_value(3874810232832,lang,currency='USD',compact=True),expected)
        self.assertEqual(format_value(-244189994944,'ru',compact=True),'-244,19 млрд')
        self.assertEqual(format_value(1234,'ru',kind='price',compact=True),'1 234')
        self.assertEqual(format_value(.102,'ru',kind='percent',compact=True),'10,2 %')
        for value in (None,float('nan'),float('inf'),True):self.assertEqual(format_value(value,'ru',compact=True),'нет данных')

    def test_overview_keeps_exact_source_and_currency_and_rounds_only_display(self):
        raw=dict(marketCap=3874810232832,totalRevenue=445865984000,netIncomeToCommon=244189994944,
                 trailingPE=15.8971,trailingEps=19.93,currency='USD',financialCurrency='USD')
        before=copy.deepcopy(raw)
        with patch('i18n._language','ru'):rows=overview_metrics(raw)
        values={label:(value,exact) for label,value,exact in rows}
        self.assertEqual(values['Капитализация'],('3,87 трлн USD','3874810232832 USD'))
        self.assertEqual(values['P/E TTM'][0],'15,9');self.assertEqual(values['EPS TTM'][0],'19,93 USD')
        self.assertEqual(values['Выручка'][0],'445,87 млрд USD');self.assertEqual(raw,before)

    def test_statement_units_keep_ratios_and_per_share_values_unscaled(self):
        with patch('i18n._language','ru'):
            self.assertEqual(statement_value({'value':.102,'unit':'ratio'},True),'10,2 %')
            self.assertEqual(statement_value({'value':1500,'unit':'per_share','currency':'USD'},True),'1 500 USD')
            self.assertEqual(statement_value({'value':1500000,'unit':'money','currency':'EUR'},True),'1,5 млн EUR')
            self.assertEqual(statement_value({'value':1234.5,'unit':'multiple'}),'1 234,5')

    def test_dates_are_berlin_dst_aware_and_naive_external_dates_remain_explicit(self):
        self.assertEqual(format_date('2026-10-28T12:49:12.027488+00:00','ru',clock=True),'28.10.26 13:49 CET')
        self.assertEqual(format_date('2026-03-29T01:30:00+00:00','de',clock=True),'29.03.26 03:30 CEST')
        self.assertEqual(format_date('2026-10-25T01:30:00+00:00','en',clock=True),'2026-10-25 02:30 CET')
        self.assertIn('пояс не указан',format_date('2026-11-23T19:30:00','ru',clock=True))
        self.assertEqual(format_date('2026-11-23T19:30:00','ru',clock=True,assume_zone='Europe/Berlin'),'23.11.26 19:30 CET')
        self.assertEqual(format_date('2026-11-23T19:30:00+00:00','ru',clock=True,short=True),'23.11 20:30')
        self.assertEqual(format_date('2026-09-30','ru'),'30.09.26')
        self.assertEqual(format_date('invalid','en'),'—')

    def test_metadata_does_not_retimestamp_stale_sources_or_hide_identity(self):
        data={'source':'Yahoo Finance (через yfinance), не GETTEX','updated_at':'2026-10-28T12:49:12.027488+00:00'}
        before=copy.deepcopy(data)
        with patch('i18n._language','ru'):summary,status=metadata_summary(data)
        self.assertEqual(status,'STALE');self.assertEqual(summary,'Yahoo Finance · 28.10.26 13:49 CET · Устарело')
        self.assertEqual(data,before)

    def test_calendar_keeps_source_dates_missing_dates_and_forecasts(self):
        data={'Earnings Date':['2026-10-29'],'Revenue High':445865984000,'Ex-Dividend Date':None};before=copy.deepcopy(data)
        with patch('i18n._language','ru'):text=calendar_text(data)
        self.assertIn('29.10.26',text);self.assertIn('445,87 млрд',text);self.assertIn('Нет данных',text)
        self.assertEqual(data,before)

import test_main_screen as layout_fixture

class CompactLayoutTests(unittest.TestCase):
    setUp=layout_fixture.MainScreenTests.setUp

    def ready(self):
        self.app.watchlist[0]['profile_symbol']='APC'
        cache={'card:A':dict(symbol='APC',overview=dict(currency='USD',financialCurrency='USD',marketCap=3874810232832,totalCash=50000000000)),
               'fundamentals:A':dict(isin='A',symbol='APC',metrics=[dict(metric='EBITDA',label='EBITDA',frequency='quarterly',period_end='2025-12-31',value=46760000000,unit='money',currency='USD',status='AVAILABLE')])}
        self.app.store.setting.side_effect=lambda key,default=None:cache.get(key,default)
        self.app.job.side_effect=lambda work,done,*args,**kw:done(work()) or True

    def descendants(self,widget):
        for child in widget.winfo_children():
            yield child
            yield from self.descendants(child)

    def test_all_seven_summary_values_fit_the_normal_window(self):
        self.ready();pages=self.screen.pages;pages.select('overview');self.root.update()
        items=list(self.descendants(pages.body));canvas=next(w for w in items if w.winfo_class()=='Canvas')
        cash=next(w for w in items if w.winfo_class()=='TLabel' and w.cget('text')=='50 млрд USD')
        self.assertGreater(cash.winfo_height(),10)
        self.assertGreaterEqual(cash.winfo_rooty(),canvas.winfo_rooty())
        self.assertLessEqual(cash.winfo_rooty()+cash.winfo_height(),canvas.winfo_rooty()+canvas.winfo_height())

    def test_exact_financial_value_remains_visible_below_the_table(self):
        self.ready();pages=self.screen.pages;pages.select('finances');self.root.update()
        tree=next(w for w in self.descendants(pages.body) if w.winfo_class()=='Treeview')
        tree.selection_set(tree.get_children()[0]);tree.event_generate('<<TreeviewSelect>>');self.root.update()
        exact=next(w for w in self.descendants(pages.body) if w.winfo_class()=='TLabel' and str(w.cget('text')).startswith('Значение источника: 46760000000 USD'))
        self.assertTrue(exact.winfo_ismapped());self.assertGreater(exact.winfo_height(),10)
        self.assertLessEqual(exact.winfo_rooty()+exact.winfo_height(),pages.body.winfo_rooty()+pages.body.winfo_height())

if __name__=='__main__':unittest.main()
