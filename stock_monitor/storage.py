"""Durable local state for Stock Monitor. Timestamps are Europe/Berlin wall time."""
import json
import sqlite3
from pathlib import Path
from contextlib import contextmanager, closing


class Store:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory / 'stock_monitor.sqlite3'
        with self.connect() as db:
            # Readers on Tk must not wait for a feed transaction to commit.
            # Keep SQLite's FULL durability and automatic checkpointing.
            if sqlite3.sqlite_version_info >= (3,51,3):
                db.execute('PRAGMA journal_mode=WAL')
            db.execute('CREATE TABLE IF NOT EXISTS instruments (isin TEXT PRIMARY KEY, payload TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS candles (isin TEXT NOT NULL, source TEXT NOT NULL, base_tf TEXT NOT NULL, ts TEXT NOT NULL, open REAL, high REAL, low REAL, close REAL, volume REAL, PRIMARY KEY(isin, source, base_tf, ts))')
            db.execute('CREATE TABLE IF NOT EXISTS trades (isin TEXT NOT NULL, ts TEXT NOT NULL, price REAL NOT NULL, volume REAL NOT NULL, PRIMARY KEY(isin, ts, price, volume))')
            db.execute('CREATE INDEX IF NOT EXISTS candles_lookup ON candles(isin, base_tf, ts)')
            db.execute('CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS journal (id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT DEFAULT CURRENT_TIMESTAMP, kind TEXT NOT NULL, payload TEXT NOT NULL)')
            # Status readers request the newest entries of one kind. Without
            # this index an absent kind scans the entire growing journal on Tk.
            db.execute('CREATE INDEX IF NOT EXISTS journal_kind_id ON journal(kind, id DESC)')
            db.execute('CREATE TABLE IF NOT EXISTS series (isin TEXT, source TEXT, venue TEXT, currency TEXT, symbol TEXT, base_tf TEXT, ts TEXT, open REAL, high REAL, low REAL, close REAL, volume REAL, PRIMARY KEY(isin,source,venue,currency,symbol,base_tf,ts))')
            db.execute('PRAGMA user_version=3')
            from performance_cache import install_input_revisions
            install_input_revisions(db)
            from candle_timing import install
            install(db)
            from revision_watcher import install as install_revision_history
            install_revision_history(db)

    def save_series(self, info, frame, base):
        if frame.empty:return
        meta=frame.attrs
        required=('source','venue','currency','symbol')
        if not all(meta.get(k) for k in required):raise ValueError('Не указана идентичность ряда')
        rows=[(info['isin'],*[meta[k] for k in required],base,str(ts),*[float(r[k]) for k in ('open','high','low','close','volume')]) for ts,r in frame.iterrows()]
        with self.connect() as db:db.executemany('INSERT OR REPLACE INTO series VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',rows)

    def load_series(self, isin, base, source, symbol=None, venue=None, currency=None):
        import pandas as pd
        with self.connect() as db:
            sql='SELECT ts,open,high,low,close,volume,source,venue,currency,symbol FROM series WHERE isin=? AND base_tf=? AND source=?'
            args=[isin,base,source]
            if symbol:sql+=' AND symbol=?';args.append(symbol)
            if venue:sql+=' AND venue=?';args.append(venue)
            if currency:sql+=' AND currency=?';args.append(currency)
            rows=db.execute(sql+' ORDER BY ts',args).fetchall()
        if not rows:return pd.DataFrame()
        frame=pd.DataFrame(rows,columns=['ts','open','high','low','close','volume','source','venue','currency','symbol']).set_index('ts')
        if len(frame[['venue','currency','symbol']].drop_duplicates())!=1:raise ValueError('Выберите один ряд котировок')
        frame.index=pd.to_datetime(frame.index);frame.attrs={k:str(frame.iloc[-1][k]) for k in ('source','venue','currency','symbol')}
        if source=='gettex_chart' and base=='1M':
            from gettex_chart import attach_monthly_receipt
            frame=attach_monthly_receipt(self,isin,frame)
        if source=='proxy' and base in ('1m','5m','15m','1H'):
            coverage=self.setting('history_coverage:'+isin+':'+frame.attrs['symbol']+':'+base,{})
            if all(coverage.get(k)==frame.attrs[k] for k in ('source','venue','currency','symbol')):
                for k in ('timezone','intraday_span','intraday_blockers','intraday_normalization'):
                    if k in coverage:frame.attrs[k]=coverage[k]
            from intraday import normalize
            from data_quality import CALENDARS,TIMEZONES,QualityError
            # Known venue calendar only; never guess a different listing/timezone.
            frame.attrs.setdefault('timezone',TIMEZONES.get(CALENDARS.get(frame.attrs['venue'])))
            try:frame=normalize(frame,base,cached_quotes_only=True)
            except QualityError as error:
                # Keep all original rows and fail closed for subsequent DQ consumers.
                frame.attrs['intraday_blockers']=list(frame.attrs.get('intraday_blockers',[]))+[str(error)]
        from data_quality import attach_interval_evidence
        from data_quality import TIMEZONES,CALENDARS
        from history_service import attach_parent_provenance
        frame.attrs.setdefault('timezone',TIMEZONES.get(CALENDARS.get(frame.attrs['venue'])))
        return attach_parent_provenance(self,attach_interval_evidence(self,frame,isin),isin,base)

    def load_official(self, isin, base='15m'):
        import pandas as pd
        with self.connect() as db:rows=db.execute("SELECT ts,open,high,low,close,volume FROM candles WHERE isin=? AND source='gettex' AND base_tf=? ORDER BY ts",(isin,base)).fetchall()
        if not rows:return pd.DataFrame()
        frame=pd.DataFrame(rows,columns=['ts','open','high','low','close','volume']).set_index('ts');frame.index=pd.to_datetime(frame.index)
        frame['source']='gettex';frame.attrs={'source':'gettex','venue':'GETTEX','currency':'EUR','symbol':isin,'timezone':'Europe/Berlin','session_open':'07:30','session_close':'23:00','session_policy':'current published GETTEX stock hours; historical exceptions not verified'}
        from data_quality import attach_interval_evidence
        from history_service import attach_parent_provenance
        return attach_parent_provenance(self,attach_interval_evidence(self,frame,isin),isin,base)

    def save_watchlist(self, items):
        # A normal Save must not revoke every input revision through DELETE/INSERT.
        # Validate before writing; retain the existing atomic whole-list contract.
        rows=[(i['isin'],json.dumps(i,ensure_ascii=False,allow_nan=False)) for i in items]
        wanted=[isin for isin,_ in rows]
        if len(set(wanted))!=len(wanted):raise ValueError('Duplicate ISIN in watchlist')
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            self._write_watchlist(db,rows)

    @staticmethod
    def _write_watchlist(db, rows):
        # Shared with Save on Exit; caller owns the atomic write transaction.
        from eur_listings import OWNER_HISTORY_POLICY,owner_history_info
        policy=db.execute('SELECT value FROM settings WHERE key=?',(OWNER_HISTORY_POLICY,)).fetchone()
        policy=json.loads(policy[0]) if policy else {}
        rows=[(isin,json.dumps(owner_history_info(json.loads(payload),policy),ensure_ascii=False)) for isin,payload in rows]
        wanted=[isin for isin,_ in rows]
        if len(set(wanted))!=len(wanted):raise ValueError('Duplicate ISIN in watchlist')
        existing=dict(db.execute('SELECT isin,payload FROM instruments'))
        for isin in existing.keys()-set(wanted):
            db.execute('DELETE FROM instruments WHERE isin=?',(isin,))
        for isin,payload in rows:
            if isin not in existing:
                db.execute('INSERT INTO instruments VALUES (?,?)',(isin,payload))
            elif json.loads(existing[isin])!=json.loads(payload):
                db.execute('UPDATE instruments SET payload=? WHERE isin=?',(payload,isin))
        current=list(db.execute('SELECT rowid,isin FROM instruments ORDER BY rowid'))
        if [isin for _,isin in current]!=wanted:
            # Display order is not a calculation input.
            floor=min([0]+[rowid for rowid,_ in current])-len(wanted)-1
            for n,isin in enumerate(wanted):
                db.execute('UPDATE instruments SET rowid=? WHERE isin=?',(floor-n,isin))
            for n,isin in enumerate(wanted,1):
                db.execute('UPDATE instruments SET rowid=? WHERE isin=?',(n,isin))

    def setting(self, key, default=None):
        with self.connect() as db:
            row=db.execute('SELECT value FROM settings WHERE key=?',(key,)).fetchone()
        return json.loads(row[0]) if row else default

    def seed_defaults(self, items):
        if self.setting('initialised',False):return
        existing={i['isin'] for i in self.instruments()}
        for item in items:
            if item['isin'] not in existing:self.save_instrument(item)
        self.set_setting('initialised',True)

    def set_setting(self, key, value):
        with self.connect() as db:
            db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)',(key,json.dumps(value,ensure_ascii=False,allow_nan=False)))

    def event(self, kind, payload):
        with self.connect() as db:
            db.execute('INSERT INTO journal(kind,payload) VALUES (?,?)',(kind,json.dumps(payload,ensure_ascii=False,allow_nan=False)))

    def events(self, kind, limit=30):
        with self.connect() as db:
            rows=db.execute('SELECT at,payload FROM journal WHERE kind=? ORDER BY id DESC LIMIT ?',(kind,limit)).fetchall()
        return [{'at':r[0],**json.loads(r[1])} for r in rows]

    def remove_instrument(self, isin):
        with self.connect() as db:db.execute('DELETE FROM instruments WHERE isin=?',(isin,))
        self.event('instrument_removed',{'isin':isin})

    def backup(self, destination):
        with self.connect() as src:
            with closing(sqlite3.connect(destination)) as dest:src.backup(dest)

    def import_legacy(self, path):
        import re
        with closing(sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True)) as source:
            if 'payload' not in [r[1] for r in source.execute('PRAGMA table_info(instruments)')]:
                raise ValueError('Это не база Stock Monitor v0.10/v0.11.')
            instruments=[json.loads(r[0]) for r in source.execute('SELECT payload FROM instruments')]
            for item in instruments:
                if not re.fullmatch(r'[A-Z]{2}[A-Z0-9]{9}\d',item.get('isin','')) or not item.get('name'):
                    raise ValueError('В базе есть некорректный инструмент.')
            with self.connect() as target:
                for item in instruments:
                    target.execute('INSERT OR REPLACE INTO instruments VALUES (?,?)',(item['isin'],json.dumps(item,ensure_ascii=False)))
                for table,columns in [('candles','isin,source,base_tf,ts,open,high,low,close,volume'),('trades','isin,ts,price,volume')]:
                    placeholders=','.join('?' for _ in columns.split(','))
                    cursor=source.execute('SELECT '+columns+' FROM '+table)
                    while rows:=cursor.fetchmany(10000):
                        target.executemany('INSERT OR IGNORE INTO '+table+' VALUES ('+placeholders+')',rows)
        return len(instruments)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        try:
            with db:
                yield db
        finally:
            db.close()

    def save_instrument(self, item):
        with self.connect() as db:
            from eur_listings import OWNER_HISTORY_POLICY,owner_history_info
            policy=db.execute('SELECT value FROM settings WHERE key=?',(OWNER_HISTORY_POLICY,)).fetchone()
            item=owner_history_info(item,json.loads(policy[0]) if policy else {})
            db.execute('INSERT OR REPLACE INTO instruments VALUES (?,?)', (item['isin'], json.dumps(item, ensure_ascii=False)))

    def instruments(self):
        with self.connect() as db:
            return [json.loads(row[0]) for row in db.execute('SELECT payload FROM instruments ORDER BY rowid')]

    def save_frame(self, isin, source, base_tf, frame):
        rows = [(isin, source, base_tf, str(ts), float(r.open), float(r.high), float(r.low), float(r.close), float(r.volume)) for ts, r in frame.iterrows()]
        with self.connect() as db:
            db.executemany('INSERT OR REPLACE INTO candles VALUES (?,?,?,?,?,?,?,?,?)', rows)

    def load_frame(self, isin, base_tf='15m'):
        import pandas as pd
        with self.connect() as db:
            rows = db.execute("SELECT ts,open,high,low,close,volume,source FROM candles WHERE isin=? AND base_tf=? ORDER BY ts, CASE source WHEN 'gettex' THEN 2 WHEN 'csv' THEN 1 ELSE 0 END", (isin, base_tf)).fetchall()
        if not rows:
            return pd.DataFrame(), set()
        frame = pd.DataFrame(rows, columns=['ts','open','high','low','close','volume','source'])
        frame = frame.drop_duplicates('ts', keep='last').set_index('ts')
        frame.index = pd.to_datetime(frame.index)
        return frame, set(frame.source)

    def save_trades(self, isin, trades):
        rows = [(isin, str(ts), float(r.price), float(r.volume)) for ts, r in trades.iterrows()]
        with self.connect() as db:
            db.executemany('INSERT OR IGNORE INTO trades VALUES (?,?,?,?)', rows)

    def load_trades(self, isin):
        import pandas as pd
        with self.connect() as db:
            rows = db.execute('SELECT ts,price,volume FROM trades WHERE isin=? ORDER BY ts', (isin,)).fetchall()
        if not rows:
            return pd.DataFrame(columns=['price','volume'])
        frame = pd.DataFrame(rows, columns=['ts','price','volume']).set_index('ts')
        frame.index = pd.to_datetime(frame.index)
        return frame


from performance_cache import CachedStoreMixin
from save_exit import DraftSettingsMixin
class Store(DraftSettingsMixin, CachedStoreMixin, Store):
    pass
