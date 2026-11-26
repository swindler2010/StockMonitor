"""Main asset navigation: real Tk, no live feed/account or synthetic orders."""
import copy,sys,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
import test_main_screen as layout_fixture
from workspace_pages import load,technical,SECTIONS


class WorkspaceNavigationTests(unittest.TestCase):
    setUp=layout_fixture.MainScreenTests.setUp

    def immediate(self):
        # Auxiliary forecasts have their own source tests; navigation stays offline.
        provider=patch('analyst_forecast.load',return_value={});provider.start();self.addCleanup(provider.stop)
        self.app.job.side_effect=lambda work,done,*args,**kwargs:done(work()) or True

    def test_all_seven_primary_tabs_have_distinct_in_window_pages_and_keep_plot_view(self):
        self.immediate();app=self.app;screen=self.screen;canvas=app.canvas;axis=app.ax_price
        axis.set_xlim(2,5);axis.set_ylim(10,20);self.root.update()
        before=[w for w in self.root.winfo_children() if w.winfo_class()=='Toplevel']
        self.assertEqual(set(screen.section_buttons),set(SECTIONS))
        for section in SECTIONS:
            screen.section_buttons[section].invoke();self.root.update()
            self.assertEqual(screen.pages.active,section)
            self.assertEqual(screen.pages.host.winfo_ismapped(),section!='chart')
            self.assertEqual([k for k,b in screen.section_buttons.items() if str(b['style'])=='DeskActive.TButton'],[section])
            self.assertIs(app.canvas,canvas);self.assertIs(app.ax_price,axis)
            self.assertEqual(axis.get_xlim(),(2,5));self.assertEqual(axis.get_ylim(),(10,20))
        self.assertEqual([w for w in self.root.winfo_children() if w.winfo_class()=='Toplevel'],before)
        app.show_card.assert_not_called();app.show_news.assert_not_called();app.refresh_chart.assert_not_called()
        screen.section_buttons['chart'].invoke();self.root.update();self.assertFalse(screen.pages.host.winfo_ismapped())

    def test_bottom_report_and_event_buttons_do_not_open_news_or_generic_card(self):
        self.immediate()
        for label,section in [('Отчёты','reports'),('События','events'),('Новости','news')]:
            self.screen.bottom_buttons[label].invoke();self.root.update()
            self.assertEqual(self.screen.pages.active,section)
        self.app.show_news.assert_not_called();self.app.show_card.assert_not_called()

    def test_late_page_read_is_rejected_on_tab_stock_tf_and_language_changes(self):
        pending=[];self.app.job.side_effect=lambda w,d,*a,**k:pending.append((w,d)) or True
        pages=self.screen.pages
        pages.select('overview');work,done=pending[-1];packet=work();pages.render=Mock()
        pages.select('finances');done(packet);pages.render.assert_not_called()
        pages.select('overview');work,done=pending[-1];packet=work();self.app.symbol.set('NVIDIA');done(packet);pages.render.assert_not_called()
        self.root.update();pages.select('overview',force=True);work,done=pending[-1];packet=work();self.app.timeframe.set('4H');done(packet);pages.render.assert_not_called()
        self.root.update();pages.select('overview',force=True);work,done=pending[-1];packet=work()
        with patch('i18n._language','de'):done(packet)
        pages.render.assert_not_called()

    def test_analysis_rechecks_closed_inputs_immediately_before_display(self):
        pending=[];self.app.job.side_effect=lambda w,d,*a,**k:pending.append((w,d)) or True
        cache=SimpleNamespace(current=Mock(return_value=True));self.app.store.read_cache=cache
        prepared={};values={'same':'capture'};self.app._accepted_trading_chart=(values,prepared)
        packet={'technical':dict(prepared=prepared,token={'v':1},files=[])}
        pages=self.screen.pages
        with patch('chart_worker.capture',return_value=values),patch('workspace_pages.technical',return_value=packet),patch('partial_chart.current',return_value=True):
            pages.select('analysis');work,done=pending[-1];result=work()
            cache.current.side_effect=RuntimeError('revision unavailable')
            done(result);self.assertIsNone(pages.model['technical'])
            cache.current.side_effect=None;cache.current.return_value=True
            packet={'technical':dict(prepared=prepared,token={'v':1},files=[])}
            with patch('workspace_pages.technical',return_value=packet):
                pages.request();work,done=pending[-1];result=work()
                with patch('chart_worker.capture',return_value={'changed':'draft'}):done(result)
                self.assertIsNone(pages.model['technical'])

    def test_missing_data_uses_actual_empty_states_and_embedded_news_does_not_replace_popup_listener(self):
        self.immediate();listener=Mock();self.app._news_listener=listener
        pages=self.screen.pages;pages.select('news');self.root.update()
        self.assertTrue(pages.news.embedded);self.assertIs(self.app._news_listener,listener)
        self.assertEqual(str(pages.news.choice['state']),'disabled')
        pages.select('finances');self.root.update();self.assertIs(self.app._news_listener,listener)
        self.assertFalse(pages.model['rows']);self.assertEqual(pages.model['data']['status'],'NOT_LOADED')
        pages.select('analysis');self.root.update();self.assertIsNone(pages.model['technical'])


class WorkspaceQueueTests(unittest.TestCase):
    def test_53_200_background_jobs_cannot_occupy_the_interactive_section_slot(self):
        import threading,time
        from performance_dispatcher import Dispatcher
        for n in (53,200):
            release=threading.Event();started=[threading.Event() for _ in range(4)];ready=threading.Event()
            dispatcher=Dispatcher(lambda fn,*args:fn(*args),workers=5,sample=lambda old:({'cpu':0,'ram':0},None))
            try:
                def hold(index):started[index].set();release.wait(5)
                for k,lane in enumerate(('feed_update','eligibility','misc','trading')):
                    dispatcher.submit(lambda k=k:hold(k),lambda _:None,lambda _:None,key=('held',k),lane=lane)
                    self.assertTrue(started[k].wait(2))
                for k in range(n):dispatcher.submit(lambda:None,lambda _:None,lambda _:None,key=('background',k),lane='feed_update')
                # Use the real Pages.request submission rather than a guessed lane.
                class Root:
                    def after(self,*args):return 'timer'
                from workspace_pages import Pages
                app=SimpleNamespace(_accepted_trading_chart=None,closing=False,root=Root(),job=dispatcher.submit)
                pages=SimpleNamespace(closed=False,active='overview',capture=lambda:({'isin':'A'},'1H','Max','ru'),context=None,
                                      sequence=0,app=app,timer=None,same=lambda _:True,render=lambda _:ready.set(),periodic=lambda:None)
                with patch('workspace_pages.load',return_value={'technical':None}):Pages.request(pages);self.assertTrue(ready.wait(2))
            finally:release.set();dispatcher.close()


class WorkspaceDataTests(unittest.TestCase):
    def test_profile_identity_currency_and_missing_sources_are_not_guessed(self):
        info=dict(isin='A',name='Apple',profile_symbol='APC',currency='EUR')
        card=dict(symbol='WRONG',overview={'marketCap':999,'currency':'USD'},reports={'2026':{}})
        data=dict(isin='A',symbol='WRONG',metrics=[{'metric':'TotalRevenue','value':999}])
        state={'card:A':card,'fundamentals:A':data}
        app=SimpleNamespace(store=SimpleNamespace(setting=lambda key,default=None:copy.deepcopy(state.get(key,default))))
        result=load(app,info)
        self.assertFalse(result['card']);self.assertFalse(result['rows']);self.assertFalse(result['overview'])
        state['card:A']=dict(symbol='APC',overview={'symbol':'APC','marketCap':123,'currency':'USD','financialCurrency':'USD'})
        result=load(app,info)
        self.assertEqual(result['overview']['marketCap'],123);self.assertEqual(result['overview']['currency'],'USD');self.assertEqual(result['info']['currency'],'EUR')

    def test_overview_events_and_reports_keep_their_own_source_receipt_dates(self):
        info=dict(isin='A',name='Apple',profile_symbol='APC')
        state={'card:A':dict(symbol='APC',overview={'marketCap':123},source='card-feed',fetched_at='2026-11-01T00:00:00+00:00'),
               'fundamentals:A':dict(isin='A',symbol='APC',source='report-feed',updated_at='2026-11-02T00:00:00+00:00',metrics=[{'value':1}]),
               'company_events:A':dict(isin='A',symbol='APC',source='calendar-feed',calendar={'Earnings Date':'2026-12-01'},calendar_fetched_at='2026-11-03T00:00:00+00:00')}
        app=SimpleNamespace(store=SimpleNamespace(setting=lambda key,default=None:copy.deepcopy(state.get(key,default))))
        result=load(app,info)
        self.assertEqual(result['overview_metadata'],{'source':'card-feed','updated_at':'2026-11-01T00:00:00+00:00'})
        self.assertEqual(result['events_metadata'],{'source':'calendar-feed','updated_at':'2026-11-03T00:00:00+00:00'})
        self.assertEqual(result['data']['source'],'report-feed');self.assertEqual(result['data']['updated_at'],'2026-11-02T00:00:00+00:00')
        del state['company_events:A']['calendar_fetched_at'];self.assertIsNone(load(app,info)['events_metadata']['updated_at'])

    def test_analysis_reuses_current_accepted_result_and_rejects_changed_dependency(self):
        import pandas as pd
        info=dict(isin='A',name='Apple',profile_symbol='APC');cache=SimpleNamespace(current=Mock(return_value=True))
        app=SimpleNamespace(store=SimpleNamespace(setting=lambda *_a:{},read_cache=cache))
        frame=pd.DataFrame({'close':[1,2],'ema10':[1,2],'rsi':[40,45]},index=pd.date_range('2026-11-01',periods=2,freq='h'))
        frame.attrs.update(source='gettex_chart',venue='GETTEX',currency='EUR')
        prepared=dict(frame=frame,analysis=(frame,dict(closed_bar_count=1,support=1,resistance=3,stop=.5)),trading_engine={'status':'READY','signal':'WAIT'},_input_revisions={'captured':True})
        values=dict(symbol='Apple',timeframe='1H',period='Max',watchlist=[info]);cached=(values,prepared)
        with patch('partial_chart.current',return_value=True):
            result=technical(app,info,'1H','Max',cached)
            self.assertEqual(result['technical']['indicators']['rsi'],45);self.assertIs(result['technical']['prepared'],prepared)
            self.assertIsNone(technical(app,info,'4H','Max',cached)['technical'])
            cache.current.return_value=False;self.assertIsNone(technical(app,info,'1H','Max',cached)['technical'])
            cache.current.return_value=True
            other={**info,'isin':'OTHER'};self.assertIsNone(technical(app,other,'1H','Max',cached)['technical'])
            cache.current.side_effect=RuntimeError('revision unavailable');self.assertIsNone(technical(app,info,'1H','Max',cached)['technical'])

if __name__=='__main__':unittest.main()
