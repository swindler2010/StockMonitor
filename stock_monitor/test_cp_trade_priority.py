"""Priority is maintenance only; never grants or extends an execution permit."""
import json,unittest
from unittest.mock import patch
import pandas as pd
import candle_schedule as s
import test_cp_fresh_fair_share as fair
Memory=fair.Memory
from trading_engine import VERSION,profile

class TradePriority(unittest.TestCase):
 setup_universe=fair.FairLiveTurn.setup_universe
 complete=fair.FairLiveTurn.complete
 def prepare(self,n=5):
  self.setup_universe(n)
  self.store.db.execute('create table input_revisions(scope text primary key,revision integer)')
  for i in self.infos:self.store.db.execute('insert into input_revisions values (?,1)',('instrument:'+i['isin'],))
  self.store.db.commit()
 def ready(self,index=2):
  isin=self.infos[index]['isin'];meta=self.store.setting('gettex_chart:'+isin+':15m')['identity']
  token={'instrument:'+isin:1,'market:'+isin+':15m':0,'setting:cp_manual_risk:'+isin:0}
  c=dict(timeframe='15m',identity=meta,execution_identity=meta,analysis_ready=True,signal_ready=True,trade_ready=True,
   strategy='CORE:'+VERSION,required_tf=['15m',*profile('15m')],result={'status':'READY','signal':'BUY'},
   execution_revisions=token,execution_expires_at=(self.now+pd.Timedelta(minutes=5)).isoformat(),
   context='bounded_200_exact_reference_v1')
  record=dict(isin=isin,execution_version=1,context='bounded_200_exact_reference_v1',
   checked_at=self.now.isoformat(),candidates=[c],_input_revisions=token)
  self.store.set_setting('cp_eligibility:'+isin,record);return record
 def plan(self,positions=True,focus=True):
  with patch('paper_account.ledger',return_value={'positions':[self.position] if positions else []}),patch('gettex_chart.confirmed_state',side_effect=lambda info,v:bool(v.get('identity'))),patch.object(s,'decision',side_effect=lambda store,isin,meta,tf,now:dict(due=meta['source']=='gettex_chart' and tf in ('15m','1D'),session_open=True,key='unused',close_at='2026-11-01T22:00:00Z')):
   return s.fresh_candidate(self.store,self.infos,self.now,self.focus if focus else None)
 def test_ready_stock_precedes_focus_without_open_position(self):
  self.prepare();self.ready()
  item,stats=self.plan(positions=False)
  self.assertEqual(item['isin'],'I002')
  self.assertEqual(item['_cp_focus']['priority_reason'],'TRADE_CONTEXT_MAINTENANCE')
  self.assertFalse(item['_cp_focus']['execution_permission'])
 def test_position_ready_focus_background_then_history(self):
  self.prepare();self.ready();seen=[]
  for _ in range(4):
   item,_=self.plan();seen.append(item['isin']);self.complete(item)
  self.assertEqual(seen,['I000','I002','I001','I003']);self.assertIsNone(self.plan()[0])
 def test_no_ready_preserves_existing_three_live_slots(self):
  self.prepare();seen=[]
  for _ in range(3):
   item,_=self.plan();seen.append(item['isin']);self.complete(item)
  self.assertEqual(seen,['I000','I001','I002']);self.assertIsNone(self.plan()[0])
 def test_53_and_200_continuous_ready_load_does_not_starve_background(self):
  for n in (53,200):
   self.prepare(n);self.ready();seen=set();history=0
   for _ in range(5*(n-3)):
    item,_=self.plan()
    if item is None:s.history_finished(self.store,self.now);history+=1
    else:seen.add(item['isin']);self.complete(item)
   self.assertEqual(len(seen),n);self.assertEqual(history,n-3)
 def test_expiry_or_market_change_means_recheck_not_permission(self):
  self.prepare();record=self.ready();record['candidates'][0]['execution_expires_at']=(self.now-pd.Timedelta(seconds=1)).isoformat()
  self.store.set_setting('cp_eligibility:I002',record)
  self.store.db.execute("insert into input_revisions values ('market:I002:15m',1)");self.store.db.commit()
  item,_=self.plan(positions=False)
  self.assertEqual(item['isin'],'I002');self.assertEqual(item['_cp_focus']['proof_state'],'RECHECK_REQUIRED');self.assertFalse(item['_cp_focus']['execution_permission'])
 def test_policy_or_identity_revision_revokes_priority(self):
  for scope in ('instrument:I002','setting:cp_manual_risk:I002'):
   self.prepare();self.ready();self.store.db.execute('insert or replace into input_revisions values (?,2)',(scope,));self.store.db.commit()
   self.assertNotEqual(self.plan(positions=False)[0]['isin'],'I002')
 def test_no_priority_for_blocked_analysis_only_or_wrong_currency(self):
  for change in ('trade_ready','analytical_only','currency','identity','strategy'):
   self.prepare();record=self.ready();c=record['candidates'][0]
   if change=='trade_ready':c['trade_ready']=False
   elif change=='analytical_only':c['analytical_only']=True
   elif change=='currency':c['identity']={**c['identity'],'currency':'USD'};c['execution_identity']=c['identity']
   elif change=='identity':c['execution_identity']={**c['identity'],'venue':'GER'}
   else:c['strategy']='invented'
   self.store.set_setting('cp_eligibility:I002',record);self.assertNotEqual(self.plan(positions=False)[0]['isin'],'I002')
 def test_required_daily_tail_and_exact_provider_before_optional_5m(self):
  self.prepare();self.ready();isin='I002';meta=self.store.setting('gettex_chart:'+isin+':15m')['identity']
  self.store.set_setting('gettex_chart:'+isin+':1D',dict(identity=meta,last='2026-10-30',received_at='2026-10-30T23:00Z'))
  item,_=self.plan(positions=False);self.assertEqual(item['isin'],isin);self.assertEqual(item['_cp_focus']['timeframe'],'1D')
 def test_rate_limit_and_source_cooldown_not_bypassed(self):
  self.prepare();self.ready();self.store.set_setting('gettex_chart_next_request',(self.now+pd.Timedelta(minutes=1)).isoformat())
  self.assertIsNone(self.plan()[0])
  meta=self.store.setting('gettex_chart:I002:15m')['identity'];self.store.set_setting('cp_source_retry:gettex_chart',(self.now+pd.Timedelta(hours=1)).isoformat())
  self.assertEqual(s.decision(self.store,'I002',meta,'15m',self.now)['reason'],'SOURCE_COOLDOWN')
 def test_restart_keeps_background_slot_and_read_only_evidence(self):
  self.prepare();self.ready();before=self.store.setting('cp_eligibility:I002')
  for _ in range(3):self.complete(self.plan()[0])
  copied=Memory();self.addCleanup(copied.db.close);copied.db.execute('create table input_revisions(scope text primary key,revision integer)')
  for row in self.store.db.execute('select * from input_revisions'):copied.db.execute('insert into input_revisions values (?,?)',row)
  for key,value in self.store.db.execute('select * from settings'):copied.set_setting(key,json.loads(value))
  self.store=copied;self.assertEqual(self.plan()[0]['isin'],'I003');self.assertEqual(before,self.store.setting('cp_eligibility:I002'))
 def test_unknown_strategy_signal_or_other_provider_is_not_promoted(self):
  for change in ('context','signal','provider'):
   self.prepare();record=self.ready();c=record['candidates'][0]
   if change=='context':record['context']='unknown'
   elif change=='signal':c['result']['signal']='WAIT'
   else:c['identity']={**c['identity'],'source':'proxy'};c['execution_identity']=c['identity']
   self.store.set_setting('cp_eligibility:I002',record);item,_=self.plan(positions=False)
   self.assertNotEqual(item['_cp_focus'].get('priority_reason'),'TRADE_CONTEXT_MAINTENANCE')
 def test_priority_metadata_survives_existing_worker_route(self):
  import sys
  from pathlib import Path
  sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
  import test_fresh_route_upgrade as route
  self.prepare();self.ready();item,_=self.plan(positions=False)
  sent=route.FreshRouteUpgrade().capture({**self.infos[2],'_eligibility_only':True},item)
  self.assertEqual(sent['_cp_focus'],item['_cp_focus']);self.assertNotIn('_eligibility_only',sent)
 def test_pending_archive_request_cannot_consume_fresh_price_turn(self):
  import queue
  from types import SimpleNamespace
  from feed_worker import FeedContext
  events=queue.Queue();calls=[];info={'isin':'I000','_gettex_poll_only':True,'_cp_position':True}
  def acquire(item):calls.append('acquire');events.put((None,(item,[])))
  host=SimpleNamespace(store=object(),events=events,_run_feed=acquire,_recover_gettex_range=None)
  with patch('cp_controls.service_request',return_value={'state':'WAITING'}) as archive:
   self.assertEqual(FeedContext._calculate(host,info),{'errors':[]})
   self.assertEqual(calls,['acquire']);archive.assert_not_called()
  with patch('cp_controls.service_request',return_value={'state':'WAITING'}) as archive:
   self.assertEqual(FeedContext._calculate(host,{'isin':'I000'}),{'errors':[],'cp_priority':{'state':'WAITING'}})
   archive.assert_called_once()

if __name__=='__main__':unittest.main()
