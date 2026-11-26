"""Contract for read-only charts, saved alerts and the separate replay ledger."""
from contextlib import contextmanager
from datetime import date
from pathlib import Path
import json
import sqlite3
import sys
import tempfile
from types import SimpleNamespace
import unittest

import pandas as pd

sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
from gettex_chart import identity, state_key
from gettex_sessions import warning
from intraday_alerts import add, disable, evaluate
from intraday_data import ALERT_KEY, TRAINING_KEY, Replay, coverage, export_frame, panels, read, save_training, setting
from timeframes import schedule


ISIN='US0378331005'
INFO={'isin':ISIN,'name':'Apple','profile_symbol':'AAPL'}
DAY='2026-11-23'
CLOCK=pd.Timestamp('2026-11-23T10:00:00+01:00')


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temp=self.enterContext(tempfile.TemporaryDirectory())
        self.path=Path(self.temp)/'test.sqlite3'
        self.store=SimpleNamespace(path=self.path,connect=self.connect)
        with self.connect() as db:
            db.executescript('CREATE TABLE instruments(isin TEXT,payload TEXT);'
                'CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT);'
                'CREATE TABLE candles(isin TEXT,source TEXT,base_tf TEXT,ts TEXT,open REAL,high REAL,low REAL,close REAL,volume REAL);'
                'CREATE TABLE series(isin TEXT,source TEXT,venue TEXT,currency TEXT,symbol TEXT,base_tf TEXT,ts TEXT,open REAL,high REAL,low REAL,close REAL,volume REAL);')
            db.execute('INSERT INTO instruments VALUES(?,?)',(ISIN,json.dumps(INFO)))
            for i,at in enumerate(pd.date_range(DAY+' 07:30',periods=62,freq='15min')):
                p=100+i
                db.execute('INSERT INTO candles VALUES(?,?,?,?,?,?,?,?,?)',
                    (ISIN,'gettex','15m',str(at),p,p+1,p-1,p,30))
                db.execute('INSERT INTO series VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',
                    (ISIN,'gettex_chart','GETTEX','EUR','AAPLE.GTX','15m',str(at),2*p,2*p+2,2*p-2,2*p,40))
            meta=identity(ISIN,'AAPLE.GTX')
            saved=dict(state='SAVED',quality='READY',identity=meta,listing_proof=dict(meta, isin=ISIN,status='CONFIRMED'))
            self.put(db,state_key(ISIN,'15m'),saved)

    @contextmanager
    def connect(self):
        db=sqlite3.connect(self.path)
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def put(db,key,value):
        db.execute('INSERT OR REPLACE INTO settings VALUES(?,?)',(key,json.dumps(value)))

    def test_archives_and_closed_timeframes_remain_separate(self):
        a=panels(self.path,ISIN,'gettex',('15m','1H','4H'),DAY,15,CLOCK)
        b=panels(self.path,ISIN,'gettex_chart',('15m','1H'),DAY,15,CLOCK)
        self.assertEqual(len(a['frames']['15m']),9)
        self.assertEqual(len(a['frames']['1H']),2)
        self.assertTrue(a['frames']['4H'].empty)
        self.assertEqual(a['frames']['15m'].open.iloc[0],100)
        self.assertEqual(b['frames']['15m'].open.iloc[0],200)
        self.assertEqual(str(a['frames']['1H'].attrs['ends'][0]),'2026-11-23 07:30:00+00:00')
        rows=coverage(self.path,'gettex_chart','1H','V5',CLOCK)
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['archive_quality'],'READY')

    def test_replay_next_open_rejects_hole_and_has_independent_cash(self):
        with self.connect() as db:
            db.execute("DELETE FROM candles WHERE ts=?", (DAY+' 08:00:00',))
        f=panels(self.path,ISIN,'gettex',('15m',),DAY,15,CLOCK)['frames']['15m']
        r=Replay(f,DAY)
        self.assertEqual(r.quantity,0)
        self.assertEqual(str(r.clock),'2026-11-23 06:45:00+00:00')
        r.order('BUY',2)
        self.assertTrue(r.step())
        self.assertEqual(r.quantity,2)
        self.assertEqual(r.history[0][3],'101.05')
        r.order('SELL',1)
        self.assertTrue(r.step())
        self.assertEqual(r.history[-1][-1],'Пропуск в истории')
        self.assertEqual(r.quantity,2)
        r.order('SELL',1)
        self.assertTrue(r.step())
        self.assertEqual(r.quantity,1)
        save_training(self.store,{'id':r.id,'history':r.history})
        with read(self.path) as db:
            self.assertEqual(len(setting(db,TRAINING_KEY)[0]['history']),3)
            self.assertIsNone(db.execute("select name from sqlite_master where name='paper_fills'").fetchone())

    def test_csv_includes_listing_and_utc_bar_end(self):
        frame=panels(self.path,ISIN,'gettex_chart',('15m',),DAY,15,CLOCK)['frames']['15m']
        path=self.path.with_suffix('.csv')
        export_frame(path,ISIN,frame)
        data=path.read_text(encoding='utf-8-sig')
        self.assertIn('source,venue,currency,symbol,timeframe',data)
        self.assertIn('gettex_chart,GETTEX,EUR,AAPLE.GTX,15m,2026-11-23T07:30:00+01:00,2026-11-23T06:45:00+00:00',data)

    def test_replay_delay_cannot_fill_before_signal_is_visible(self):
        frame=panels(self.path,ISIN,'gettex',('15m',),DAY,15,CLOCK)['frames']['15m']
        replay=Replay(frame,DAY,delay=15)
        self.assertEqual(str(replay.last_visible_close),'2026-11-23 06:45:00+00:00')
        self.assertEqual(str(replay.clock),'2026-11-23 07:00:00+00:00')
        replay.order('BUY',1)
        replay.step()
        self.assertEqual(replay.quantity,0)
        replay.step()
        self.assertEqual(replay.history[0][0],'2026-11-23T08:00:00')

    def test_saved_alert_fires_once_after_threshold_crossing(self):
        rule=add(self.store,dict(isin=ISIN,name='Apple',source='gettex',kind='UP',level='108'))
        with self.connect() as db:
            rows=setting(db,ALERT_KEY)
            rows[0]['created']=CLOCK.timestamp()-991
            self.put(db,ALERT_KEY,rows)
        self.assertEqual([x['id'] for x in evaluate(self.store,CLOCK)],[rule['id']])
        self.assertEqual(evaluate(self.store,CLOCK),[])
        disable(self.store,rule['id'])
        with read(self.path) as db:
            self.assertFalse(setting(db,ALERT_KEY)[0]['enabled'])

    def test_signal_alert_uses_strategy_specific_current_core(self):
        r=add(self.store,dict(isin=ISIN,name='Apple',source='gettex',kind='BUY',book='V5'))
        with self.connect() as db:
            rows=setting(db,ALERT_KEY)
            rows[0]['created']=CLOCK.timestamp()-500
            self.put(db,ALERT_KEY,rows)
            report={'timeframes':{'4H':dict(core='DATA BLOCKED',signal='BUY',at=CLOCK.timestamp()-1,timeframe='4H')}}
            self.put(db,'paper_auto_status:V5:'+ISIN,report)
        self.assertFalse(evaluate(self.store,CLOCK))
        with self.connect() as db:
            report['timeframes']['4H']['core']='READY'
            self.put(db,'paper_auto_status:V5:'+ISIN,report)
        self.assertEqual(evaluate(self.store,CLOCK)[0]['id'],r['id'])

    def test_calendar_does_not_invent_sessions_past_confirmed_year(self):
        self.assertIn('2026-12-31',warning(date(2026,11,23)))
        meta={'venue':'GETTEX','timezone':'Europe/Berlin','timeframe':'15m'}
        self.assertTrue(schedule(meta,'2026-12-24','2026-12-24').empty)
        with self.assertRaises(ValueError):
            schedule(meta,'2027-01-04','2027-01-04')


if __name__=='__main__':
    unittest.main()
