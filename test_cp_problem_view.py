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
    def test_live_official_empty_receipt_resolves_stale_dq_and_changed_inputs_reopen(self):
        import pandas as pd
        import cp_reconciliation as rv
        import revision_watcher as rw
        from test_cp_tv_legacy_reference import FlatStore
        f=tempfile.NamedTemporaryFile(prefix='CP native view isolated ',suffix='.sqlite3',dir='C:/Users/panus/Documents/Codex/СМ-Архив',delete=False)
        path=Path(f.name);f.close();store=FlatStore(path)
        def cleanup():
            store.read_cache.close()
            for suffix in ('','-wal','-shm'):Path(str(path)+suffix).unlink(missing_ok=True)
        self.addCleanup(cleanup)
        isin='US69608A1088';info=dict(isin=isin,name='Fixture',kind='stock',history_venue_lock='GETTEX')
        meta={**META,'symbol':'PTXG.GTX'};official={**meta,'source':'gettex','symbol':isin}
        store.save_instrument(info);first='2026-11-13 21:15:00';now=pd.Timestamp('2026-11-14T10:00Z')
        proof=dict(isin=isin,identity=official,basis='complete_published_trade_files',checked_at='2026-11-14T09:00Z',
                   no_trades=[],trade_intervals=[],zero_quantity_intervals=[],reference={'GETTEX:20261113':{}})
        rows=[('2026-11-13 21:00:00',100.,100.,100.,100.,2.),('2026-11-13 21:30:00',101.,101.,101.,101.,3.)]
        for at in pd.date_range('2026-11-13 21:00','2026-11-13 21:45',freq='5min',inclusive='left'):
            field='trade_intervals' if str(at) in {r[0] for r in rows} else 'no_trades'
            proof[field].append([str(at),str(at+pd.Timedelta(minutes=5))])
        for tm in ('20.15','20.30','20.45'):
            for board in ('munc','mund'):proof['reference']['GETTEX:20261113']['posttrade.20261113.'+tm+'.'+board+'.csv.gz']='a'*64
        for base in ('5m','15m'):
            store.set_setting('gettex_chart:'+isin+':'+base,dict(state='SAVED',identity=meta,
                listing_proof={**meta, 'isin':isin,'status':'CONFIRMED','verified_at':'2026-11-13T12:00Z'}))
        policy=dict(version=rw.RETIME_VERSION,approved=True)
        store.set_setting(rw.RETIME_POLICY,policy);store.set_setting('interval_evidence:'+isin,proof)
        key='data_quality:'+isin+':'+meta['symbol']+':15m'
        report=dict(identity=meta,state='UNCONFIRMED',base_tf='15m',ranges=[[first,first]])
        store.set_setting(key,report)
        with store.connect() as db:
            for base in ('5m','15m'):
                db.executemany('INSERT INTO candles VALUES (?,?,?,?,?,?,?,?,?)',[(isin,'gettex',base,*r) for r in rows])
                db.executemany('INSERT INTO series VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',[(isin,*[meta[k] for k in rw.IDENTITY[:4]],base,*r) for r in rows])
            db.executemany('INSERT INTO trades VALUES (?,?,?,?)',[(isin,str(pd.Timestamp(r[0])+pd.Timedelta(minutes=1)),r[1],r[5]) for r in rows])
        self.assertEqual(read(path,isin)['active_count'],1)
        with patch('gettex_history.attach_known_interruptions',return_value=False):rv.turn(store,info,now)
        with store.connect() as db:
            before={t:db.execute('SELECT * FROM '+t+' ORDER BY rowid').fetchall() for t in ('candles','series','settings')}
        active=read(path,isin);resolved=read(path,isin,native_only=True)
        self.assertEqual((active['active_count'],active['native_count'],active['accepted_count']),(0,1,0))
        row=resolved['rows'][0];self.assertEqual(row['title'],'Отсутствие сделок подтверждено GETTEX')
        self.assertEqual(row['problem']['state'],'UNCONFIRMED');self.assertEqual(row['tv'],'Не проверено')
        self.assertFalse(row['action']['check']);self.assertIsNone(row['next_retry'])
        self.assertEqual(store.setting(key),report);self.assertIsNone(store.setting('cp_manual_risk:'+isin))
        with store.connect() as db:
            self.assertEqual(before,{t:db.execute('SELECT * FROM '+t+' ORDER BY rowid').fetchall() for t in before})
        store.set_setting(rw.RETIME_POLICY,{**policy,'approved':False})
        self.assertEqual((read(path,isin)['active_count'],read(path,isin,native_only=True)['total']),(1,0))
        store.set_setting(rw.RETIME_POLICY,policy)
        store.set_setting(key,{**report,'state':'INVALID'})
        self.assertEqual((read(path,isin)['active_count'],read(path,isin)['native_count']),(1,0))
        store.set_setting(key,report)
        with store.connect() as db:
            db.execute('INSERT INTO series VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',(isin,*[meta[k] for k in rw.IDENTITY[:4]],'5m',first,100.,100.,100.,100.,1.))
        self.assertEqual((read(path,isin)['active_count'],read(path,isin)['native_count']),(1,0))

    def test_accepted_tv_reviews_leave_active_errors_but_remain_readable_and_reopen(self):
        f=tempfile.NamedTemporaryFile(prefix='CP TV accepted isolated ',suffix='.sqlite3',dir='C:/Users/panus/Documents/Codex/СМ-Архив',delete=False)
        path=Path(f.name);f.close();self.addCleanup(lambda:path.unlink(missing_ok=True))
        with closing(sqlite3.connect(path)) as db:
            db.execute('create table settings(key primary key,value)');db.commit()
        problem={**copy.deepcopy(PROBLEM),'accepted':True,'accepted_by':'candlepilot_owner_tv_policy',
                 'acceptance_evidence':dict(status='OWNER_ACCEPTED_TV_ABSENCE',price_model='actual_closed_bars_only_v1',
                    identity=copy.deepcopy(META),timeframe='15m',range=PROBLEM['range'][:],job_ids=['review'],native_binding='current')}
        before=path.read_bytes();original=copy.deepcopy(problem)
        invalid={**PROBLEM,'id':'invalid','state':'INVALID'}
        with patch('cp_controls.view',return_value=[problem,invalid]):
            active=read(path,'A');accepted=read(path,'A',accepted_only=True)
        self.assertEqual((active['total'],active['active_count'],active['accepted_count']),(1,1,1))
        self.assertEqual(active['rows'][0]['problem']['state'],'INVALID')
        self.assertEqual(accepted['total'],1);self.assertEqual(accepted['rows'][0]['problem'],original)
        with patch('cp_controls.view',return_value=[{**problem,'accepted':False},invalid]):
            active=read(path,'A');accepted=read(path,'A',accepted_only=True)
        self.assertEqual((active['total'],accepted['total']),(2,0))
        self.assertEqual(path.read_bytes(),before);self.assertEqual(problem,original)

    def test_current_tv_acceptance_supersedes_waiting_receipt_without_changing_dq(self):
        problem={**copy.deepcopy(PROBLEM),'accepted':True,'accepted_by':'candlepilot_owner_tv_policy',
                 'repair':dict(state='BLOCKED',reason='old missing source',next_retry='later'),
                 'acceptance_evidence':dict(status='OWNER_ACCEPTED_TV_ABSENCE',
                    price_model='actual_closed_bars_only_v1',identity=copy.deepcopy(META),
                    timeframe=PROBLEM['tf'],range=PROBLEM['range'][:],job_ids=['review'],native_binding='current')}
        before=copy.deepcopy(problem);row=present(problem,[])
        self.assertEqual(row['action']['label'],'Принято по TV')
        self.assertEqual(row['action']['color'],'Green');self.assertFalse(row['action']['check'])
        self.assertEqual(row['title'],'Пропуск сверён с TV и принят')
        self.assertIsNone(row['next_retry']);self.assertEqual(problem,before)
        self.assertEqual(row['problem']['state'],'UNCONFIRMED')
        for change in [dict(accepted=False),dict(accepted_by='manual'),dict(state='INVALID'),
                       dict(identity={**META,'source':'gettex'}),dict(tf='5m'),
                       dict(range=['2026-11-12 08:30','2026-11-12 08:30']),
                       dict(acceptance_evidence={**problem['acceptance_evidence'],'native_binding':None})]:
            with self.subTest(change=change):
                candidate={**problem,**change};negative=present(candidate,[])
                self.assertNotEqual(negative['action']['label'],'Принято по TV')
                self.assertEqual(negative['title'],{'INVALID':'Некорректные цены или объёмы'}.get(candidate['state'],'Интервал пока не подтверждён'))

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
        self.assertEqual(row['next'],'Нет сохранённого задания восстановления')
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
            policy=dict(version=1,approved=True,scope='registered_stocks',exclusive_venue='GETTEX')
            db.execute('insert into settings values (?,?)',('cp_gettex_source_policy',json.dumps(policy)))
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
        other={**PROBLEM,'identity':{**META,'source':'proxy','venue':'GER'}}
        with patch('cp_controls.view',return_value=[old,PROBLEM,other]):
            fresh=read(path,'A',now='2026-11-21T00:00:00Z')
            full=read(path,'A',include_old=True,now='2026-11-21T00:00:00Z')
        self.assertEqual((fresh['total'],fresh['old_archive'],fresh['archived_count'],full['total']),(1,1,2,3))
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
