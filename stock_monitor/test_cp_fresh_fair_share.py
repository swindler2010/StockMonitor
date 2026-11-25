"""Continuous position/focus load must leave a live-source turn for others."""
import unittest,sqlite3,json,contextlib
from unittest.mock import patch
import pandas as pd
import candle_schedule as s

class Memory:
 def __init__(self):
  self.db=sqlite3.connect(':memory:');self.db.execute('create table settings(key text primary key,value text)')
 @contextlib.contextmanager
 def connect(self):
  with self.db:yield self.db
 def setting(self,k,d=None):
  row=self.db.execute('select value from settings where key=?',(k,)).fetchone();return json.loads(row[0]) if row else d
 def set_setting(self,k,v):self.db.execute('insert or replace into settings values (?,?)',(k,json.dumps(v)));self.db.commit()

class FairLiveTurn(unittest.TestCase):
 def setup_universe(self,n):
  self.store=Memory();self.addCleanup(self.store.db.close)
  self.infos=[{'isin':f'I{i:03}','kind':'stock','name':f'Stock{i}'} for i in range(n)]
  self.now=pd.Timestamp('2026-11-02T12:00Z')
  for i in self.infos:
   self.store.set_setting('gettex_chart:'+i['isin']+':15m',{'identity':dict(source='gettex_chart',venue='GETTEX',currency='EUR',symbol=i['isin']+'.GTX',timezone='Europe/Berlin'),'received_at':'2026-11-02T10:00:00Z'})
  self.position={'isin':'I000','quantity':1,'timeframe':'15m','identity':self.store.setting('gettex_chart:I000:15m')['identity']}
  self.focus={'isin':'I001','timeframe':'15m'}
 def plan(self,positions=True,focus=True):
  with patch('paper_account.ledger',return_value={'positions':[self.position] if positions else []}),patch('gettex_chart.confirmed_state',side_effect=lambda info,v:bool(v.get('identity'))),patch.object(s,'decision',side_effect=lambda store,isin,meta,tf,now:dict(due=meta['source']=='gettex_chart' and tf=='15m',session_open=True,key='unused')):
   return s.fresh_candidate(self.store,self.infos,self.now,self.focus if focus else None)[0]
 def complete(self,item):
  s.fresh_finished(self.store,item,'gettex_chart','SAVED',self.now)
  key='gettex_chart:'+item['isin']+':15m';v=self.store.setting(key);v['received_at']=self.now.isoformat();self.store.set_setting(key,v);self.now+=pd.Timedelta(seconds=1)
 def test_position_focus_and_background_each_get_one_live_turn(self):
  self.setup_universe(5);seen=[]
  for _ in range(3):
   item=self.plan();seen.append(item['isin']);self.complete(item)
  self.assertEqual(seen,['I000','I001','I002']);self.assertIsNone(self.plan())
 def test_long_load_53_and_200_stocks_has_no_live_source_starvation(self):
  for n in (53,200):
   self.setup_universe(n);seen=set();history=0
   for _ in range(4*(n-2)):
    item=self.plan()
    if item is None:s.history_finished(self.store,self.now);history+=1
    else:seen.add(item['isin']);self.complete(item)
   self.assertEqual(len(seen),n);self.assertEqual(history,n-2)
 def test_rotation_reloaded_from_saved_state_keeps_background_slot(self):
  self.setup_universe(4)
  for _ in range(2):self.complete(self.plan())
  copied=Memory();self.addCleanup(copied.db.close)
  for key,value in self.store.db.execute('select * from settings'):copied.set_setting(key,json.loads(value))
  self.store=copied;self.assertEqual(self.plan()['isin'],'I002')
 def test_no_position_or_focus_still_services_all_stocks(self):
  for positions,focus in ((False,True),(True,False),(False,False)):
   self.setup_universe(5);seen=set()
   for _ in range(24):
    item=self.plan(positions,focus)
    if item is None:s.history_finished(self.store,self.now)
    else:seen.add(item['isin']);self.complete(item)
   self.assertEqual(len(seen),5)
 def test_two_stocks_do_not_create_an_empty_background_slot(self):
  self.setup_universe(2)
  for _ in range(3):
   item=self.plan();self.assertIsNotNone(item);self.complete(item)
  self.assertIsNone(self.plan())
 def test_rate_reservation_still_prevents_chart_request(self):
  self.setup_universe(3);self.store.set_setting('gettex_chart_next_request',(self.now+pd.Timedelta(minutes=1)).isoformat())
  self.assertIsNone(self.plan())

if __name__=='__main__':unittest.main()
