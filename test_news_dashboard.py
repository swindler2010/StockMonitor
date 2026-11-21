import sys,time
import tkinter as tk
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch
from datetime import datetime,timezone

sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
from news_dashboard import Dialog,prepare,filtered,details,chart_preview,PAGE_SIZE
from main_screen import theme
from themes import THEMES


def article(name='Apple',n=0):
    return dict(title=f'{name} earnings {n}',url=f'https://example.com/{name}/{n}',published=datetime.now(timezone.utc).isoformat(),
                provider='Example',summary='Original source text',scope='company',publisher_status='LINK_DOMAIN_ONLY_ORIGINAL_PUBLISHER_UNCONFIRMED')


class NewsDashboardTests(unittest.TestCase):
    def setUp(self):
        self.root=tk.Tk();self.root.withdraw();self.addCleanup(self.root.destroy)
        self.jobs=[]
        self.saved={'news:A':dict(status='AVAILABLE',articles=[article()]),'news:N':dict(status='AVAILABLE',articles=[article('NVIDIA')])}
        self.app=SimpleNamespace(root=self.root,watchlist=[dict(name='Apple',isin='A',profile_symbol='APC'),dict(name='NVIDIA',isin='N',profile_symbol='NVD')],
            symbol=tk.StringVar(self.root,'Apple'),store=SimpleNamespace(setting=lambda key,default=None:self.saved.get(key,default)),
            news_runtime=SimpleNamespace(refresh=Mock(return_value=True)),tooltip=Mock(),
            job=lambda work,done,failed,**kwargs:self.jobs.append(kwargs) or done(work()) or True)
        theme(self.app,THEMES['Тёмная']);self.dialog=Dialog(self.app);self.root.update()

    def test_old_target_reply_cannot_replace_selected_news_and_close_removes_listener(self):
        d=self.dialog;generation=d.generation
        self.assertEqual(self.jobs[0]['lane'],'read')
        self.assertTrue(d.rows[0]['title'].startswith('Apple'))
        d.choice.current(2);d.load();self.root.update()
        d.receive('A',generation,prepare(self.saved['news:A']))
        self.assertTrue(d.rows[0]['title'].startswith('NVIDIA'))
        d.refresh();self.app.news_runtime.refresh.assert_called_once_with('N','NVD')
        d.close();self.assertIsNone(self.app._news_listener)

    def test_paging_caps_widgets_and_provider_filters_do_not_mutate_sources(self):
        d=self.dialog
        value=dict(status='AVAILABLE',articles=[article(n=i) for i in range(40)])
        packet=prepare(value);d.receive('A',d.generation,packet)
        self.assertEqual(len(d.shown),PAGE_SIZE);self.assertEqual(len(d.cards.winfo_children()),PAGE_SIZE)
        d.change_page(1);self.assertEqual(len(d.shown),PAGE_SIZE)
        d.query.set('nothing matches');d.render();self.assertFalse(d.shown)
        self.assertEqual(len(value['articles']),40)

    def test_original_links_and_source_uncertainty_remain_visible(self):
        row=prepare(dict(articles=[article()]))['articles'][0]
        self.assertIn('первичный издатель может отличаться',details(row))
        self.assertIn('не проверка биржевой записи',details(row))
        with patch('news_dashboard.webbrowser.open') as opened:
            self.dialog.original(row);opened.assert_called_once_with(row['url'])
            self.dialog.original({'url':'javascript:alert(1)'});self.assertEqual(opened.call_count,1)

    def test_periodic_cache_wait_keeps_last_visible_articles(self):
        d=self.dialog;before=list(d.rows);pending=[]
        self.app.job=lambda work,done,failed,**kwargs:pending.append((work,done,failed)) or True
        d.load(clear=False);self.assertEqual(d.rows,before)
        pending[0][2]('QUEUE_BUSY');self.assertEqual(d.rows,before)

    def test_time_filters_do_not_turn_unknown_or_future_dates_into_recent_news(self):
        now=time.time();rows=prepare(dict(articles=[article(n=1),{**article(n=2),'published':None},
            {**article(n=3),'published':datetime.fromtimestamp(now+86400,timezone.utc).isoformat()}]))['articles']
        self.assertEqual(len(filtered(rows,days=1,now=now+1)),1)
        self.assertEqual(len(filtered(rows,days=None,now=now+1)),3)

    def test_preview_never_borrows_another_instruments_chart(self):
        import pandas as pd
        frame=pd.DataFrame({'close':range(100,170)},index=pd.date_range('2026-11-01',periods=70,freq='h'))
        frame.attrs.update(currency='EUR',venue='GETTEX')
        self.app._accepted_trading_chart=({'watchlist':self.app.watchlist,'symbol':'Apple','timeframe':'1H'},{'frame':frame})
        self.assertIsNone(chart_preview(self.app,'N'))
        self.assertEqual(len(chart_preview(self.app,'A')['prices']),60)


if __name__=='__main__':unittest.main()
