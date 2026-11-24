"""Five independent portfolios over the ordinary guarded PAPER executor."""
import copy
import sqlite3
from contextlib import closing
import unittest

import test_paper_protection as positions
import paper_account as paper
import paper_hourly as hourly
from paper_entry_modes import STRATEGY_CODES
from paper_journal import read as journal, save_note


class PortfolioTests(unittest.TestCase):
    def setUp(self):
        self.setup = positions.ProtectionTests()
        self.setup.setUp()
        self.addCleanup(self.setup.doCleanups)
        self.f=self.setup.f
        start=self.f.now
        self.setup.buy()
        self.setup.advance(90)
        self.f.args=self.setup.command()
        self.f.trade()
        self.f.now=start
        self.old = paper.operation_rows(self.f.db,paper.ACCOUNT)
        self.f.db.commit()
        paper.initialize_portfolios(self.f.db)
        self.f.db.commit()

    def prepare(self,mode, *, managing=None):
        f=self.f
        if mode=='V1':
            # The old hourly strategy still has a supported saved config.
            f.cfg=hourly.read(f.db,hourly.CONFIG)
            f.cfg[hourly.STRATEGY_KEY]=mode
            f.cfg.pop(hourly.long_term.PROFILE_KEY,None)
            f.write(hourly.CONFIG,f.cfg)
            self.setup.s.prepare('BUY')
        else:
            self.setup.use_strategy(mode)
            self.setup.s.prepare('WAIT')
            self.setup.strategy_facts(mode)
        portfolio=managing or STRATEGY_CODES[mode]
        self.bind(portfolio,manage=bool(managing))

    def bind(self,portfolio,*,manage=False):
        f=self.f
        f.result.update(paper_portfolio=portfolio,paper_manage_only=manage)
        token=f.result['_input_revisions']
        token.setdefault('setting:'+paper.portfolio_epoch_key(portfolio),0)
        f.result['hourly']['ledger_token']=hourly.ledger_token(f.db,portfolio)

    def test_all_start_equal_and_charge_the_same_stock_to_their_own_balance(self):
        f=self.f
        self.assertEqual(paper.operation_rows(f.db,paper.ACCOUNT),self.old)
        for book in paper.PORTFOLIOS:
            self.assertEqual(paper.ledger(f.db,book)['cash'],'10000.00')
        seen={}
        for mode,book in STRATEGY_CODES.items():
            with self.subTest(book=book):
                self.prepare(mode)
                fill=self.setup.legacy_v2_buy() if mode=='V2' else self.setup.buy()
                self.assertEqual(fill['paper_portfolio'],book)
                self.assertEqual(fill['initiator'],book)
                self.assertEqual(paper.ledger(f.db,book)['cash'],
                                 str(paper.money(10000)-paper.money(fill['gross'])-paper.money(fill['fee'])))
                self.assertTrue(f.trade()['idempotent'])
                seen[book]=fill['request_id']
        self.assertEqual(len(set(seen.values())),5)
        bought_at=f.now
        self.assertEqual(len(paper.maintained_positions(f.db)),5)
        self.assertEqual(paper.operation_rows(f.db,paper.ACCOUNT),self.old)
        f.db.commit()
        with closing(sqlite3.connect(':memory:')) as reopened:
            f.db.backup(reopened)
            for book,request in seen.items():
                self.assertEqual(paper.saved_fill(reopened,request)[0],book)
                self.assertEqual(len(paper.ledger(reopened,book)['fills']),1)
        for book in paper.PORTFOLIOS:
            with self.subTest(exit_portfolio=book):
                f.now=bought_at
                self.setup.advance(90)
                self.bind(book,manage=book!='V5')
                f.args=self.setup.command()
                fill=f.trade()['fill']
                self.assertEqual((fill['paper_portfolio'],fill['side']),(book,'SELL'))
                self.assertEqual(fill['smart_eligibility']['protective_exit']['reason'],'STOP_LOSS')
                self.assertEqual(paper.ledger(f.db,book)['positions'][0]['quantity'],'0')
        self.assertEqual(paper.maintained_positions(f.db),[])

    def test_reset_and_history_are_separate(self):
        f=self.f
        self.prepare('V2')
        first=self.setup.legacy_v2_buy()
        self.prepare('PULLBACK')
        second=self.setup.buy()
        save_note(f.db,first['request_id'],'Retained across reset')
        old_mode=hourly.read(f.db,hourly.CONFIG)
        result=hourly.reset(f.db,'reset-only-v3')
        self.assertEqual((result['account'],result['cash']),('V3','10000.00'))
        self.assertEqual(paper.ledger(f.db,'V2')['fills'],[first])
        self.assertEqual(hourly.read(f.db,hourly.CONFIG),old_mode)
        self.assertTrue(paper.saved_fill(f.db,second['request_id'])[3])
        view=journal(f.db,[],portfolio='V3',legacy=True)
        self.assertEqual(len(view['indices']['archive']['all']),1)
        self.assertEqual(len(view['indices']['legacy']['all']),2)
        self.assertEqual(view['entries'][view['indices']['archive']['all'][0]]['fill'],second)

    def test_pending_buy_cannot_follow_selection_and_inactive_stop_still_closes(self):
        f=self.f
        self.prepare('MOMENTUM')
        f.args=self.setup.command()
        old_result,old_args=copy.deepcopy(f.result),copy.deepcopy(f.args)
        self.prepare('PULLBACK')
        with self.assertRaisesRegex(ValueError,'PAPER_PORTFOLIO_CHANGED'):
            paper.execute(f.db,f.info,old_result,old_args,f.now)
        v3=self.setup.buy()
        self.setup.use_strategy('MOMENTUM')
        self.setup.advance(95)
        self.setup.strategy_facts('PULLBACK',falling=True)
        f.result['execution_context']['trading_engine']['timestamp']='2026-11-04 10:30:00'
        self.bind('V3',manage=True)
        f.args=self.setup.command()
        self.assertEqual(f.args['side'],'SELL')
        exit_fill=f.trade()['fill']
        self.assertEqual((exit_fill['paper_portfolio'],exit_fill['side']),('V3','SELL'))
        self.assertEqual(exit_fill['smart_eligibility']['protective_exit']['reason'],'STOP_LOSS')
        self.assertEqual(paper.ledger(f.db,'V4')['cash'],'10000.00')
        self.assertEqual(paper.ledger(f.db,'V3')['fills'][0],v3)



    def test_current_long_term_v1_and_owner_orders_are_bound_to_their_portfolio(self):
        from test_paper_v1_long_term import LongTermTests
        from paper_orders import VERSION as OWNER_ORDER

        daily=LongTermTests()
        daily.setUp()
        self.addCleanup(daily.doCleanups)
        f=daily.f
        paper.initialize_portfolios(f.db)
        f.db.commit()
        daily.prepare('2026-11-03T07:00:00+00:00',100)
        f.result['paper_portfolio']='V1'
        f.result['_input_revisions']['setting:paper_epoch:V1']=0
        f.args=daily.command()
        fill=f.trade()['fill']
        self.assertEqual((fill['paper_portfolio'],fill['timeframe']),('V1','1D'))
        self.assertEqual(fill['smart_eligibility']['strategy_model'],hourly.long_term.MODEL)

        f=self.f
        for initiator,mode,book in (('MANUAL','V2','V2'),('CHATGPT','MOMENTUM','V4')):
            with self.subTest(initiator=initiator):
                self.prepare(mode)
                intent=dict(version=OWNER_ORDER,initiator=initiator,isin=f.isin,side='BUY',
                            quantity=1,request_id=initiator+'-book',quote_tf='15m')
                f.result['discretionary']=intent
                f.args=dict(side='BUY',isin=f.isin,timeframe='1H',quantity=1,
                    request_id=intent['request_id'],_paper_portfolio=book,_initiator=initiator,
                    _quote_tf='15m',_deadline=f.now+30)
                fill=f.trade()['fill']
                self.assertEqual((fill['initiator'],fill['paper_portfolio']),(initiator,book))
                self.assertEqual(paper.ledger(f.db,book)['fills'],[fill])
