import json
import queue
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from urllib.request import Request,build_opener,ProxyHandler
from urllib.error import HTTPError
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
from bridge_queue import CommandQueue
from bridge import Bridge
import mcp_server

class QueueTests(unittest.TestCase):
    def setUp(self):
        self.events=queue.Queue();self.calls=[]
        self.q=CommandQueue('test-instance',self.events,lambda a,x:self.calls.append((a,x)) or {'ok':True},lambda:True)
        self.e={'instance_id':'test-instance','command_id':'one','deadline':time.time()+30}
    def run_pending(self):
        f,args=self.events.get(timeout=2);f(*args)
    def test_timeout_cancels_queued_callback(self):
        self.assertEqual(self.q.submit('select',{},self.e,wait=0)['status'],'EXPIRED')
        self.run_pending();self.assertEqual(self.calls,[])
    def test_duplicate_expired_never_requeued(self):
        self.q.submit('select',{},self.e,wait=0)
        self.assertEqual(self.q.submit('select',{},self.e)['status'],'EXPIRED')
        self.assertEqual(self.events.qsize(),1)
    def test_id_reuse_different_arguments_rejected(self):
        self.q.submit('select',{},self.e,wait=0)
        with self.assertRaises(ValueError):self.q.submit('select',{'timeframe':'1D'},self.e)
    def test_wrong_instance_rejected(self):
        with self.assertRaises(ValueError):self.q.submit('select',{},dict(self.e,instance_id='store'))
        self.assertTrue(self.events.empty())
    def test_stale_and_excessive_deadline_rejected(self):
        for dt in (-1,121):
            with self.assertRaises(ValueError):self.q.submit('select',{},dict(self.e,deadline=time.time()+dt))
    def test_expiry_before_dispatch(self):
        result=[]
        t=threading.Thread(target=lambda:result.append(self.q.submit('select',{},self.e)));t.start()
        f,args=self.events.get(timeout=2)
        self.q.records['one']['expires']=time.monotonic()-1;f(*args);t.join(2)
        self.assertEqual(result[0]['status'],'EXPIRED');self.assertFalse(self.calls)
    def test_complete_once_and_readback(self):
        worker=threading.Thread(target=self.run_pending);worker.start()
        self.assertEqual(self.q.submit('select',{},self.e)['status'],'COMPLETED');worker.join()
        self.assertEqual(self.q.submit('select',{},self.e)['status'],'COMPLETED')
        self.assertEqual(len(self.calls),1)
    def test_running_timeout_is_explicit(self):
        entered=threading.Event();release=threading.Event()
        self.q.dispatch=lambda a,x:(entered.set(),release.wait(2),{'ok':True})[-1]
        worker=threading.Thread(target=self.run_pending);worker.start()
        result=self.q.submit('select',{},self.e,wait=.1)
        self.assertTrue(entered.is_set());self.assertEqual(result['status'],'INDETERMINATE')
        release.set();worker.join();self.assertEqual(self.q.status('one')['status'],'COMPLETED')
    def test_permission_checked_at_execution(self):
        self.q.writable=lambda:False
        worker=threading.Thread(target=self.run_pending);worker.start()
        self.assertEqual(self.q.submit('select',{},self.e)['status'],'FAILED');worker.join()
        self.assertFalse(self.calls)
    def test_close_invalidates_queue(self):
        self.q.submit('select',{},self.e,wait=0);self.q.close();self.run_pending()
        with self.assertRaises(ValueError):self.q.submit('select',{},dict(self.e,command_id='two'))
        self.assertFalse(self.calls)
    def test_receipt_capacity_does_not_forget_duplicates(self):
        self.q.limit=1;self.q.submit('select',{},self.e,wait=0)
        with self.assertRaises(ValueError):self.q.submit('select',{},dict(self.e,command_id='two'))
    def test_audit_excludes_arguments_and_raw_id(self):
        events=[];self.q.audit=events.append
        self.q.submit('select',{'isin':'private-test-value'},self.e,wait=0)
        self.assertEqual([e['status'] for e in events],['QUEUED','EXPIRED'])
        self.assertNotIn('private-test-value',json.dumps(events));self.assertNotIn('command_id',events[0])

class TransportTests(unittest.TestCase):
    def test_live_authenticated_health_snapshot_and_wrong_token(self):
        with tempfile.TemporaryDirectory() as d:
            b=Bridge(Path(d),queue.Queue(),lambda a,x:None)
            try:
                cfg=json.loads((Path(d)/'bridge.json').read_text())
                def call(action,token):
                    req=Request(cfg['url'],data=json.dumps({'action':action,'arguments':{}}).encode(),headers={'Authorization':'Bearer '+token})
                    with build_opener(ProxyHandler({})).open(req,timeout=2) as r:return json.load(r)
                h=call('health',b.token)
                self.assertEqual(h['instance_id'],cfg['instance_id']);self.assertEqual(h['scope'],'test')
                self.assertEqual(h['version'],'0.16-dev');self.assertNotIn(b.token,json.dumps(h))
                b.publish({'ready':True,'version':'0.16-dev'});s=call('snapshot',b.token)
                self.assertTrue(s['ready']);self.assertIsNotNone(s['runtime']['snapshot_published_at'])
                with self.assertRaises(HTTPError) as e:call('health','wrong')
                self.assertEqual(e.exception.code,401)
            finally:b.close();b.server.server_close()
    def test_adapter_version(self):
        self.assertEqual(mcp_server.handle({'method':'initialize'})['serverInfo']['version'],'0.16-dev')
    def test_write_schema_requires_binding(self):
        schema=next(t['inputSchema'] for t in mcp_server.TOOLS if t['name']=='stock_monitor_select')
        with self.assertRaises(ValueError):mcp_server.validate_schema({'timeframe':'1D'},schema)
        mcp_server.validate_schema({'timeframe':'1D','instance_id':'test','request_id':'one','deadline':time.time()+20},schema)
    def test_business_schema_rejects_extra_and_wrong_types(self):
        for args in ({'shell':'bad'},{'timeframe':'7H'},{'timeframe':True}):
            with self.assertRaises(ValueError):mcp_server.validate_arguments('select',args)

if __name__=='__main__':unittest.main()
