"""New tests for finite saved-review batches sharing the existing CP queue."""
import json,sqlite3,sys,unittest
from pathlib import Path
import pandas as pd
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
import cp_reconciliation as rv

class ReviewRotation(unittest.TestCase):
 def setUp(self):
  self.db=sqlite3.connect(':memory:');self.addCleanup(self.db.close)
  self.db.execute('CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT)')
  self.infos=[];self.now=pd.Timestamp('2026-11-03T11:25:00Z')
 def put(self,key,value):self.db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)',(key,json.dumps(value)))
 def add(self,isin,observed,last_turn=None,count=1,confirmed=True):
  self.infos.append({'isin':isin,'kind':'stock'})
  p={'counts':{'RECHECK_QUEUED':count}}
  if last_turn:p['last_turn']=last_turn
  self.put(rv.PROGRESS+isin,p)
  for n in range(count):
   self.put(rv.PREFIX+isin+':'+str(n),{'id':isin+str(n),'status':'RECHECK_QUEUED','next_check':observed,
    'observation':{'identity_confirmed':confirmed,'at':observed}})
 def test_old_partial_batch_yields_to_waiting_instrument(self):
  self.add('BASF','2026-11-03T10:34:32Z','2026-11-03T11:19:15Z',10)
  self.add('ENR','2026-11-03T11:01:08Z','2026-11-03T04:58:57Z',2)
  self.assertEqual(rv.due_observation(self.db,self.infos,self.now)['isin'],'ENR')
 def test_cooldown_and_unconfirmed_identity_still_excluded(self):
  self.add('RECENT','2026-11-03T10:00:00Z','2026-11-03T11:24:00Z')
  self.add('UNCONFIRMED','2026-11-03T09:00:00Z',confirmed=False)
  self.add('ELIGIBLE','2026-11-03T11:10:00Z')
  choice=rv.due_observation(self.db,self.infos,self.now)
  self.assertEqual(choice['isin'],'ELIGIBLE')
  self.assertEqual(choice['observed_at'],'2026-11-03T11:10:00+00:00')
 def test_unregistered_and_future_retry_not_scheduled(self):
  self.add('REMOVED','2026-11-03T09:00:00Z');self.infos=[]
  self.add('FUTURE','2026-11-03T12:00:00Z')
  self.assertIsNone(rv.due_observation(self.db,self.infos,self.now))
 def test_57_continuously_pending_stocks_all_receive_turns(self):
  for n in range(57):self.add(f'I{n:02d}',(self.now-pd.Timedelta(hours=2)+pd.Timedelta(seconds=n)).isoformat(),count=4 if n==0 else 1)
  received={i['isin']:[] for i in self.infos}
  for turn in range(200):
   choice=rv.due_observation(self.db,self.infos,self.now);self.assertIsNotNone(choice)
   isin=choice['isin'];received[isin].append(turn)
   p=json.loads(self.db.execute('SELECT value FROM settings WHERE key=?',(rv.PROGRESS+isin,)).fetchone()[0])
   p['last_turn']=self.now.isoformat();self.put(rv.PROGRESS+isin,p)
   self.now+=pd.Timedelta(minutes=6)
  self.assertTrue(all(received.values()))
  self.assertEqual(len({k for k,v in received.items() if v[0]<57}),57)
  self.assertTrue(all(b-a<=57 for v in received.values() for a,b in zip(v,v[1:])))

if __name__=='__main__':unittest.main()
