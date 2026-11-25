"""Unopened-stock prefetch and exact seven-day news expiry, no market data."""
import copy,json,sqlite3,sys,time,unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
from news_retention import WEEK,iso,merge,project,prune_store,digest
from news_engine import snapshot
from news_runtime import NewsRuntime


def news(n=0,at=None):
    return dict(title='Original company headline '+str(n),summary='Original facts',url='https://example.org/'+str(n),published=iso(time.time() if at is None else at))

class MemoryStore:
    def __init__(self):
        self.db=sqlite3.connect(':memory:');self.db.execute('CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT)')
    @contextmanager
    def connect(self):
        with self.db:yield self.db
    def setting(self,key,default=None):
        row=self.db.execute('SELECT value FROM settings WHERE key=?',(key,)).fetchone()
        return json.loads(row[0]) if row else default
    def set_setting(self,key,value):
        with self.db:self.db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)',(key,json.dumps(value)))

class RetentionTests(unittest.TestCase):
    def test_http_retry_after_date_is_not_shortened_or_bypassed(self):
        from email.utils import formatdate
        from news_http import FeedClient,FeedError
        now=time.time();response=Mock(status_code=429,headers={'Retry-After':formatdate(now+3*86400,usegmt=True)})
        response.__enter__=Mock(return_value=response);response.__exit__=Mock(return_value=False)
        session=SimpleNamespace(get=Mock(return_value=response))
        with patch('pathlib.Path.mkdir'),patch('pathlib.Path.read_text',side_effect=OSError),patch('pathlib.Path.write_text'):
            client=FeedClient(Path('.'),session=session,clock=lambda:now)
            with self.assertRaises(FeedError):client('https://feeds.finance.yahoo.com/rss/test')
            self.assertGreaterEqual(client.blocked['feeds.finance.yahoo.com'],int(now+3*86400)-1)
            with self.assertRaisesRegex(FeedError,'PROVIDER_COOLDOWN'):client('https://feeds.finance.yahoo.com/rss/test')
            self.assertEqual(session.get.call_count,1)

    def test_exact_boundary_merge_keeps_week_and_never_renews_publication_age(self):
        now=time.time();previous=snapshot([news(1,now-WEEK+20),news(2,now-100)],{},now=now)
        after=snapshot([news(3,now+10)],{},previous,now=now+20)
        self.assertEqual({r['url'] for r in after['articles']},{news(2)['url'],news(3)['url']})
        self.assertEqual(after['articles'][1]['first_seen_at'],previous['articles'][0]['first_seen_at'])
        expired=snapshot([news(2,now-100)],{},after,now=now+WEEK)
        self.assertNotIn(news(2)['url'],[r['url'] for r in expired['articles']])

    def test_missing_future_dates_and_failed_or_repeated_receipts_do_not_make_news_immortal(self):
        now=time.time();rows=[{**news(1),'published':None},{**news(2),'published':iso(now+2*WEEK)}]
        packet=snapshot(rows,{},now=now)
        empty=snapshot([],{'source':'OFFLINE'},packet,now=now+WEEK)
        self.assertFalse(empty['articles'])
        repeated=snapshot(rows,{},empty,now=now+WEEK+1)
        self.assertFalse(repeated['articles'])
        self.assertEqual(len(repeated['first_seen']),2)

    def test_more_than_one_feed_page_is_retained_and_visible_without_100_item_loss(self):
        from news_dashboard import prepare
        from news_translation import project as translated
        now=time.time();packet=snapshot([news(n,now-60) for n in range(100)],{},now=now)
        packet=snapshot([news(n,now) for n in range(100,200)],{},packet,now=now)
        self.assertEqual(len(packet['articles']),200)
        self.assertEqual(len(prepare(packet)['articles']),200)
        self.assertEqual(len(translated(packet['articles'],{},'ru')),200)

    def test_cleanup_prunes_articles_translations_bookmarks_and_preserves_account_and_calendar(self):
        from news_translation import fingerprint
        store=MemoryStore();self.addCleanup(store.db.close);now=time.time();old=news(1,now-WEEK);fresh=news(2,now-30)
        store.set_setting('news:A',{'articles':[old,fresh],'fetched_at':iso(now)})
        store.set_setting('news_translation:ru:A',{'items':{fingerprint(old):{'title':'old'},fingerprint(fresh):{'title':'fresh'}}})
        store.set_setting('ui_news_collection_v1',{'version':1,'items':{
            'old':{'item':{'target':'A','article':old},'saved':True},'fresh':{'item':{'target':'A','article':fresh},'favorite':True}}})
        store.set_setting('paper_account',{'sentinel':'KEEP'});store.set_setting('company_events:A',{'sentinel':'KEEP'})
        result=prune_store(store,now)
        self.assertEqual((result['removed'],result['translations'],result['marks']),(1,1,1))
        self.assertEqual(len(store.setting('news:A')['articles']),1)
        self.assertEqual(list(store.setting('ui_news_collection_v1')['items']),['fresh'])
        self.assertEqual(store.setting('paper_account'),{'sentinel':'KEEP'});self.assertEqual(store.setting('company_events:A'),{'sentinel':'KEEP'})
        self.assertEqual(prune_store(store,now)['changed'],0)

    def test_view_filters_expired_news_before_next_physical_cleanup(self):
        now=time.time();source={'articles':[news(1,now-WEEK),news(2,now-1)]};original=copy.deepcopy(source)
        self.assertEqual(len(project(source,now)['articles']),1);self.assertEqual(source,original)

    def test_background_translation_metadata_changes_only_on_relevant_source_and_respects_retry(self):
        from news_translation import update_cache
        now=time.time();rows=[news(n) for n in range(5)];translator=lambda row,target:dict(status='ALREADY_TARGET',target=target,title=row['title'],summary=row['summary'])
        cache=update_cache(rows,'en',{},translator=translator,now=now)
        self.assertEqual(cache['pending_count'],2);self.assertEqual(cache['source_digest'],digest(rows))
        cache=update_cache(rows,'en',cache,translator=translator,now=now+1);self.assertEqual(cache['pending_count'],0)
        self.assertNotEqual(cache['source_digest'],digest([{**rows[0],'summary':'Changed'}]))
        failed=update_cache(rows[:1],'de',{},translator=lambda *_:(_ for _ in ()).throw(ValueError('OFFLINE')),now=now)
        self.assertEqual(failed['pending_count'],0);self.assertEqual(failed['next_retry_at'],now+3600)

class RuntimeTests(unittest.TestCase):
    def app(self,store,stocks,job):
        store.directory=Path('.')
        return SimpleNamespace(store=store,watchlist=stocks,closing=False,root=SimpleNamespace(after=lambda *args:1,after_cancel=lambda *_:None),job=job)

    def test_53_and_200_unopened_stocks_all_prefetch_without_calendar_monopoly(self):
        for count in (53,200):
            store=MemoryStore();self.addCleanup(store.db.close);stocks=[dict(isin=str(n),name='Company '+str(n),profile_symbol='S'+str(n)) for n in range(count)];calls=[];lanes=[]
            def job(work,done,failed,**meta):
                lanes.append(meta['lane']);done(work());return True
            app=self.app(store,stocks,job);runtime=NewsRuntime(app);runtime.maintenance_next=time.time()+10000;runtime.company_next=time.time()+10000
            runtime.background_translation=lambda _:False
            def calendar():runtime.calendar_next=time.time()+10000;calls.append('calendar');return True
            runtime.refresh_calendar=calendar
            def company(info,client,previous):
                calls.append(info['isin']);return snapshot([news(int(info['isin']))],{},previous)
            def market(previous,fetch):calls.append('market');return snapshot([news(999)],{},previous)
            with patch('news_runtime.FeedClient',return_value=object()),patch('news_runtime.company_news',side_effect=company),patch('news_runtime.market_news',side_effect=market):
                for _ in range(count+4):runtime.tick()
            self.assertEqual(set(calls),{str(n) for n in range(count)}|{'market','calendar'})
            self.assertEqual(len(calls),count+2);self.assertEqual(set(lanes),{'news'})
            self.assertNotIn('symbol',app.__dict__)  # No chart/window selection is needed.

    def test_identity_change_before_worker_commit_discards_old_result(self):
        store=MemoryStore();self.addCleanup(store.db.close);queue=[]
        def job(work,done,failed,**meta):queue.append((work,done));return True
        app=self.app(store,[dict(isin='A',name='A',profile_symbol='A')],job);runtime=NewsRuntime(app)
        runtime.refresh('A','A');work,done=queue.pop(0)
        with patch('news_runtime.FeedClient',return_value=object()),patch('news_runtime.company_news',return_value=snapshot([news()],{})):result=work()
        done(result);self.assertIsNone(store.setting('news:A'))
        app.watchlist[0]['profile_symbol']='CHANGED';work,done=queue.pop(0);done(work())
        self.assertIsNone(store.setting('news:A'));self.assertFalse(runtime.busy)

    def test_background_translation_finds_unopened_stock_and_skips_ready_until_new_source(self):
        from news_translation import update_cache
        store=MemoryStore();self.addCleanup(store.db.close);rows=[news()];packet=snapshot(rows,{})
        store.set_setting('news:A',packet);app=self.app(store,[],lambda *_a,**_k:True);runtime=NewsRuntime(app);runtime.translate_news=Mock(return_value=True)
        with patch('i18n._language','en'):
            self.assertTrue(runtime.background_translation([('A','A')]));runtime.translate_news.assert_called_with('A',[],'en')
            cache=update_cache(packet['articles'],'en',{},translator=lambda r,t:dict(status='ALREADY_TARGET',target=t,title=r['title'],summary=r['summary']))
            store.set_setting('news_translation:en:A',cache);self.assertFalse(runtime.background_translation([('A','A')]))
            store.set_setting('news:A',snapshot([{**rows[0],'summary':'Changed facts'}],{},packet));self.assertTrue(runtime.background_translation([('A','A')]))

    def test_aged_news_progress_keeps_chart_trading_and_explicit_reads_ahead(self):
        from performance_dispatcher import Dispatcher
        dispatcher=Dispatcher.__new__(Dispatcher);dispatcher.visible=None;dispatcher.positions=set();dispatcher.resume=set()
        for total in (53,200):
            tasks=[dict(lane='feed_update',isin=str(n),queued=0,key=n,sequence=n) for n in range(total)]
            news_job=dict(lane='news',isin=None,queued=0,key='news',sequence=total)
            with patch('performance_dispatcher.time.monotonic',return_value=31):
                self.assertLess(dispatcher._rank(news_job),min(map(dispatcher._rank,tasks)))
                for lane in ('chart','trading','read'):
                    self.assertLess(dispatcher._rank({**news_job,'lane':lane}),dispatcher._rank(news_job))

if __name__=='__main__':unittest.main()
