"""Current assisted price discrepancies must survive valid native OHLCV geometry."""
import copy
import unittest
from unittest.mock import patch
from test_review_recovery_route import ReviewRecoveryRoute, rv


class NativeDiscrepancy(unittest.TestCase):
    setUp = ReviewRecoveryRoute.setUp
    observe = ReviewRecoveryRoute.observe
    turn = ReviewRecoveryRoute.turn
    requests = ReviewRecoveryRoute.requests

    def prepare(self, invalid=False):
        self.s.db.execute('INSERT INTO candles VALUES (?,?,?,?,?,?,?,?,?)',
            (self.info['isin'], 'gettex', '15m', self.spec['first'],
             1362.8, 1365.4, 1355.6, 1400 if invalid else 1364., 122))
        self.s.db.commit()
        self.observe('DIFFERENT')

    def job(self):
        return self.s.setting(rv.key(self.spec))

    def classify(self):
        job = self.job()
        return rv._classify(job, rv._input(self.s.db, job), self.now)

    def test_current_difference_overrides_native_rows_confirmed(self):
        self.prepare()
        result = self.classify()
        self.assertEqual(result['state'], 'SOURCE_DISCREPANCY')
        self.assertEqual(result['discrepancy'], 'TV_DIFFERENCE')
        self.assertEqual(result['native_dq'], 'READY')
        self.assertTrue(result['observation_current'])
        self.assertFalse(result['can_affect_dq'])

    def test_turn_keeps_discrepancy_open_without_price_quality_or_grant_writes(self):
        self.prepare()
        rows = self.s.db.execute('SELECT * FROM candles').fetchall()
        report = copy.deepcopy(self.s.setting(self.report_key))
        self.turn()
        job = self.job()
        self.assertEqual(job['status'], 'AWAITING_SOURCE')
        self.assertEqual(job['finding']['state'], 'SOURCE_DISCREPANCY')
        self.assertEqual(rows, self.s.db.execute('SELECT * FROM candles').fetchall())
        self.assertEqual(report, self.s.setting(self.report_key))
        self.assertFalse(self.requests())
        self.assertIsNone(self.s.setting('cp_manual_risk:' + self.info['isin']))

    def test_native_invalid_is_preserved_despite_visual_difference(self):
        self.prepare(invalid=True)
        result = self.classify()
        self.assertEqual(result['state'], 'INVALID')
        self.assertEqual(result['native_dq'], 'INVALID')
        self.assertFalse(result['can_affect_dq'])

    def test_changed_native_input_cannot_reuse_old_visual_difference(self):
        self.prepare()
        self.s.db.execute('UPDATE candles SET volume=123 WHERE ts=?', (self.spec['first'],))
        self.s.db.commit()
        result = self.classify()
        self.assertFalse(result['observation_current'])
        self.assertEqual(result['state'], 'NATIVE_ROWS_CONFIRMED')

    def test_unconfirmed_visual_identity_cannot_override_native_quality(self):
        self.prepare()
        job = self.job()
        job['observation']['identity_confirmed'] = False
        self.s.set_setting(rv.key(job), job)
        self.assertEqual(self.classify()['state'], 'NATIVE_ROWS_CONFIRMED')

    def test_input_revision_race_does_not_publish_precomputed_result(self):
        self.prepare()
        before = copy.deepcopy(self.job())
        original = rv._classify
        def changed(job, inputs, now):
            result = original(job, inputs, now)
            self.s.db.execute('UPDATE candles SET volume=123 WHERE ts=?', (self.spec['first'],))
            self.s.db.commit()
            return result
        with patch.object(rv, '_classify', side_effect=changed):
            self.turn()
        self.assertEqual(self.job(), before)
        self.assertFalse(self.requests())


if __name__ == '__main__':
    unittest.main(verbosity=2)
