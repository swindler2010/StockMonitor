"""Sequential news refresh using the application's existing Dispatcher."""
import copy
from datetime import datetime
import time
from news_engine import company_news, market_news
from news_http import FeedClient


def due_targets(store, targets, now):
    """Only timestamps on Tk, one bounded query, not every article archive."""
    if not targets:return []
    keys=['news:'+key for key,_ in targets]
    with store.connect() as db:
        times=dict(db.execute("SELECT key,json_extract(value,'$.attempted_at') FROM settings WHERE key IN ("+
                              ','.join('?' for _ in keys)+')',keys))
    result=[]
    for key,symbol in targets:
        try:attempted=datetime.fromisoformat(times.get('news:'+key)).timestamp()
        except (ValueError,TypeError):attempted=0
        if now-attempted>1800:result.append((attempted,key,symbol))
    return result


class NewsRuntime:
    def __init__(self, app):
        self.app, self.busy, self.timer = app, False, None
        self.client = None
        self.calendar_next = None

    def start(self):
        if self.timer is None:
            self.timer = self.app.root.after(10000, self.tick)

    def tick(self):
        self.timer = None
        if self.app.closing:
            return
        if self.calendar_next is None:
            from official_calendar import SETTING
            with self.app.store.connect() as db:
                row = db.execute("SELECT json_extract(value,'$.attempted_at') FROM settings WHERE key=?", (SETTING,)).fetchone()
            try: self.calendar_next = datetime.fromisoformat(row[0]).timestamp() + 86400
            except (ValueError, TypeError, IndexError): self.calendar_next = 0
        if not self.busy and time.time() >= self.calendar_next:
            self.refresh_calendar()
        if not self.busy:
            targets = [('market', None)] + [(i['isin'], i.get('profile_symbol') or i.get('history_symbol'))
                                           for i in self.app.watchlist if i.get('kind') != 'benchmark']
            due=due_targets(self.app.store,targets,time.time())
            if due:
                _,key,symbol=min(due,key=lambda x:x[0])
                self.refresh(key,symbol)
        self.timer = self.app.root.after(10000, self.tick)

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
                return  # Removed or remapped instrument: never publish stale identity.
            self.app.store.set_setting('news:'+key, result)
            listener=getattr(self.app,'_news_listener',None)
            if listener:listener(key,result)
            if callback:
                callback(result)
        def failed(message):
            from datetime import timezone
            if previous is None:
                # Failed before loading cache: retain it verbatim for the next
                # bounded timer attempt, never overwrite it with empty news.
                self.busy=False
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
                     done, failed, key='news:'+key, lane='misc', isin=None if key == 'market' else key)
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
            if self.app.closing: return
            listener = getattr(self.app, '_calendar_listener', None)
            if listener: listener(result)
        def failed(_):
            self.busy = False
            self.calendar_next = time.time() + 3600
        accepted = self.app.job(work, done, failed, key='official-calendar', lane='misc')
        if not accepted: failed(None)
        return bool(accepted)

    def translate_news(self, key, rows, target, callback=None):
        """At most three local articles per turn; same single misc-worker ownership."""
        if self.busy or self.app.closing: return False
        from news_translation import setting, update_cache, cached_snapshot, fingerprint
        wanted = [fingerprint(r) for r in rows[:12]]
        self.busy = True
        def work():
            snapshot = self.app.store.setting('news:'+key, {})
            current = snapshot.get('articles') or []
            ordered = sorted(current[:100], key=lambda r: fingerprint(r) not in wanted)
            cache = update_cache(ordered, target, self.app.store.setting(setting(key, target), {}))
            # Re-read source fingerprints before saving a display cache. A changed
            # headline/summary can never inherit another input's translation.
            latest = self.app.store.setting('news:'+key, {})
            valid = {fingerprint(r) for r in (latest.get('articles') or [])[:100]}
            cache['items'] = {k: v for k, v in cache['items'].items() if k in valid}
            self.app.store.set_setting(setting(key, target), cache)
            return cached_snapshot(self.app.store, key, target)
        def done(result):
            self.busy = False
            if self.app.closing: return
            if callback: callback(result)
        def failed(_):
            self.busy = False
        accepted = self.app.job(work, done, failed, key=('news-translation', key, target), lane='misc')
        if not accepted: failed(None)
        return bool(accepted)
