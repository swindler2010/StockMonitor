"""Publication telemetry regression; isolated SQLite, no network."""
import unittest,copy,time
from unittest.mock import patch
from test_input_revisions import InputRevisionTests,inputs,Features,Client

class FeedTelemetryTests(unittest.TestCase):
    setUp=InputRevisionTests.setUp
    tearDown=InputRevisionTests.tearDown
    def state(self):
        with self.store.read_cache.track_inputs() as token:
            self.store.load_series(self.info['isin'],'1D','proxy','APC.DE')
            self.store.setting('data_quality:X',{})
            self.store.setting('history_identity:X',{})
        return {'stocks':[{'isin':self.info['isin'],'name':'Apple','candles':[],
                          'trading_engine':{'signal':'WAIT','blockers':['DQ blocked']}}],
                '_input_revisions':token,'_calculation_timing':{'finished_at':time.time()}}
    def test_feed_changes_publish_current_diagnostic_without_engine_call(self):
        values=inputs(self.ui);state=self.state();engine=copy.deepcopy(state['stocks'][0]['trading_engine'])
        self.ui.feed_errors[self.info['isin']]='Source unavailable'
        with patch('analytics.analyse_multiframe',side_effect=AssertionError('No UI calculation')):
            self.assertTrue(Features.accept_background_snapshot(self.ui,state,values))
        self.assertEqual(state['stocks'][0]['error'],'Source unavailable')
        self.assertEqual(state['stocks'][0]['trading_engine'],engine)
        self.assertIn('FEED_REPORTED_ERROR',[c['code'] for c in state['diagnostic_detail'][0]['causes']])
        self.ui.feed_errors.clear()
        self.assertTrue(Features.accept_background_snapshot(self.ui,state,values))
        self.assertIsNone(state['stocks'][0]['error'])
        self.assertNotIn('FEED_REPORTED_ERROR',[c['code'] for c in state['diagnostic_detail'][0]['causes']])
    def test_dq_and_identity_change_still_blocks(self):
        for key in ('data_quality:X','history_identity:X'):
            values=inputs(self.ui);state=self.state()
            self.store.set_setting(key,{'state':'INVALID','currency':'USD'})
            self.assertFalse(Features.accept_background_snapshot(self.ui,state,values))
    def test_ohlcv_change_still_blocks(self):
        values=inputs(self.ui);state=self.state()
        with self.store.connect() as db:db.execute('INSERT INTO series VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',(self.info['isin'],'proxy','GER','EUR','APC.DE','1D','2026-10-12',1,2,1,2,3))
        self.assertFalse(Features.accept_background_snapshot(self.ui,state,values))
    def test_selection_tf_and_drafts_still_block(self):
        for field in ('symbol','timeframe','drafts'):
            values=inputs(self.ui);state=self.state()
            if field=='drafts':self.ui.drafts['X']={'position':1}
            else:getattr(self.ui,field).value='changed'
            self.assertFalse(Features.accept_background_snapshot(self.ui,state,values))
    def test_real_worker_timing_and_publication(self):
        client=Client(self.store.path)
        try:
            values=inputs(self.ui);state=client.calculate('snapshot',values,0)
            self.ui.feed_errors[self.info['isin']]='New diagnostic'
            self.assertTrue(Features.accept_background_snapshot(self.ui,state,values))
            self.assertTrue(state['ready'])
            self.assertGreaterEqual(state['_calculation_timing']['compute_seconds'],0)
            self.assertGreaterEqual(state['_calculation_timing']['publication_wait_seconds'],0)
            self.ui.bridge.publish.assert_called_once_with(state)
        finally:
            client.close()
            if client.process:client.process.wait(timeout=10);client.process.stdin.close();client.process.stdout.close()

if __name__=='__main__':unittest.main()
