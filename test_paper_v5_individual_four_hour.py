"""Individual V5 rules share the execution path and pin protection at entry."""
import unittest

import pandas as pd

import test_paper_protection as fixtures
import paper_v5_four_hour as four_hour
from trading_context import plan


class IndividualFourHourTests(unittest.TestCase):
    def setUp(self):
        self.runner = fixtures.ProtectionTests()
        self.runner.setUp()
        self.addCleanup(self.runner.doCleanups)
        self.f = self.runner.f
        self.hourly = fixtures.fixture.hourly
        self.runner.use_strategy('AGGRESSIVE')
        self.hourly.activate_v5_four_hour(self.f.db)
        self.f.db.commit()
        self.configure({self.f.isin:{'hold_by_close':{'19:45':840}}})

    def configure(self, rules):
        self.f.db.commit()
        self.hourly.set_v5_individual_rules(self.f.db, '2026-11-22', rules)
        self.f.db.commit()
        self.f.cfg = self.hourly.read(self.f.db, self.hourly.CONFIG)

    def prepare(self, timestamp, *, opening_signal=False, price=100):
        f = self.f
        bar_close = pd.Timestamp(timestamp)
        bar_open = bar_close-four_hour.QUARTER
        f.now = (bar_close+pd.Timedelta(minutes=2)).timestamp()
        self.runner.s.prepare('WAIT', str(bar_open.tz_convert('Europe/Berlin').tz_localize(None)))
        result = f.result
        result['timeframe'] = '4H'
        contract = plan('4H')
        result['hourly'].update(required_tf=contract['required_tf'], timeframe_contract=contract)
        quote = result['execution_quote']['bars'][-1]
        quote.update(open_at=bar_open.isoformat(),close_at=bar_close.isoformat(),
                     open=price,high=price+1,low=price-1,close=price)
        if opening_signal:
            result['execution_context']['trading_engine']['v5_four_hour'] = dict(
                setup=four_hour.SETUP, atr=2., entry_level=99., relative_volume=1.4,
                signal_closed=bar_open.isoformat(), opening=bar_open.isoformat(), quote_end=bar_close.isoformat(),
                weekly_trend=dict(rule=four_hour.WEEKLY_RULE,rising=True), entry_rule=four_hour.ENTRY_RULE,
                weighted_entry=dict(price='99.50',positive_bars=8))

    def test_individual_entry_uses_same_ledger_and_keeps_original_exit_after_config_change(self):
        f = self.f
        self.prepare('2026-11-03T18:45:00Z',opening_signal=True)
        entry = self.runner.buy()
        receipt = entry['smart_eligibility']
        profile = receipt['entry_evidence']['individual_4h']
        self.assertEqual((profile['isin'],profile['close_window'],profile['hold_minutes']),
                         (f.isin,'19:45',840))
        self.assertEqual(receipt['protection']['hold_minutes'],840)
        self.assertEqual((entry['quantity'],entry['timeframe']),('10','4H'))
        self.assertTrue(f.trade()['idempotent'])
        self.configure({})
        self.runner.use_strategy('V1')

        self.prepare('2026-11-04T06:45:00Z')
        self.assertIsNone(self.runner.command())
        self.prepare('2026-11-04T08:45:00Z')
        f.args = self.runner.command()
        sell = f.trade()['fill']
        self.assertEqual(sell['smart_eligibility']['protective_exit']['reason'],'STRATEGY_TIME_EXIT')
        self.assertEqual(sell['quantity'],'10')

    def test_individual_rules_preserve_trend_and_other_windows(self):
        f = self.f
        self.prepare('2026-11-04T10:45:00Z',opening_signal=True)
        _, decision = self.hourly.strategy_signal(f.cfg, f.result)
        self.assertEqual((decision['action'],decision['hold_minutes']),('BUY',720))

        self.configure({f.isin:{'allowed_closes':['19:45'],'hold_by_close':{'19:45':840}}})
        self.prepare('2026-11-04T10:45:00Z',opening_signal=True)
        _, decision = self.hourly.strategy_signal(f.cfg, f.result)
        self.assertEqual(decision['reason'],four_hour.INDIVIDUAL_WINDOW_WAIT)
        self.assertIsNone(self.runner.command())

        self.prepare('2026-11-03T18:45:00Z',opening_signal=True)
        evidence = f.result['execution_context']['trading_engine']['v5_four_hour']
        evidence['weekly_trend']['rising'] = False
        _, decision = self.hourly.strategy_signal(f.cfg, f.result)
        self.assertEqual(decision['reason'],four_hour.RECOVERY_UNAVAILABLE)
        self.assertIsNone(self.runner.command())

    def test_individual_stop_has_priority_over_later_expiry(self):
        f = self.f
        self.prepare('2026-11-03T18:45:00Z',opening_signal=True)
        self.runner.buy()
        self.prepare('2026-11-03T19:00:00Z',price=96)
        f.args = self.runner.command()
        self.assertEqual(f.trade()['fill']['smart_eligibility']['protective_exit']['reason'],'STOP_LOSS')

    def test_invalid_rule_is_rejected_without_replacing_active_config(self):
        f = self.f
        original = self.hourly.read(f.db,self.hourly.CONFIG)
        for rule in ({f.isin:{'hold_by_close':{'19:45':841}}},
                     {f.isin:{'allowed_closes':['10:10']}},
                     {'unregistered':{'hold_by_close':{'19:45':840}}}):
            with self.assertRaisesRegex(ValueError,'PAPER_V5_INDIVIDUAL_RULES_INVALID'):
                self.configure(rule)
            f.db.rollback()
        self.assertEqual(self.hourly.read(f.db,self.hourly.CONFIG),original)

