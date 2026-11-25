"""Ready owner-reviewed WAIT contexts get maintenance, never trade permission."""
import unittest
import pandas as pd
import candle_schedule as s
from cp_controls import DEMO_ALL_INITIALIZATION
import test_cp_trade_priority as prior
Memory=prior.Memory

class DemoPriority(unittest.TestCase):
 prepare=prior.TradePriority.prepare
 setup_universe=prior.TradePriority.setup_universe
 complete=prior.TradePriority.complete
 plan=prior.TradePriority.plan
 def ready(self,index=2):
  record=prior.TradePriority.ready(self,index);c=record['candidates'][0];isin=record['isin']
  c['required_tf']=list(dict.fromkeys(c['required_tf']))
  c.update(context=DEMO_ALL_INITIALIZATION,owner_reviewed_analysis=True,owner_initialization_semantics=3,
   input_binding_version=2,signal_ready=False,trade_ready=False,analytical_only=False,
   result={'status':'READY','signal':'WAIT'},expires_at=c['execution_expires_at'],
   closed_inputs={'version':1,'strict':{'instrument:'+isin:1},'revisions':c['execution_revisions'],
    'series':[{'spec':{'isin':isin,'identity':c['identity'],'timeframe':tf},'count':200} for tf in c['required_tf']]})
  self.store.set_setting('cp_eligibility:'+isin,record);return record
 def test_wait_demo_precedes_different_open_chart_without_grant(self):
  self.prepare();self.ready();item,stats=self.plan(positions=False)
  self.assertEqual(item['isin'],'I002');self.assertEqual(stats['trade_maintenance_stocks'],1)
  self.assertEqual(item['_cp_focus']['proof_state'],'OWNER_REVIEWED_DEMO_CONTEXT')
  self.assertFalse(item['_cp_focus']['execution_permission'])
 def test_actual_unique_required_tfs_accepted_for_automatic_context_too(self):
  self.prepare();v=prior.TradePriority.ready(self,2);c=v['candidates'][0]
  c['required_tf']=list(dict.fromkeys(c['required_tf']));self.store.set_setting('cp_eligibility:I002',v)
  proof=s.trade_maintenance(self.store,self.infos,self.now)['I002'][0]
  self.assertEqual(proof['required_tf'],['15m','4H','1D'])
  c['required_tf'].remove('4H');self.store.set_setting('cp_eligibility:I002',v)
  self.assertNotIn('I002',s.trade_maintenance(self.store,self.infos,self.now))
 def test_position_demo_chart_background_history_all_keep_slots(self):
  self.prepare();self.ready();seen=[]
  for _ in range(4):
   item,_=self.plan();seen.append(item['isin']);self.complete(item)
  self.assertEqual(seen,['I000','I002','I001','I003']);self.assertIsNone(self.plan()[0])
 def test_future_stock_enters_automatically_and_ready_stocks_rotate(self):
  self.prepare(7);self.ready(2);self.ready(5)
  seen=set()
  for _ in range(15):
   item,_=self.plan()
   if item is None:s.history_finished(self.store,self.now)
   else:
    if item['_cp_focus'].get('proof_state')=='OWNER_REVIEWED_DEMO_CONTEXT':seen.add(item['isin'])
    self.complete(item)
  self.assertEqual(seen,{'I002','I005'})
 def test_expiry_market_revision_requeues_without_extending_permission(self):
  self.prepare();v=self.ready();v['candidates'][0]['expires_at']=(self.now-pd.Timedelta(seconds=1)).isoformat()
  self.store.set_setting('cp_eligibility:I002',v);self.store.db.execute("insert into input_revisions values ('market:I002:15m',1)");self.store.db.commit()
  item,_=self.plan(positions=False);self.assertEqual(item['isin'],'I002')
  self.assertEqual(item['_cp_focus']['proof_state'],'RECHECK_REQUIRED');self.assertFalse(item['_cp_focus']['execution_permission'])
 def test_owner_revocation_or_identity_revision_removes_priority(self):
  for scope in ('instrument:I002','setting:cp_manual_risk:I002','setting:cp_demo_initialization_policy'):
   self.prepare();v=self.ready();v['candidates'][0]['closed_inputs']['revisions'][scope]=0 if scope.startswith('setting:') else 1
   self.store.set_setting('cp_eligibility:I002',v);self.store.db.execute('insert or replace into input_revisions values (?,2)',(scope,));self.store.db.commit()
   self.assertNotIn('I002',s.trade_maintenance(self.store,self.infos,self.now))
 def test_bad_context_source_dependency_and_blocked_do_not_promote(self):
  for bad in ('blocked','currency','venue','source','proof','context','analytical_only','strategy'):
   self.prepare();v=self.ready();c=v['candidates'][0]
   if bad=='blocked':c['analysis_ready']=False
   elif bad in ('currency','venue','source'):c['identity']={**c['identity'],bad:{'currency':'USD','venue':'GER','source':'proxy'}[bad]}
   elif bad=='proof':c['closed_inputs']['series'].pop()
   elif bad=='context':c['context']='unapproved'
   elif bad=='analytical_only':c['analytical_only']=True
   else:c['strategy']='new'
   self.store.set_setting('cp_eligibility:I002',v)
   with self.subTest(bad=bad):self.assertNotIn('I002',s.trade_maintenance(self.store,self.infos,self.now))
 def test_53_200_load_two_demo_contexts_do_not_starve_history_or_others(self):
  for n in (53,200):
   self.prepare(n);self.ready(2);self.ready(3);seen=set();history=0
   for _ in range(5*(n-3)):
    item,_=self.plan()
    if item is None:s.history_finished(self.store,self.now);history+=1
    else:seen.add(item['isin']);self.complete(item)
   self.assertEqual(len(seen),n);self.assertGreater(history,0)
 def test_reopened_rotation_keeps_next_background_slot(self):
  self.prepare();self.ready()
  for _ in range(3):self.complete(self.plan()[0])
  old=self.store;self.store=Memory();self.addCleanup(self.store.db.close)
  self.store.db.execute('create table input_revisions(scope text primary key,revision integer)')
  for row in old.db.execute('select * from input_revisions'):self.store.db.execute('insert into input_revisions values (?,?)',row)
  for key,value in old.db.execute('select * from settings'):
   import json
   self.store.set_setting(key,json.loads(value))
  self.assertEqual(self.plan()[0]['isin'],'I003')

if __name__=='__main__':unittest.main()
