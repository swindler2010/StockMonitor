"""One affected empty-interval scenario; no market I/O or working database."""
import copy
import sys
import unittest
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent / 'stock_monitor'))
import cp_reconciliation as rv


class IntervalScope(unittest.TestCase):
    def test_unrelated_zero_quantity_does_not_hide_exact_no_trades(self):
        isin = 'US0378331005'
        native = dict(source='gettex_chart', symbol='AAPL.GTX', venue='GETTEX',
                      currency='EUR', timezone='Europe/Berlin')
        official = dict(native, source='gettex', symbol=isin)
        now = pd.Timestamp('2026-11-13T09:00Z')
        start, end = '2026-11-12 22:45:00', '2026-11-12 23:00:00'
        proof = dict(isin=isin, identity=official, basis='complete_published_trade_files',
                     reference={'fixture': 'validated paired trade files'}, checked_at=now.isoformat(),
                     no_trades=[[start, end]], trade_intervals=[],
                     zero_quantity_intervals=[['2026-11-12 22:40:00', start]])
        def classify(meta, p):
            spec = rv._spec(isin, meta, '15m', start, start)
            inputs = dict(rows=[], proof=p, fingerprint='fixture')
            original = copy.deepcopy(inputs)
            result = rv._classify(spec, inputs, now)
            self.assertEqual(inputs, original)
            return result
        result = classify(native, proof)
        self.assertEqual(result['market_state'], 'NO_TRADES')
        self.assertEqual(result['state'], 'SOURCE_TIMEBASE_UNCONFIRMED')
        self.assertFalse(result['can_affect_dq'])
        result = classify(official, proof)
        self.assertEqual(result['state'], 'NO_TRADES')
        for field in ('zero_quantity_intervals', 'trade_intervals'):
            changed = dict(proof, **{field: [[start, end]]})
            result = classify(official, changed)
            self.assertNotEqual(result['market_state'], 'NO_TRADES')
            self.assertFalse(result['can_affect_dq'])
        for changed in (dict(proof, no_trades=[]), dict(proof, reference={}),
                        dict(proof, identity=dict(official, currency='USD')),
                        dict(proof, checked_at='2026-11-14T09:00Z')):
            result = classify(official, changed)
            self.assertNotEqual(result['market_state'], 'NO_TRADES')
            self.assertFalse(result['can_affect_dq'])


if __name__ == '__main__':
    unittest.main()
