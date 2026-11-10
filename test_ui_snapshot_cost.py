"""Bounded regression tests for metadata propagation and redundant UI updates."""
import copy
import sys
import unittest
import tempfile
import sqlite3
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import numpy as np
import pandas as pd
sys.path.insert(0, str(Path(__file__).parent/'stock_monitor'))
from desktop014 import Desktop014
from features import set_text
from chart_indicators import partial_indicators


class Probe:
    copies = 0
    def __deepcopy__(self, memo):
        type(self).copies += 1
        return self
    def __str__(self): return 'unchanged evidence'


def bars():
    x = np.arange(60, dtype=float)+100
    f = pd.DataFrame(dict(open=x, high=x+1, low=x-1, close=x+.1,
                          volume=x*2, source='gettex_chart'),
                     index=pd.date_range('2026-11-06 07:30', periods=60, freq='15min'))
    f.attrs = dict(isin='TEST', source='gettex_chart', symbol='TEST.GTX',
                   venue='GETTEX', currency='EUR', timezone='Europe/Berlin',
                   data_quality={'state':'UNCONFIRMED','ranges':['unchanged']},
                   proof=Probe())
    return f


class Widget:
    def __init__(self, text='saved'):
        self.text=text; self.state='disabled'; self.writes=0; self.exists=True
    def winfo_exists(self): return self.exists
    def get(self,*a): return self.text
    def cget(self,*a): return self.state
    def configure(self,**k): self.state=k['state']
    def delete(self,*a): self.text=''; self.writes+=1
    def insert(self,at,text): self.text=text; self.writes+=1


class SnapshotCostTests(unittest.TestCase):
    def test_long_read_cannot_consume_chart_reservation(self):
        from performance_dispatcher import Dispatcher
        d=Dispatcher(lambda cb,value:cb(value),sample=lambda _:({'cpu':0,'ram':0},None))
        release=threading.Event();started=[threading.Event() for _ in range(3)]
        chart=threading.Event();read=threading.Event()
        try:
            for e in started:d.submit(lambda e=e:(e.set(),release.wait(3)),lambda _:None,lambda _:None,lane='misc')
            self.assertTrue(all(e.wait(1) for e in started))
            d.submit(lambda:(read.set(),release.wait(3)),lambda _:None,lambda _:None,lane='read')
            time.sleep(.05)
            d.submit(chart.set,lambda _:None,lambda _:None,lane='chart')
            self.assertTrue(chart.wait(.5),'background/read used the reserved chart worker')
            self.assertFalse(read.is_set())
            release.set();self.assertTrue(read.wait(1))
            self.assertEqual(len(d.threads),4)
        finally:
            release.set();d.close();d.monitor.join(2)
            for worker in d.threads:worker.join(2)

    def test_continuous_charts_and_long_read_do_not_starve_53_feeds(self):
        from performance_dispatcher import Dispatcher
        d=Dispatcher(lambda cb,value:cb(value),sample=lambda _:({'cpu':0,'ram':0},None))
        release=threading.Event();reading=threading.Event();feeds=[];charts=[]
        try:
            d.submit(lambda:(reading.set(),release.wait(5)),lambda _:None,lambda _:None,lane='read')
            self.assertTrue(reading.wait(1))
            for i in range(53):d.submit(lambda i=i:feeds.append(i),lambda _:None,lambda _:None,key='f'+str(i),lane='feed_update')
            for i in range(200):d.submit(lambda i=i:charts.append(i),lambda _:None,lambda _:None,key='c'+str(i),lane='chart')
            end=time.monotonic()+2
            while (len(feeds)<53 or len(charts)<200) and time.monotonic()<end:time.sleep(.005)
            self.assertEqual(sorted(feeds),list(range(53)));self.assertEqual(sorted(charts),list(range(200)))
            self.assertFalse(release.is_set());self.assertEqual(len(d.threads),4)
        finally:
            release.set();d.close();d.monitor.join(2)
            for worker in d.threads:worker.join(2)

    def test_news_due_uses_one_bounded_read_and_preserves_oldest_first(self):
        from news_runtime import due_targets
        from contextlib import contextmanager
        import json
        db=sqlite3.connect(':memory:');self.addCleanup(db.close)
        db.execute('CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT)')
        now=pd.Timestamp('2026-11-10T16:00:00Z').timestamp()
        records={'old':'2026-11-10T14:00:00+00:00','fresh':'2026-11-10T15:50:00+00:00',
                 'boundary':'2026-11-10T15:30:00+00:00','invalid':None}
        for key,at in records.items():db.execute('INSERT INTO settings VALUES (?,?)',('news:'+key,json.dumps({'attempted_at':at,'articles':['retained']*500})))
        calls=[]
        @contextmanager
        def connect():calls.append(1);yield db
        targets=[(k,k.upper()) for k in (*records,'missing')]
        got=due_targets(SimpleNamespace(connect=connect),targets,now)
        self.assertEqual([x[1] for x in got],['old','invalid','missing'])
        self.assertEqual(min(got,key=lambda x:x[0])[1],'invalid')
        self.assertEqual(len(calls),1)
        self.assertEqual(due_targets(SimpleNamespace(connect=connect),[],now),[])
        self.assertEqual(len(calls),1)

    def test_news_cache_load_is_deferred_and_removed_identity_cannot_publish(self):
        from news_runtime import NewsRuntime
        from unittest.mock import Mock
        queued={};store=SimpleNamespace(directory=Path('.'),setting=Mock(return_value={'articles':['saved']}),set_setting=Mock())
        def job(work,done,failed,**kw):queued.update(work=work,done=done,failed=failed);return True
        app=SimpleNamespace(closing=False,watchlist=[{'isin':'A','name':'A','history_symbol':'A.F'}],store=store,job=job)
        runtime=NewsRuntime(app)
        self.assertTrue(runtime.refresh('A','A.F'));store.setting.assert_not_called()
        with patch('news_runtime.FeedClient',return_value=object()),patch('news_runtime.company_news',return_value={'articles':['new']}):
            result=queued['work']()
        store.setting.assert_called_once();app.watchlist[0]['history_symbol']='B.F'
        queued['done'](result);store.set_setting.assert_not_called();self.assertFalse(runtime.busy)

    def test_news_failure_before_cache_read_never_erases_saved_articles(self):
        from news_runtime import NewsRuntime
        from unittest.mock import Mock
        queued={};store=SimpleNamespace(directory=Path('.'),setting=Mock(side_effect=RuntimeError('database busy')),set_setting=Mock())
        def job(work,done,failed,**kw):queued.update(work=work,done=done,failed=failed);return True
        app=SimpleNamespace(closing=False,watchlist=[],store=store,job=job)
        runtime=NewsRuntime(app);runtime.refresh('market',None)
        with self.assertRaises(RuntimeError):queued['work']()
        queued['failed']('database busy');store.set_setting.assert_not_called();self.assertFalse(runtime.busy)

    def test_journal_upgrade_preserves_rows_and_uses_index_for_absent_kind(self):
        from storage import Store
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'stock_monitor.sqlite3'
            with sqlite3.connect(path) as db:
                db.execute('CREATE TABLE journal(id INTEGER PRIMARY KEY AUTOINCREMENT,at TEXT DEFAULT CURRENT_TIMESTAMP,kind TEXT NOT NULL,payload TEXT NOT NULL)')
                db.executemany('INSERT INTO journal(kind,payload) VALUES (?,?)',[('feed','{"n":1}'),('ai_response','{"text":"old"}'),('ai_response','{"text":"new"}')])
                before=db.execute('SELECT * FROM journal ORDER BY id').fetchall()
            db.close()
            for _ in range(2):
                store=Store(directory)
                try:
                    self.assertEqual([r['text'] for r in store.events('ai_response',1)],['new'])
                    self.assertEqual(store.events('missing'),[])
                    with store.connect() as db:
                        self.assertEqual(db.execute('SELECT * FROM journal ORDER BY id').fetchall(),before)
                        plan=str(db.execute('EXPLAIN QUERY PLAN SELECT at,payload FROM journal WHERE kind=? ORDER BY id DESC LIMIT 1',('missing',)).fetchall())
                        self.assertIn('journal_kind_id',plan);self.assertNotIn('SCAN journal',plan)
                finally:store.read_cache.close()

    def test_snapshot_preserves_values_metadata_and_bounded_rows_without_archive_copies(self):
        f=bars(); info=dict(isin='TEST',name='Test')
        expected=[dict(at=str(ts),**row.to_dict()) for ts,row in pd.DataFrame(f,copy=False).tail(30).iterrows()]
        ctx=SimpleNamespace(frame_for=lambda *a:f,user_state=lambda *a:{'position':{'quantity':1}},
            timeframe=SimpleNamespace(get=lambda:'15m'),source_mode=lambda *a:'proxy',feed_errors={},
            store=SimpleNamespace(setting=lambda k,default=None:default),
            analyse_cached=lambda *a:(f,{'decision':'WAIT','reason':'DQ blocked'}))
        Probe.copies=0
        result=Desktop014.snapshot_stock(ctx,info)
        self.assertEqual(result['candles'],expected)
        self.assertEqual(result['data_quality'],f.attrs['data_quality'])
        self.assertEqual(result['decision'],'WAIT')
        self.assertEqual(result['count'],60)
        self.assertEqual(Probe.copies,0)

    def test_analysis_key_still_binds_values_dq_identity_and_settings(self):
        f=bars(); keys=[]
        def get(key,loader):keys.append(key); return loader()
        ctx=SimpleNamespace(store=SimpleNamespace(read_cache=SimpleNamespace(get=get)))
        with patch('desktop014.analyse',return_value=('same','WAIT')):
            def run():return Desktop014.analyse_cached(ctx,{'isin':'TEST'},f,{'risk':1})
            Probe.copies=0;self.assertEqual(run(),('same','WAIT'));run()
            self.assertEqual(keys[-1],keys[-2]);self.assertEqual(Probe.copies,0)
            f.iloc[-1,f.columns.get_loc('close')]+=0.1;run()
            self.assertNotEqual(keys[-1],keys[-2])
            f.attrs['data_quality']['state']='INVALID';run()
            self.assertNotEqual(keys[-1],keys[-2])
            f.attrs['currency']='USD';run()
            self.assertNotEqual(keys[-1],keys[-2])
            Desktop014.analyse_cached(ctx,{'isin':'TEST'},f,{'risk':2})
            self.assertNotEqual(keys[-1],keys[-2])

    def test_partial_projection_checks_identity_without_copying_evidence_per_column(self):
        f=bars();f['venue']='GETTEX';f['currency']='EUR';f['isin']='TEST'
        f.attrs.update(timeframe='15m',partial_chart={'display_only':True,'break_before':[]})
        plain=pd.DataFrame(f,copy=True);plain.attrs=copy.deepcopy(f.attrs);plain.attrs.pop('proof')
        expected=partial_indicators(plain)
        Probe.copies=0;actual=partial_indicators(f)
        pd.testing.assert_frame_equal(pd.DataFrame(actual),pd.DataFrame(expected))
        self.assertEqual(Probe.copies,1)
        self.assertEqual(actual.attrs['data_quality']['state'],'UNCONFIRMED')
        actual.attrs['data_quality']['state']='INVALID'
        self.assertEqual(f.attrs['data_quality']['state'],'UNCONFIRMED')
        f.loc[f.index[10],'venue']='XETRA'
        self.assertEqual(partial_indicators(f).attrs['partial_indicators']['blocked_reason'],'MIXED_IDENTITY')

    def test_unchanged_status_does_not_reinsert_text(self):
        w=Widget()
        for _ in range(100):set_text(w,'saved')
        self.assertEqual(w.writes,0);self.assertEqual(w.state,'disabled')

    def test_short_segments_keep_vwap_without_claiming_warmup(self):
        for tf in ('15m','1D','1M'):
            f=bars().iloc[:9].copy()
            f.attrs.update(timeframe=tf,partial_chart={'display_only':True,'break_before':[]})
            f.loc[f.index[:2],'volume']=0
            with patch('analytics.indicators',side_effect=AssertionError('Unwarmed fixed indicators')):
                out=partial_indicators(f)
            self.assertTrue(out[['ema10','ema30','rsi','atr','macd','macd_signal','volume_mean20','chart_ema_fast']].isna().all().all())
            v=pd.DataFrame(f);expected=((v.high+v.low+v.close)/3*v.volume).cumsum()/v.volume.cumsum().replace(0,np.nan)
            np.testing.assert_allclose(out.chart_vwap,expected,equal_nan=True)
            self.assertEqual(out.attrs['partial_indicators']['latest_segment_bars'],9)
            self.assertFalse(out.attrs['partial_indicators']['execution_allowed'])

    def test_short_segment_with_custom_early_indicator_still_computes(self):
        f=bars().iloc[:9].copy()
        f.attrs.update(timeframe='15m',partial_chart={'display_only':True,'break_before':[]})
        out=partial_indicators(f,{'ema_fast':3,'volume':2})
        np.testing.assert_allclose(out.chart_ema_fast,pd.DataFrame(f).close.ewm(span=3,min_periods=3,adjust=False).mean(),equal_nan=True)
        self.assertTrue(out.ema10.isna().all());self.assertTrue(out.chart_volume_mean.iloc[1:].notna().all())

    def test_unwarmed_segment_does_not_hide_invalid_macd_configuration(self):
        f=bars().iloc[:5].copy()
        f.attrs.update(timeframe='15m',partial_chart={'display_only':True,'break_before':[]})
        for settings in ({'macd_fast':26,'macd_slow':12},{'macd_signal':True},{'macd_signal':501}):
            with self.assertRaisesRegex(ValueError,'MACD'):partial_indicators(f,settings)

    def test_changed_or_externally_modified_text_is_updated(self):
        w=Widget();set_text(w,'new');self.assertEqual(w.text,'new');self.assertEqual(w.writes,2)
        w.text='external edit';set_text(w,'new');self.assertEqual(w.text,'new');self.assertEqual(w.writes,4)

    def test_existing_editable_text_becomes_read_only_and_destroyed_widget_is_ignored(self):
        w=Widget();w.state='normal';set_text(w,'saved')
        self.assertEqual(w.state,'disabled');self.assertEqual(w.writes,0)
        w.exists=False;set_text(w,'other');self.assertEqual(w.text,'saved')


if __name__=='__main__':unittest.main()
