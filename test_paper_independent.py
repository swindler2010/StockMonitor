"""Navigation, arbitration and revision guards for automatic PAPER accounts."""
from contextlib import contextmanager
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import test_paper_portfolios as fixture
import paper_account as paper
import paper_hourly as hourly
from paper_entry_modes import STRATEGY_CODES


class IndependentTests(unittest.TestCase):
    def setUp(self):
        self.scenario=fixture.PortfolioTests()
        self.scenario.setUp()
        self.addCleanup(self.scenario.doCleanups)
        self.f=self.scenario.f
        self.db=self.f.db
        paper.initialize_independent_automation(self.db)
        self.assertEqual(paper.independent_portfolios(self.db),('V4','V5'))
        # The fixture enables other books to exercise routing independently.
        policy=hourly.read(self.db,paper.AUTO_PORTFOLIOS_SETTING)
        policy['ready']=list(paper.PORTFOLIOS)
        self.f.write(paper.AUTO_PORTFOLIOS_SETTING,policy)
        self.db.commit()

    def view(self,mode):
        self.db.commit()
        hourly.set_trading_mode(self.db,mode)
        self.db.commit()

    def make_order(self,mode):
        f=self.f
        f.cfg=paper.portfolio_config(hourly.read(self.db,hourly.CONFIG),STRATEGY_CODES[mode])
        self.scenario.setup.s.prepare('WAIT')
        self.scenario.setup.strategy_facts(mode)
        self.scenario.bind(STRATEGY_CODES[mode])
        from performance_cache import capture_closed_inputs
        token=f.result['_input_revisions']
        token.pop('setting:'+paper.VIEW_PORTFOLIO_SETTING,None)
        proof=f.result['hourly']['proof']
        f.result['hourly']['proof']=capture_closed_inputs(self.db,[row['spec'] for row in proof['series']],token)
        f.args=hourly.order_command(self.db,f.info,f.result,f.now)
        return f.args

    def test_view_does_not_change_pending_order_or_active_models(self):
        f=self.f
        before=hourly.read(self.db,hourly.CONFIG)
        ready=paper.independent_portfolios(self.db)
        self.view('PULLBACK')
        self.assertEqual(paper.selected_portfolio(self.db),'V3')
        self.assertIsNotNone(self.make_order('AGGRESSIVE'))
        self.view('MOMENTUM')
        fill=f.trade()['fill']
        self.assertEqual(fill['paper_portfolio'],'V5')
        self.assertEqual(paper.ledger(self.db)['account'],'V4')
        self.assertEqual(paper.ledger(self.db)['cash'],'10000.00')
        self.assertEqual(hourly.read(self.db,hourly.CONFIG),before)
        self.assertEqual(paper.independent_portfolios(self.db),ready)

    def test_every_alternate_strategy_buys_without_being_viewed(self):
        self.view('V1')
        f=self.f
        for mode in ('PULLBACK','MOMENTUM','AGGRESSIVE'):
            with self.subTest(mode=mode):
                book=STRATEGY_CODES[mode]
                self.assertEqual(self.make_order(mode)['side'],'BUY')
                fill=f.trade()['fill']
                self.assertEqual((fill['paper_portfolio'],fill['initiator']),(book,book))
                self.assertEqual(len(paper.ledger(self.db,book)['fills']),1)
        self.assertEqual(paper.ledger(self.db)['fills'],[])

    def test_v2_only_has_a_30m_research_route(self):
        import sqlite3
        import tempfile
        from contextlib import closing
        from pathlib import Path
        from paper_entry_modes import V2_RESEARCH_BLOCKER, decision
        self.view('V2')
        self.assertEqual(paper.selected_portfolio(self.db),'V2')
        self.assertIsNone(self.make_order('V2'))
        cfg=paper.portfolio_config(hourly.read(self.db,hourly.CONFIG),'V2')
        self.assertEqual(hourly.configured_timeframes(cfg),('30m',))
        for tf,reason in (('1H','V2_30M_ONLY'),('4H','V2_30M_ONLY'),('30m',V2_RESEARCH_BLOCKER)):
            self.assertEqual(decision({'status':'READY','signal':'BUY'},'V2',tf),
                             dict(action='WAIT',reason=reason))
        self.assertEqual(paper.ledger(self.db,'V2')['cash'],'10000.00')
        key=paper.entry_status_key('V2',self.f.isin)
        legacy=dict(candidate_timeframes=['1H','4H'],timeframes={'1H':{'signal':'BUY'}})
        self.f.write(key,legacy)
        self.db.commit()
        with tempfile.TemporaryDirectory() as directory:
            database=Path(directory)/'stock_monitor.sqlite3'
            with closing(sqlite3.connect(database)) as output:self.db.backup(output)
            state=paper.status(database,portfolio='V2')
        self.assertEqual(state['automation']['instruments'],{})
        self.assertEqual(hourly.read(self.db,key),legacy)

    def test_readiness_blocks_buy_and_preserves_open_position_exit(self):
        f=self.f
        self.make_order('PULLBACK')
        f.trade()
        self.make_order('MOMENTUM')
        config=hourly.read(self.db,paper.AUTO_PORTFOLIOS_SETTING)
        config['ready'].remove('V3')
        config['ready'].remove('V4')
        f.write(paper.AUTO_PORTFOLIOS_SETTING,config)
        with self.assertRaisesRegex(ValueError,'PAPER_STRATEGY_NOT_READY'):
            f.trade()
        f.cfg=paper.portfolio_config(hourly.read(self.db,hourly.CONFIG),'V3')
        self.scenario.setup.advance(90)
        self.scenario.bind('V3',manage=True)
        f.args=hourly.order_command(self.db,f.info,f.result,f.now)
        self.assertEqual((f.args['side'],f.trade()['fill']['paper_portfolio']),('SELL','V3'))
        self.assertEqual(paper.ledger(self.db,'V2')['cash'],'10000.00')

    def test_single_lane_rotates_all_ready_books_and_prioritizes_a_stop(self):
        self.view('V1')
        f=self.f
        calls=[]
        timers=[]

        @contextmanager
        def connect():
            self.db.commit()
            try:
                yield self.db
            finally:
                self.db.rollback()

        def job(work,done,failed,**kwargs):
            self.assertEqual(kwargs['lane'],'trading')
            done(work())
            return True

        app=SimpleNamespace(store=SimpleNamespace(path='unused',connect=connect,
            setting=lambda k,d=None:hourly.read(self.db,k,d),
            read_cache=SimpleNamespace(token_for=lambda scopes:{})),watchlist=[f.info],ai_config={},
            closing=False,user_dirty=False,root=SimpleNamespace(after=lambda ms,fn:timers.append(ms)),job=job)
        runtime=hourly.Runtime(app)
        runtime.independent=True
        def record(app,info,values,cfg,**kwargs):
            calls.append((kwargs.get('entry_portfolio'),kwargs.get('manage_portfolio')))
        with patch.object(hourly,'run_turn',side_effect=record), \
             patch.object(hourly.long_term,'ranked_isins',return_value=([f.isin],{})), \
             patch.object(hourly.paper_v5_four_hour,'entry_window',return_value=(None,None)), \
             patch.object(hourly.time,'time',return_value=f.now):
            for _ in range(5):
                runtime.tick()
            self.assertEqual(calls,[('V5',None),('V2',None),('V3',None),('V4',None)])
            self.make_order('PULLBACK')
            f.trade()
            self.db.commit()
            runtime.tick()
            self.assertEqual(calls[-1],(None,'V3'))
        self.assertEqual(timers,[2000]*6)

