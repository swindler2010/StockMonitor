import json
import queue
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import Future
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
import indicator_controls as controls
from bridge_queue import CommandQueue
import mcp_server


class IndicatorControlsTests(unittest.TestCase):
    def setUp(self):
        archive=Path('C:/Users/panus/Documents/Codex/СМ-Архив')
        f=tempfile.NamedTemporaryFile(prefix='Indicators47 test ',suffix='.sqlite3',dir=archive if archive.exists() else None,delete=False)
        self.path=Path(f.name);f.close()
        with closing(sqlite3.connect(self.path)) as db:
            db.executescript("""CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT);
                CREATE TABLE input_revisions(scope TEXT PRIMARY KEY,revision INTEGER);
                CREATE TRIGGER rev AFTER INSERT ON settings BEGIN
                INSERT INTO input_revisions VALUES ('setting:'||NEW.key,1) ON CONFLICT(scope) DO UPDATE SET revision=revision+1; END;""")
            db.execute('INSERT INTO settings VALUES (?,?)',(controls.KEY,json.dumps(controls.DEFAULT_IND)))
            db.execute('INSERT INTO settings VALUES (?,?)',('unrelated','"preserve me"'));db.commit()
        class Store:
            def __init__(s,path):s.path=path;s.pending_user_settings={}
            def setting(s,key,default=None):
                if key=='mcp_allow_edits':return True
                if key in s.pending_user_settings:return s.pending_user_settings[key]
                with closing(sqlite3.connect(s.path)) as db:row=db.execute('SELECT value FROM settings WHERE key=?',(key,)).fetchone()
                return json.loads(row[0]) if row else default
        self.app=SimpleNamespace(store=Store(self.path),closing=False)

    def tearDown(self):self.path.unlink(missing_ok=True)

    def state(self):return controls.snapshot(self.app)

    def test_complete_catalog_readonly_draft_and_dependency(self):
        before=self.path.read_bytes();state=self.state()
        self.assertEqual(len(state['indicators']),12);self.assertTrue(state['saved'])
        self.app.store.pending_user_settings[controls.KEY]={**controls.DEFAULT_IND,'Средний объём':True,'Объём':False}
        draft=self.state();mean=next(i for i in draft['indicators'] if i['name']=='Средний объём')
        self.assertTrue(mean['enabled']);self.assertFalse(mean['effective_enabled'])
        self.assertFalse(draft['saved']);self.assertNotEqual(state['revision'],draft['revision'])
        self.assertEqual(self.path.read_bytes(),before)

    def test_atomic_update_persistence_and_unrelated_settings(self):
        before=self.state();result=controls.persist(self.path,before['revision'],{'RSI':True,'rsi':21},lambda:True)
        self.assertTrue(result['saved']);self.assertEqual(self.state()['settings']['rsi'],21)
        self.assertEqual(self.app.store.setting('unrelated'),'preserve me')
        self.assertTrue(self.state()['settings']['EMA']);self.assertEqual(result['revision'],self.state()['revision'])
        with self.assertRaisesRegex(ValueError,'INDICATORS_CHANGED'):
            controls.persist(self.path,before['revision'],{'RSI':False},lambda:True)

    def test_noop_does_not_bump_revision(self):
        before=self.state();result=controls.persist(self.path,before['revision'],{'RSI':False},lambda:True)
        self.assertEqual(result['revision'],before['revision'])

    def test_validation_all_settings_and_no_partial_write(self):
        before=self.state()
        for patch in ({},{'risk':1},{'RSI':1},{'rsi':True},{'rsi':2.5},{'rsi':0},{'rsi':501},{'bb_dev':float('nan')},{'bb_dev':float('inf')},{'macd_fast':26}):
            with self.subTest(patch=patch),self.assertRaises(ValueError):
                controls.persist(self.path,before['revision'],patch,lambda:True)
            self.assertEqual(self.state(),before)
        all_values={k:(not v if type(v) is bool else v+1) for k,v in controls.DEFAULT_IND.items()}
        result=controls.persist(self.path,before['revision'],all_values,lambda:True)
        self.assertEqual(result['settings'],all_values)

    def test_revocation_at_final_guard_rolls_back(self):
        checks=iter([True,False]);before=self.state()
        with self.assertRaisesRegex(ValueError,'NO_LONGER_VALID'):
            controls.persist(self.path,before['revision'],{'EMA':False},lambda:next(checks))
        self.assertEqual(self.state(),before)

    def test_aba_revision_rejected_and_unknown_rows_preserved(self):
        old=self.state()
        controls.persist(self.path,old['revision'],{'RSI':True},lambda:True)
        controls.persist(self.path,self.state()['revision'],{'RSI':False},lambda:True)
        self.assertEqual(old['settings'],self.state()['settings'])
        with self.assertRaisesRegex(ValueError,'INDICATORS_CHANGED'):
            controls.persist(self.path,old['revision'],{'EMA':False},lambda:True)

    def test_local_drafts_block_remote_without_saving_them(self):
        old=self.state();self.app.store.pending_user_settings={controls.KEY:{'RSI':True},'theme':'draft'}
        result=controls.dispatch(self.app,dict(expected_revision=old['revision'],changes={'EMA':False}))
        self.assertEqual(result['code'],'INDICATORS_UNSAVED_LOCAL_CHANGES')
        self.assertEqual(self.app.store.pending_user_settings['theme'],'draft')
        self.assertTrue(self.app.store.setting(controls.KEY)['RSI'])

    def test_async_existing_queue_publishes_saved_readback_and_refresh(self):
        events=queue.Queue();seen=[];work=[]
        self.app.refresh_chart=lambda:seen.append('refresh')
        self.app.job=lambda run,done,failed,**kw:work.append((run,done)) or True
        q=CommandQueue('instance',events,lambda action,args:controls.dispatch(self.app,args),lambda:True)
        self.app.bridge=SimpleNamespace(commands=q)
        envelope=dict(instance_id='instance',command_id='change',deadline=time.time()+30)
        args=dict(expected_revision=self.state()['revision'],changes={'RSI':True})
        answers=[];thread=threading.Thread(target=lambda:answers.append(q.submit('set_indicators',args,envelope)));thread.start()
        callback,values=events.get(timeout=2);callback(*values)
        run,done=work.pop();done(run());thread.join(2)
        self.assertEqual(answers[0]['status'],'COMPLETED');self.assertTrue(answers[0]['result']['saved'])
        self.assertEqual(seen,['refresh']);self.assertTrue(self.state()['settings']['RSI'])
        self.assertEqual(q.submit('set_indicators',args,envelope),answers[0]);self.assertFalse(work)

    def test_queued_read_with_edits_disabled_and_write_rejected(self):
        events=queue.Queue();calls=[]
        q=CommandQueue('instance',events,lambda a,x:calls.append(a) or self.state(),lambda:False)
        for action,expected in [('get_indicators','COMPLETED'),('set_indicators','FAILED')]:
            results=[];envelope=dict(instance_id='instance',command_id=action,deadline=time.time()+20)
            thread=threading.Thread(target=lambda:results.append(q.submit(action,{},envelope)));thread.start()
            cb,values=events.get(timeout=2);cb(*values);thread.join(2)
            self.assertEqual(results[0]['status'],expected)
        self.assertEqual(calls,['get_indicators'])

    def test_saved_receipt_not_relabelled_expired_after_commit(self):
        events=queue.Queue();future=Future();q=CommandQueue('instance',events,lambda a,x:future,lambda:True)
        e=dict(instance_id='instance',command_id='saved',deadline=time.time()+30);answers=[]
        t=threading.Thread(target=lambda:answers.append(q.submit('set_indicators',{},e)));t.start()
        cb,values=events.get(timeout=2);cb(*values)
        q.records['saved']['expires']=time.monotonic()-1
        future.set_result(dict(saved=True,status='SAVED'));t.join(2)
        self.assertTrue(answers[0]['result']['saved']);self.assertEqual(answers[0]['status'],'COMPLETED')

    def test_mcp_schemas_and_instance_guard(self):
        tools={t['name']:t for t in mcp_server.handle({'method':'tools/list'})['tools']}
        for action in ('get_indicators','set_indicators'):
            self.assertIn('instance_id',tools['stock_monitor_'+action]['inputSchema']['required'])
        self.assertTrue(tools['stock_monitor_get_indicators']['annotations']['readOnlyHint'])
        args=dict(expected_revision=self.state()['revision'],changes={'RSI':True,'rsi':21})
        mcp_server.validate_arguments('set_indicators',args)
        with self.assertRaises(ValueError):mcp_server.validate_arguments('set_indicators',{**args,'risk':False})
        q=CommandQueue('test',queue.Queue(),lambda a,x:None,lambda:True)
        with self.assertRaisesRegex(ValueError,'Wrong instance'):
            q.submit('set_indicators',args,dict(instance_id='store',command_id='one',deadline=time.time()+30))
        with self.assertRaisesRegex(ValueError,'Expired'):
            q.submit('set_indicators',args,dict(instance_id='test',command_id='one',deadline=time.time()-1))

    def test_responsive_settings_with_53_and_200_queued_history_jobs(self):
        from performance_dispatcher import Dispatcher
        for count in (53,200):
            with self.subTest(count=count):
                delivery=queue.Queue();writer_started=threading.Event();release=threading.Event()
                dispatcher=Dispatcher(lambda f,x:delivery.put((f,x)),workers=2,sample=lambda _:({'cpu':95,'ram':10},None))
                try:
                    dispatcher.submit(lambda:(writer_started.set(),release.wait(5)),lambda _:None,lambda _:None,lane='feed_update',key='writer')
                    self.assertTrue(writer_started.wait(2))
                    for n in range(count):
                        dispatcher.submit(lambda:None,lambda _:None,lambda _:None,lane='feed_update',key=('history',n))
                    commands=SimpleNamespace(proposal_valid=lambda cid:True)
                    self.app.bridge=SimpleNamespace(commands=commands);self.app.job=dispatcher.submit
                    self.app.refresh_chart=lambda:None
                    future=controls.dispatch(self.app,dict(_command_id='responsive',expected_revision=self.state()['revision'],changes={'RSI':True}))
                    callback,result=delivery.get(timeout=2);callback(result)
                    self.assertTrue(future.result(timeout=1)['saved'])
                    self.assertFalse(release.is_set())
                finally:
                    dispatcher.close();release.set()
                    for t in dispatcher.threads:t.join(2)
                    dispatcher.monitor.join(2)


if __name__=='__main__':unittest.main()
