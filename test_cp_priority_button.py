import sys,unittest,copy
from pathlib import Path
from unittest.mock import patch,Mock
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
import cp_controls as cp
from cp_problem_view import repair_capability,present,priority_request
import test_cp_automatic_repair as fixture
import test_cp_problem_view as view_fixture
import pandas as pd


class PriorityButtonTests(unittest.TestCase):
    def test_colors_follow_automatic_route_not_prior_success(self):
        p=view_fixture.PROBLEM
        self.assertTrue(repair_capability(p,None)['known'])
        self.assertEqual(repair_capability(p,None)['label'],'Автоисправление')
        job={'input_fingerprint':'fp','finding':{'state':'NATIVE_ROWS_CONFIRMED'}}
        self.assertTrue(repair_capability(p,job)['known'])
        self.assertTrue(repair_capability(p,{**job,'status':'RECHECK_QUEUED'})['known'])
        self.assertTrue(repair_capability(p,{**job,'finding':{'state':'SOURCE_DISCREPANCY'}})['known'])
        self.assertFalse(repair_capability({**p,'tf':'1W'},job)['check'])
        with patch('cp_controls.request_repair') as request:
            priority_request(Mock(),p);self.assertTrue(request.call_args.kwargs['priority'])
            with self.assertRaises(ValueError):priority_request(Mock(),{**p,'identity':{**p['identity'],'venue':'XETRA'}})
            self.assertEqual(request.call_count,1)

    def test_priority_preserves_cooldown_attempts_origin_and_deduplication(self):
        store=fixture.Memory();self.addCleanup(store.db.close)
        isin='A';p={**view_fixture.PROBLEM,'id':'p'}
        normal=dict(problem={**p,'id':'old','range':['2026-11-01','2026-11-01']},state='QUEUED',requested_at='2026-11-01T00:00Z')
        target=dict(problem=p,state='WAITING',requested_at='2026-11-02T00:00Z',attempts=2,next_retry='2026-11-15T12:00Z',origin={'kind':'AUTOMATIC_DEPENDENCY'})
        store.set_setting('cp_repair_requests:A',{cp.repair_id(normal['problem']):normal,cp.repair_id(p):target})
        with patch('cp_controls.problems',return_value=[p]),patch('cp_controls.stamp',return_value='2026-11-15T11:00Z'):
            result=cp.request_repair(store,p,priority=True)
        self.assertEqual((result['attempts'],result['next_retry'],result['origin']),(2,'2026-11-15T12:00Z',target['origin']))
        self.assertEqual(len(store.setting('cp_repair_requests:A')),2)
        self.assertEqual(cp.due_request(store.db,now=pd.Timestamp('2026-11-15T11:30Z'))['problem']['id'],'old')
        self.assertEqual(cp.due_request(store.db,now=pd.Timestamp('2026-11-15T12:01Z'),include_automatic=False)['problem']['id'],'p')
        with patch('cp_controls.problems',return_value=[]),self.assertRaises(ValueError):cp.request_repair(store,p,priority=True)

    def test_normal_rotation_is_unchanged_and_latest_explicit_click_goes_first(self):
        store=fixture.Memory();self.addCleanup(store.db.close)
        def item(n,created,priority=None):
            row=dict(problem={**view_fixture.PROBLEM,'id':n},state='QUEUED',requested_at=created)
            if priority:row['owner_priority_at']=priority
            return row
        values={'a':item('a','2026-11-01T00:00Z'),'b':item('b','2026-11-02T00:00Z')}
        store.set_setting('cp_repair_requests:A',values)
        self.assertEqual(cp.due_request(store.db,now=pd.Timestamp('2026-11-20T00:00Z'))['problem']['id'],'a')
        values['a']['owner_priority_at']='2026-11-19T00:00Z';values['b']['owner_priority_at']='2026-11-19T01:00Z'
        store.set_setting('cp_repair_requests:A',values)
        self.assertEqual(cp.due_request(store.db,now=pd.Timestamp('2026-11-20T00:00Z'))['problem']['id'],'b')
        for count in (53,200):
            rows={str(n):item(str(n),'2026-11-01T00:00Z') for n in range(count)}
            rows[str(count-1)]['owner_priority_at']='2026-11-19T02:00Z'
            store.set_setting('cp_repair_requests:A',rows)
            self.assertEqual(cp.due_request(store.db,now=pd.Timestamp('2026-11-20T00:00Z'))['problem']['id'],str(count-1))


class PriorityWindowTests(unittest.TestCase):
    def test_row_button_enqueues_once_and_does_not_submit_a_trade(self):
        import tkinter as tk
        from types import SimpleNamespace
        from themes import THEMES
        from main_screen import theme
        from cp_problem_view import Dialog
        root=tk.Tk();root.withdraw();self.addCleanup(root.destroy);jobs=[]
        app=SimpleNamespace(root=root,store=SimpleNamespace(setting=lambda k,d=None:d,path='not-opened'),
            watchlist=[dict(isin='A',name='First')],instrument_cfg=lambda:dict(isin='A'),
            start_feed=Mock(),dispatcher=SimpleNamespace(prioritize_feed=Mock()),
            job=lambda work,done,failed,**meta:jobs.append((work,done,failed,meta)) or True)
        theme(app,THEMES['Тёмная']);d=Dialog(app);root.update_idletasks()
        p=copy.deepcopy(view_fixture.PROBLEM);r=present(p,[])
        red=present({**p,'id':'red','tf':'1W'},[])
        green_problem={**p,'id':'green'}
        job={**view_fixture.ProblemEvidenceTests().job(),'finding':{'state':'NATIVE_ROWS_CONFIRMED'}}
        green=present(green_problem,[job])
        jobs[0][1](dict(isin='A',at='fixture',total=3,page=0,pages=1,rows=[r,green,red]));root.update()
        self.assertIn('.CPGreen.TButton',str(d.action_buttons['green']['style']))
        self.assertIn('.CPRed.TButton',str(d.action_buttons['red']['style']))
        # Native tree scrolling must keep an actionable overlay on each visible row.
        rows=[present({**p,'id':f'row-{n}'},[]) for n in range(40)]
        d.shown={r['problem']['id']:r for r in rows};d.tree.delete(*d.tree.get_children())
        for key in d.shown:d.tree.insert('','end',iid=key,values=['15m','','range','fixture'])
        d.schedule_buttons();root.update();d.tree.yview_scroll(5,'units');root.update()
        visible={k for k in d.shown if d.tree.bbox(k,'action')}
        self.assertTrue(visible)
        self.assertTrue(all(d.action_buttons[k].winfo_ismapped() for k in visible))
        self.assertTrue(all(d.action_buttons[k].winfo_y()==d.tree.bbox(k,'action')[1]+2 for k in visible))
        d.shown={p['id']:r,'green':green};d.tree.delete(*d.tree.get_children())
        for key in d.shown:d.tree.insert('','end',iid=key,values=['15m','','range','fixture'])
        d.act(p['id']);d.act(p['id']);self.assertEqual(len(jobs),2)
        with patch('cp_controls.request_repair',return_value={'state':'QUEUED'}) as request:
            jobs[1][1](jobs[1][0]());request.assert_called_once();self.assertTrue(request.call_args.kwargs['priority'])
        app.start_feed.assert_called_once_with(app.watchlist[0]);app.dispatcher.prioritize_feed.assert_called_once_with('feed:A')
        self.assertEqual(len(jobs),2) # receipt applied locally; watcher is timer-driven
        d.close()

if __name__=='__main__':unittest.main()

