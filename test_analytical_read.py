import unittest,time,copy
from concurrent.futures import Future
from unittest.mock import patch
from test_input_revisions import InputRevisionTests,Client
from analytical_read import capture,calculate
from bridge_queue import CommandQueue
import pandas as pd
import mcp_server

class ReadTests(unittest.TestCase):
    setUp=InputRevisionTests.setUp
    tearDown=InputRevisionTests.tearDown
    def values(self,**extra):return capture(self.ui,{'isin':self.info['isin'],'timeframe':'4H',**extra})
    def frame(self):
        f=pd.DataFrame({'open':10.,'high':12.,'low':9.,'close':11.,'volume':100.},index=pd.date_range('2026-09-01',periods=40,freq='4h'))
        f.attrs={'source':'proxy','symbol':'APC.DE','venue':'GER','currency':'EUR','timezone':'Europe/Berlin','timeframe':'4H','data_quality':{'state':'GAP'}}
        return f
    def test_pages_values_identity_and_no_ui_selection(self):
        before=self.ui.symbol.get()
        with patch('snapshot_worker.SnapshotContext.frame_for',return_value=self.frame()):
            a=calculate(self.store,self.values(limit=3))
            b=calculate(self.store,self.values(limit=3,offset=3,expected_revision=a['revision']))
        self.assertEqual(len(a['bars']),3);self.assertNotEqual(a['bars'][-1]['open_at'],b['bars'][0]['open_at'])
        self.assertEqual(a['bars'][0]['close'],11);self.assertEqual(a['identity']['currency'],'EUR')
        self.assertEqual(a['data_quality']['state'],'GAP');self.assertEqual(self.ui.symbol.get(),before)
        self.assertIn('EMA',a['chart_indicators']);self.assertIsNone(a['bars'][0]['ema30'])
    def test_wrong_listing_and_stale_page_rejected(self):
        with patch('snapshot_worker.SnapshotContext.frame_for',return_value=self.frame()):
            for args,code in [({'currency':'USD'},'IDENTITY_MISMATCH'),({'offset':3,'expected_revision':'old'},'STALE_PAGE')]:
                with self.assertRaisesRegex(ValueError,code):calculate(self.store,self.values(**args))
    def test_range_requires_timezone(self):
        with patch('snapshot_worker.SnapshotContext.frame_for',return_value=self.frame()):
            with self.assertRaisesRegex(ValueError,'RANGE_REQUIRES_TIMEZONE'):calculate(self.store,self.values(**{'from':'2026-09-01'}))
    def test_worker_read_only_no_history(self):
        client=Client(self.store.path)
        try:
            result=client.calculate('read_analytics',self.values(),0)
            self.assertEqual(result['status'],'NO_DATA');self.assertIn('_input_revisions',result)
        finally:
            client.close()
            if client.process:client.process.wait(timeout=10);client.process.stdin.close();client.process.stdout.close()
    def test_schema_retains_original_twelve_and_read_annotation(self):
        self.assertEqual(len(mcp_server.TOOLS),13)
        t=next(t for t in mcp_server.TOOLS if t['name']=='stock_monitor_read_analytics')
        self.assertTrue(t['annotations']['readOnlyHint'])
        self.assertIn('instance_id',t['inputSchema']['required'])
        with self.assertRaises(ValueError):mcp_server.validate_arguments('read_analytics',{'isin':'X','timeframe':'4H','limit':501})

class QueueReadTests(unittest.TestCase):
    def queue(self,future):
        class Events:
            def put(self,event):event[0](*event[1])
        return CommandQueue('test',Events(),lambda *a:future,lambda:False)
    def envelope(self):return {'instance_id':'test','command_id':'r','deadline':time.time()+30}
    def test_read_runs_without_write_permission_and_receipt_completes(self):
        f=Future();q=self.queue(f);r=q.submit('read_analytics',{},self.envelope(),wait=0)
        self.assertEqual(r['status'],'INDETERMINATE');f.set_result({'bars':[]})
        self.assertEqual(q.status('r')['status'],'COMPLETED')
    def test_expired_read_discards_late_result(self):
        f=Future();q=self.queue(f);q.submit('read_analytics',{},self.envelope(),wait=0)
        q.records['r']['expires']=0;f.set_result({'bars':[]})
        self.assertEqual(q.status('r')['status'],'EXPIRED');self.assertNotIn('result',q.status('r'))
    def test_writes_still_denied_and_wrong_instance_rejected(self):
        q=self.queue(Future());self.assertEqual(q.submit('select',{},self.envelope(),wait=0)['status'],'FAILED')
        with self.assertRaisesRegex(ValueError,'Wrong instance'):q.submit('read_analytics',{},dict(self.envelope(),instance_id='other'),wait=0)

if __name__=='__main__':unittest.main()
