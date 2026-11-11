"""Fair retry scheduling under recurring requests; no live data or providers."""
import unittest
from unittest.mock import patch
import test_cp_automatic_repair as fixture
import cp_assessment as worker
from paper_hourly import CONFIG


class RetryFairness(unittest.TestCase):
    def setUp(self):
        self.store = fixture.Memory()
        self.addCleanup(self.store.db.close)

    def request_at(self, isin, now):
        with patch.object(worker.time, 'time', return_value=now):
            worker.request(self.store, isin)
        return self.store.setting(worker.PREFIX + isin)

    def finish_at(self, item, now, error=None):
        with patch.object(worker.time, 'time', return_value=now):
            worker.finish(self.store, item, error=error)

    def test_old_retry_precedes_newer_unattempted_request(self):
        old = self.request_at('OLD', 100)
        self.finish_at(old, 110, error='INPUT_CHANGED')
        self.request_at('NEW', 200)
        self.assertEqual(worker.choose(self.store.db, 250)['isin'], 'OLD')
        self.assertIsNone(self.store.setting('cp_eligibility:OLD'))

    def test_retry_deadline_remains_binding(self):
        old = self.request_at('OLD', 100)
        self.finish_at(old, 110, error='INPUT_CHANGED')
        new = self.request_at('NEW', 120)
        self.assertEqual(worker.choose(self.store.db, 169)['isin'], 'NEW')
        self.finish_at(new, 169)
        self.assertEqual(worker.choose(self.store.db, 170)['isin'], 'OLD')

    def test_53_and_200_requeued_successes_cannot_starve_failed_request(self):
        for count in (53, 200):
            with self.subTest(count=count):
                with self.store.connect() as db:
                    db.execute('DELETE FROM settings')
                old = self.request_at('FAILED', 100)
                self.finish_at(old, 110, error='INPUT_CHANGED')
                self.store.set_setting(CONFIG, dict(enabled=True, approved=True,
                    identities={'APPLE': {}, 'NVIDIA': {}}))
                for i in range(count):
                    self.request_at('STOCK' + str(i), 200 + i)
                seen = []
                for turn in range(3):
                    now = 1000 + turn
                    self.request_at('APPLE', now)
                    self.request_at('NVIDIA', now)
                    item = worker.choose(self.store.db, now)
                    seen.append(item['isin'])
                    self.finish_at(item, now)
                    self.request_at(item['isin'], now + 0.1)
                self.assertEqual(seen[-1], 'FAILED')
                self.assertTrue(all(x in ('APPLE', 'NVIDIA') for x in seen[:2]))
                self.assertIsNone(self.store.setting('cp_eligibility:FAILED'))


if __name__ == '__main__':
    unittest.main()
