"""Independent chart/trading slots, unchanged background and stale guards."""
import sys,threading,time,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
from performance_dispatcher import Dispatcher
from chart_worker import LatestRequest

class Slots(unittest.TestCase):
    def make(self,pressure=None,workers=5):
        d=Dispatcher(lambda fn,v:fn(v),workers=workers,sample=lambda _: (pressure or {'cpu':10,'ram':30},None))
        self.addCleanup(self.stop,d);return d
    def stop(self,d):
        d.close()
        for t in d.threads+[d.monitor]:t.join(3)
        self.assertFalse(any(t.is_alive() for t in d.threads+[d.monitor]))
    def gated(self,d,key,lane,release,durable=False):
        started=threading.Event()
        def work():started.set();release.wait(4)
        self.assertTrue(d.submit(work,lambda _:None,lambda _:None,key=key,lane=lane,durable=durable))
        return started
    def test_53_200_charts_start_during_three_background_jobs_and_long_trade(self):
        d=self.make();release=threading.Event();self.addCleanup(release.set)
        for key,lane in [('writer','feed_update'),('assessment','eligibility'),('read','read'),('trade','trading')]:
            self.assertTrue(self.gated(d,key,lane,release,durable=key=='writer').wait(2))
        self.assertFalse(d.cancel('writer'))
        waits=[]
        for count in (53,200):
            for n in range(count):
                done=threading.Event();start=time.perf_counter()
                d.submit(lambda:None,lambda _:done.set(),lambda _:done.set(),key=('chart',count,n),lane='chart')
                self.assertTrue(done.wait(.5));waits.append(time.perf_counter()-start)
        self.assertLess(max(waits),.5);self.assertEqual(d.snapshot()['running'],4)
    def test_three_background_limit_and_serial_writer_stay_in_force(self):
        d=self.make();release=threading.Event();self.addCleanup(release.set)
        for key,lane in [('writer','feed_update'),('read','read'),('misc','misc')]:
            self.assertTrue(self.gated(d,key,lane,release).wait(2))
        fourth=self.gated(d,'background4','misc',release)
        second_writer=self.gated(d,'writer2','feed_update',release)
        self.assertFalse(fourth.wait(.05));self.assertFalse(second_writer.is_set())
        trade=self.gated(d,'trade','trading',release);self.assertTrue(trade.wait(.5))
        chart=self.gated(d,'chart','chart',release);self.assertTrue(chart.wait(.5))
        self.assertEqual(d.snapshot()['running'],5);self.assertFalse(second_writer.is_set())
    def test_53_200_assessments_progress_during_long_snapshot_with_trade_started_first(self):
        d=self.make();release=threading.Event();self.addCleanup(release.set)
        for key,lane in [('trade','trading'),('writer','feed_update'),('snapshot','snapshot')]:
            self.assertTrue(self.gated(d,key,lane,release,durable=key=='writer').wait(1))
        self.assertFalse(d.cancel('writer'))
        for count in (53,200):
            for n in range(count):
                finished=threading.Event()
                d.submit(lambda:None,lambda _:finished.set(),lambda _:finished.set(),key=('assessment',count,n),lane='eligibility')
                self.assertTrue(finished.wait(.5),'Assessment was excluded by the long snapshot')
                self.assertIn('snapshot',d.snapshot()['running_lanes'])
        assessment=self.gated(d,'held-assessment','eligibility',release)
        self.assertTrue(assessment.wait(.5))
        chart=self.gated(d,'chart','chart',release);self.assertTrue(chart.wait(.5))
        self.assertEqual(d.snapshot()['running'],5)
        extra=self.gated(d,'extra-background','misc',release);self.assertFalse(extra.wait(.05))
        duplicate=self.gated(d,'second-assessment','eligibility',release);self.assertFalse(duplicate.wait(.05))
        self.assertEqual(len(d.threads),5)
    def test_chart_and_trade_first_leave_exactly_three_background_slots(self):
        d=self.make();release=threading.Event();self.addCleanup(release.set)
        for key,lane in [('chart','chart'),('trade','trading'),('writer','feed_update'),('snapshot','snapshot'),('assessment','eligibility')]:
            self.assertTrue(self.gated(d,key,lane,release,durable=key=='writer').wait(.5),lane)
        state=d.snapshot();self.assertEqual(state['running'],5)
        self.assertEqual(sum(n for lane,n in state['running_lanes'].items() if lane not in ('chart','trading')),3)
    def test_pressure_and_smaller_explicit_pool_retain_existing_limits(self):
        for pressure,workers,capacity in [({'cpu':95,'ram':30},5,2),({'cpu':5,'ram':87},5,2),({'cpu':5,'ram':30},4,4)]:
            d=self.make(pressure,workers)
            with d.cv:d.pressure=pressure;self.assertEqual(d._capacity(),capacity)
            if capacity==2:
                release=threading.Event();self.addCleanup(release.set)
                self.assertTrue(self.gated(d,'writer','feed_update',release).wait(2))
                self.assertTrue(self.gated(d,'read','read',release).wait(2))
                chart=self.gated(d,'chart','chart',release);self.assertFalse(chart.wait(.05))
                release.set();self.assertTrue(chart.wait(1))
        with d.cv:d.busy_until=time.monotonic()+1;self.assertEqual(d._capacity(),2)
    def test_superseded_result_is_still_discarded_before_latest_apply(self):
        d=self.make();first=threading.Event();release=threading.Event();done=threading.Event();applied=[]
        self.addCleanup(release.set)
        requests=LatestRequest(lambda work,ok,bad:d.submit(work,ok,bad,lane='chart'),
            lambda v:(applied.append(v),done.set()),lambda e:self.fail(str(e)))
        def old():first.set();release.wait(3);return 'old'
        requests.request(old);self.assertTrue(first.wait(1));requests.request(lambda:'current');release.set()
        self.assertTrue(done.wait(1));self.assertEqual(applied,['current'])

if __name__=='__main__':unittest.main()
