"""Only repair dispatch ordering; isolated in-memory metadata, no market requests."""
import copy
import json
import sqlite3
import sys
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent / 'stock_monitor'))
import cp_controls as cp


class MemoryStore:
    def __init__(self):
        self.db = sqlite3.connect(':memory:')
        self.db.execute('CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT)')

    @contextmanager
    def connect(self):
        with self.db:
            yield self.db

    def setting(self, key, default=None):
        return cp.read(self.db, key, default)

    def save(self, key, value):
        with self.connect() as db:
            cp.write(db, key, value)


class RepairFairness(unittest.TestCase):
    def setUp(self):
        self.store = MemoryStore()
        self.addCleanup(self.store.db.close)
        self.isin = 'US0378331005'
        self.info = {'isin': self.isin}
        self.meta = dict(source='gettex', symbol=self.isin, venue='GETTEX',
                         currency='EUR', timezone='Europe/Berlin')
        self.key = 'cp_repair_requests:' + self.isin
        self.now = pd.Timestamp('2026-11-03T18:30:01Z')
        self.first = self.problem('2026-10-23 18:00:00')
        self.second = self.problem('2026-10-20 22:15:00')
        self.requests = {
            cp.repair_id(self.first): dict(problem=self.first, state='WAITING', attempts=5,
                requested_at='2026-11-03T15:54:09Z', started_at='2026-11-03T17:30:00Z',
                next_retry='2026-11-03T18:30:00Z'),
            cp.repair_id(self.second): dict(problem=self.second, state='WAITING', attempts=0,
                requested_at='2026-11-03T16:31:49Z', started_at='2026-11-03T17:35:50Z',
                next_retry='2026-11-03T18:30:00Z')}
        self.store.save(self.key, self.requests)

    def problem(self, ts):
        return dict(isin=self.isin, kind='archive', tf='15m', identity=self.meta,
                    state='UNCONFIRMED', range=[ts, ts], id=ts)

    def due(self, now=None):
        return cp.due_request(self.store.db, self.isin, now or self.now)

    def test_real_order_repro_unattempted_range_precedes_repeated_empty_reply(self):
        self.assertEqual(self.due()['problem']['range'], self.second['range'])

    def test_long_queue_rotates_actual_attempts_without_touching_waits_or_completed(self):
        jobs = {}
        for index in range(12):
            p = self.problem('2026-10-20 %02d:00:00' % (8 + index))
            jobs[cp.repair_id(p)] = dict(problem=p, state='QUEUED', attempts=0,
                requested_at=(self.now - pd.Timedelta(hours=1) + pd.Timedelta(seconds=index)).isoformat())
        future = copy.deepcopy(next(iter(jobs.values())))
        future.update(state='WAITING', next_retry=(self.now + pd.Timedelta(days=1)).isoformat())
        jobs['future'] = future
        jobs['terminal'] = dict(future, state='COMPLETED', next_retry=None)
        self.store.save(self.key, jobs)
        selected = []
        for turn in range(48):
            clock = self.now + pd.Timedelta(minutes=turn)
            item = self.due(clock)
            target = cp.repair_id(item['problem'])
            selected.append(target)
            jobs[target].update(state='WAITING', attempts=jobs[target]['attempts'] + 1,
                last_attempt_at=clock.isoformat(), next_retry=(clock + pd.Timedelta(minutes=1)).isoformat())
            self.store.save(self.key, jobs)
        for offset in range(0, 48, 12):
            self.assertEqual(len(set(selected[offset:offset + 12])), 12)
        self.assertEqual(jobs['future'], future)
        self.assertEqual(jobs['terminal']['state'], 'COMPLETED')

    def test_retry_after_is_not_bypassed_and_read_does_not_change_queue(self):
        before = self.store.setting(self.key)
        self.assertIsNone(self.due(self.now - pd.Timedelta(seconds=2)))
        self.assertEqual(self.store.setting(self.key), before)

    def test_service_records_actual_attempt_and_preserves_unresolved_dq(self):
        report = dict(state='UNCONFIRMED', ranges=[self.second['range']], recovery_provider_attempted=True,
                      next_retry=(self.now + pd.Timedelta(hours=1)).isoformat())
        with patch.object(cp, 'problems', return_value=[self.first, self.second]), \
             patch('eur_listings.stored_identity', return_value=self.meta), \
             patch('data_quality.repair', return_value=report) as repair:
            out = cp.service_request(self.info, self.store, lambda *a: None, self.now)
        self.assertEqual(repair.call_args.kwargs['target_range'], self.second['range'])
        self.assertEqual(out['last_attempt_at'], self.now.isoformat())
        self.assertEqual(out['attempts'], 1)
        self.assertEqual(out['state'], 'WAITING')
        self.assertEqual(out['quality'], 'UNCONFIRMED')
        self.assertEqual(self.store.setting(self.key)[cp.repair_id(self.first)],
                         self.requests[cp.repair_id(self.first)])
        self.assertEqual(self.due()['problem']['range'], self.first['range'])
        self.assertIsNone(self.store.setting('cp_manual_risk:' + self.isin))

    def test_provider_wait_is_not_an_attempt_and_identity_change_still_rejected(self):
        retry = (self.now + pd.Timedelta(minutes=10)).isoformat()
        self.store.save('cp_source_retry:gettex', retry)
        with patch.object(cp, 'problems', return_value=[self.first, self.second]), \
             patch('eur_listings.stored_identity', return_value=self.meta), \
             patch('data_quality.repair', return_value=dict(state='UNCONFIRMED',ranges=[self.second['range']])) as repair:
            out = cp.service_request(self.info, self.store, lambda *a: None, self.now)
        self.assertTrue(repair.call_args.kwargs['local_only'])
        self.assertEqual(out['attempts'], 0)
        self.assertNotIn('last_attempt_at', out)
        self.assertEqual(out['next_retry'], retry)
        self.store.save(self.key, self.requests)
        changed = dict(self.second, identity=dict(self.meta, currency='USD'))
        with patch.object(cp, 'problems', return_value=[changed]), \
             patch('data_quality.repair') as repair:
            out = cp.service_request(self.info, self.store, lambda *a: None, self.now)
        repair.assert_not_called()
        self.assertEqual(out['state'], 'SUPERSEDED')

    def test_persisted_priority_survives_reopen_including_interrupted_request(self):
        jobs = copy.deepcopy(self.requests)
        jobs[cp.repair_id(self.first)].update(state='RUNNING', last_attempt_at=self.now.isoformat())
        self.store.save(self.key, jobs)
        restored = MemoryStore()
        self.addCleanup(restored.db.close)
        self.store.db.backup(restored.db)
        out = cp.due_request(restored.db, self.isin, self.now)
        self.assertEqual(out['problem']['range'], self.second['range'])
        self.assertEqual(restored.setting(self.key), jobs)

    def test_legacy_attempt_time_survives_cooldown_only_visit(self):
        jobs = copy.deepcopy(self.requests)
        jobs[cp.repair_id(self.second)].update(attempts=1,
            last_attempt_at='2026-11-03T18:39:02Z',
            next_retry='2026-11-03T19:39:02Z')
        self.store.save(self.key, jobs)
        self.store.save('cp_source_retry:gettex', '2026-11-03T19:39:02Z')
        with patch.object(cp, 'problems', return_value=[self.first, self.second]), \
             patch('eur_listings.stored_identity', return_value=self.meta), \
             patch('data_quality.repair',return_value=dict(state='UNCONFIRMED',ranges=[self.first['range']])) as repair:
            out = cp.service_request(self.info, self.store, lambda *a: None,
                                     pd.Timestamp('2026-11-03T18:50:00Z'))
        self.assertTrue(repair.call_args.kwargs['local_only'])
        self.assertEqual(out['attempts'], 5)
        self.assertEqual(out.get('legacy_rotation_at'), '2026-11-03T17:30:00Z')
        self.assertNotIn('last_attempt_at', out)  # no invented historical measurement
        self.assertEqual(self.due(pd.Timestamp('2026-11-03T19:39:03Z'))['problem']['range'],
                         self.second['range'])

    def test_mixed_legacy_and_new_receipts_keep_alternating_after_failed_attempts(self):
        self.requests[cp.repair_id(self.first)]['attempts']=1
        self.store.save(self.key,self.requests)
        chosen = []
        for turn in range(3):
            clock = self.now + pd.Timedelta(minutes=2 * turn)
            report = dict(state='UNCONFIRMED', ranges=[self.first['range'], self.second['range']], recovery_provider_attempted=True,
                          next_retry=(clock + pd.Timedelta(minutes=1)).isoformat())
            with patch.object(cp, 'problems', return_value=[self.first, self.second]), \
                 patch('eur_listings.stored_identity', return_value=self.meta), \
                 patch('data_quality.repair', return_value=report):
                out = cp.service_request(self.info, self.store, lambda *a: None, clock)
            chosen.append(out['problem']['range'])
            self.assertEqual(out['state'], 'WAITING')
            self.assertEqual(out['last_attempt_at'], clock.isoformat())
        self.assertEqual(chosen, [self.second['range'], self.first['range'], self.second['range']])
        self.assertIsNone(self.store.setting('cp_manual_risk:' + self.isin))


if __name__ == '__main__':
    unittest.main()
