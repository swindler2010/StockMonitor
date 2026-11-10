"""Fresh source reservation must precede archive recovery on the same lane."""
import unittest,contextlib,queue
from types import SimpleNamespace
from unittest.mock import Mock,patch
import pandas as pd
from desktop014 import Desktop014

class AcquisitionOrder(unittest.TestCase):
 def run_feed(self,info=None,confirmed=True,chart_state='SAVED'):
  calls=[];budget={'used':False}
  def recover(*a,**kw):calls.append('archive');budget['used']=True;return None,[]
  def chart(*a,**kw):
   calls.append(('chart',kw.get('preferred_tf'),'RATE_WAIT' if budget['used'] else chart_state));budget['used']=True
   return {'state':'RATE_WAIT' if calls[-1][2]=='RATE_WAIT' else chart_state,'reason':'controlled source response'}
  store=SimpleNamespace(setting=lambda k,d=None:d,set_setting=Mock(),event=Mock(),load_official=lambda *a:pd.DataFrame())
  ctx=SimpleNamespace(store=store,closing=False,feed_slots=contextlib.nullcontext(),events=queue.Queue(),feed_done=lambda *a:None,
   _download_gettex_trades=lambda *a:pd.DataFrame(),_recover_gettex_range=Mock())
  patches=[patch('history_service.recover',side_effect=recover),patch('history_service.inspect_saved_archive',side_effect=lambda *a,**k:calls.append('audit')),
   patch('gettex_history.acquire_missing_evidence'),patch('gettex_history.classify_cached_session'),patch('revision_watcher.check',side_effect=lambda *a,**k:calls.append('watcher')),
   patch('gettex_chart.confirmed_state',return_value=confirmed),patch('gettex_chart.turn',side_effect=chart),patch('candle_schedule.history_finished'),patch('candle_schedule.fresh_finished')]
  with contextlib.ExitStack() as stack:
   for p in patches:stack.enter_context(p)
   Desktop014._run_feed(ctx,info or {'isin':'US00287Y1091','kind':'stock'})
  return calls,ctx
 def test_confirmed_default_source_acquired_before_recovery_can_consume_budget(self):
  calls,ctx=self.run_feed();self.assertEqual(calls[0],('chart','15m','SAVED'));self.assertIn('archive',calls);self.assertIn('audit',calls)
  self.assertEqual(len([c for c in calls if isinstance(c,tuple)]),1)
 def test_unconfirmed_source_keeps_bootstrap_without_false_priority(self):
  calls,_=self.run_feed(confirmed=False);self.assertEqual(calls[0],'archive');self.assertEqual(calls[-1],('chart',None,'RATE_WAIT'))
 def test_explicit_proxy_foreground_is_not_replaced(self):
  calls,_=self.run_feed({'isin':'I','kind':'stock','_cp_focus':{'isin':'I','source':'proxy','timeframe':'1D'}})
  self.assertEqual(calls[0],'archive');self.assertEqual(calls[-1],('chart',None,'RATE_WAIT'))
 def test_existing_explicit_chart_foreground_retains_tf_and_one_call(self):
  calls,_=self.run_feed({'isin':'I','kind':'stock','_cp_focus':{'isin':'I','source':'gettex_chart','timeframe':'5m'}})
  self.assertEqual(calls[0],('chart','5m','SAVED'));self.assertEqual(len([c for c in calls if isinstance(c,tuple)]),1)
 def test_native_poll_remains_short_without_chart_or_archive_requests(self):
  calls,_=self.run_feed({'isin':'I','kind':'stock','_gettex_poll_only':True});self.assertEqual(calls,[])
 def test_chart_poll_remains_short(self):
  calls,_=self.run_feed({'isin':'I','kind':'stock','_gettex_poll_only':True,'_cp_focus':{'isin':'I','source':'gettex_chart','timeframe':'15m'}})
  self.assertEqual(calls,[('chart','15m','SAVED')])
 def test_rate_wait_and_source_error_are_not_retried_in_same_turn(self):
  for status in ('RATE_WAIT','SOURCE_COOLDOWN','ERROR'):
   calls,ctx=self.run_feed(chart_state=status);self.assertEqual(len([c for c in calls if isinstance(c,tuple)]),1);self.assertIn('audit',calls)
   errors=ctx.events.get()[1][1]
   if status=='ERROR':self.assertTrue(any('controlled source response' in e for e in errors))

if __name__=='__main__':unittest.main()
