"""AH: a delayed next candle does not revoke a valid <=15-minute PAPER quote.

RAM only, existing shared quote/execute paths; no source/account writes.
"""
import copy
import unittest
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import patch
import test_paper_sizing as fixtures
import pandas as pd
import data_quality as dq
import paper_hourly as hourly
import paper_account as paper


class QuoteDelayTests(unittest.TestCase):
    def test_closed_quote_until_15_minutes_with_all_other_gates_preserved(self):
        s = fixtures.SizingTests(); s.setUp()
        self.addCleanup(s.tearDown)
        f = s.f
        f.db.execute('INSERT INTO series VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
            (f.isin, 'gettex_chart', 'GETTEX', 'EUR', 'AAPL.GTX', '15m',
             '2026-11-04 10:15:00', 100, 102, 99, 100, 20))
        store = SimpleNamespace(connect=lambda: nullcontext(f.db),
            read_cache=SimpleNamespace(depend=lambda _: None))
        close = pd.Timestamp('2026-11-04T09:30:00Z')
        audit = dq.audit

        def at_clock(*args, **kwargs):
            report = audit(*args, **kwargs)
            report['checked_at'] = now.isoformat()
            return report

        # No 5m rows or 5m listing receipt: the valid 15m quote is sufficient.
        for age in (0, 899, 900, 900.001):
            with self.subTest(age=age):
                now = close + pd.Timedelta(seconds=age)
                f.now = now.timestamp()
                with patch.object(hourly.time, 'time', return_value=f.now), \
                        patch.object(dq, 'audit', side_effect=at_clock):
                    if age > 900:
                        with self.assertRaisesRegex(ValueError, 'PAPER_STALE_QUOTE'):
                            hourly.fresh_quote(store, f.info, f.identity, now)
                        continue
                    q = hourly.fresh_quote(store, f.info, f.identity, now)
                self.assertEqual(q['timeframe'], '15m')
                self.assertEqual(q['data_quality']['state'], 'READY')

        # Fresh calculation, same 15-minute-old quote: normal V1 BUY reaches
        # the real shared executor, then the isolated savepoint is rolled back.
        f.now = (close + pd.Timedelta(minutes=15)).timestamp()
        s.prepare('BUY'); f.result['execution_quote'] = copy.deepcopy(q)
        f.args['quantity'] = s.size()['quantity']
        good = copy.deepcopy(f.result)
        for quality in ('READY', 'UNCONFIRMED'):
            f.result=copy.deepcopy(good)
            f.result['execution_quote']['data_quality']['state']=quality
            f.db.execute('SAVEPOINT example')
            try:
                # A confirmed GETTEX quote is audited on its own evidence.
                self.assertEqual(f.trade()['fill']['execution_quote_timeframe'], '15m')
            finally:
                f.db.execute('ROLLBACK TO example'); f.db.execute('RELEASE example')
        for case, code in (('age', 'STALE_QUOTE'), ('core_dq', 'AUTO_DQ_BLOCKED'),
                           ('quote_dq', 'QUOTE_DQ_BLOCKED'), ('volume', 'BAD_OHLCV'),
                           ('identity', 'QUOTE_IDENTITY'), ('execution_tf', 'REQUIRES_15M'),
                           ('wait', 'SIGNAL_MISMATCH')):
            f.result = copy.deepcopy(good)
            if case == 'age':
                f.result['execution_quote']['bars'][0]['close_at'] = '2026-11-04T09:29:59Z'
            elif case == 'core_dq':
                f.result['execution_context']['trading_engine']['timeframe_context']['1H']['data_quality']['state']='UNCONFIRMED'
            elif case == 'quote_dq':
                f.result['execution_quote']['data_quality']['state'] = 'UNCONFIRMED'
                f.result['execution_quote']['bars'][0]['open_at']='2026-11-04T10:16:00+01:00'
            elif case == 'volume':
                f.result['execution_quote']['bars'][0]['volume']=0
            elif case == 'identity':
                f.result['execution_quote']['identity']['currency'] = 'USD'
            elif case == 'execution_tf':
                f.result['execution_quote']['timeframe'] = '5m'
            else:
                f.result['execution_context']['trading_engine']['signal'] = 'WAIT'
            f.db.execute('SAVEPOINT reject_example')
            try:
                with self.subTest(case=case), self.assertRaisesRegex(ValueError, code):
                    f.trade()
            finally:
                f.db.execute('ROLLBACK TO reject_example'); f.db.execute('RELEASE reject_example')
        self.assertEqual(paper.ledger(f.db)['fills'], [])


if __name__ == '__main__':
    unittest.main()
