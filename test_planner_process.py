"""CP planning uses the owned read worker; acquisition/policies stay unchanged."""
import hashlib,os,sqlite3,sys,tempfile,unittest
from datetime import datetime,timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
from storage import Store
from performance_cache import ReadCache,install_input_revisions
from compute_process import Client
from candle_schedule import planned_feeds

class PlannerProcess(unittest.TestCase):
    def setUp(self):
        fd,name=tempfile.mkstemp(suffix='.sqlite3');os.close(fd)
        self.path=Path(name);self.s=Store.__new__(Store)
        self.s.path=self.path;self.s.directory=self.path.parent;self.s.read_cache=ReadCache(self.path)
        with self.s.connect() as db:
            db.executescript('''
                CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE instruments(isin TEXT PRIMARY KEY,payload TEXT NOT NULL);
                CREATE TABLE candles(isin TEXT,source TEXT,base_tf TEXT,ts TEXT,open REAL,high REAL,low REAL,close REAL,volume REAL);
                CREATE TABLE trades(isin TEXT,ts TEXT,price REAL,volume REAL);
                CREATE TABLE journal(id INTEGER PRIMARY KEY,at TEXT,kind TEXT,payload TEXT);
                CREATE TABLE series(isin TEXT,source TEXT,venue TEXT,currency TEXT,symbol TEXT,base_tf TEXT,ts TEXT,open REAL,high REAL,low REAL,close REAL,volume REAL);
            ''')
            install_input_revisions(db)
        self.clients=[]
    def tearDown(self):
        for client in self.clients:
            process=client.process;client.close()
            if process:
                process.wait(timeout=5);process.stdin.close();process.stdout.close()
        self.s.read_cache.close();self.path.unlink()
    def client(self,**kw):
        c=Client(self.path,**kw);self.clients.append(c);return c
    def test_child_plan_matches_direct_plan_is_readonly_and_observes_next_input(self):
        infos=[dict(isin='TEST',name='Test',kind='stock',venue='GETTEX',currency='EUR'),
               dict(isin='INDEX',kind='benchmark',history_source='cboe')]
        values=dict(watchlist=infos,now='2026-11-23T15:00:00Z',focus={'isin':'TEST','timeframe':'4H'})
        client=self.client();before=hashlib.sha256(self.path.read_bytes()).hexdigest()
        actual=client.calculate('feed_plan',values);actual.pop('_calculation_timing')
        self.assertEqual(actual,planned_feeds(self.s,infos,values['now'],values['focus']))
        self.assertEqual(hashlib.sha256(self.path.read_bytes()).hexdigest(),before)
        self.assertTrue(any(i['isin']=='INDEX' for i,_ in actual['feeds']))
        self.s.set_setting('benchmark_history:INDEX',{'attempted_at':values['now']})
        next_plan=client.calculate('feed_plan',values);next_plan.pop('_calculation_timing')
        self.assertEqual(next_plan,planned_feeds(self.s,infos,values['now'],values['focus']))
        self.assertFalse(any(i['isin']=='INDEX' for i,_ in next_plan['feeds']))
        self.assertFalse(next_plan['freshness']['execution_permission'])
    def test_serial_writer_cannot_execute_read_planner(self):
        with self.assertRaisesRegex(RuntimeError,'capability mismatch'):
            self.client(feed_updates=True).calculate('feed_plan',{'watchlist':[]})
    def test_child_order_matches_direct_readonly_projection_and_changed_inputs(self):
        from watchlist_order import collect
        infos=[dict(isin='TEST',name='Test',kind='stock')]
        now=datetime(2026,11,23,15,tzinfo=timezone.utc);client=self.client()
        before=hashlib.sha256(self.path.read_bytes()).hexdigest()
        actual=client.calculate('watchlist_order',dict(watchlist=infos,now=now))
        expected=collect(self.path,infos,now)
        for k in ('rows','token','checked_at'):self.assertEqual(actual[k],expected[k])
        self.assertEqual(hashlib.sha256(self.path.read_bytes()).hexdigest(),before)
        with self.s.connect() as db:db.execute('INSERT OR REPLACE INTO input_revisions VALUES (?,?)',('instrument:TEST',2))
        next_value=client.calculate('watchlist_order',dict(watchlist=infos,now=now))
        self.assertEqual(next_value['token'],collect(self.path,infos,now)['token'])
        self.assertNotEqual(next_value['token'],actual['token'])
    def test_order_stale_token_or_selection_is_rejected_after_worker_result(self):
        from watchlist_order import Runtime
        infos=[dict(isin='TEST',name='Test',kind='stock')]
        for cause in ('token','selection'):
            app=SimpleNamespace(closing=False,watchlist=[dict(i) for i in infos],job=Mock(),compute_feed=Mock(),
                store=SimpleNamespace(read_cache=Mock()))
            runtime=Runtime(app);runtime.apply=Mock();runtime.refresh()
            work,done=app.job.call_args.args[:2]
            app.compute_feed.calculate.assert_not_called();work()
            app.compute_feed.calculate.assert_called_once_with('watchlist_order',{'watchlist':infos})
            app.store.read_cache.token_for.return_value={'changed':2} if cause=='token' else {'old':1}
            if cause=='selection':app.watchlist[0]['name']='Changed'
            done(dict(token={'old':1},rows={'TEST':{'tier':0}}))
            runtime.apply.assert_not_called();self.assertEqual(runtime.rows,{})
            self.assertFalse(runtime.busy);self.assertGreater(runtime.next_try,0)
    def test_53_200_gui_schedules_capture_only_and_use_existing_owned_client(self):
        from desktop014 import Desktop014
        for count in (53,200):
            for _ in range(count):
                infos=[dict(isin='INDEX',kind='benchmark',name='Original')]
                app=SimpleNamespace(closing=False,watchlist=infos,instrument_cfg=lambda:infos[0],
                    timeframe=SimpleNamespace(get=lambda:'4H'),job=Mock(),root=Mock(),compute_feed=Mock(),
                    _scheduled_refresh=Mock())
                Desktop014._scheduled_refresh(app)
                app.compute_feed.calculate.assert_not_called()
                work=next(c.args[0] for c in app.job.call_args_list if c.kwargs['key']=='cp-schedule')
                infos[0]['name']='Changed'
                work()
                app.compute_feed.calculate.assert_called_once_with('feed_plan',
                    {'watchlist':[dict(isin='INDEX',kind='benchmark',name='Original')],
                     'focus':{'isin':'INDEX','timeframe':'4H'}})
                app.root.after.assert_called_once_with(30000,app._scheduled_refresh)

if __name__=='__main__':unittest.main()
