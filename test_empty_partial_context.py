"""An empty verified chart context is unavailable, not a calendar exception."""
import copy
import unittest
from unittest.mock import patch
import pandas as pd
import partial_chart
from timeframes import LABELS


class EmptyPartialContext(unittest.TestCase):
    def frame(self, **attrs):
        frame = pd.DataFrame(columns=['open', 'high', 'low', 'close', 'volume'],
                             index=pd.DatetimeIndex([]))
        frame.attrs = {'partial_chart': {'display_only': True}, **attrs}
        return frame

    def check_blocked(self, frame):
        original = copy.deepcopy(frame.attrs)
        with patch('analytics.bar_ends', side_effect=AssertionError('No empty calendar lookup')):
            out = partial_chart.bind({'trading_engine': {'status': 'READY', 'signal': 'BUY'}}, frame)
        self.assertTrue(out['frame'].empty)
        self.assertEqual(out['zones'], [])
        self.assertFalse(out['analysis'][1]['display_levels_available'])
        self.assertFalse(out['analysis'][1]['ohlcv_valid'])
        self.assertIsNone(out['analysis'][1]['support'])
        self.assertIsNone(out['analysis'][1]['stop'])
        self.assertEqual(out['trading_engine']['status'], 'DATA BLOCKED')
        self.assertEqual(out['trading_engine']['signal'], 'WAIT')
        self.assertEqual(frame.attrs, original)
        return out

    def test_empty_without_timeframe_does_not_raise_or_grant_permission(self):
        self.check_blocked(self.frame())

    def test_empty_all_eleven_timeframes_skip_calendar_and_preserve_dq(self):
        for tf in LABELS:
            with self.subTest(tf=tf):
                out = self.check_blocked(self.frame(timeframe=tf, venue='GETTEX',
                    timezone='Europe/Berlin', data_quality={'state': 'UNCONFIRMED'}))
                self.assertEqual(out['frame'].attrs['data_quality']['state'], 'UNCONFIRMED')

    def test_empty_retains_source_failure_and_never_invents_indicators(self):
        out = self.check_blocked(self.frame(timeframe='4H', venue='GETTEX',
            timezone='Europe/Berlin', data_quality={'state': 'SOURCE_UNAVAILABLE',
                'issues': ['verified source returned no bars']}))
        enriched = out['analysis'][0]
        self.assertEqual(enriched.attrs['data_quality']['issues'], ['verified source returned no bars'])
        self.assertFalse(enriched.attrs['partial_indicators']['execution_allowed'])
        self.assertTrue(all(n == 0 for n in enriched.attrs['partial_indicators']['available_values'].values()))


if __name__ == '__main__':
    unittest.main()
