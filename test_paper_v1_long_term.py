"""Long-term V1 selection, closed signals, risk and persistent protection."""
from datetime import datetime, timedelta
from decimal import Decimal
from contextlib import closing
import sqlite3
import unittest

import pandas as pd
import test_paper_sizing as fixture
import paper_hourly as hourly
import paper_v1_long_term as v1
from performance_cache import capture_closed_inputs
from trading_context import plan
from timeframes import schedule


class LongTermTests(unittest.TestCase):
    def setUp(self):
        self.sizing = fixture.SizingTests()
        self.sizing.setUp()
        self.addCleanup(self.sizing.tearDown)
        self.f = self.sizing.f
        self.select('V1')
        self.evidence = dict(setup='DAILY_20_SESSION_BREAKOUT', atr=2, support=96,
            entry_level=99, signal_close=100, signal_day='2026-11-02',
            signal_closed='2026-11-02T22:00:00+01:00', session_open='2026-11-03T06:30:00+00:00',
            session_close='2026-11-03T22:00:00+01:00', weekly={'rising':True},
            relative_strength={'benchmark':'INDEX:STOXX600','3m_pp':4.0})
        self.prepare('2026-11-03T07:00:00+00:00', 100)

    def select(self, mode):
        self.f.db.commit()
        hourly.set_trading_mode(self.f.db, mode)
        self.f.db.commit()

    def prepare(self, close_at, price):
        f = self.f
        close = datetime.fromisoformat(close_at)
        f.now = close.timestamp() + 120
        f.cfg = hourly.read(f.db, hourly.CONFIG)
        self.sizing.prepare('WAIT', '2026-11-02 00:00:00')
        result = f.result
        result['timeframe'] = '1D'
        engine = result['execution_context']['trading_engine']
        engine['timeframe_context']['1W'] = dict(data_quality=dict(state='READY'))
        engine['v1_long_term'] = self.evidence.copy()
        specs = [item['spec'] for item in result['hourly']['proof']['series'] if item['spec']['timeframe'] in ('4H','1D')]
        specs.append(dict(specs[-1], timeframe='1W'))
        result['hourly'].update(proof=capture_closed_inputs(f.db,specs,result['_input_revisions']),
                                required_tf=['4H','1D','1W'], timeframe_contract=plan('1D'))
        result['execution_quote']['bars'][0].update(open=price,high=price+1,low=price-1,
            close=price,open_at=(close-timedelta(minutes=15)).isoformat(),close_at=close_at)

    def command(self, *, manage=False):
        f = self.f
        return hourly.order_command(f.db,f.info,f.result,f.now,manage=manage)

    def test_buy_trail_restart_and_sell_after_mode_switch(self):
        from paper_journal import read as journal
        f = self.f
        self.assertEqual(hourly.configured_timeframes(f.cfg), ('1D',))
        f.args = self.command()
        fill = f.trade()['fill']
        self.assertTrue(f.trade()['idempotent'])
        receipt = fill['smart_eligibility']
        self.assertEqual((fill['side'],fill['timeframe'],fill['execution_quote_timeframe']),('BUY','1D','15m'))
        self.assertEqual(receipt['strategy_model'], v1.MODEL)
        self.assertIsNone(receipt['protection']['target'])
        self.assertLessEqual(Decimal(receipt['sizing']['estimated_risk']), Decimal('100'))
        self.assertEqual(receipt['entry_evidence']['relative_strength']['3m_pp'],4.0)
        self.assertIn('INDEX:STOXX600', journal(f.db,[fill])['entries'][0]['reason'])
        self.select('AGGRESSIVE')
        self.evidence.update(signal_closed='2026-11-04T22:00:00+01:00',signal_close=114,atr=2)
        self.prepare('2026-11-05T07:00:00+00:00', 112)
        self.assertIsNone(self.command(manage=True))
        self.assertIsNone(self.command(manage=True))
        f.db.commit()
        with closing(sqlite3.connect(':memory:')) as restarted:
            f.db.backup(restarted)
            holding = hourly.held_position(restarted,f.isin)
            self.assertEqual(holding['active_stop'],'108')
            self.assertEqual(hourly.turn_timeframes(f.cfg,holding),('1D',))
        self.evidence.update(signal_closed='2026-11-05T22:00:00+01:00',signal_close=111)
        self.prepare('2026-11-06T07:00:00+00:00', 107)
        f.args = self.command()
        exit_fill = f.trade()['fill']
        self.assertEqual(exit_fill['smart_eligibility']['protective_exit']['reason'],'STOP_LOSS')
        self.assertEqual(exit_fill['smart_eligibility']['strategy_label'],'V1')
        self.assertFalse(hourly.held_position(f.db,f.isin))

    def test_stale_price_entry_level_and_revisions_block(self):
        f = self.f
        self.prepare('2026-11-03T07:00:00+00:00', 98)
        with self.assertRaisesRegex(ValueError,'ENTRY_LEVEL_LOST'):
            self.command()
        self.prepare('2026-11-04T07:00:00+00:00', 100)
        with self.assertRaisesRegex(ValueError,'QUOTE_OUTSIDE_SIGNAL_SESSION'):
            self.command()
        self.prepare('2026-11-03T07:00:00+00:00', 100)
        f.args = self.command()
        f.now += 901
        with self.assertRaisesRegex(ValueError,'STALE_QUOTE'):
            self.command()
        f.now -= 901
        f.db.execute("UPDATE series SET volume=30 WHERE base_tf='1D'")
        with self.assertRaisesRegex(ValueError,'STALE_INPUTS'):
            f.trade()
        self.assertFalse(fixture.paper.ledger(f.db)['fills'])

    def test_day_and_week_are_closed_before_admitting_buy(self):
        meta = {**self.f.identity, 'timeframe':'1D'}
        days = schedule(meta,'2026-08-31','2026-11-23')
        frame = pd.DataFrame({'close':100.0,'high':101.0,'low':99.0,'open':100.0},index=days.index)
        frame.loc['2026-11-20',['close','high']] = [104,105]
        frame.loc['2026-11-23',['close','high']] = [500,501]
        weeks = pd.date_range(end='2026-11-20',periods=33,freq='W-FRI')
        weekly = pd.DataFrame({'close':range(70,103)},index=weeks)
        weekly.attrs = {**meta, 'timeframe':'1W'}
        when = pd.Timestamp('2026-11-23T09:00:00Z')
        evidence = v1.context(frame,weekly,meta,when,held=False)
        self.assertEqual(evidence['setup'],'DAILY_20_SESSION_BREAKOUT')
        self.assertEqual(evidence['signal_day'],'2026-11-20')
        self.assertEqual(evidence['signal_close'],104)
        self.assertEqual(v1.decision({'v1_long_term':evidence})['action'],'BUY')
        blocked = v1.context(frame.drop(pd.Timestamp('2026-11-19')),weekly,meta,when,held=False)
        self.assertEqual(blocked['reason'],'V1_DAILY_HISTORY_WAIT')
        stale = v1.context(frame,weekly.iloc[:-1],meta,when,held=False)
        self.assertEqual(stale['reason'],'V1_WEEKLY_HISTORY_WAIT')
        closed = v1.context(frame,weekly,meta,pd.Timestamp('2026-11-22T09:00:00Z'),held=False)
        self.assertEqual(closed['reason'],'V1_NEXT_SESSION_WAIT')

    def test_pullback_and_relative_returns_use_only_past_rows(self):
        days = pd.bdate_range('2026-10-01',periods=26)
        price = pd.Series([99 + .1*i for i in range(26)],index=days)
        frame = pd.DataFrame(dict(close=price, high=price+.5,low=price-.5,open=price-.1))
        frame.iloc[-6] = [100,106,99,100]
        frame.iloc[-1] = [103,104,102,102.5]
        self.assertEqual(v1.daily_setup(frame)['setup'],'DAILY_MA20_REBOUND')
        dates = pd.to_datetime(['2026-05-20','2026-08-20','2026-11-20','2026-11-23'])
        stock = pd.Series([100,110,125,160],index=dates)
        index = pd.Series([100,105,112,92],index=dates)
        self.assertEqual(v1.relative_strength(stock,index,'2026-11-20'),
                         {'3m_pp':6.969697,'6m_pp':13.0})

