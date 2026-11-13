"""Explicit isolated Windows/Bridge acceptance harness; never opens TEST data.

Run with --serve (then --bridge-test in another process). All files go to the
owner's SM_TEST_ARCHIVE. Fictional prices/CORE WAIT are test fixtures only.
"""
import argparse
import json
import os
import queue
import sqlite3
import sys
import time
from pathlib import Path
from types import SimpleNamespace

ROOT=Path(__file__).resolve().parent
sys.path.insert(0,str(ROOT/'stock_monitor'))
ARCHIVE=Path(os.environ['SM_TEST_ARCHIVE']).resolve() if os.environ.get('SM_TEST_ARCHIVE') else None
DB=ARCHIVE/'AD изолированный PAPER.sqlite3' if ARCHIVE else None


def require_archive():
    if ARCHIVE is None or not ARCHIVE.is_dir():
        raise ValueError('Set SM_TEST_ARCHIVE to the existing isolated archive')


class Directory:
    def __truediv__(self,name):
        return DB if name=='stock_monitor.sqlite3' else ARCHIVE/('AD isolated '+name)
    def mkdir(self,**kwargs):
        if not ARCHIVE.is_dir():raise ValueError('Existing archive required')


def serve():
    require_archive()
    import tkinter as tk
    import pandas as pd
    from test_paper_sizing import SizingTests
    import paper_hourly as h
    import paper_protection as protection
    from performance_cache import ReadCache
    from performance_dispatcher import Dispatcher
    from features import Features
    from bridge import Bridge
    import paper_view
    fresh=not DB.exists()
    s=SizingTests();s.setUp();f=s.f
    nv=dict(f.info,isin='US67066G1040',name='NVIDIA')
    ni=dict(f.identity,symbol='NVDA.GTX')
    if fresh:
        f.cfg['protection']=protection.POLICY.copy();f.cfg['identities'][nv['isin']]=ni
        f.write(h.CONFIG,f.cfg);f.write('mcp_allow_edits',True)
        f.db.execute('INSERT INTO instruments VALUES (?,?)',(nv['isin'],json.dumps(nv)))
        f.write('gettex_chart:'+nv['isin']+':15m',dict(state='SAVED',identity=ni,
            listing_proof=dict(ni,status='CONFIRMED',isin=nv['isin'])))
        f.db.execute("INSERT INTO series SELECT ?,source,venue,currency,?,base_tf,ts,open,high,low,close,volume FROM series WHERE isin=?",(nv['isin'],ni['symbol'],f.isin))
        f.db.commit()
        with sqlite3.connect(DB) as disk:f.db.backup(disk)
    f.db.close()

    class FixtureCompute:
        def calculate(self,kind,values):
            assert kind=='paper_hourly'
            f.db=sqlite3.connect(DB);f.now=time.time();f.info=values['watchlist'][0];f.isin=f.info['isin']
            f.cfg=h.read(f.db,h.CONFIG);f.identity=f.cfg['identities'][f.isin]
            try:
                now=pd.Timestamp.now(tz='UTC');end=now.floor('15min');opening=end-pd.Timedelta(minutes=15)
                # Explicit test-only quote, never a provider row or live archive.
                f.db.execute('DELETE FROM series WHERE isin=? AND base_tf=?',(f.isin,'15m'))
                f.db.execute('INSERT INTO series VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                    (f.isin,'gettex_chart','GETTEX','EUR',f.identity['symbol'],'15m',
                     str(opening.tz_convert('Europe/Berlin').tz_localize(None)),100,102,99,100,20))
                f.db.commit();s.prepare('WAIT')
                r=f.result;r['discretionary']=values['_paper_order'].copy()
                r['execution_quote']['bars'][0].update(open_at=opening.isoformat(),close_at=end.isoformat())
                mode=ARCHIVE/'AD fixture blocked.json'
                if mode.exists() and json.loads(mode.read_text()):
                    r['execution_context']['trading_engine']['timeframe_context']['1D']['data_quality']['state']='UNCONFIRMED'
                return r
            finally:f.db.close()

    root=tk.Tk();root.title('Stock Monitor AD — ИЗОЛИРОВАННЫЙ PAPER / WAIT fixture');root.geometry('1180x820')
    events=queue.Queue()
    store=SimpleNamespace(path=DB,directory=Directory(),read_cache=ReadCache(DB),
        connect=lambda:sqlite3.connect(DB),setting=lambda k,d=None:h.read(sqlite3.connect(DB),k,d))
    app=SimpleNamespace(root=root,events=events,closing=False,user_dirty=False,
        store=store,watchlist=[dict(isin='US0378331005',name='Apple',kind='stock'),nv],ai_config={},compute_trading=FixtureCompute())
    app.dispatcher=Dispatcher(lambda cb,value:events.put((cb,(value,))),None)
    app.job=lambda work,done,failed=None,**kw:app.dispatcher.submit(work,done,failed or (lambda e:None),**kw)
    bridge=Bridge(Directory(),events,lambda action,args:Features.bridge_dispatch(app,action,args),lambda:True)
    tk.Label(root,text='ИЗОЛИРОВАННЫЙ СЧЁТ • вымышленные тестовые цены • CORE WAIT • рабочая база не открывается',bg='#ffd98b').pack(fill='x')
    paper_view.attach(app,root)
    def pump():
        while True:
            try:fn,args=events.get_nowait()
            except queue.Empty:break
            fn(*args)
        if not app.closing:root.after(25,pump)
    def close():
        app.closing=True;bridge.close();store.read_cache.close();root.destroy()
    root.protocol('WM_DELETE_WINDOW',close);pump();root.mainloop()


def bridge_test():
    require_archive()
    import mcp_server as mcp
    mcp.DATA=Directory()
    descriptor=json.loads((Directory()/'bridge.json').read_text())
    results=[]
    for index,side in enumerate(('BUY','SELL')):
        args=dict(isin='US67066G1040',side=side,quantity=1,timeframe='15m',
            request_id='ad-chatgpt-'+side.lower())
        args.update(instance_id=descriptor['instance_id'],deadline=time.time()+110)
        call=mcp.handle(dict(method='tools/call',params=dict(name='stock_monitor_paper_trade',arguments=args)))
        assert not call.get('isError'),call
        answer=json.loads(call['content'][0]['text'])
        results.append(answer)
        assert answer['result']['status']=='COMPLETED',answer
        assert answer['result']['fill']['initiator']=='CHATGPT'
    (ARCHIVE/'AD Bridge acceptance.json').write_text(json.dumps(results,indent=2),encoding='utf8')
    print('Bridge/Feature/worker/shared executor: BUY + SELL at WAIT passed (isolated NVIDIA).')


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--serve',action='store_true');parser.add_argument('--bridge-test',action='store_true')
    args=parser.parse_args()
    if args.serve:serve()
    elif args.bridge_test:bridge_test()
