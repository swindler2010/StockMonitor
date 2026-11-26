"""Measured forecast startup, exact alternative profile and coherent currency regressions."""
import sys,copy,threading,unittest
from pathlib import Path
from datetime import datetime,timezone
from types import SimpleNamespace
from unittest.mock import Mock,patch
sys.path[:0]=[str(Path(__file__).parent),str(Path(__file__).parent/'stock_monitor')]
import analyst_forecast as f
from test_analyst_forecast import INFO,ticker
NOW=datetime(2026,11,26,22,0,tzinfo=timezone.utc)

class CompletionTests(unittest.TestCase):
    def test_uncovered_configured_profile_uses_whole_verified_alternative_not_mixed_currency(self):
        info={**INFO,'profile_symbol':'MSF.DE','history_symbol':'MSF.DE','currency':'EUR'}
        primary=dict(identity=f.identity(info),symbol='MSF.DE',targets={'mean':None,'currency':'EUR'},history=[['2026-11-25',90]],errors={'ratings':'SOURCE_EMPTY'},attempted_at=NOW.isoformat())
        alt=dict(identity=f.identity(info),symbol='MSFT',targets={'mean':200,'currency':'USD'},history=[['2026-11-25',100]],errors={})
        binding=dict(status='CONFIRMED',isin=info['isin'],symbol='MSFT',resolved_at=NOW.isoformat())
        with patch.object(f,'_fetch_bound',side_effect=[copy.deepcopy(primary),copy.deepcopy(alt)]),patch.object(f,'resolve_profile',return_value=binding):
            result=f.fetch(info,Path('.'),now=NOW)
        self.assertEqual(result['targets']['currency'],'USD');self.assertEqual(result['history'],alt['history']);self.assertEqual(result['identity'],f.identity(info));self.assertEqual(result['configured_symbol'],'MSF.DE');self.assertEqual(info['profile_symbol'],'MSF.DE')
    def test_failed_or_uncovered_alternative_preserves_original_snapshot(self):
        primary=dict(identity=f.identity(INFO),symbol='ORCL',targets={'mean':None,'currency':'USD'},history=[['2026-11-25',90]],errors={'ratings':'SOURCE_EMPTY'})
        with patch.object(f,'_fetch_bound',return_value=copy.deepcopy(primary)),patch.object(f,'resolve_profile',side_effect=ValueError('PROFILE_IDENTITY_UNCONFIRMED')):
            result=f.fetch(INFO,Path('.'),now=NOW)
        self.assertEqual(result['history'],primary['history']);self.assertEqual(result['symbol'],'ORCL');self.assertIn('coverage_note',result)
    def test_changed_symbol_response_remains_blocked_without_fallback(self):
        primary=dict(identity=f.identity(INFO),symbol='ORCL',errors={'profile':'IDENTITY_MISMATCH'})
        with patch.object(f,'_fetch_bound',return_value=primary),patch.object(f,'resolve_profile') as resolver:
            self.assertEqual(f.fetch(INFO,Path('.'),now=NOW),primary);resolver.assert_not_called()
    def test_bound_alternative_drops_prior_currency_sections_even_when_new_history_fails(self):
        info={**INFO,'profile_symbol':'MSF.DE','currency':'EUR'};old=dict(identity=f.identity(info),symbol='MSF.DE',history=[['2026-11-25',90]],history_at=NOW.isoformat())
        t=ticker();t.get_info.return_value['symbol']='MSFT';t.get_history_metadata.return_value={'symbol':'MSFT','currency':'EUR'}
        result=f._fetch_bound(info,Path('.'),old,ticker=t,now=NOW,symbol='MSFT',binding={'isin':info['isin'],'symbol':'MSFT','status':'CONFIRMED'})
        self.assertEqual(result['targets']['currency'],'USD');self.assertNotIn('history',result);self.assertEqual(result['errors']['history'],'SOURCE_EMPTY')
    def test_isin_symbol_catalogue_accepts_only_exact_stock_record(self):
        good=['Company','Stocks','ACTUAL|'+INFO['isin']+'|ACTUAL||RIC','75','','actual|ACTUAL|1|1']
        wrong=['Other','Stocks','WRONG|US0000000000|WRONG||RIC','75','','wrong|WRONG|1|2']
        import json
        raw=';'.join('new Array('+','.join(json.dumps(v) for v in row)+')' for row in (wrong,good))
        response=SimpleNamespace(text=raw,raise_for_status=lambda:None)
        with patch('requests.get',return_value=response):self.assertEqual(f._isin_symbols(INFO['isin']),['ACTUAL'])
    def test_exact_isin_catalogue_can_supply_a_confirmed_literal_symbol(self):
        candidate={'symbol':'ADS.DE','quoteType':'EQUITY'};second={'symbol':'ADDDF','quoteType':'EQUITY'}
        def search(q):return [candidate] if q==INFO['isin'] else [second]
        with patch('market.search',side_effect=search),patch.object(f,'_isin_symbols',return_value=['ADDDF']),patch('market.resolve_symbol',side_effect=[{'status':'BLOCKED'},{'status':'CONFIRMED','isin':INFO['isin'],'symbol':'ADDDF'}]) as resolver:
            result=f.resolve_profile(INFO,Path('.'),now=NOW)
        self.assertEqual(result['symbol'],'ADDDF');self.assertEqual(resolver.call_count,2)
    def test_known_changed_isin_is_reported_and_never_accepted(self):
        with patch('market.search',return_value=[{'symbol':'XOM','quoteType':'EQUITY'}]),patch.object(f,'_isin_symbols',return_value=[]),patch('market.resolve_symbol',return_value={'status':'CONFIRMED','symbol':'XOM','isin':'NEW_ISIN'}):
            with self.assertRaisesRegex(ValueError,'PROFILE_ISIN_MISMATCH:'):f.resolve_profile(INFO,Path('.'),now=NOW)
    def test_fresh_partial_result_is_not_falsely_labelled_expired(self):
        import analyst_forecast_view as view
        stamp=datetime.now(timezone.utc).isoformat()
        packet=dict(updated_at=stamp,targets_at=stamp,errors={'decisions':'SOURCE_EMPTY'})
        self.assertEqual(f.state(packet),'PARTIAL');self.assertTrue(view.stamp(packet).startswith(view.word('partial_state')))
        self.assertTrue(view.stamp(packet,'targets').startswith(view.word('available')))
    def test_missing_sections_have_plain_localized_reasons(self):
        import analyst_forecast_view as view
        packet=dict(errors={'decisions':'SOURCE_EMPTY','identity':'PROFILE_ISIN_MISMATCH:OLD:NEW'})
        with patch('i18n._language','ru'):
            notes=view.display_notes(packet)
        self.assertIn('источник не предоставил',notes[0]);self.assertIn('OLD → NEW',notes[1]);self.assertNotIn('ValueError',' '.join(notes))

    def test_share_class_separator_requires_exact_class_isin_and_listing(self):
        import json
        info={'isin':'US0846707026'};item=dict(symbol='BRK-B',exchange='NYQ',currency='USD')
        meta=dict(symbol='BRK-B',exchangeName='NYQ',currency='USD',instrumentType='EQUITY')
        row=['Berkshire B','Stocks','BRK.B|US0846707026|BRK.B||BRKb','75','','brk-b|BRK.B|1|46227']
        raw='new Array('+','.join(json.dumps(v) for v in row)+')'
        chart=SimpleNamespace(raise_for_status=lambda:None,json=lambda:{'chart':{'result':[{'meta':meta}]}})
        reference=SimpleNamespace(raise_for_status=lambda:None,text=raw)
        with patch('requests.get',side_effect=[chart,reference]):result=f._class_profile(info,item)
        self.assertEqual(result['symbol'],'BRK-B');self.assertEqual(result['isin'],info['isin']);self.assertEqual(result['evidence']['class_separator']['reference'],'BRK.B')
        wrong=SimpleNamespace(raise_for_status=lambda:None,text=raw.replace('US0846707026','US0846701086'))
        with patch('requests.get',side_effect=[chart,wrong]):self.assertIsNone(f._class_profile(info,item))
    def test_class_format_does_not_normalize_country_suffix_or_different_venue(self):
        with patch('requests.get') as request:
            self.assertIsNone(f._class_profile({'isin':'DE000A1EWWW0'},{'symbol':'ADS-DE'}));request.assert_not_called()
        chart=SimpleNamespace(raise_for_status=lambda:None,json=lambda:{'chart':{'result':[{'meta':dict(symbol='BRK-B',instrumentType='EQUITY',exchangeName='OTHER',currency='USD')}]}})
        with patch('requests.get',return_value=chart) as request:
            self.assertIsNone(f._class_profile({'isin':'US0846707026'},dict(symbol='BRK-B',exchange='NYQ',currency='USD')));self.assertEqual(request.call_count,1)

    def test_large_window_reads_cached_data_through_reserved_slot_before_network(self):
        import tkinter as tk
        import analyst_forecast_view as view
        root=tk.Tk();root.withdraw()
        packet=dict(identity=f.identity(INFO),updated_at=datetime.now(timezone.utc).isoformat())
        pending=[];app=SimpleNamespace(root=root,watchlist=[INFO],closing=False,job=lambda work,done,*a,**k:pending.append((work,done,k)) or True)
        try:
            with patch.object(f,'load',return_value=packet):
                win=view.show(app,INFO);work,done,options=pending.pop(0);self.assertEqual(options['lane'],'chart');done(work());self.assertFalse(pending)
        finally:
            for timer in root.tk.call('after','info'):root.after_cancel(timer)
            root.destroy()
    def test_compact_main_panel_keeps_forecast_and_rating_side_by_side(self):
        import tkinter as tk
        import analyst_forecast_view as view
        from test_analyst_forecast import close_fixture
        root=tk.Tk();root.geometry('840x660')
        try:
            packet=dict(identity=f.identity(INFO));panel=view.Panel(root,SimpleNamespace(root=root),INFO,packet,compact=True)
            timer=root.after(50,root.quit);root.mainloop();self.assertEqual(panel.rating.grid_info()['column'],1)
        finally:close_fixture(root)
    def test_english_plot_dates_follow_the_selected_language(self):
        import tkinter as tk
        import analyst_forecast_view as view
        from test_analyst_forecast import close_fixture
        root=tk.Tk();root.geometry('800x400')
        try:
            packet=dict(targets={'current':100,'currency':'USD','mean':120,'low':90,'high':130,'status':'AVAILABLE'},targets_at=NOW.isoformat())
            with patch('i18n._language','en'):
                plot=view.Plot(root,packet);plot.pack(fill='both',expand=True);timer=root.after(50,root.quit);root.mainloop();plot.paint()
                texts=[plot.itemcget(i,'text') for i in plot.find_all() if plot.type(i)=='text']
                self.assertIn('2026-11-26',texts);self.assertIn('2027-11-26',texts)
        finally:close_fixture(root)

    def test_periodic_refresh_keeps_forecast_scroll_position(self):
        import tkinter as tk
        from tkinter import ttk
        from workspace_pages import Pages
        from test_analyst_forecast import close_fixture
        root=tk.Tk();root.geometry('840x600');canvas=ttk.Frame(root);canvas.pack(fill='both',expand=True)
        app=SimpleNamespace(root=root,closing=False,watchlist=[INFO],symbol=tk.StringVar(root,INFO['name']),timeframe=tk.StringVar(root,'1H'),period=tk.StringVar(root,'Max'),canvas=SimpleNamespace(get_tk_widget=lambda:canvas))
        app.job=lambda w,d,*a,**k:d(w()) or True
        pages=Pages(SimpleNamespace(app=app,workspace=canvas))
        packet=dict(identity=f.identity(INFO),updated_at=datetime.now(timezone.utc).isoformat())
        try:
            with patch.object(f,'load',return_value=packet):
                pages.select('forecast');root.after(100,root.quit);root.mainloop()
                pages.forecast_panel.canvas.yview_moveto(.25);before=pages.forecast_panel.canvas.yview()[0]
                self.assertGreater(before,.1);pages.request();root.after(100,root.quit);root.mainloop()
                self.assertAlmostEqual(pages.forecast_panel.canvas.yview()[0],before,delta=.02)
        finally:pages.close();close_fixture(root)

    def test_unchanged_paint_key_is_wire_stable_but_rejects_new_values_and_expiry(self):
        import json
        from datetime import timedelta
        packet=dict(updated_at=NOW.isoformat(),targets_at=NOW.isoformat(),targets={'mean':120},history=[('2026-11-25',100)])
        key=f.presentation_key(packet,NOW)
        self.assertEqual(key,f.presentation_key(json.loads(json.dumps(packet)),NOW))
        self.assertNotEqual(key,f.presentation_key(packet,NOW+timedelta(days=2)))
        changed=copy.deepcopy(packet);changed['targets']['mean']=121
        self.assertNotEqual(key,f.presentation_key(changed,NOW))

    def test_cached_view_gets_reserved_slot_with_53_200_background_jobs(self):
        from performance_dispatcher import Dispatcher
        from workspace_pages import Pages
        for count in (53,200):
            release=threading.Event();started=[threading.Event() for _ in range(3)];ready=threading.Event()
            dispatcher=Dispatcher(lambda fn,*a:fn(*a),workers=5,sample=lambda _:({'cpu':0,'ram':0},None))
            try:
                def hold(n):started[n].set();release.wait(5)
                for n,lane in enumerate(('misc','snapshot','eligibility')):
                    dispatcher.submit(lambda n=n:hold(n),lambda _:None,lambda _:None,key=('held',n),lane=lane);self.assertTrue(started[n].wait(2))
                for n in range(count):dispatcher.submit(lambda:None,lambda _:None,lambda _:None,key=('background',n),lane='feed_update')
                app=SimpleNamespace(root=SimpleNamespace(after=lambda *a:'timer'),closing=False,job=dispatcher.submit)
                pages=SimpleNamespace(closed=False,active='forecast',sequence=0,app=app,timer=None,context=None,capture=lambda:(INFO,'1H','Max','ru'),same=lambda _:True,render=lambda _:ready.set(),periodic=lambda:None)
                packet=dict(identity=f.identity(INFO),updated_at=datetime.now(timezone.utc).isoformat())
                with patch.object(f,'load',return_value=packet):Pages.request(pages);self.assertTrue(ready.wait(2))
            finally:release.set();dispatcher.close()

if __name__=='__main__':unittest.main()
