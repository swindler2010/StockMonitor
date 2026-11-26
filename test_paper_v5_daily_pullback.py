"""Integrated V5 daily entries use the ordinary PAPER ledger and guards."""
from datetime import datetime, timedelta
from decimal import Decimal
import unittest

import test_paper_sizing as fixture
import paper_hourly as hourly
import paper_v5_daily_pullback as daily
from performance_cache import capture_closed_inputs
from trading_context import plan


class DailyPullbackTests(unittest.TestCase):
    def setUp(self):
        self.sizing = fixture.SizingTests()
        self.sizing.setUp()
        self.addCleanup(self.sizing.tearDown)
        self.f = self.sizing.f
        self.f.db.commit()
        hourly.set_trading_mode(self.f.db, 'AGGRESSIVE')
        self.f.db.commit()
        hourly.activate_v5_four_hour(self.f.db)
        self.f.db.commit()
        hourly.activate_v5_daily_pullback(self.f.db)
        self.f.db.commit()
        self.evidence = dict(setup=daily.SETUP, entry_rule=daily.MODEL, atr=2, signal_close=100,
            signal_day='2026-11-02', signal_closed='2026-11-02T22:00:00+01:00',
            opening='2026-11-03T06:45:00+00:00', quote_end='2026-11-03T07:00:00+00:00',
            relative_volume=1.1, defensive_score=0, pattern_score=0.5,
            market=dict(complete_median=0.01))
        self.prepare('2026-11-03T07:00:00+00:00', 100)

    def prepare(self, close_at, price):
        f = self.f
        close = datetime.fromisoformat(close_at)
        f.now = close.timestamp()+120
        f.cfg = hourly.read(f.db, hourly.CONFIG)
        self.sizing.prepare('WAIT', '2026-11-02 00:00:00')
        result = f.result
        result['timeframe'] = '1D'
        engine = result['execution_context']['trading_engine']
        engine['timeframe_context']['1W'] = dict(data_quality=dict(state='READY'))
        engine['v5_daily_pullback'] = dict(self.evidence)
        original = result['hourly']['proof']
        specs = [item['spec'] for item in original['series'] if item['spec']['timeframe'] in ('4H','1D')]
        specs.append(dict(specs[-1], timeframe='1W'))
        proof = capture_closed_inputs(f.db, specs, result['_input_revisions'])
        result['hourly'].update(proof=proof, required_tf=['4H','1D','1W'], timeframe_contract=plan('1D'))
        result['execution_quote']['bars'][0].update(open=price, high=price+1, low=price-1,
            close=price, open_at=(close-timedelta(minutes=15)).isoformat(), close_at=close_at)

    def command(self):
        f = self.f
        return hourly.order_command(f.db, f.info, f.result, f.now)

    def buy(self):
        f = self.f
        f.args = self.command()
        return f.trade()['fill']

    def test_integrated_order_and_stop_survive_strategy_switch(self):
        f = self.f
        self.assertEqual(hourly.configured_timeframes(f.cfg), ('4H','1D'))
        entry = self.buy()
        self.assertEqual((entry['side'], entry['quantity'], entry['timeframe']), ('BUY','10','1D'))
        self.assertTrue(f.trade()['idempotent'])
        receipt = entry['smart_eligibility']
        protection = receipt['protection']
        self.assertEqual(receipt['entry_evidence']['signal_day'], '2026-11-02')
        self.assertEqual(protection['model'], daily.MODEL)
        self.assertEqual((Decimal(protection['stop']), Decimal(protection['target']), protection['hold_minutes']),
                         (Decimal('97.2'), Decimal('106'), 4320))
        f.db.commit()
        hourly.set_trading_mode(f.db, 'V1')
        f.db.commit()
        self.prepare('2026-11-03T07:15:00+00:00', 96)
        f.args = self.command()
        fill = f.trade()['fill']
        self.assertEqual((fill['side'],fill['timeframe']), ('SELL','1D'))
        self.assertEqual(fill['smart_eligibility']['protective_exit']['reason'], 'STOP_LOSS')
        self.assertEqual(len(fixture.paper.ledger(f.db)['fills']), 2)

    def test_take_profit_uses_daily_target(self):
        f = self.f
        self.buy()
        self.prepare('2026-11-03T07:15:00+00:00', 107)
        f.args = self.command()
        fill = f.trade()['fill']
        self.assertEqual(fill['smart_eligibility']['protective_exit']['reason'], 'TAKE_PROFIT')

    def test_time_exit_uses_execution_bar_after_72_hours(self):
        f = self.f
        self.buy()
        self.prepare('2026-11-06T06:45:00+00:00', 100)
        self.assertIsNone(self.command())
        self.prepare('2026-11-06T07:00:00+00:00', 100)
        f.args = self.command()
        self.assertEqual(f.trade()['fill']['smart_eligibility']['protective_exit']['reason'], 'STRATEGY_TIME_EXIT')

    def test_requires_second_bar_near_daily_close_and_current_revisions(self):
        f = self.f
        self.prepare('2026-11-03T06:45:00+00:00', 100)
        self.assertIsNone(self.command())
        self.prepare('2026-11-03T07:00:00+00:00', 102)
        self.assertIsNone(self.command())
        self.prepare('2026-11-03T07:00:00+00:00', 100)
        f.args = self.command()
        f.db.execute("UPDATE series SET close=100 WHERE base_tf='1D'")
        f.blocked('PAPER_STALE_INPUTS')

