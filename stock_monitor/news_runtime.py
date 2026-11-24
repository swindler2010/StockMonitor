"""Sequential news refresh using the application's existing Dispatcher."""
import copy
from datetime import datetime
import time
from news_engine import company_news, market_news
from news_http import FeedClient


def due_targets(store, targets, now, *, prefix='news:', interval=1800):
    """Only timestamps on Tk, one bounded query, not every article archive."""
    if not targets:return []
    keys=[prefix+key for key,_ in targets]
    with store.connect() as db:
        times=dict(db.execute("SELECT key,json_extract(value,'$.attempted_at') FROM settings WHERE key IN ("+
                              ','.join('?' for _ in keys)+')',keys))
    result=[]
    for key,symbol in targets:
        try:attempted=datetime.fromisoformat(times.get(prefix+key)).timestamp()
        except (ValueError,TypeError):attempted=0
        if now-attempted>interval:result.append((attempted,key,symbol))
    return result


class NewsRuntime:
    def __init__(self, app):
        self.app, self.busy, self.timer = app, False, None
        self.client = None
        self.calendar_next = None
        self.company_next = 0
        self.premarket_next = 0
        self.maintenance_next=0;self.news_streak=0;self.aux_turn=0

    def wake(self,delay=250):
        root=getattr(self.app,'root',None)
        if root is None or self.app.closing:return
        if self.timer is not None:root.after_cancel(self.timer)
        self.timer=root.after(delay,self.tick)

    def start(self):
        if self.timer is None:self.wake(1000)

    def maintain(self):
        from news_retention import prune_store
        self.busy=True
        def done(result):
            self.busy=False;self.maintenance_next=time.time()+60
            manager=getattr(self.app,'_ui_collections',{}).get('ui_news_collection_v1')
            if manager and manager.loaded and not manager.busy and result.get('collection') is not None:
                from ui_collections import decode
                items=decode(result['collection'],manager.clean,manager.flags,manager.limit)
                if manager.items!=items:manager.items=items;manager.notify()
            self.wake()
        def failed(_):self.busy=False;self.maintenance_next=time.time()+60;self.wake()
        if self.app.job(lambda:prune_store(self.app.store),done,failed,key='news-retention',lane='news') is False:failed(None)

    def background_translation(self,targets):
        from i18n import language
        target=language();keys=['news:'+key for key,_ in targets]
        if not keys:return False
        sql="""SELECT n.key,json_extract(t.value,'$.attempted_at') FROM settings n
        LEFT JOIN settings t ON t.key='news_translation:'||?||':'||substr(n.key,6)
        WHERE n.key IN ("""+','.join('?' for _ in keys)+""")
        AND json_array_length(n.value,'$.articles')>0 AND
        (json_type(t.value,'$.pending_count') IS NULL OR
         json_extract(n.value,'$.content_digest') IS NOT json_extract(t.value,'$.source_digest') OR
         json_extract(t.value,'$.pending_count')>0 OR json_extract(t.value,'$.next_retry_at')<=?)
        ORDER BY coalesce(json_extract(t.value,'$.attempted_at'),'') LIMIT 1"""
        with self.app.store.connect() as db:row=db.execute(sql,[target,*keys,time.time()]).fetchone()
        return bool(row and self.translate_news(row[0][5:],[],target))

    def tick(self):
        self.timer=None
        if self.app.closing:return
        self.wake(10000)
        if self.busy:return
        now=time.time()
        if now>=self.maintenance_next:self.maintain();return
        targets=[('market',None)]+[(i['isin'],i.get('profile_symbol') or i.get('history_symbol'))
            for i in self.app.watchlist if i.get('kind')!='benchmark']
        due=due_targets(self.app.store,targets,now)
        if due and self.news_streak<4:
            _,key,symbol=min(due,key=lambda x:x[0])
            if self.refresh(key,symbol):self.news_streak+=1
            return
        # Four source turns, then at most one auxiliary turn. Company calendar
        # requests can no longer consume every tick before unopened stocks.
        if self.calendar_next is None:
            from official_calendar import SETTING
            with self.app.store.connect() as db:
                row=db.execute("SELECT json_extract(value,'$.attempted_at') FROM settings WHERE key=?",(SETTING,)).fetchone()
            try:self.calendar_next=datetime.fromisoformat(row[0]).timestamp()+86400
            except (ValueError,TypeError,IndexError):self.calendar_next=0
        for offset in range(4):
            choice=(self.aux_turn+offset)%4
            if choice==0:
                accepted=self.background_translation(targets)
            elif choice==1:
                accepted=now>=self.calendar_next and self.refresh_calendar()
            elif choice==2:
                accepted=False
                if now>=self.company_next and callable(getattr(self,'refresh_company_events',None)):
                    from company_events import CALENDAR_TTL,PREFIX
                    from paper_account import instrument_allowed
                    stocks=[i for i in self.app.watchlist if instrument_allowed(i) and (i.get('profile_symbol') or i.get('history_symbol'))]
                    by_isin={i['isin']:i for i in stocks}
                    events=due_targets(self.app.store,[(i['isin'],i.get('profile_symbol') or i.get('history_symbol')) for i in stocks],now,prefix=PREFIX,interval=CALENDAR_TTL)
                    self.company_next=now+60
                    if events:accepted=self.refresh_company_events(by_isin[min(events,key=lambda v:v[0])[1]])
            else:
                accepted=now>=self.premarket_next and self.refresh_premarket()
            if accepted:self.aux_turn=(choice+1)%4;self.news_streak=0;return
        if due:
            _,key,symbol=min(due,key=lambda x:x[0])
            if self.refresh(key,symbol):self.news_streak+=1

    def refresh(self, key, symbol, callback=None):
        if self.busy or self.app.closing:
            return False
        previous = None
        instrument=copy.deepcopy(next((i for i in self.app.watchlist if i['isin']==key),None))
        if key!='market' and not instrument:return False
        identity=lambda i: (i.get('isin'),i.get('name'),i.get('profile_symbol'),i.get('history_symbol')) if i else None
        self.busy = True
        def done(result):
            self.busy = False
            if self.app.closing:
                return
            current = next((i for i in self.app.watchlist if i['isin'] == key), None)
            if key != 'market' and identity(current)!=identity(instrument):
                self.wake();return  # Removed/remapped: never publish stale identity.
            self.busy=True
            def publish():
                current=next((i for i in self.app.watchlist if i['isin']==key),None)
                if self.app.closing or (key!='market' and identity(current)!=identity(instrument)):return None
                self.app.store.set_setting('news:'+key,result)
                return result
            def published(value):
                self.busy=False;self.wake()
                current=next((i for i in self.app.watchlist if i['isin']==key),None)
                if value is None or self.app.closing or (key!='market' and identity(current)!=identity(instrument)):return
                listener=getattr(self.app,'_news_listener',None)
                if listener:listener(key,value)
                if callback:callback(value)
            def publish_failed(_):self.busy=False;self.wake(10000)
            if self.app.job(publish,published,publish_failed,key=('news-publish',key),lane='news') is False:publish_failed(None)
        def failed(message):
            from datetime import timezone
            if previous is None:
                # Failed before loading cache: retain it verbatim for the next
                # bounded timer attempt, never overwrite it with empty news.
                self.busy=False;self.wake(10000)
                return
            done({**previous, 'status':'STALE_CACHE' if previous.get('articles') else 'ERROR',
                  'attempted_at':datetime.now(timezone.utc).isoformat(),
                  'errors':{'dispatcher':'JOB_FAILED'}})
        cache = self.app.store.directory/'provider_cache'
        def work():
            nonlocal previous
            previous=copy.deepcopy(self.app.store.setting('news:'+key, {}))
            if self.client is None:self.client=FeedClient(cache/'news-rss')
            return market_news(previous,fetch=self.client) if key=='market' else company_news(instrument,self.client,previous)
        accepted=self.app.job(work,
                     done, failed, key='news:'+key, lane='news', isin=None if key == 'market' else key)
        if not accepted:self.busy=False
        return bool(accepted)

    def refresh_premarket(self):
        if self.busy or self.app.closing:return False
        from premarket_runtime import PREFIX,COOLDOWN,fetch,identity,matches
        self.premarket_next=time.time()+60
        selected=getattr(self.app,'symbol',None)
        if selected is None:return False
        current=next((i for i in self.app.watchlist if i.get('name')==selected.get() and i.get('kind')!='benchmark'),None)
        if not current:return False
        info=copy.deepcopy(current);key=PREFIX+info['isin'];self.busy=True
        def work():
            previous=self.app.store.setting(key,{});blocked=self.app.store.setting(COOLDOWN,{})
            packet,cooldown=fetch(info,previous,blocked)
            return packet,cooldown,previous,blocked
        def done(value):
            self.busy=False;self.wake()
            if self.app.closing:return
            packet,cooldown,previous,blocked=value
            current=next((i for i in self.app.watchlist if i.get('isin')==info['isin']),None)
            if not current or identity(current)!=identity(info) or not matches(packet,current):return
            if packet!=previous:self.app.store.set_setting(key,packet)
            if cooldown and cooldown!=blocked:self.app.store.set_setting(COOLDOWN,cooldown)
            screen=getattr(self.app,'main_screen',None)
            if screen and self.app.symbol.get()==current['name']:screen.load_premarket(current,force=True)
        def failed(_):self.busy=False;self.premarket_next=time.time()+300;self.wake()
        accepted=self.app.job(work,done,failed,key=key,lane='news',isin=info['isin'])
        if not accepted:self.busy=False
        return bool(accepted)

    def refresh_calendar(self):
        """One global daily turn in the existing sequential auxiliary worker."""
        if self.busy or self.app.closing: return False
        from official_calendar import SETTING, refresh
        self.busy = True
        def work():
            previous = self.app.store.setting(SETTING, {})
            if self.client is None: self.client = FeedClient(self.app.store.directory/'provider_cache'/'news-rss')
            result = refresh(previous, self.client)
            self.app.store.set_setting(SETTING, result)
            return result
        def done(result):
            self.busy = False
            self.calendar_next = time.time() + 86400
            self.wake()
            if self.app.closing: return
            listener = getattr(self.app, '_calendar_listener', None)
            if listener: listener(result)
        def failed(_):
            self.busy = False
            self.calendar_next = time.time() + 3600
            self.wake()
        accepted = self.app.job(work, done, failed, key='official-calendar', lane='news')
        if not accepted: failed(None)
        return bool(accepted)

    def translate_news(self, key, rows, target, callback=None):
        """At most three local articles per turn; same sequential news owner."""
        if self.busy or self.app.closing: return False
        from news_translation import setting, update_cache, cached_snapshot, fingerprint
        wanted = [fingerprint(r) for r in rows[:12]]
        self.busy = True
        def work():
            snapshot = self.app.store.setting('news:'+key, {})
            current = snapshot.get('articles') or []
            ordered = sorted(current, key=lambda r: fingerprint(r) not in wanted)
            cache = update_cache(ordered, target, self.app.store.setting(setting(key, target), {}))
            # Re-read source fingerprints before saving a display cache. A changed
            # headline/summary can never inherit another input's translation.
            latest = self.app.store.setting('news:'+key, {})
            valid = {fingerprint(r) for r in (latest.get('articles') or [])}
            cache['items'] = {k: v for k, v in cache['items'].items() if k in valid}
            self.app.store.set_setting(setting(key, target), cache)
            return cached_snapshot(self.app.store, key, target)
        def done(result):
            self.busy = False
            if self.app.closing: return
            if callback: callback(result)
            else:
                listener=getattr(self.app,'_news_listener',None)
                if listener:listener(key,result)
            self.wake()
        def failed(_):
            self.busy = False;self.wake(10000)
        accepted = self.app.job(work, done, failed, key=('news-translation', key, target), lane='news')
        if not accepted: failed(None)
        return bool(accepted)
