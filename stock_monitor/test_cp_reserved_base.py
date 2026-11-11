"""A reserved fresh TF must survive the downloader's history fairness hint."""
import json, sqlite3, unittest
from unittest.mock import Mock, patch
import pandas as pd
import gettex_chart as chart

class Memory:
    directory='unused'
    def __init__(self):
        self.db=sqlite3.connect(':memory:')
        self.db.executescript('CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT);'
            'CREATE TABLE instruments(isin TEXT PRIMARY KEY,payload TEXT);'
            'CREATE TABLE input_revisions(scope TEXT PRIMARY KEY,revision INTEGER);')
        self.db.execute('INSERT INTO instruments VALUES (?,?)',('I',json.dumps({'isin':'I','kind':'stock'})))
        self.db.commit()
    def connect(self):return self.db
    def setting(self,key,default=None):
        row=self.db.execute('SELECT value FROM settings WHERE key=?',(key,)).fetchone()
        return json.loads(row[0]) if row else default
    def set_setting(self,key,value):
        with self.db:self.db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)',(key,json.dumps(value)))

class ReservedBase(unittest.TestCase):
    def setUp(self):
        self.store=Memory();self.addCleanup(self.store.db.close)
        self.now=pd.Timestamp('2026-11-04T09:30:00Z')
        self.store.set_setting('gettex_chart_focus:I',{'streak':2})
        for base,stamp in [('5m','2026-11-03T08:00:00Z'),('15m','2026-11-04T08:00:00Z')]:
            self.store.set_setting(chart.state_key('I',base),{'received_at':stamp})
        self.client=Mock();self.client.fetch.side_effect=ValueError('CONTROLLED_STOP_BEFORE_DATA')
    def run_turn(self,reserved=True,due=('5m','15m')):
        info={'isin':'I','kind':'stock','_gettex_poll_only':reserved}
        with patch.object(chart.scheduler,'decision',side_effect=lambda st,i,m,b,n:{'due':b in due}),patch.object(chart.scheduler,'finish'),patch('delivery_observations.outcome'):
            return chart._fetch_turn(info,self.store,self.now,self.client,preferred_tf='15m')
    def test_exact_reserved_base_despite_oldest_and_foreground_streak(self):
        result=self.run_turn()
        self.assertEqual(self.client.fetch.call_args.args[1],'15m')
        self.assertEqual(result['base'],'15m')
        self.assertEqual(result['state'],'ERROR') # deliberate stop, no OHLCV accepted
    def test_reservation_no_longer_due_does_not_fetch_another_base(self):
        self.assertEqual(self.run_turn(due=('5m',))['state'],'NOT_DUE')
        self.client.fetch.assert_not_called()
    def test_reserved_base_cooldown_does_not_fall_back(self):
        self.store.set_setting(chart.state_key('I','15m'),{'retry_after':(self.now+pd.Timedelta(minutes=10)).isoformat()})
        self.assertEqual(self.run_turn()['state'],'NOT_DUE')
        self.client.fetch.assert_not_called()
    def test_ordinary_history_turn_keeps_oldest_after_two_preferences(self):
        self.run_turn(reserved=False)
        self.assertEqual(self.client.fetch.call_args.args[1],'5m')
    def test_global_rate_reservation_still_prevents_request(self):
        self.store.set_setting('gettex_chart_next_request',(self.now+pd.Timedelta(minutes=1)).isoformat())
        self.assertEqual(self.run_turn()['state'],'RATE_WAIT')
        self.client.fetch.assert_not_called()

if __name__=='__main__':unittest.main()
