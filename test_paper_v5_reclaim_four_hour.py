"""4H retry uses the existing PAPER order, revision checks and protection."""
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import pandas as pd

import paper_v5_four_hour as model
import test_paper_protection as fixtures
from timeframes import schedule
from trading_context import plan


class ReclaimFourHourTests(unittest.TestCase):
    def setUp(self):
        self.runner = fixtures.ProtectionTests()
        self.runner.setUp()
        self.addCleanup(self.runner.doCleanups)
        self.f = self.runner.f
        self.hourly = fixtures.fixture.hourly
        self.runner.use_strategy('AGGRESSIVE')
        self.hourly.activate_v5_four_hour(self.f.db)
        self.f.db.commit()
        self.f.cfg = self.hourly.read(self.f.db,self.hourly.CONFIG)
        self.signal = pd.Timestamp('2026-11-04T18:30:00Z')
        self.signal_open = self.signal - model.FOUR_HOURS

    def at(self, minutes):
        return self.signal + pd.Timedelta(minutes=minutes)

    def setup_evidence(self, minutes=30, *, first_price=99.2, initial=model.WEIGHTED_PRICE_WAIT):
        evidence = dict(setup=model.SETUP, atr=2., entry_level=99., relative_volume=1.4,
            signal_closed=self.signal.isoformat(), opening=self.at(minutes-15).isoformat(),
            quote_end=self.at(minutes).isoformat(), weekly_trend=dict(rule=model.WEEKLY_RULE,rising=True),
            entry_rule=model.ENTRY_RULE, weighted_entry=dict(price='99.50',positive_bars=8))
        if minutes>15:
            evidence["reclaim"] = dict(rule=model.RECLAIM_RULE, first_opening=self.signal.isoformat(),
                                       first_price=str(first_price), initial_blocker=initial)
        return evidence

    def prepare(self, minutes, evidence, price=100):
        f = self.f
        f.now=self.at(minutes+2).timestamp()
        self.runner.s.prepare('WAIT',str(self.signal_open.tz_convert('Europe/Berlin').tz_localize(None)))
        result=f.result
        result['timeframe']='4H'
        contract=plan('4H')
        result['hourly'].update(required_tf=contract['required_tf'],timeframe_contract=contract)
        result['execution_context']['trading_engine']['v5_four_hour']=evidence
        quote=result['execution_quote']['bars'][-1]
        quote.update(open_at=self.at(minutes-15).isoformat(),close_at=self.at(minutes).isoformat(),
                     open=price,high=price+1,low=price-1,close=price)
        return result

    def test_schedule_first_four_closed_quotes_and_exact_current_quote(self):
        cal=schedule(dict(self.f.identity,timeframe='1D'),'2026-11-03','2026-11-04')
        for offset in (15,30,45,60):
            with self.subTest(offset=offset):
                now=self.at(offset+2)
                window=model.opening_window(cal,now,reclaim=True)
                self.assertEqual(window,(self.signal_open,self.signal,self.signal,self.at(offset)))
                self.assertEqual(bool(model.opening_window(cal,now)),offset==15)
        self.assertIsNone(model.opening_window(cal,self.at(75),reclaim=True))
        evidence=self.setup_evidence()
        bar=dict(open_at=self.at(15),close_at=self.at(30))
        self.assertIsNone(model.quote_blocker(evidence,bar,self.at(32)))
        self.assertEqual(model.quote_blocker(evidence,bar,self.at(45)),'V5_4H_OPENING_WINDOW_EXPIRED')
        self.assertEqual(model.quote_blocker(evidence,dict(open_at=self.signal,close_at=self.at(15))),
                         'V5_4H_WAIT_FIRST_QUOTE')

    def test_original_and_reclaim_use_same_request_and_protected_position(self):
        first=self.prepare(15,self.setup_evidence(15))
        original_id=self.runner.command()['request_id']
        self.assertEqual(self.hourly.strategy_signal(self.f.cfg,first)[1]['reason'],'V5_4H_COMPRESSION_BREAKOUT')
        self.prepare(30,self.setup_evidence())
        fill=self.runner.buy()
        self.assertEqual(fill['request_id'],original_id)
        self.assertEqual(fill['quantity'],'10')
        self.assertEqual(fill['smart_eligibility']['signal_reason'],model.RECLAIM_BUY)
        self.assertEqual(fill['smart_eligibility']['entry_evidence']['reclaim']['first_price'],'99.2')
        self.assertEqual(fill['smart_eligibility']['protection']['hold_minutes'],model.HOLD_MINUTES)
        self.assertTrue(self.f.trade()['idempotent'])
        self.prepare(45,self.setup_evidence(45))
        self.assertIsNone(self.runner.command())
        self.assertEqual(len(fixtures.fixture.paper.ledger(self.f.db)['fills']),1)
        self.prepare(60,{},price=95)
        self.f.args=self.runner.command()
        sell=self.f.trade()['fill']
        self.assertEqual(sell['smart_eligibility']['protective_exit']['reason'],'STOP_LOSS')

    def test_current_price_and_core_remain_mandatory(self):
        self.prepare(30,self.setup_evidence(),price=101)
        self.assertEqual(self.hourly.strategy_signal(self.f.cfg,self.f.result)[1]['reason'],model.ENTRY_TOO_FAR)
        self.assertIsNone(self.runner.command())
        result=self.prepare(30,self.setup_evidence())
        result['execution_context']['trading_engine']['status']='BLOCKED'
        self.assertEqual(self.hourly.strategy_signal(self.f.cfg,result)[1]['action'],'BLOCKED')
        self.assertIsNone(self.runner.command())

    def test_micron_keeps_ordinary_entry_and_option_can_disable_only_reclaim(self):
        isin='US5951121038'
        for minutes,expected in ((15,'BUY'),(30,'WAIT')):
            result=self.prepare(minutes,self.setup_evidence(minutes))
            result['isin']=isin
            cfg=dict(self.f.cfg,identities={isin:self.f.identity})
            self.assertEqual(self.hourly.strategy_signal(cfg,result)[1]['action'],expected)
        result=self.prepare(30,self.setup_evidence())
        cfg=dict(self.f.cfg,**{model.RECLAIM_KEY:False})
        self.assertEqual(self.hourly.strategy_signal(cfg,result)[1]['action'],'WAIT')

    def test_setup_checks_first_and_intermediate_closed_quotes(self):
        f=self.f
        store=SimpleNamespace(connect=lambda:f.db)
        cal,window=model.entry_window(f.identity,self.at(47),reclaim=True)
        row=pd.DataFrame(dict(coverage_complete=[True]),index=[self.signal_open])
        compression=pd.DataFrame(dict(eligible=[True],atr=[2.],entry_level=[99.],relative_volume=[1.4]))
        cases=(
            ([(0,98.9,20),(15,99.1,20)],None,model.LEVEL_LOST),
            ([(0,99.2,20),(15,99.99,20)],model.RECLAIM_EARLIER_WAIT,None),
            ([(0,102.,20)],model.ENTRY_TOO_FAR,None),
            ([(0,100.,20)],model.RECLAIM_EARLIER_WAIT,None),
            ([(0,99.2,0),(15,99.1,20)],model.RECLAIM_DATA_WAIT,None),
        )
        for quotes,reason,initial in cases:
            with self.subTest(quotes=quotes):
                raw=pd.DataFrame([dict(close=p,volume=v) for _,p,v in quotes],
                                 index=[self.at(t) for t,_,_ in quotes])
                with (patch.object(model,'weekly_context',return_value=dict(rule=model.WEEKLY_RULE,rising=True)),
                      patch('paper_v4._series',return_value=raw) as read,
                      patch('paper_v4.intraday',return_value=row),
                      patch.object(model,'compression',return_value=compression),
                      patch.object(model,'weighted_entry_context',return_value=dict(price='99.50',positive_bars=8))):
                    evidence=model.closed_setup(store,f.info,f.identity,cal,window,{}, {})
                self.assertEqual(read.call_args.args[6],self.at(15).tz_convert('Europe/Berlin').tz_localize(None))
                self.assertEqual(evidence.get('reason'),reason)
                self.assertEqual(evidence.get('reclaim',{}).get('initial_blocker'),initial)

