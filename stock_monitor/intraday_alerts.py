"""Saved desktop alerts. Consumes confirmed closed observations, submits no orders."""
from datetime import datetime, timedelta, timezone
from uuid import uuid4
import json
import math
import time

import pandas as pd

from intraday_data import ALERT_KEY, BOOKS, SOURCES, base_frame, clean_frame, read, setting, BERLIN
from timeframes import endpoints


def add(store,config):
    rule={k:config[k] for k in ('isin','name','source','kind')}
    if rule['source'] not in SOURCES or rule['kind'] not in ('UP','DOWN','BUY','SELL'):
        raise ValueError('Некорректное правило')
    if rule['kind'] in ('UP','DOWN'):
        rule['level']=float(config['level'])
        if not math.isfinite(rule['level']) or rule['level']<=0:
            raise ValueError('Укажите положительную цену EUR')
    else:
        rule['book']=config['book']
        if rule['book'] not in BOOKS:
            raise ValueError('Укажите стратегию')
    rule.update(id=uuid4().hex,created=time.time(),enabled=True)
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        rows=setting(db,ALERT_KEY,[])
        if sum(r['enabled'] for r in rows)>=50:
            raise ValueError('Лимит 50 действующих уведомлений')
        rows=[r for r in rows if r['enabled']]+[r for r in rows if not r['enabled']][-149:]
        save(db,[*rows,rule])
    return rule


def save(db,rows):
    db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)',(ALERT_KEY,json.dumps(rows,ensure_ascii=False,allow_nan=False)))


def disable(store,key):
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        rows=setting(db,ALERT_KEY,[])
        for rule in rows:
            if rule['id']==key:
                rule['enabled']=False
        save(db,rows)


def crossing(before,after,level,direction):
    return before<level<=after if direction=='UP' else before>level>=after


def match(db,rule,now):
    kind=rule['kind']
    if kind in ('BUY','SELL'):
        report=setting(db,'paper_auto_status:'+rule['book']+':'+rule['isin'],{})
        for row in report.get('timeframes',{}).values():
            at=row.get('at')
            if (isinstance(at,(int,float)) and max(rule['created'],now.timestamp()-900)<at<=now.timestamp()
                    and row.get('core')=='READY' and row.get('signal')==kind):
                return dict(at=at,text=f"{rule['book']} {kind} {row.get('timeframe','')} · {row.get('reason') or ''}")
        return None
    local=now.tz_convert(BERLIN).tz_localize(None)
    frame=base_frame(db,rule['isin'],rule['source'],'15m',local-pd.Timedelta(hours=3),local)
    frame=clean_frame(frame,'15m',now).tail(2)
    if len(frame)!=2 or (frame.index[1]-frame.index[0]).total_seconds()!=900:
        return None
    at=endpoints(frame)[-1].timestamp()
    if not max(rule['created'],now.timestamp()-1800)<at<=now.timestamp():
        return None
    before,after=frame.close
    if crossing(before,after,rule['level'],kind):
        return dict(at=at,text=f"{after:.2f} EUR · {'выше' if kind=='UP' else 'ниже'} {rule['level']:.2f} EUR")
    return None


def evaluate(store,now=None):
    when=pd.Timestamp(now or datetime.now(timezone.utc))
    with read(store.path) as db:
        rows=setting(db,ALERT_KEY,[])
        candidates=[]
        for rule in rows:
            if not rule['enabled']:
                continue
            try:
                hit=match(db,rule,when)
            except (ValueError,TypeError,OverflowError):
                continue
            if hit:
                candidates.append((rule,hit))
    if not candidates:
        return []
    fired=[]
    with store.connect() as db:
        db.execute('BEGIN IMMEDIATE')
        rules=setting(db,ALERT_KEY,[])
        by_id={r['id']:r for r in rules}
        for original,hit in candidates:
            rule=by_id.get(original['id'])
            if rule==original:
                rule.update(enabled=False,hit=hit)
                fired.append(rule)
        if fired:
            save(db,rules)
    return fired


def start(app):
    """One bounded auxiliary job, using the existing Dispatcher."""
    busy=False

    def finished(result):
        nonlocal busy
        busy=False
        if not app.closing and result:
            from i18n import tr
            app.status.set(tr('Уведомление')+' · '+result[0]['name']+' · '+result[0]['hit']['text'])

    def work():
        fired=evaluate(app.store)
        if fired and app.store.setting('alerts_enabled',True):
            try:
                from plyer import notification
                notification.notify(title='Stock Monitor',message='\n'.join(f"{r['name']} · {r['hit']['text']}" for r in fired[:3]),
                                    app_name='Stock Monitor',timeout=10)
            except (ImportError,NotImplementedError,OSError):
                pass
        return fired

    def tick():
        nonlocal busy
        if app.closing:
            return
        if not busy:
            busy=True
            if app.job(work,finished,lambda _:finished([]),lane='misc',key='intraday-workspace-alerts') is False:
                busy=False
        app.root.after(30000,tick)

    app.root.after(30000,tick)
