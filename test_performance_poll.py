import sys,unittest,threading,time
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
from tunnel_runtime import TunnelRuntime
from performance_dispatcher import Dispatcher
class PollTests(unittest.TestCase):
    def test_poll_only_enqueues_and_deduplicates(self):
        queued=[];runtime=TunnelRuntime({},submit=lambda work:queued.append(work))
        with patch('tunnel_runtime.threading.Thread.start',side_effect=AssertionError('UI started thread')):
            runtime.poll();runtime.poll()
        self.assertEqual(len(queued),1);self.assertTrue(runtime.busy)
        queued[0]();self.assertFalse(runtime.busy);self.assertEqual(runtime.snapshot()['status'],'disabled')
    def test_queue_rejection_and_exception_release_busy(self):
        for submit in (lambda _:False,lambda _:(_ for _ in ()).throw(RuntimeError('private details'))):
            runtime=TunnelRuntime({},submit=submit);self.assertFalse(runtime.poll());self.assertFalse(runtime.busy)
            self.assertNotIn('private details',str(runtime.snapshot()))
    def test_existing_dispatcher_runs_check_off_main_thread(self):
        seen=[];done=threading.Event();main=threading.get_ident()
        d=Dispatcher(lambda cb,r:cb(r),sample=lambda _:({'cpu':0,'ram':0},None))
        runtime=TunnelRuntime({},submit=lambda work:d.submit(work,lambda _:done.set(),lambda _:done.set(),key='connection-poll'))
        original=runtime._work
        def work():seen.append(threading.get_ident());original()
        runtime._work=work
        try:
            runtime.poll();self.assertTrue(done.wait(3));self.assertNotEqual(seen,[main]);self.assertEqual(len(seen),1)
            self.assertFalse(runtime.busy);self.assertEqual(runtime.snapshot()['status'],'disabled')
        finally:
            d.close();d.monitor.join(3)
            for thread in d.threads:thread.join(3)
if __name__=='__main__':unittest.main()
