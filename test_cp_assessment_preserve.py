"""A failed refresh must not erase still-valid independent analysis evidence."""
import copy,unittest
import pandas as pd
from unittest.mock import patch
import test_admission_expiry as fixture
import calculation_window as cw
import cp_assessment as worker
from candlepilot_health import collect


class PreserveAssessment(unittest.TestCase):
    def setUp(self):
        self.f = fixture.Publication(methodName='runTest')
        self.f.setUp(); self.addCleanup(self.f.tearDown)
        self.store, self.info = self.f.s, self.f.i
        self.prior = self.f.turn()
        self.failure = dict(isin='TEST',error='InputRevisionChanged: unrelated 5m update',
            checked_at=pd.Timestamp.now(tz='UTC').isoformat(),
            expires_at=(pd.Timestamp.now(tz='UTC')+pd.Timedelta(minutes=5)).isoformat())

    def fail_refresh(self):
        worker.request(self.store,'TEST')
        with self.store.connect() as db:item=worker.choose(db)
        worker.finish(self.store,item,self.failure)
        return self.store.setting('cp_eligibility:TEST')

    def test_failed_refresh_preserves_independent_weekly_analysis_and_retry(self):
        saved=self.fail_refresh()
        rows={r['tf']:r for r in collect(self.store.path)['eligibility_rows']}
        self.assertTrue(rows['1W']['analysis_ready'])
        self.assertFalse(rows['5m']['analysis_ready'])
        self.assertFalse(any(r['trade_ready'] for r in rows.values()))
        self.assertEqual(saved['candidates'],self.prior['candidates'])
        self.assertEqual(saved['_input_revisions'],self.prior['_input_revisions'])
        self.assertEqual(saved['expires_at'],self.prior['expires_at'])
        self.assertEqual(rows['1W']['last_failed_attempt'],self.failure)
        request=self.store.setting(worker.PREFIX+'TEST')
        self.assertEqual(request['error'],self.failure['error'])
        with self.store.connect() as db:
            self.assertIsNone(worker.choose(db,request['retry_at']-1))
            self.assertIsNotNone(worker.choose(db,request['retry_at']))

    def test_preserved_candidate_still_expires_and_rejects_changed_identity(self):
        saved=self.fail_refresh()
        with self.store.connect() as db:
            self.assertEqual(cw.candidate_state(db,saved,saved['candidates'][1],self.f.now+pd.Timedelta(minutes=11)),'EXPIRED')
        self.store.save_instrument({**self.info,'name':'changed'})
        rows={r['tf']:r for r in collect(self.store.path)['eligibility_rows']}
        self.assertFalse(rows['1W']['analysis_ready'])
        self.assertFalse(any(r['trade_ready'] for r in rows.values()))

    def test_first_failure_remains_error_without_any_ready(self):
        with self.store.connect() as db:db.execute("DELETE FROM settings WHERE key='cp_eligibility:TEST'")
        self.assertEqual(self.fail_refresh(),self.failure)
        rows=collect(self.store.path)['eligibility_rows']
        self.assertTrue(all(r['state']=='ERROR' for r in rows))
        self.assertFalse(any(r['analysis_ready'] or r['trade_ready'] for r in rows))

    def test_next_success_replaces_failure_without_carrying_old_attempt(self):
        self.fail_refresh()
        expected=copy.deepcopy(self.prior)
        with patch.object(cw,'eligibility_state',return_value='EXPIRED'):
            cw.publish_eligibility(self.store,self.info,expected)
        saved=self.store.setting('cp_eligibility:TEST')
        self.assertNotIn('last_failed_attempt',saved)
        self.assertEqual(saved,expected)


if __name__=='__main__':unittest.main()
