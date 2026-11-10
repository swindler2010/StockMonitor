"""CP's read-only planner must run before aged history jobs, without a new lane."""
import sys,time,unittest,copy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
from desktop014 import Desktop014
from performance_dispatcher import Dispatcher


class PlannerDispatch(unittest.TestCase):
 def capture(self,closing=False):
  jobs=[];timers=[];feeds=[];promotions=[]
  ctx=SimpleNamespace(closing=closing,watchlist=[{'isin':'EXAMPLE','name':'saved'}],
   timeframe=SimpleNamespace(get=lambda:'15m'),instrument_cfg=lambda:{'isin':'EXAMPLE'},store=object(),
   root=SimpleNamespace(after=lambda *args:timers.append(args)),_scheduled_refresh=lambda:None,
   job=lambda work,done,**kwargs:jobs.append((work,done,kwargs)),start_feed=feeds.append,
   dispatcher=SimpleNamespace(prioritize_feed=lambda *args:promotions.append(args)))
  Desktop014._scheduled_refresh(ctx)
  return ctx,jobs,timers,feeds,promotions

 def ranker(self):
  d=Dispatcher.__new__(Dispatcher);d.visible=None;d.positions=set();d.resume=set()
  return d

 def test_existing_read_lane_prevents_planner_starvation_by_57_aged_feeds(self):
  _,jobs,_,_,_=self.capture();settings=jobs[0][2];d=self.ranker();now=time.monotonic()
  planner=dict(settings,key='cp-schedule',isin=None,queued=now,sequence=100)
  history=[dict(key='feed:'+str(i),lane='feed_update',isin=str(i),queued=now-600,sequence=i) for i in range(57)]
  self.assertEqual(min([*history,planner],key=d._rank)['key'],'cp-schedule')

 def test_planner_is_read_only_and_dispatches_no_feed_until_result_is_applied(self):
  plan=dict(fresh=None,priority=None,review=[],eligibility=[],feeds=[],planned_at='now')
  with patch('candle_schedule.planned_feeds',return_value=plan) as read:
   ctx,jobs,timers,feeds,promotions=self.capture();work,apply,settings=jobs[0]
   self.assertEqual(settings,dict(key='cp-schedule',lane='read'))
   self.assertEqual(timers[0][0],30000);self.assertEqual(feeds,[]);self.assertEqual(promotions,[])
   self.assertEqual(work(),plan);read.assert_called_once()
  self.assertEqual(feeds,[]);apply(plan);self.assertIs(ctx.cp_schedule_last,plan)

 def test_chart_keeps_priority_and_history_runs_when_the_single_planner_finishes(self):
  _,jobs,_,_,_=self.capture();d=self.ranker();now=time.monotonic()
  planner=dict(jobs[0][2],isin=None,queued=now,sequence=2)
  chart=dict(key='chart',lane='chart',isin='EXAMPLE',queued=now,sequence=3)
  history=dict(key='feed:other',lane='feed_update',isin='other',queued=now-600,sequence=1)
  ready=[history,planner,chart];order=[]
  while ready:
   chosen=min(ready,key=d._rank);ready.remove(chosen);order.append(chosen['key'])
  self.assertEqual(order,['chart','cp-schedule','feed:other'])

 def test_unchanged_plan_preserves_fresh_repair_and_background_routes(self):
  ctx,jobs,_,feeds,promotions=self.capture()
  fresh={'isin':'position','_cp_position':True,'_gettex_poll_only':True}
  priority={'isin':'repair'};review={'isin':'review','_review_only':True};eligible={'isin':'eligibility'};other={'isin':'history'}
  plan=dict(fresh=fresh,priority=priority,review=[review],eligibility=[eligible],feeds=[(other,'HISTORY_MAINTENANCE')])
  before=copy.deepcopy(plan);jobs[0][1](plan)
  self.assertEqual(feeds,[fresh,priority,review,dict(eligible,_eligibility_only=True),other])
  self.assertEqual(promotions,[('feed:position',1.1),('feed:repair',)])
  self.assertEqual(plan,before)

 def test_closing_does_not_schedule_or_apply_a_late_plan(self):
  _,jobs,timers,feeds,_=self.capture(True);self.assertFalse(jobs or timers or feeds)
  ctx,jobs,_,feeds,_=self.capture();ctx.closing=True
  jobs[0][1]({'fresh':{'isin':'late'}});self.assertFalse(feeds)

if __name__=='__main__':unittest.main()
