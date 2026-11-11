import sys,unittest,queue,tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
from snapshot_worker import diagnostic_detail
from bridge import Bridge
from features import Features

class DiagnosticsTests(unittest.TestCase):
    def test_blocked_tf_links_evidence_not_invented_provider_failure(self):
        stock={'isin':'test','count':50,'data_quality':{'state':'SOURCE_GAP','ranges':[['a','b']]},
               'trading_engine':{'signal':'WAIT','blockers':['1H gap'], 'timeframe_context':{'1H':{'data_quality':{'state':'GAP'},'closed_bar_count':49}}}}
        report=diagnostic_detail(stock)
        self.assertEqual(report['upstream_root_cause'],'UNCONFIRMED')
        self.assertIn('REQUIRED_TF_BLOCKED',[c['code'] for c in report['causes']])
        self.assertEqual(report['causes'][1]['evidence']['ranges'],[['a','b']])
    def test_no_new_signal_or_mutation(self):
        stock={'candles':[{'close':7,'ema10':8}],'trading_engine':{'signal':'WAIT','entry_blockers':['volume']}}
        report=diagnostic_detail(stock)
        self.assertEqual(report['signal']['signal'],'WAIT')
        self.assertEqual(report['causes'][0]['code'],'ENTRY_NOT_CONFIRMED')
        self.assertIn('rsi',report['indicators']['missing'])
        self.assertNotIn('diagnostic_detail',stock)
    def test_missing_engine_does_not_use_legacy_buy(self):
        report=diagnostic_detail({'signal':'BUY','decision':'BUY'})
        self.assertIsNone(report['signal']['signal'])
    def test_pipeline_available_without_full_snapshot(self):
        with tempfile.TemporaryDirectory() as d:
            b=Bridge(Path(d),queue.Queue(),lambda a,x:None)
            try:
                b.diagnostic_event('RUNNING',instrument_count=25)
                b.diagnostic_event('FAILED',cause='SQLITE_COMMIT_DURING_ENGINE')
                h=b.identity()['diagnostic_pipeline']
                self.assertEqual((h['attempts'],h['failed']),(1,1))
                self.assertFalse(b.state['ready'])
                h['status']='tampered';self.assertEqual(b.identity()['diagnostic_pipeline']['status'],'FAILED')
                b.diagnostic_event('RUNNING',cause=None)
                self.assertEqual(b.identity()['diagnostic_pipeline']['last_outcome']['cause'],'SQLITE_COMMIT_DURING_ENGINE')
            finally:b.close();b.server.server_close()
    def test_worker_exception_is_classified_without_secret_text(self):
        callbacks=[];events=[]
        ui=SimpleNamespace(closing=False,snapshot_busy=False,root=SimpleNamespace(after=lambda *a:1),
            tunnel_runtime=SimpleNamespace(snapshot=lambda:{}),bridge=SimpleNamespace(last_client=0,diagnostic_event=lambda s,**e:events.append((s,e))),
            store=SimpleNamespace(read_cache=SimpleNamespace(version=lambda:3)),_write_log=Mock())
        ui.job=lambda work,done,failed:callbacks.append(failed)
        ui.publish_bridge=Mock()
        with patch('snapshot_worker.inputs',return_value={'watchlist':[],'data_generation':2}):Features.publish_bridge(ui)
        callbacks[0]('Stale Trading Engine data generation secret-value')
        self.assertEqual(events[-1][1]['cause'],'SQLITE_COMMIT_DURING_ENGINE')
        self.assertNotIn('secret-value',str(events))

if __name__=='__main__':unittest.main()
