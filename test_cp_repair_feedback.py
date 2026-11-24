import copy,json,sqlite3,sys,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
from cp_problem_view import Dialog,present,repair_capability,read_requests
from cp_controls import repair_id
from test_cp_problem_view import PROBLEM

class RepairFeedback(unittest.TestCase):
 def test_button_reflects_receipt_not_just_supported_route(self):
  colors={'QUEUED':'Amber','RUNNING':'Blue','WAITING':'Red','BLOCKED':'Red','COMPLETED':'Amber','SUPERSEDED':'Red'}
  for state,color in colors.items():
   with self.subTest(state=state):
    row=present({**PROBLEM,'repair':{'state':state,'reason':'provider evidence absent'}},[])
    self.assertEqual(row['action']['color'],color)
    self.assertTrue(row['action']['check']);self.assertEqual(row['next'],'provider evidence absent')
  self.assertEqual(repair_capability(PROBLEM,None)['color'],'Green')
 def test_receipt_reader_is_scoped_and_does_not_write(self):
  archive=Path('C:/Users/panus/Documents/Codex/СМ-Архив')
  with tempfile.NamedTemporaryFile(dir=archive,suffix='.sqlite3',delete=False) as f:path=Path(f.name)
  self.addCleanup(lambda:path.unlink(missing_ok=True))
  with sqlite3.connect(path) as db:
   db.execute('CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT)')
   db.executemany('INSERT INTO settings VALUES (?,?)',[('cp_repair_requests:A',json.dumps({'x':{'state':'WAITING'}})),('cp_repair_requests:B','{}')])
  db.close()
  before=path.read_bytes()
  self.assertEqual(read_requests(path,'A'),{'x':{'state':'WAITING'}})
  self.assertEqual(path.read_bytes(),before)
 def test_real_button_then_background_receipt_changes_color_and_rejects_old_stock(self):
  import tkinter as tk
  from themes import THEMES
  from main_screen import theme
  root=tk.Tk();root.withdraw();self.addCleanup(root.destroy);jobs=[]
  app=SimpleNamespace(root=root,store=SimpleNamespace(setting=lambda k,d=None:d,path='not-opened'),
      watchlist=[{'isin':PROBLEM['isin'],'name':'First'},{'isin':'OTHER','name':'Other'}],
      instrument_cfg=lambda:{'isin':PROBLEM['isin']},start_feed=Mock(),dispatcher=SimpleNamespace(prioritize_feed=Mock()),
      job=lambda work,done,failed,**meta:jobs.append((work,done,failed,meta)) or True)
  theme(app,THEMES['Тёмная']);d=Dialog(app);root.update_idletasks();p=copy.deepcopy(PROBLEM);key=p['id']
  jobs[0][1](dict(isin=p['isin'],at='fixture',total=1,page=0,pages=1,rows=[present(p,[])]));root.update()
  self.assertIn('CPGreen',str(d.action_buttons[key]['style']))
  d.action_buttons[key].invoke()
  queued={'state':'QUEUED','requested_at':'2026-11-21T21:00:00+00:00','owner_priority_at':'2026-11-21T21:00:00+00:00'}
  with patch('cp_controls.request_repair',return_value=queued) as request:
   jobs[1][1](jobs[1][0]());request.assert_called_once()
  root.update();self.assertIn('CPAmber',str(d.action_buttons[key]['style']))
  self.assertIn('Приоритетная очередь',d.tree.set(key,'work'))
  d.watch();self.assertEqual(len(jobs),3)
  waiting={**queued,'state':'WAITING','finished_at':'2026-11-21T21:00:04+00:00','reason':'No confirmed candles','next_retry':'2026-11-21T21:05:00+00:00'}
  jobs[2][1]({repair_id(p):waiting});root.update()
  self.assertIn('CPRed',str(d.action_buttons[key]['style']))
  self.assertEqual(d.action_buttons[key]['text'],'Ждёт источник')
  self.assertIn('22:05:00',d.tree.set(key,'work'));self.assertIn('No confirmed candles',d.detail.get('1.0','end'))
  d.apply_receipts({repair_id(p):queued});self.assertEqual(d.shown[key]['action']['color'],'Red')
  d.receipts[repair_id(p)]=queued
  d.apply_receipts({repair_id(p):queued});self.assertEqual(d.shown[key]['action']['color'],'Red')
  d.watch();late=jobs[-1][1];d.choice.current(1);d.select();late({repair_id(p):waiting})
  self.assertFalse(d.shown);self.assertFalse(d.receipts)
  d.close();late({repair_id(p):waiting})

if __name__=='__main__':unittest.main()

