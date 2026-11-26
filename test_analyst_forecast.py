"""Analyst source contract and real Tk presentation; no account or production writes."""
import copy,json,sys,unittest,threading
from pathlib import Path
from datetime import datetime,timezone,timedelta
from types import SimpleNamespace,MappingProxyType
from unittest.mock import Mock,patch
import pandas as pd
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
import analyst_forecast as f

NOW=datetime(2026,11,26,21,0,tzinfo=timezone.utc)
def close_fixture(root):
    try:
        root.update_idletasks()
        for timer in root.tk.call('after','info'):root.after_cancel(timer)
    finally:root.destroy()

INFO=dict(isin='US68389X1054',name='Oracle',profile_symbol='ORCL',history_symbol='ORCL',currency='USD',venue='NYSE')

def ticker():
    t=Mock()
    t.get_info.return_value=dict(symbol='ORCL',currency='USD',financialCurrency='EUR',currentPrice=100.,targetMeanPrice=120.,targetLowPrice=80.,targetHighPrice=150.,numberOfAnalystOpinions=35)
    t.get_recommendations.return_value=pd.DataFrame([dict(period='0m',strongBuy=32,buy=5,hold=5,sell=0,strongSell=1),dict(period='-1m',strongBuy=90,buy=5,hold=5,sell=0,strongSell=0)])
    t.get_upgrades_downgrades.return_value=pd.DataFrame([dict(Firm='Example',FromGrade='Hold',ToGrade='Buy',Action='up',currentPriceTarget=120.,priorPriceTarget=110.)],index=pd.DatetimeIndex(['2026-11-02']))
    t.history.return_value=pd.DataFrame({'Close':[99.,100.]},index=pd.date_range('2026-11-24',periods=2))
    t.get_history_metadata.return_value=MappingProxyType(dict(symbol='ORCL',currency='USD'))
    return t

class SourceTests(unittest.TestCase):
    def test_complete_currency_consistent_packet_and_separate_analyst_denominators(self):
        value=f.fetch(INFO,Path('.'),ticker=ticker(),now=NOW)
        self.assertEqual(value['errors'],{});self.assertEqual(value['targets']['currency'],'USD')
        self.assertEqual(value['targets']['analysts'],35);self.assertEqual(value['ratings']['total'],43)
        self.assertEqual(len(value['history']),2);self.assertEqual(len(value['decisions']),1)
        self.assertIsNone(value['decisions'][0]['target_currency']);json.dumps(value,allow_nan=False)
    def test_other_symbol_rejected_before_other_calls(self):
        t=ticker();t.get_info.return_value['symbol']='OTHER';value=f.fetch(INFO,Path('.'),ticker=t,now=NOW)
        self.assertEqual(value['errors'],{'profile':'ValueError'});t.get_recommendations.assert_not_called();self.assertNotIn('targets',value)
    def test_no_currency_cannot_borrow_reporting_or_selected_stock_currency(self):
        t=ticker();del t.get_info.return_value['currency'];value=f.fetch(INFO,Path('.'),ticker=t,now=NOW)
        self.assertIsNone(value['targets']['currency']);self.assertEqual(value['targets']['status'],'CURRENCY_UNKNOWN');self.assertNotIn('history',value)
    def test_inverted_targets_and_nonfinite_values_are_not_a_forecast(self):
        self.assertEqual(f.targets(dict(targetLowPrice=150.,targetMeanPrice=120.,targetHighPrice=80.,currency='EUR'))['status'],'TARGET_RANGE_INVALID')
        result=f.targets(dict(targetMeanPrice=float('nan'),targetHighPrice=True,financialCurrency='EUR'))
        self.assertIsNone(result['mean']);self.assertIsNone(result['high']);self.assertIsNone(result['currency'])
    def test_missing_recommendations_do_not_become_zero_or_sum_historical_periods(self):
        self.assertIsNone(f.ratings(pd.DataFrame()))
        frame=ticker().get_recommendations();self.assertEqual(f.ratings(frame)['total'],43)
        self.assertIsNone(f.ratings(frame[frame.period!='0m']))
        frame.loc[0,'hold']=-1;self.assertIsNone(f.ratings(frame))
    def test_real_zero_counts_have_zero_and_no_false_hold_score(self):
        row=dict(period='0m',**dict.fromkeys(f.RATING_KEYS,0));r=f.ratings(pd.DataFrame([row]))
        self.assertEqual(r['total'],0);self.assertIsNone(r['score'])
    def test_history_rejects_another_currency_or_symbol_and_accepts_mapping(self):
        t=ticker();frame=t.history();self.assertTrue(f.history(frame,t.get_history_metadata(),'ORCL','USD'))
        self.assertFalse(f.history(frame,dict(symbol='OTHER',currency='USD'),'ORCL','USD'))
        self.assertFalse(f.history(frame,dict(symbol='ORCL',currency='EUR'),'ORCL','USD'))
    def test_partial_failure_keeps_original_success_time_and_values(self):
        old=f.fetch(INFO,Path('.'),ticker=ticker(),now=NOW);t=ticker();t.get_recommendations.side_effect=RuntimeError('network')
        current=f.fetch(INFO,Path('.'),old,ticker=t,now=NOW+timedelta(days=2))
        self.assertEqual(current['ratings'],old['ratings']);self.assertEqual(current['ratings_at'],old['ratings_at'])
        self.assertNotEqual(current['targets_at'],old['targets_at']);self.assertEqual(f.state(current,NOW+timedelta(days=2)),'STALE')
    def test_previous_profile_never_survives_changed_identity(self):
        old=f.fetch(INFO,Path('.'),ticker=ticker(),now=NOW);other={**INFO,'profile_symbol':'NEW'}
        result=f.fetch(other,Path('.'),old,ticker=ticker(),now=NOW)
        self.assertNotIn('targets',result);self.assertFalse(f.matches(old,other))
    def test_cooldown_and_future_timestamp_are_explicit(self):
        v=f.fetch(INFO,Path('.'),ticker=ticker(),now=NOW)
        self.assertFalse(f.due(v,INFO,NOW+timedelta(minutes=2)));self.assertTrue(f.due(v,INFO,NOW+timedelta(days=2)))
        self.assertEqual(f.state(v,NOW-timedelta(hours=1)),'FUTURE_TIMESTAMP')
    def test_saved_card_reuse_keeps_real_old_date_and_rejects_changed_profile(self):
        raw=ticker().get_info();card=dict(symbol='ORCL',overview=raw,fetched_at='2026-10-01T10:00:00Z')
        store=SimpleNamespace(setting=lambda key,default=None:card if key.startswith('card:') else default)
        value=f.saved(store,INFO);self.assertEqual(value['targets_at'],'2026-10-01T10:00:00+00:00')
        self.assertEqual(f.state(value,NOW),'STALE');self.assertFalse(f.saved(store,{**INFO,'profile_symbol':'NEW'}))

    def test_missing_profile_is_resolved_by_proved_isin_only_without_changing_instrument(self):
        info={**INFO,'profile_symbol':None,'history_symbol':None};original=copy.deepcopy(info)
        binding=dict(status='CONFIRMED',isin=INFO['isin'],symbol='ORCL',currency='USD',venue='NYQ')
        candidate=dict(symbol='ORCL',quoteType='EQUITY',currency='USD')
        with patch('market.search',return_value=[candidate]),patch('market.resolve_symbol',return_value=binding):
            result=f.fetch(info,Path('.'),ticker=ticker(),now=NOW)
        self.assertEqual(result['symbol'],'ORCL');self.assertEqual(result['resolved_profile']['isin'],INFO['isin']);self.assertEqual(info,original)
        self.assertEqual(result['targets']['currency'],'USD')
    def test_wrong_isin_and_more_than_two_ambiguous_profiles_are_rejected(self):
        info={**INFO,'profile_symbol':None,'history_symbol':None};candidate=dict(symbol='WRONG',quoteType='EQUITY')
        with patch('market.search',return_value=[candidate]*5),patch('market.resolve_symbol',return_value=dict(status='CONFIRMED',symbol='WRONG',isin='WRONG')) as resolver:
            result=f.fetch(info,Path('.'),ticker=ticker(),now=NOW)
        self.assertEqual(resolver.call_count,2);self.assertEqual(result['errors']['identity'],'PROFILE_IDENTITY_UNCONFIRMED');self.assertNotIn('targets',result)
    def test_resolved_profile_expires_without_renewing_saved_proof(self):
        binding=dict(status='CONFIRMED',isin=INFO['isin'],symbol='ORCL',resolved_at=NOW.isoformat())
        with patch('market.search',side_effect=RuntimeError('no provider')) as search:
            same=f.resolve_profile(INFO,Path('.'),dict(resolved_profile=binding),NOW+timedelta(days=2))
            self.assertEqual(same,binding);search.assert_not_called()
            with self.assertRaises(RuntimeError):f.resolve_profile(INFO,Path('.'),dict(resolved_profile=binding),NOW+timedelta(days=8))

    def test_rate_limit_does_not_continue_auxiliary_calls(self):
        class YFRateLimitError(Exception):pass
        t=ticker();t.get_recommendations.side_effect=YFRateLimitError();v=f.fetch(INFO,Path('.'),ticker=t,now=NOW)
        self.assertTrue(v['rate_limited']);t.history.assert_not_called();t.get_upgrades_downgrades.assert_not_called()
    def test_missing_company_profile_never_requests_an_index(self):
        t=ticker();v=f.fetch({**INFO,'kind':'benchmark'},Path('.'),ticker=t,now=NOW)
        self.assertEqual(v['errors'],{'identity':'NO_COMPANY_PROFILE'});t.get_info.assert_not_called()
    def test_persist_only_dedicated_cache_and_reject_profile_change_during_fetch(self):
        store=SimpleNamespace(directory=Path('.'),setting=Mock(return_value={}),set_setting=Mock())
        app=SimpleNamespace(store=store,watchlist=[INFO])
        value=f.fetch(INFO,Path('.'),ticker=ticker(),now=NOW)
        with patch.object(f,'fetch',return_value=value):self.assertEqual(f.load(app,INFO,True),value)
        self.assertEqual(store.set_setting.call_args.args[0],f.PREFIX+INFO['isin']);store.set_setting.reset_mock()
        def changed(*_):app.watchlist=[{**INFO,'profile_symbol':'NEW'}];return value
        with patch.object(f,'fetch',side_effect=changed):self.assertEqual(f.load(app,INFO,True),{})
        store.set_setting.assert_not_called()

class ViewTests(unittest.TestCase):
    def setUp(self):
        import tkinter as tk
        from main_screen import theme
        from themes import THEMES
        self.root=tk.Tk();self.root.withdraw();self.addCleanup(close_fixture,self.root)
        self.app=SimpleNamespace(root=self.root,watchlist=[INFO],closing=False)
        theme(self.app,THEMES['Тёмная'])
        self.value=f.fetch(INFO,Path('.'),ticker=ticker(),now=NOW)
    def test_full_panel_handles_resize_without_new_threads_or_trade_calls(self):
        from analyst_forecast_view import Panel
        before=threading.active_count();self.root.deiconify();self.root.geometry('1200x800')
        pane=Panel(self.root,self.app,INFO,self.value);self.root.update()
        self.assertEqual(len(pane.tree.get_children()),1);self.assertEqual(pane.rating.grid_info()['column'],1)
        self.root.geometry('700x750');self.root.update();self.assertEqual(pane.rating.grid_info()['row'],1)
        self.assertEqual(threading.active_count(),before)
    def test_empty_and_benchmark_render_without_false_numbers(self):
        from analyst_forecast_view import Panel
        import tkinter.ttk as ttk
        host=ttk.Frame(self.root);host.pack();pane=Panel(host,self.app,INFO,{})
        self.root.update();self.assertFalse(pane.tree.get_children());host.destroy()
        pane=Panel(self.root,self.app,{**INFO,'kind':'benchmark'},{});self.root.update()
    def test_all_language_keys_have_three_variants_and_mapped_grades(self):
        import analyst_forecast_view as view
        for key,values in view.WORDS.items():self.assertEqual(len(values),3,key)
        with patch('i18n._language','de'):self.assertEqual(view.word('buy'),'Kaufen');self.assertEqual(view.grade('Hold'),'Halten')
        with patch('i18n._language','en'):self.assertEqual(view.word('rating'),'Analyst rating')
    def test_plot_does_not_mix_history_from_retained_different_snapshot(self):
        import analyst_forecast_view as view
        self.value['history_at']='2026-11-20T00:00:00Z';plot=view.Plot(self.root,self.value,background='#0b1220');plot.pack()
        self.root.deiconify();self.root.geometry('800x400');self.root.update();plot.paint()
        self.assertTrue(any(plot.itemcget(i,'text')==view.word('historymissing') for i in plot.find_all() if plot.type(i)=='text'))


class ForecastNavigationTests(unittest.TestCase):
    def setUp(self):
        import tkinter as tk
        from tkinter import ttk
        from matplotlib.figure import Figure
        from workspace_pages import Pages,SECTIONS
        self.root=tk.Tk();self.root.withdraw();self.addCleanup(close_fixture,self.root)
        canvas=ttk.Frame(self.root);canvas.pack(fill='both',expand=True)
        self.app=SimpleNamespace(root=self.root,closing=False,watchlist=[dict(isin='A',name='Apple'),dict(isin='N',name='NVIDIA')],
            symbol=tk.StringVar(self.root,'Apple'),timeframe=tk.StringVar(self.root,'1H'),period=tk.StringVar(self.root,'Max'),
            canvas=SimpleNamespace(get_tk_widget=lambda:canvas),ax_price=Figure().add_subplot(),job=Mock(),refresh_chart=Mock(),show_portfolio=Mock(),store=SimpleNamespace(setting=lambda k,d=None:d))
        self.screen=SimpleNamespace(app=self.app,workspace=canvas)
        self.screen.pages=Pages(self.screen);self.screen.section_buttons={}
        for key,label in SECTIONS.items():
            self.screen.section_buttons[key]=ttk.Button(canvas,text=label,command=lambda key=key:self.screen.pages.select(key))
        self.screen.pages.buttons=self.screen.section_buttons
        self.addCleanup(self.screen.pages.close)
    def test_cached_forecast_paints_before_network_and_late_other_stock_result_is_ignored(self):
        from workspace_pages import Pages
        pending=[]
        self.app.job.side_effect=lambda w,d,*a,**k:pending.append((w,d,k)) or True
        pages=self.screen.pages
        with patch('analyst_forecast.load',return_value={}):
            pages.select('forecast');work,done,kwargs=pending.pop(0)
            self.assertEqual(kwargs['lane'],'misc');packet=work();done(packet)
            self.assertIsNotNone(pages.forecast_panel);self.assertEqual(len(pending),1)
            work,done,kwargs=pending.pop(0);self.assertEqual(kwargs['key'],('analyst-forecast','A'))
            packet=work();pages.render=Mock();self.app.symbol.set('NVIDIA');done(packet);pages.render.assert_not_called()
    def test_forecast_tab_keeps_same_chart_and_no_trade_settings_calls(self):
        provider=patch('analyst_forecast.load',return_value={});provider.start();self.addCleanup(provider.stop)
        self.app.job.side_effect=lambda w,d,*a,**k:d(w()) or True
        ax=self.app.ax_price;ax.set_xlim(2,5);ax.set_ylim(10,20)
        self.screen.section_buttons['forecast'].invoke();self.root.update()
        self.assertEqual(self.screen.pages.active,'forecast');self.assertEqual(ax.get_xlim(),(2,5));self.assertEqual(ax.get_ylim(),(10,20))
        self.app.refresh_chart.assert_not_called();self.app.show_portfolio.assert_not_called()
    def test_53_200_distinct_profiles_do_not_reuse_another_company_cache(self):
        for count in (53,200):
            old=f.fetch(INFO,Path('.'),ticker=ticker(),now=NOW)
            for n in range(count):
                other={**INFO,'isin':str(n),'profile_symbol':'P'+str(n)}
                self.assertFalse(f.matches(old,other));self.assertTrue(f.due(old,other,NOW))

if __name__=='__main__':unittest.main()
