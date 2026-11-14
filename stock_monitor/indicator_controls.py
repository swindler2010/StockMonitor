"""Bounded chart-setting control. Never changes indicator formulas or CORE."""
import copy
import hashlib
import json
import math
import sqlite3
from concurrent.futures import Future
from contextlib import closing
from pathlib import Path

DEFAULT_IND={'EMA':True,'SMA':False,'Bollinger':False,'VWAP':False,'RSI':False,'MACD':False,'ATR':False,'Объём':True,'Средний объём':False,'Относительный объём':False,'ema_fast':10,'ema_slow':30,'sma':20,'rsi':14,'atr':14,'bb':20,'bb_dev':2,'volume':20}
DEFAULT_IND.update(macd_fast=12,macd_slow=26,macd_signal=9)
DEFAULT_IND['Volume Profile']=False
DEFAULT_IND['Уровни анализа']=True
KEY='global_indicators'
GROUPS={'EMA':('ema_fast','ema_slow'),'SMA':('sma',),'Bollinger':('bb','bb_dev'),'VWAP':(),
        'RSI':('rsi',),'MACD':('macd_fast','macd_slow','macd_signal'),'ATR':('atr',),
        'Объём':(),'Средний объём':('volume',),'Относительный объём':('volume',),
        'Volume Profile':(),'Уровни анализа':()}


def validate(changes,current):
    if not isinstance(changes,dict) or not changes or set(changes)-set(DEFAULT_IND):
        raise ValueError('INDICATORS_INVALID_KEYS')
    for key,value in changes.items():
        if isinstance(DEFAULT_IND[key],bool):
            if type(value) is not bool:raise ValueError('INDICATORS_BOOLEAN_REQUIRED')
        elif key=='bb_dev':
            if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or not 1<=value<=500:
                raise ValueError('INDICATORS_INVALID_DEVIATION')
        elif type(value) is not int or not 1<=value<=500:raise ValueError('INDICATORS_INVALID_PERIOD')
    result={**DEFAULT_IND,**current,**changes}
    if result['macd_fast']>=result['macd_slow']:raise ValueError('INDICATORS_MACD_ORDER')
    return result


def _stored(db):
    row=db.execute('SELECT value FROM settings WHERE key=?',(KEY,)).fetchone()
    value=json.loads(row[0]) if row else {}
    if not isinstance(value,dict):raise ValueError('INDICATORS_INVALID_STORED_SETTINGS')
    rev=db.execute('SELECT revision FROM input_revisions WHERE scope=?',('setting:'+KEY,)).fetchone()
    return value,rev[0] if rev else 0


def revision(value,number,pending):
    return hashlib.sha256(json.dumps([value,number,pending],sort_keys=True,ensure_ascii=False,allow_nan=False).encode()).hexdigest()


def snapshot(app):
    # Only one small settings row; no history, analysis or account read.
    with closing(sqlite3.connect(Path(app.store.path).resolve().as_uri()+'?mode=ro',uri=True,timeout=.2)) as db:
        db.execute('BEGIN');stored,number=_stored(db)
    pending=copy.deepcopy(getattr(app.store,'pending_user_settings',{}).get(KEY))
    settings={**DEFAULT_IND,**(pending if pending is not None else stored)}
    groups=[dict(name=name,enabled=settings[name],parameters={k:settings[k] for k in keys},
                 effective_enabled=bool(settings[name] and (settings['Объём'] if name=='Средний объём' else True)))
            for name,keys in GROUPS.items()]
    return dict(status='AVAILABLE',scope='ALL_CHARTS',settings=settings,indicators=groups,
                revision=revision(stored,number,pending),saved=pending is None,unsaved_local_changes=pending is not None,
                write_in_progress=bool(getattr(app,'_indicator_write_busy',False)),
                parameter_limits={'periods':'integers 1..500','bb_dev':'finite number 1..500','macd':'fast < slow'},
                volume_profile={'bins':24,'approximation':True,'method':'Existing OHLCV price-bin allocation; not exchange tick volume',
                                'poc_vah_val':'NOT_CALCULATED','scope':'visible viewport in UI; requested history in analytical export'},
                semantics='Chart settings only. Fixed CORE/strategy indicators and trade permissions are unchanged.')


def persist(path,expected,changes,valid):
    """Existing setting + revision triggers; atomic CAS, never saves other drafts."""
    with closing(sqlite3.connect(path,timeout=.2)) as db:
        with db:
            db.execute('BEGIN IMMEDIATE');stored,number=_stored(db)
            if revision(stored,number,None)!=expected:raise ValueError('INDICATORS_CHANGED')
            selected=validate(changes,stored)
            if not valid():raise ValueError('INDICATORS_COMMAND_NO_LONGER_VALID')
            if selected!={**DEFAULT_IND,**stored}:
                db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)',(KEY,json.dumps(selected,ensure_ascii=False,allow_nan=False)))
            if not valid():raise ValueError('INDICATORS_COMMAND_NO_LONGER_VALID')
            after,number=_stored(db)
        return dict(status='SAVED',saved=True,settings={**DEFAULT_IND,**after},revision=revision(after,number,None))


def dispatch(app,args):
    def blocked(code):return dict(status='BLOCKED',saved=False,code=code)
    if getattr(app,'_indicator_write_busy',False):return blocked('INDICATORS_BUSY')
    if not app.store.setting('mcp_allow_edits',False):return blocked('APPLICATION_EDITS_DISABLED')
    current=snapshot(app)
    if current['unsaved_local_changes']:return blocked('INDICATORS_UNSAVED_LOCAL_CHANGES')
    if current['revision']!=args['expected_revision']:return blocked('INDICATORS_CHANGED')
    try:validate(args['changes'],current['settings'])
    except ValueError as error:return blocked(str(error))
    queue=app.bridge.commands;cid=args['_command_id'];future=Future();app._indicator_write_busy=True
    def valid():return not getattr(app,'closing',False) and queue.proposal_valid(cid)
    def work():
        try:return persist(app.store.path,args['expected_revision'],args['changes'],valid)
        except sqlite3.Error:return blocked('INDICATORS_STORE_BUSY')
        except ValueError as error:
            code=str(error)
            return blocked(code if code.startswith('INDICATORS_') else 'INDICATORS_WRITE_FAILED')
    def done(result):
        app._indicator_write_busy=False
        if result.get('saved'):
            try:
                # The persisted setting is read by the existing rendering path.
                app.refresh_chart();result['ui_refresh_requested']=True
            except Exception:result['ui_refresh_requested']=False
        future.set_result(result)
    def failed(_):
        app._indicator_write_busy=False;future.set_result(blocked('INDICATORS_WRITE_FAILED'))
    # Explicit bounded UI request uses the existing responsive lane. It is
    # never a cancellable chart task and never interrupts a running writer.
    if app.job(work,done,failed,lane='read',key='indicator-settings-write') is False:failed(None)
    return future
