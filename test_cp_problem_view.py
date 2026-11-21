import unittest,sys,json,sqlite3,tempfile,copy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch
from contextlib import closing
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
from cp_problem_view import tv_result,present,read,Dialog

META=dict(source='gettex_chart',venue='GETTEX',currency='EUR',symbol='ABC',timezone='Europe/Berlin')
PROBLEM=dict(isin='A',tf='15m',identity=META,range=['2026-11-12 08:15','2026-11-12 08:15'],state='UNCONFIRMED',kind='archive',id='fixture',reasons=['fixture'])


class ProblemEvidenceTests(unittest.TestCase):
    def job(self):
        return dict(isin='A',tf='15m',identity=META,first='2026-11-12 08:15',last='2026-11-12 08:15',
                    input_fingerprint='same',checked_at='2026-11-14',observation={'identity_confirmed':True,'input_fingerprint':'same','result':'ABSENT'},
                    source_comparison={'observation_current':True,'closed':True,'presence_match':True,'ohlcv_match':None})

    def test_tv_presence_never_becomes_ohlcv_match_or_no_trades(self):
        job=self.job();self.assertEqual(tv_result(PROBLEM,[job])[0],'Ранее совпало наличие свечей')
        self.assertNotIn('OHLCV',tv_result(PROBLEM,[job])[0])
        job['source_comparison']['ohlcv_match']=True;self.assertEqual(tv_result(PROBLEM,[job])[0],'Ранее совпали OHLCV')
        for key,value in [('isin','B'),('tf','1H'),('first','2026-11-12 08:00')]:
            other={**job,key:value};self.assertEqual(tv_result(PROBLEM,[other])[0],'Не проверено')
        self.assertEqual(tv_result(PROBLEM,[{**job,'identity':{**META,'venue':'XETRA'}}])[0],'Не проверено')
        job['observation']['input_fingerprint']='old';self.assertEqual(tv_result(PROBLEM,[job])[0],'Нужна повторная сверка')
        job['status']='RECHECK_QUEUED';self.assertEqual(tv_result(PROBLEM,[job])[0],'Ожидает повторной проверки')

    def test_severity_and_acceptance_are_display_only(self):
        before=copy.deepcopy(PROBLEM);row=present(PROBLEM,[])
        self.assertEqual(row['influence'],'Диагностика архива');self.assertEqual(PROBLEM,before)
        self.assertEqual(present({**PROBLEM,'state':'INVALID'},[])['severity'],'Высокая')
        accepted=present({**PROBLEM,'accepted':True},[]);self.assertEqual(accepted['severity'],'Низкая')
        self.assertEqual(accepted['tv'],'Не проверено')

    def test_read_is_scoped_paged_and_never_writes_or_calls_provider(self):
        f=tempfile.NamedTemporaryFile(prefix='CP view isolated ',suffix='.sqlite3',dir='C:/Users/panus/Documents/Codex/СМ-Архив',delete=False)
        path=Path(f.name);f.close();self.addCleanup(lambda:path.unlink(missing_ok=True))
        with closing(sqlite3.connect(path)) as db:
            db.execute('create table settings(key primary key,value)')
            db.execute('insert into settings values (?,?)',('cp_reconciliation_job:A:1',json.dumps(self.job())))
            db.execute('insert into settings values (?,?)',('cp_reconciliation_job:B:1',json.dumps({**self.job(),'isin':'B'})))
            db.commit()
        before=path.read_bytes()
        def problems(db,isin):
            self.assertEqual(isin,'A');self.assertEqual(db.execute('pragma query_only').fetchone()[0],1)
            return [{**PROBLEM,'id':str(i)} for i in range(205)]
        with patch('cp_controls.view',side_effect=problems):
            packet=read(path,'A',99)
        self.assertEqual(packet['total'],205);self.assertEqual(packet['page'],2);self.assertEqual(len(packet['rows']),5)
        self.assertTrue(all(r['job']['isin']=='A' for r in packet['rows']));self.assertEqual(path.read_bytes(),before)
        old={**PROBLEM,'range':['2011-01-01','2011-01-01']}
        with patch('cp_controls.view',return_value=[old,PROBLEM]):
            fresh=read(path,'A',now='2026-11-21T00:00:00Z')
            full=read(path,'A',include_old=True,now='2026-11-21T00:00:00Z')
        self.assertEqual((fresh['total'],fresh['old_archive'],full['total']),(1,1,2))
        self.assertEqual(path.read_bytes(),before)


class ProblemWindowTests(unittest.TestCase):
    def test_stock_switch_rejects_old_response_and_close_rejects_late_callback(self):
        import tkinter as tk
        from main_screen import theme
        from themes import THEMES
        root=tk.Tk();root.withdraw();self.addCleanup(root.destroy);jobs=[]
        app=SimpleNamespace(root=root,store=SimpleNamespace(setting=lambda k,d=None:d,path='not-opened'),
            watchlist=[dict(isin='A',name='First'),dict(isin='B',name='Second')],instrument_cfg=lambda:dict(isin='A'),
            job=lambda work,done,failed,**meta:jobs.append((work,done,failed,meta)) or True)
        theme(app,THEMES['Тёмная']);d=Dialog(app);root.update_idletasks()
        self.assertEqual(jobs[0][3]['lane'],'read');d.choice.current(1);d.select()
        jobs[0][1](dict(isin='A',at='old',total=1,page=0,pages=1,rows=[present(PROBLEM,[])]))
        self.assertFalse(d.tree.get_children());self.assertEqual(len(jobs),2)
        jobs[1][1](dict(isin='B',at='new',total=0,page=0,pages=1,rows=[]));self.assertEqual(d.packet['isin'],'B')
        d.close();jobs[1][1](dict(isin='B',at='late',total=1,page=0,pages=1,rows=[present(PROBLEM,[])]))
        self.assertTrue(d.closed)

if __name__=='__main__':unittest.main()
