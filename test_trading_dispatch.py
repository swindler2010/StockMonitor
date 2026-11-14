"""Operational trading is independent of slow readonly/all-TF assessment."""
import sys,time,threading,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
from performance_dispatcher import Dispatcher


class TradingDispatch(unittest.TestCase):
 def test_close_finishes_writer_response_before_termination(self):
  import io
  from unittest.mock import Mock,patch
  from compute_process import Client
  client=Client('unused',feed_updates=True)
  process=Mock(stdin=io.BytesIO(),stdout=io.BytesIO());process.poll.return_value=None
  client.process=process
  def response(stream):
   client.close();process.terminate.assert_not_called()
   return True,{'committed':True}
  with patch('compute_process.pickle.load',side_effect=response):
   self.assertEqual(client.calculate('feed_update',{}),{'committed':True})
  process.terminate.assert_called_once();self.assertIsNone(client.process)
 def test_trading_runs_while_feed_and_assessment_are_occupied(self):
  release=threading.Event();assessment=threading.Event();feed=threading.Event();trade=threading.Event()
  dispatcher=Dispatcher(lambda cb,v:cb(v),sample=lambda _:({'cpu':0,'ram':0},None))
  self.addCleanup(dispatcher.close);self.addCleanup(release.set)
  def hold(event):event.set();release.wait(4)
  dispatcher.submit(lambda:hold(assessment),lambda _:None,lambda _:None,key='assessment',lane='eligibility')
  dispatcher.submit(lambda:hold(feed),lambda _:None,lambda _:None,key='feed',lane='feed_update')
  self.assertTrue(assessment.wait(1));self.assertTrue(feed.wait(1))
  dispatcher.submit(trade.set,lambda _:None,lambda _:None,key='trade',lane='trading')
  self.assertTrue(trade.wait(1));self.assertFalse(release.is_set())
 def test_53_and_200_jobs_complete_with_trading_and_background(self):
  for count in (53,200):
   with self.subTest(count=count):
    seen=[];done=threading.Event();lock=threading.Lock()
    dispatcher=Dispatcher(lambda cb,v:cb(v),sample=lambda _:({'cpu':0,'ram':0},None))
    self.addCleanup(dispatcher.close)
    def completed(value):
     with lock:
      seen.append(value)
      if len(seen)==count:done.set()
    lanes=['trading','feed_update','eligibility','snapshot','chart']
    for i in range(count):dispatcher.submit(lambda n=i:n,completed,lambda e:self.fail(e),key=str(i),lane=lanes[i%5],isin=str(i))
    self.assertTrue(done.wait(4));self.assertEqual(set(seen),set(range(count)))
 def test_writer_still_cannot_be_cancelled(self):
  d=Dispatcher(lambda cb,v:None,sample=lambda _:({'cpu':0,'ram':0},None));self.addCleanup(d.close)
  with d.cv:
   d.submit(lambda:None,lambda _:None,lambda _:None,key='writer',lane='feed_update',durable=True)
   self.assertFalse(d.cancel('writer'))
 def test_snapshot_and_assessment_rank_fifo_without_starving_each_other(self):
  d=Dispatcher(lambda cb,v:None,sample=lambda _:({'cpu':0,'ram':0},None));self.addCleanup(d.close)
  common=dict(isin=None,queued=time.monotonic())
  first=dict(common,key='snapshot',lane='snapshot',sequence=1)
  second=dict(common,key='assessment',lane='eligibility',sequence=2)
  self.assertLess(d._rank(first),d._rank(second))
  first.update(sequence=3);self.assertLess(d._rank(second),d._rank(first))

if __name__=='__main__':unittest.main()
