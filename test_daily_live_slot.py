"""Missing closed 1D tails must participate in the existing live CP rotation."""
import copy,sys,unittest
from pathlib import Path
from unittest.mock import patch
import pandas as pd
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
import candle_schedule as s
from test_cp_fresh_fair_share import Memory

class DailyLiveSlot(unittest.TestCase):
 def setUp(self):
  self.store=Memory();self.addCleanup(self.store.db.close)
  self.store.db.execute('CREATE TABLE input_revisions(scope TEXT PRIMARY KEY,revision INTEGER)')
  self.now=pd.Timestamp('2026-11-03T10:00Z')
  self.info={'isin':'DE0007164600','name':'SAP','kind':'stock'}
  self.meta=dict(source='gettex_chart',symbol='SAPG.GTX',venue='GETTEX',currency='EUR',timezone='Europe/Berlin')
  self.saved=dict(state='PARTIAL',identity=self.meta,listing_proof=dict(self.meta,isin=self.info['isin'],status='CONFIRMED'),
                  received_at='2026-11-02T11:04:01Z',last='2026-10-30 00:00:00')
  self.key='gettex_chart:'+self.info['isin']+':1D';self.store.set_setting(self.key,self.saved)
  original=s.decision
  self.guard=patch.object(s,'decision',side_effect=lambda st,i,m,b,n:original(st,i,m,b,n) if m['source']=='gettex_chart' else {'due':False,'session_open':False})
  self.guard.start();self.addCleanup(self.guard.stop)
  self.paper=patch('paper_account.ledger',return_value={'positions':[]});self.paper.start();self.addCleanup(self.paper.stop)
 def choose(self,infos=None):return s.fresh_candidate(self.store,infos or [self.info],self.now)[0]
 def test_real_missing_closed_daily_tail_is_selected(self):
  item=self.choose();self.assertIsNotNone(item)
  self.assertEqual(item['_cp_focus'],{'isin':self.info['isin'],'source':'gettex_chart','timeframe':'1D'})
  self.assertTrue(item['_gettex_poll_only'])
 def test_already_present_closed_day_not_promoted_or_rewritten(self):
  self.store.set_setting(self.key,dict(self.saved,last='2026-11-02 00:00:00'))
  before=list(self.store.db.execute('select * from settings'))
  self.assertIsNone(self.choose());self.assertEqual(before,list(self.store.db.execute('select * from settings')))
 def test_closed_session_waits_for_publication_boundary(self):
  self.store.set_setting(self.key,dict(self.saved,last='2026-10-30 00:00:00'))
  self.now=pd.Timestamp('2026-11-02T22:00:59Z');self.assertIsNone(self.choose())
  self.now=pd.Timestamp('2026-11-02T22:01:00Z');self.assertEqual(self.choose()['_cp_focus']['timeframe'],'1D')
 def test_rate_reservation_source_and_unit_cooldowns_remain(self):
  keys=['gettex_chart_next_request','cp_source_retry:gettex_chart',s.unit_key(self.info['isin'],self.meta,'1D')]
  for k in keys:
   with self.subTest(k=k):
    value=(self.now+pd.Timedelta(hours=2)).isoformat()
    self.store.set_setting(k,{'retry_after':value} if k.startswith('cp_schedule:') else value)
    self.assertIsNone(self.choose());self.store.db.execute('DELETE FROM settings WHERE key=?',(k,));self.store.db.commit()
  self.store.set_setting(self.key,dict(self.saved,retry_after=(self.now+pd.Timedelta(hours=2)).isoformat()))
  self.assertIsNone(self.choose())
 def test_unconfirmed_wrong_currency_or_venue_never_promoted(self):
  for field,value in [('currency','USD'),('venue','FRA'),('symbol','SAP.DE')]:
   with self.subTest(field=field):
    saved=copy.deepcopy(self.saved);saved['identity'][field]=value
    self.store.set_setting(self.key,saved);self.assertIsNone(self.choose())
  for last in (None,'invalid','2026-11-03 00:00:00'):
   with self.subTest(last=last):
    self.store.set_setting(self.key,dict(self.saved,last=last));self.assertIsNone(self.choose())
 def test_waiting_publication_budget_is_not_bypassed(self):
  plan=s.decision(self.store,self.info['isin'],self.meta,'1D',self.now)
  s.finish(self.store,self.info['isin'],self.meta,'1D',plan,now=self.now)
  self.assertIsNone(self.choose())
  self.now+=pd.Timedelta(minutes=15);self.assertIsNotNone(self.choose())
  prior=self.store.setting(plan['key']);prior.update(pending_daily_close=plan['close_at'],publication_rechecks=12)
  self.store.set_setting(plan['key'],prior);self.now+=pd.Timedelta(hours=1)
  self.assertIsNone(self.choose())
 def test_rotation_resumes_without_repeating_saved_day(self):
  item=self.choose();s.fresh_finished(self.store,item,'gettex_chart','SAVED',self.now)
  self.store.set_setting(self.key,dict(self.saved,last='2026-11-02 00:00:00',received_at=self.now.isoformat()))
  copied=Memory();self.addCleanup(copied.db.close);copied.db.execute('CREATE TABLE input_revisions(scope TEXT PRIMARY KEY,revision INTEGER)')
  for k,v in self.store.db.execute('select * from settings'):
   import json;copied.set_setting(k,json.loads(v))
  self.store=copied;self.assertIsNone(self.choose());self.assertEqual(self.store.setting(s.FRESH_ROTATION)['consecutive'],1)
 def test_53_and_200_daily_tails_share_live_and_history_slots(self):
  for n in (53,200):
   with self.subTest(n=n):
    self.store.db.execute('DELETE FROM settings');self.store.db.commit();infos=[];seen=set();history=0
    for index in range(n):
     i=dict(isin=f'US{index:010d}',kind='stock');infos.append(i);meta=dict(self.meta,symbol=f'T{index}.GTX')
     self.store.set_setting('gettex_chart:'+i['isin']+':1D',dict(self.saved,identity=meta,listing_proof=dict(meta,isin=i['isin'],status='CONFIRMED')))
    for _ in range(n+n//3+2):
     item=self.choose(infos)
     if item is None:
      s.history_finished(self.store,self.now);history+=1
     else:
      seen.add(item['isin']);key='gettex_chart:'+item['isin']+':1D';saved=self.store.setting(key)
      self.store.set_setting(key,dict(saved,last='2026-11-02 00:00:00',received_at=self.now.isoformat()))
      s.fresh_finished(self.store,item,'gettex_chart','SAVED',self.now)
    self.assertEqual(len(seen),n);self.assertGreaterEqual(history,n//3)

if __name__=='__main__':unittest.main()
