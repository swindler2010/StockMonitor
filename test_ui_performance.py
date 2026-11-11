import copy
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
import pandas as pd
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
import timeframes
from performance_cache import ReadCache
from storage import Store
from test_timeframes import Tests

class PerformanceTests(unittest.TestCase):
    def test_feed_context_reuses_existing_handler_and_storage(self):
        from feed_worker import FeedContext
        with tempfile.TemporaryDirectory() as folder:
            store=Store(folder);context=FeedContext(store);info={'isin':'TEST','name':'Test'}
            def recover(received,db):
                self.assertIs(db,store);self.assertEqual(received,info)
                db.set_setting('feed-test',{'quality':'GAP'})
                return {},['source gap']
            try:
                with patch.object(context,'_download_gettex_trades',return_value=pd.DataFrame()),patch('history_service.recover',side_effect=recover):
                    result=context.calculate(info)
                self.assertEqual(result,{'errors':['source gap']})
                self.assertEqual(store.setting('feed-test'),{'quality':'GAP'})
                self.assertTrue(context.events.empty())
                self.assertFalse(hasattr(context,'root'))
                with store.connect() as db:self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0],'ok')
            finally:store.read_cache.close()

    def test_readonly_worker_cannot_run_feed_updates(self):
        from compute_process import Client
        with tempfile.TemporaryDirectory() as folder:
            store=Store(folder);client=Client(store.path)
            try:
                with self.assertRaisesRegex(RuntimeError,'capability mismatch'):
                    client.calculate('feed_update',{'info':{'isin':'TEST'}})
            finally:
                client.close()
                if client.process:
                    client.process.wait(timeout=10);client.process.stdin.close();client.process.stdout.close()
                store.read_cache.close()

    def test_vector_endpoints_match_session_loop(self):
        for venue,first,last in [('GER','2026-03-27','2026-03-30 17:30'),('NYQ','2026-11-27','2026-11-27 16:00'),('GETTEX','2026-10-12','2026-10-12 23:00')]:
            for tf in timeframes.MINUTES:
                frame=pd.DataFrame(index=pd.date_range(first,last,freq='1h'))
                frame.attrs={'venue':venue,'timezone':'Europe/Berlin' if venue in ('GER','GETTEX') else 'America/New_York'}
                frame.attrs['timeframe']=tf;frame.attrs.pop('bar_ends',None)
                idx=frame.index;zone=frame.attrs['timezone']
                sched=timeframes.schedule(frame.attrs,idx.min()-pd.Timedelta(days=7),idx.max()+pd.Timedelta(days=7))
                expected=[]
                for stamp in idx:
                    if stamp.normalize() not in sched.index:expected.append(pd.NaT);continue
                    row=sched.loc[stamp.normalize()];start=stamp.tz_localize(zone)
                    end=start+pd.Timedelta(minutes=timeframes.MINUTES[tf])
                    if pd.notna(row.break_start) and start<row.break_start:end=min(end,row.break_start)
                    expected.append(min(end,row.close))
                self.assertTrue(timeframes.endpoints(frame).equals(pd.DatetimeIndex(pd.to_datetime(expected,utc=True))))

    def test_feed_process_readonly_and_revision_guard(self):
        from compute_process import Client
        import os
        with tempfile.TemporaryDirectory() as folder:
            store=Store(folder);client=Client(store.path)
            info={'isin':'TEST','name':'Test','source_mode':'proxy'}
            values={'watchlist':[info],'drafts':{},'symbol':'Test','timeframe':'1H','period':'Max'}
            try:
                result=client.calculate('feed',values)
                self.assertNotEqual(client.process.pid,os.getpid())
                self.assertIsNone(result['notification'])
                self.assertIn('_input_revisions',result)
                self.assertTrue(store.read_cache.current(result['_input_revisions']))
                with store.connect() as db:
                    self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0],'ok')
                    self.assertEqual(db.execute('SELECT COUNT(*) FROM settings').fetchone()[0],0)
                values['instrument_revisions']={'instrument:TEST':-1}
                with self.assertRaisesRegex(RuntimeError,'Stale Trading Engine input dependency'):
                    client.calculate('feed',values)
            finally:
                client.close()
                if client.process:
                    client.process.wait(timeout=10);client.process.stdin.close();client.process.stdout.close()
                store.read_cache.close()

    def test_feed_apply_rejects_missing_or_stale_revisions(self):
        from desktop014 import Desktop014
        from types import SimpleNamespace
        from unittest.mock import Mock
        info={'isin':'TEST','name':'Test'};jobs=[]
        values={'watchlist':[info],'drafts':{},'instrument_revisions':{'instrument:TEST':1}}
        app=SimpleNamespace(feed_active={'TEST'},feed_errors={},instrument_cfg=lambda:{'isin':'OTHER'},
            store=SimpleNamespace(read_cache=SimpleNamespace(current=lambda token:token=={'instrument:TEST':1})),
            timeframe=SimpleNamespace(get=lambda:'1H'),user_state=lambda _:info,watchlist=[info],closing=False,
            data={},data_meta={},job=lambda *args,**kwargs:jobs.append(args),compute_feed=Mock(),_write_log=Mock(),notify_signal=Mock())
        with patch('snapshot_worker.inputs',return_value=values):
            for token,expected in [(None,False),({'instrument:TEST':0},False),({'instrument:TEST':1},True)]:
                result={'official':'rows','notification':None}
                if token is not None:result['_input_revisions']=token
                app.compute_feed.calculate.return_value=result
                Desktop014.feed_done(app,info,[])
                work,done,_=jobs.pop();done(work())
                self.assertEqual('Test' in app.data,expected)
            request=app._feed_analysis_jobs['TEST'];request.close()
            Desktop014.feed_done(app,info,[])
            self.assertEqual(jobs,[])

    def test_quality_telemetry_does_not_restart_chart(self):
        from desktop014 import Desktop014
        from types import SimpleNamespace
        report={'state':'SOURCE_GAP','issues':['gap'],'checked_at':'before','raw_retry_after':1}
        app=SimpleNamespace(instrument_cfg=lambda:{'isin':'TEST'},source_mode=lambda _: 'proxy',
            timeframe=SimpleNamespace(get=lambda:'1H'),store=SimpleNamespace(setting=lambda k,d: {'symbol':'TEST'} if k.startswith('history_identity:') else report))
        with patch('desktop014.time.time',return_value=120):
            before=Desktop014.current_quality_signature(app)
            report.update(checked_at='after',raw_retry_after=99)
            self.assertEqual(before,Desktop014.current_quality_signature(app))
            report['state']='INVALID'
            self.assertNotEqual(before,Desktop014.current_quality_signature(app))

    def test_cache_copy_isolated_and_commit_still_invalidates(self):
        with tempfile.TemporaryDirectory() as folder:
            store=Store(folder);cache=store.read_cache
            try:
                value=cache.get('key',lambda:{'rows':[1]})
                value['rows'].append(2)
                self.assertEqual(cache.get('key',lambda:None),{'rows':[1]})
                store.set_setting('test',1)
                self.assertEqual(cache.get('key',lambda:{'rows':[3]}),{'rows':[3]})
            finally:cache.close()

    def test_large_metadata_does_not_change_ohlcv_or_input(self):
        frame=Tests().frame('2026-03-27','2026-03-30 17:30',base='1H',venue='GER')
        plain=timeframes.aggregate(frame,'1H','4H')
        frame.attrs['provider_detail']={'ranges':[['2020-01-01','2026-01-01']]*3000}
        before=copy.deepcopy(frame.attrs)
        result=timeframes.aggregate(frame,'1H','4H')
        pd.testing.assert_frame_equal(plain,result)
        self.assertEqual(frame.attrs,before)
        self.assertEqual(result.attrs['provider_detail'],before['provider_detail'])
        self.assertEqual(plain.attrs['bar_ends'],result.attrs['bar_ends'])
        self.assertEqual(plain.attrs['data_quality']['state'],result.attrs['data_quality']['state'])

    def test_slow_cache_copy_never_holds_revision_lock(self):
        with tempfile.TemporaryDirectory() as folder:
            store=Store(folder);cache=store.read_cache
            cache.get('test',lambda:{'value':1})
            entered=threading.Event();release=threading.Event();original=copy.deepcopy
            def slow(value,*a,**k):
                if value=={'value':1}:entered.set();release.wait(2)
                return original(value,*a,**k)
            try:
                with patch('performance_cache.copy.deepcopy',slow):
                    worker=threading.Thread(target=lambda:cache.get('test',lambda:None));worker.start()
                    self.assertTrue(entered.wait(1))
                    acquired=cache.lock.acquire(timeout=.1)
                    if acquired:cache.lock.release()
                    release.set();worker.join(3)
                    self.assertTrue(acquired,'UI revision reads blocked behind deep copy')
                self.assertTrue(cache.current({}))
            finally:release.set();cache.close()

if __name__=='__main__':unittest.main()

