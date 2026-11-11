"""Regression for observed fetch/check timestamp starvation, no market feeds."""
import json, unittest
from test_input_revisions import InputRevisionTests, inputs, Features, Client

class RevisionMetadataTests(unittest.TestCase):
    setUp=InputRevisionTests.setUp
    tearDown=InputRevisionTests.tearDown
    calculate=InputRevisionTests.calculate

    def test_metadata_only_and_other_instrument_leave_engine_current(self):
        identity='history_identity:'+self.info['isin']
        quality='data_quality:'+self.info['isin']+':1D'
        self.store.set_setting(identity,{'fetched_at':'old','symbol':'APC.DE','venue':'GER','currency':'EUR'})
        self.store.set_setting(quality,{'state':'INVALID','checked_at':'old','raw_retry_after':1})
        def write():
            self.store.set_setting(identity,{'fetched_at':'new','symbol':'APC.DE','venue':'GER','currency':'EUR'})
            self.store.set_setting(quality,{'state':'INVALID','checked_at':'new','raw_retry_after':2})
            self.store.set_setting('gettex_last_success:'+self.info['isin'],'new')
            self.store.save_instrument({'isin':'OTHER','name':'Other'})
        self.assertEqual(self.calculate(write)['signal'],'WAIT')

    def test_relevant_dq_and_identity_changes_still_reject(self):
        for key,old,new in [
            ('data_quality:X',{'state':'READY'},{'state':'INVALID'}),
            ('data_quality:X',{'ranges':[]},{'ranges':[['a','b']]}),
            ('history_identity:X',{'venue':'GER','currency':'EUR'},{'venue':'FRA','currency':'EUR'}),
            ('history_identity:X',{'currency':'EUR'},{'currency':'USD'}),
            ('history_identity:X',{'symbol':'APC.DE'},{'symbol':'OTHER'}),
            ('history_identity:X',{'source':'proxy'},{'source':'gettex'}),
            ('history_coverage:X',{'intraday_blockers':[]},{'intraday_blockers':['gap']})]:
            with self.subTest(key=key,new=new):
                self.store.set_setting(key,old)
                with self.store.read_cache.track_inputs() as token:self.store.setting(key)
                self.store.set_setting(key,new)
                with self.assertRaisesRegex(RuntimeError,'setting:.*->'):self.store.read_cache.assert_current(token)

    def test_update_trigger_and_noop_are_semantic(self):
        key='history_identity:X'
        self.store.set_setting(key,{'fetched_at':'old','venue':'GER'})
        with self.store.read_cache.track_inputs() as token:self.store.setting(key)
        with self.store.connect() as db:
            db.execute('UPDATE settings SET value=? WHERE key=?',(json.dumps({'fetched_at':'new','venue':'GER'}),key))
        self.assertTrue(self.store.read_cache.current(token))
        self.store.set_setting(key,{'fetched_at':'new','venue':'GER'})
        self.assertTrue(self.store.read_cache.current(token))

    def test_real_ohlcv_change_still_rejects(self):
        InputRevisionTests.test_used_ohlcv_change_rejects(self)

    def test_snapshot_publication_and_identity_guard(self):
        key='history_identity:'+self.info['isin']
        self.store.set_setting(key,{'symbol':'APC.DE','venue':'GER','currency':'EUR','fetched_at':'old'})
        client=Client(self.store.path)
        try:
            values=inputs(self.ui);state=client.calculate('snapshot',values,0)
            self.store.set_setting(key,{'symbol':'APC.DE','venue':'GER','currency':'EUR','fetched_at':'new'})
            self.store.event('unrelated',{})
            self.assertTrue(Features.accept_background_snapshot(self.ui,state,values))
            self.store.set_setting(key,{'symbol':'APC.F','venue':'FRA','currency':'EUR','fetched_at':'new'})
            self.assertFalse(Features.accept_background_snapshot(self.ui,state,values))
        finally:
            client.close()
            if client.process:client.process.wait(timeout=10);client.process.stdin.close();client.process.stdout.close()

if __name__=='__main__':unittest.main()
