"""Selection read-back must not wait for the all-watchlist worker."""
import sys, queue, tempfile, unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
from bridge import Bridge
from features import Features

class ReadbackTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.bridge=Bridge(Path(self.temp.name),queue.Queue(),lambda a,x:None)
        self.old={'isin':'INDEX:VIX','timeframe':'1D','period':'6 месяцев'}
        self.new={**self.old,'timeframe':'1W'}
    def tearDown(self):
        self.bridge.close();self.bridge.server.server_close();self.temp.cleanup()
    def test_switch_removes_old_ready_analytics_immediately(self):
        self.bridge.publish({'ready':True,'selected':self.old,'stocks':[{'signal':'BUY'}]})
        self.bridge.selection_changed(self.new)
        self.assertEqual(self.bridge.state['selected'],self.new)
        self.assertFalse(self.bridge.state['ready'])
        self.assertEqual(self.bridge.state['analytics_status'],'PENDING')
        self.assertNotIn('stocks',self.bridge.state)
        self.new['timeframe']='1M'
        self.assertEqual(self.bridge.state['selected']['timeframe'],'1W')
    def test_same_selection_does_not_destroy_completed_snapshot(self):
        state={'ready':True,'selected':self.old,'stocks':[]}
        self.bridge.publish(state);at=self.bridge.published_at
        self.bridge.selection_changed(dict(self.old))
        self.assertEqual(self.bridge.state,state);self.assertEqual(self.bridge.published_at,at)
    def test_new_accepted_analytics_replace_pending(self):
        self.bridge.selection_changed(self.new)
        self.bridge.publish({'ready':True,'selected':self.new,'stocks':[]})
        self.assertTrue(self.bridge.state['ready']);self.assertNotIn('analytics_status',self.bridge.state)
    def test_repeated_commands_keep_one_timer_and_one_busy_worker(self):
        timers={};serial=iter(range(100))
        def after(ms,callback):
            token=next(serial);timers[token]=callback;return token
        ui=SimpleNamespace(closing=False,snapshot_busy=True,
            root=SimpleNamespace(after=after,after_cancel=lambda token:timers.pop(token,None)))
        ui.publish_bridge=lambda:Features.publish_bridge(ui)
        ui.snapshot_after=after(1500,ui.publish_bridge)
        with patch('snapshot_worker.inputs',side_effect=AssertionError('Duplicate worker')):
            for _ in range(20):ui.publish_bridge()
        self.assertEqual(len(timers),1)

if __name__=='__main__':unittest.main()
