"""Empty analytical views retain upstream DQ without inventing timestamp faults."""
import copy
import json
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT / 'stock_monitor'))
from calculation_window import bounded_features
from candlepilot_health import eligibility_reason
import data_quality as dq


class EmptyContext(unittest.TestCase):
    now = pd.Timestamp('2026-10-23T21:30Z')
    meta = dict(source='gettex', symbol='TEST', venue='GETTEX', currency='EUR',
                timezone='Europe/Berlin', timeframe='1D')
    info = dict(isin='TEST')

    def test_four_saved_empty_views_preserve_original_dq_and_stay_blocked(self):
        evidence = json.loads((ROOT / 'history64-evidence/cp-autonomy27/all-tf27-targeted-evidence.json').read_text(encoding='utf8'))
        cases = [r for r in evidence['empty_or_identity'] if r['prior'] == ['TIMESTAMPS_INVALID']]
        self.assertEqual(len(cases), 4)
        for row in cases:
            with self.subTest(isin=row['isin'], tf=row['tf']):
                self.assertTrue(row['empty']); self.assertFalse(row['duplicates'])
                frame = pd.DataFrame()
                frame.attrs = dict(row['identity'], timeframe=row['tf'], data_quality=row['dq'])
                before = copy.deepcopy(frame.attrs)
                with patch('calculation_window.data_quality.audit', side_effect=AssertionError('Empty view must not be audited as candles')):
                    result = bounded_features(dict(isin=row['isin']), frame, row['tf'], self.now)
                self.assertEqual(result['blockers'], ['NO_USABLE_CANDLES'])
                self.assertEqual(result['archive_state'], row['dq']['state'])
                self.assertEqual(result['archive_quality']['issues'], row['dq']['issues'])
                self.assertFalse(result['analysis_ready']); self.assertFalse(result['chart_ready'])
                self.assertEqual(frame.attrs, before)

    def test_empty_without_metadata_is_unknown_not_invalid_or_ready(self):
        result = bounded_features(self.info, pd.DataFrame(), '1D', self.now)
        self.assertEqual(result['blockers'], ['NO_USABLE_CANDLES'])
        self.assertEqual(result['archive_state'], 'UNKNOWN')
        self.assertFalse(result['analysis_ready']); self.assertFalse(result['chart_ready'])

    def test_nonempty_timestamp_and_identity_guards_remain_strict(self):
        for kind, expected in [('duplicate', 'TIMESTAMPS_INVALID'), ('unordered', 'TIMESTAMPS_INVALID'),
                               ('currency', 'IDENTITY_UNCONFIRMED'), ('venue', 'IDENTITY_MISMATCH')]:
            with self.subTest(kind=kind):
                index = pd.to_datetime(['2026-10-20', '2026-10-21'])
                if kind == 'duplicate': index = index[[0, 0]]
                if kind == 'unordered': index = index[::-1]
                frame = pd.DataFrame(dict(open=10., high=11., low=9., close=10., volume=1.), index=index)
                frame.attrs = dict(self.meta, data_quality={'state': 'INVALID', 'issues': ['upstream proof retained']})
                info = self.info.copy()
                if kind == 'currency': frame.attrs['currency'] = 'USD'
                if kind == 'venue': info['expected_identity'] = {'venue': 'GER'}
                result = bounded_features(info, frame, '1D', self.now)
                self.assertEqual(result['blockers'], [expected])
                self.assertEqual(result['archive_quality']['issues'], ['upstream proof retained'])
                self.assertFalse(result['analysis_ready'])

    def test_nonempty_valid_and_invalid_ohlcv_use_real_existing_dq(self):
        # This raw daily fixture has no session-end columns; use the existing
        # pre-close fixture clock rather than pretending today's bar is closed.
        now = pd.Timestamp('2026-10-23T18:00Z')
        index = dq.expected_index(self.meta, '1D', '2026-01-01', '2026-10-24', now)
        frame = pd.DataFrame(dict(open=10., high=11., low=9., close=10., volume=100., source='gettex'), index=index)
        frame.attrs = dict(self.meta, data_quality={'state': 'READY'})
        valid = bounded_features(self.info, frame, '1D', now)
        self.assertTrue(valid['analysis_ready'], valid)
        frame.iloc[-1, frame.columns.get_loc('volume')] = -1
        invalid = bounded_features(self.info, frame, '1D', now)
        self.assertIn('WINDOW_INVALID', invalid['blockers'])
        self.assertFalse(invalid['analysis_ready']); self.assertFalse(invalid['chart_ready'])

    def test_user_explanations_distinguish_empty_and_corrupt_timestamps(self):
        self.assertIn('Нет пригодных свечей', eligibility_reason('NO_USABLE_CANDLES'))
        self.assertIn('Повторяющиеся', eligibility_reason('TIMESTAMPS_INVALID'))


if __name__ == '__main__':
    unittest.main()
