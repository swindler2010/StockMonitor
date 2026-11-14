"""Daily assisted reviews use the existing daily calendar, not intraday coverage."""
import sys
import unittest
from pathlib import Path
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent / 'stock_monitor'))
import cp_reconciliation as review


class DailyReviewCalendar(unittest.TestCase):
    def classify(self, tf, date, result='ABSENT'):
        meta = dict(source='gettex_chart', symbol='AAPL.GTX', venue='GETTEX',
                    currency='EUR', timezone='Europe/Berlin')
        job = review._spec('US0378331005', meta, tf, date, date)
        inputs = dict(rows=[], proof={}, fingerprint='unchanged-exact-input')
        job['observation'] = dict(result=result, input_fingerprint=inputs['fingerprint'],
                                  identity_confirmed=True)
        return review._classify(job, inputs, pd.Timestamp('2026-11-11T10:49Z'))

    def test_historical_daily_matching_absence_is_reviewed_not_certified(self):
        result = self.classify('1D', '2018-05-10')
        self.assertEqual(result['discrepancy'], 'MISSING_IN_BOTH')
        self.assertEqual(result['state'], 'UNCONFIRMED')
        self.assertFalse(result['can_affect_dq'])

    def test_daily_tv_zero_volume_remains_missing_native_row(self):
        result = self.classify('1D', '2018-05-11', 'ZERO_VOLUME')
        self.assertEqual(result['discrepancy'], 'TV_ROW_MISSING_IN_SM')
        self.assertEqual(result['state'], 'UNCONFIRMED')
        self.assertFalse(result['can_affect_dq'])

    def test_old_intraday_calendar_is_still_rejected(self):
        with self.assertRaises(ValueError):
            self.classify('15m', '2018-05-11 10:00:00')

    def test_daily_before_verified_calendar_is_still_rejected(self):
        with self.assertRaises(ValueError):
            self.classify('1D', '2014-05-09')

    def test_weekly_visual_review_does_not_certify_native_aggregation(self):
        result = self.classify('1W', '2018-05-11')
        self.assertEqual(result['state'], 'UNCONFIRMED')
        self.assertFalse(result['can_affect_dq'])

    def test_monthly_visual_review_does_not_certify_native_aggregation(self):
        result = self.classify('1M', '2018-05-01')
        self.assertEqual(result['state'], 'UNCONFIRMED')
        self.assertFalse(result['can_affect_dq'])


if __name__ == '__main__':
    unittest.main()
