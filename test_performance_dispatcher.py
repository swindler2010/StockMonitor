import sys,tempfile,time,threading,json,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
from performance_dispatcher import Dispatcher
from compute_process import Client

def wait(test,seconds=3):
    end=time.monotonic()+seconds
    while time.monotonic()<end:
        if test():return
        time.sleep(.005)
    raise AssertionError('condition timed out')

class DispatcherTests(unittest.TestCase):
    def make(self,path=None,pressure=None):
        d=Dispatcher(lambda cb,v:cb(v),path,sample=lambda _: (pressure or {'cpu':0,'ram':0},None))
        self.addCleanup(self.stop,d);wait(lambda:d.loaded);return d
    def stop(self,d):
        d.close();d.monitor.join(3)
        for t in d.threads:t.join(3)
    def test_priority_and_dedup_200(self):
        d=self.make();gate=threading.Event();started=threading.Event();order=[]
        self.addCleanup(gate.set)
        d.submit(lambda:(started.set(),gate.wait(3)),lambda _:None,lambda _:None,key='held',lane='feed_update')
        wait(started.is_set)
        for n in range(200):
            self.assertTrue(d.submit(lambda n=n:order.append(n),lambda _:None,lambda _:None,key=str(n),lane='feed_update',isin=str(n)))
        self.assertFalse(d.submit(lambda:None,lambda _:None,lambda _:None,key='42',lane='feed_update'))
        d.focus('199',['198']);gate.set();wait(lambda:len(order)==200)
        self.assertEqual(order[:2],[199,198]);self.assertEqual(len(set(order)),200)
        self.assertEqual(len(d.threads),4);self.assertEqual(d.snapshot()['deduplicated'],1)
    def test_pressure_reserves_interactive_slot(self):
        d=self.make(pressure={'cpu':99,'ram':20});gate=threading.Event();started=threading.Event();interactive=threading.Event();other=threading.Event()
        self.addCleanup(gate.set)
        d.submit(lambda:(started.set(),gate.wait(3)),lambda _:None,lambda _:None,lane='feed_update')
        wait(started.is_set)
        d.submit(other.set,lambda _:None,lambda _:None,lane='snapshot')
        d.submit(interactive.set,lambda _:None,lambda _:None,lane='chart')
        wait(interactive.is_set);self.assertFalse(other.is_set());self.assertEqual(d.snapshot()['capacity'],2)
        gate.set();wait(other.is_set)
    def test_waiting_assessment_runs_before_feed_backlog_under_pressure(self):
        d=self.make(pressure={'cpu':99,'ram':20})
        gate=threading.Event();started=threading.Event();order=[]
        self.addCleanup(gate.set)
        d.submit(lambda:(started.set(),gate.wait(3)),lambda _:None,lambda _:None,lane='feed_update')
        wait(started.is_set)
        d.focus('visible')
        for n in range(53):
            d.submit(lambda:order.append('feed'),lambda _:None,lambda _:None,
                     key=str(n),lane='feed_update',isin='visible')
        d.submit(lambda:order.append('assessment'),lambda _:None,lambda _:None,
                 key='assessment',lane='eligibility')
        with d.cv:
            d.pending['assessment']['queued']-=61
        self.assertFalse(order)
        gate.set()
        wait(lambda:len(order)==54)
        self.assertEqual(order[0],'assessment')

    def test_sqlite_pressure_and_single_writer(self):
        d=self.make();d.sqlite_busy();self.assertTrue(d.snapshot()['sqlite_busy']);self.assertEqual(d.snapshot()['capacity'],2)
        active=[];maximum=[];lock=threading.Lock();done=[]
        def writer():
            with lock:active.append(1);maximum.append(len(active))
            time.sleep(.002)
            with lock:active.pop()
        for n in range(53):d.submit(writer,lambda _:done.append(1),lambda _:None,lane='feed_update')
        wait(lambda:len(done)==53);self.assertEqual(max(maximum),1)
    def test_restart_preserves_unfinished_and_skips_only_fresh_same_input(self):
        with tempfile.TemporaryDirectory() as folder:
            p=Path(folder)/'queue.json';d=self.make(p);gate=threading.Event();started=threading.Event();self.addCleanup(gate.set)
            d.submit(lambda:1,lambda _:None,lambda _:None,key='done',durable=True,generation='v1')
            wait(lambda:'done' in d.completed)
            d.submit(lambda:(started.set(),gate.wait(3)),lambda _:None,lambda _:None,key='unfinished',lane='feed_update',durable=True)
            wait(started.is_set);d.close();d.monitor.join(3);gate.set()
            for t in d.threads:t.join(3)
            saved=json.loads(p.read_text());self.assertIn('unfinished',saved['pending'])
            e=self.make(p);self.assertIn('unfinished',e.resume)
            self.assertFalse(e.submit(lambda:1,lambda _:None,lambda _:None,key='done',durable=True,generation='v1',only_if_due=True))
            self.assertTrue(e.submit(lambda:1,lambda _:None,lambda _:None,key='done',durable=True,generation='v2',only_if_due=True))
            wait(lambda:e.completed['done']['generation']=='v2')
            self.assertTrue(e.submit(lambda:1,lambda _:None,lambda _:None,key='done',durable=True,generation='v2',only_if_due=False))
            wait(lambda:not e.running and not e.pending);e.close();e.monitor.join(3)
    def test_shutdown_drops_callbacks(self):
        d=self.make();gate=threading.Event();started=threading.Event();applied=[];self.addCleanup(gate.set)
        d.submit(lambda:(started.set(),gate.wait(3)),applied.append,applied.append)
        wait(started.is_set);d.close();gate.set();wait(lambda:not d.running);self.assertEqual(applied,[])
    def test_persistence_failure_is_visible(self):
        with tempfile.TemporaryDirectory() as folder:
            d=self.make(Path(folder)/'absent'/'queue.json')
            d.submit(lambda:1,lambda _:None,lambda _:None,durable=True)
            wait(lambda:d.snapshot()['persistence_error'] is not None);d.close();d.monitor.join(3)
    def test_writer_cannot_be_interrupted(self):
        c=Client('unused',feed_updates=True);c.active=True
        self.assertFalse(c.interrupt());self.assertEqual(c.epoch,0)
    def test_readonly_interrupt_invalidates_generation(self):
        from unittest.mock import Mock
        c=Client('unused');c.active=True;c.process=Mock();c.process.poll.return_value=None
        self.assertTrue(c.interrupt());self.assertEqual(c.epoch,1);c.process.terminate.assert_called_once()

    def test_queued_chart_cancel_and_writer_protection(self):
        d=self.make();gate=threading.Event();started=threading.Event();errors=[];executed=[];self.addCleanup(gate.set)
        d.submit(lambda:(started.set(),gate.wait(3)),lambda _:None,lambda _:None,lane='chart')
        wait(started.is_set)
        d.submit(lambda:executed.append(1),lambda _:None,errors.append,lane='chart',key='next')
        self.assertTrue(d.cancel('next'));self.assertEqual(len(errors),1);self.assertEqual(executed,[])
        d.submit(lambda:None,lambda _:None,lambda _:None,lane='feed_update',key='write',durable=True)
        self.assertFalse(d.cancel('write'));gate.set()
    def test_real_readonly_process_cancellation_and_reaping(self):
        import subprocess
        from unittest.mock import patch
        real_popen=subprocess.Popen
        c=Client('unused');errors=[]
        def fake_worker(*args,**kwargs):
            return real_popen([sys.executable,'-u','-c','import time;time.sleep(30)'],**kwargs)
        def work():
            try:c.calculate('chart',{})
            except RuntimeError as e:errors.append(str(e))
        with patch('compute_process.subprocess.Popen',side_effect=fake_worker):
            t=threading.Thread(target=work);t.start();wait(lambda:c.process is not None)
            child=c.process
            self.assertFalse(c.interrupt('read_analytics'))
            self.assertTrue(c.interrupt('chart'));t.join(5)
        self.assertFalse(t.is_alive());self.assertIsNotNone(child.poll());self.assertIsNone(c.process)
        self.assertIn('Superseded',errors[0]);c.close()

if __name__=='__main__':unittest.main()
