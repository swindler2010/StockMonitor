"""BUY/protective SELL use the real executor over isolated in-memory fixtures."""
import copy,sqlite3,unittest
from unittest.mock import patch
from types import SimpleNamespace
import test_paper_sizing as fixture
import paper_protection as protection
from paper_hourly import order_command


class ProtectionTests(unittest.TestCase):
    def setUp(self):
        self.s=fixture.SizingTests();self.s.setUp();self.addCleanup(self.s.tearDown)
        self.f=self.s.f;self.f.cfg['protection']=copy.deepcopy(protection.POLICY)
        self.f.write(fixture.hourly.CONFIG,self.f.cfg);self.s.prepare('BUY')

    def buy(self):
        f=self.f;f.args=order_command(f.db,f.info,f.result,f.now)
        return f.trade()['fill']

    def advance(self,price=100,side='WAIT'):
        f=self.f;f.now+=900;self.s.prepare(side)
        bar=f.result['execution_quote']['bars'][0]
        from datetime import datetime,timezone
        bar.update(open=price,high=price+1,low=price-1,close=price,
            open_at=datetime.fromtimestamp(f.now-1200,timezone.utc).isoformat(),
            close_at=datetime.fromtimestamp(f.now-300,timezone.utc).isoformat())

    def command(self):
        f=self.f;return order_command(f.db,f.info,f.result,f.now)

    def test_buy_arms_exact_bracket_wait_stop_and_idempotent_sell(self):
        f=self.f;entry=self.buy();plan=entry['smart_eligibility']['protection']
        self.assertEqual(plan['stop'],'96');self.assertGreater(float(plan['target']),108)
        self.assertEqual(plan['identity'],f.identity)
        self.advance(94);f.args=self.command();self.assertEqual(f.args['_protection']['reason'],'STOP_LOSS')
        fill=f.trade()['fill'];self.assertEqual(fill['reference_price'],'94')
        self.assertEqual(fill['price'],'93.9530') # existing spread, not imaginary 96 stop fill
        self.assertEqual(fill['smart_eligibility']['signal'],'WAIT')
        self.assertTrue(f.trade()['idempotent'])
        self.assertEqual(len(fixture.paper.ledger(f.db)['fills']),2)
        self.assertEqual(fixture.paper.ledger(f.db)['positions'][0]['quantity'],'0')

    def test_target_and_core_sell_share_executor_without_duplicate(self):
        f=self.f;self.buy();self.advance(111,'SELL');f.args=self.command()
        self.assertEqual(f.args['_protection']['reason'],'TAKE_PROFIT')
        result=f.trade();self.assertEqual(result['fill']['reference_price'],'111')
        self.assertEqual(result['fill']['fee'],'1.00')
        with self.assertRaisesRegex(ValueError,'REQUEST_REUSED'):self.command()
        self.assertEqual(len(fixture.paper.ledger(f.db)['fills']),2)

    def test_no_trigger_from_intrabar_wick_or_entry_candle(self):
        self.buy();self.assertIsNone(protection.decision(self.f.db,self.f.info,self.f.result,self.f.now))
        self.advance(100);bar=self.f.result['execution_quote']['bars'][0];bar.update(high=120,low=90)
        self.assertIsNone(self.command()) # explicitly closed-price trigger, not an intrabar fill model
        self.assertEqual(len(fixture.paper.ledger(self.f.db)['fills']),1)

    def test_restart_restores_plan_from_existing_fill_and_stable_exit_id(self):
        f=self.f;self.buy();self.advance(94);before=self.command()
        clone=sqlite3.connect(':memory:');f.db.commit();f.db.backup(clone)
        try:self.assertEqual(order_command(clone,f.info,f.result,f.now),before)
        finally:clone.close()

    def test_trigger_cannot_be_forged_or_used_after_quote_changes(self):
        f=self.f;self.buy();self.advance(94);f.args=self.command()
        for key,value in (('close',100),('volume',0),('closed',False)):
            original=copy.deepcopy(f.result);f.result['execution_quote']['bars'][0][key]=value
            with self.assertRaises(ValueError):f.trade()
            f.result=original
        self.assertEqual(len(fixture.paper.ledger(f.db)['fills']),1)

    def test_dq_benchmark_seed_revisions_and_expiry_still_block_stop(self):
        f=self.f;self.buy();self.advance(94);f.args=self.command()
        original=copy.deepcopy(f.result)
        mutations=[lambda r:r['execution_context']['trading_engine'].update(status='DATA BLOCKED'),
            lambda r:r['execution_context']['trading_engine']['timeframe_context']['4H']['data_quality'].update(state='UNCONFIRMED'),
            lambda r:r['execution_context']['trading_engine'].update(benchmark_context={}),
            lambda r:r['execution_context']['trading_engine'].update(demo_initialization={'consistent':False}),
            lambda r:r['hourly'].update(expires_at='2026-11-01T00:00Z'),
            lambda r:r['_input_revisions'].update({'instrument:'+f.isin:999})]
        for mutate in mutations:
            f.result=copy.deepcopy(original);mutate(f.result)
            with self.assertRaises(ValueError):f.trade()
        self.assertEqual(len(fixture.paper.ledger(f.db)['fills']),1)

    def test_stale_quote_identity_currency_and_config_are_not_waivable(self):
        f=self.f;self.buy();self.advance(94);f.args=self.command();original=copy.deepcopy(f.result)
        mutations=[lambda r:r['execution_quote']['bars'][0].update(close_at='2026-11-04T08:00Z'),
            lambda r:r['execution_quote']['identity'].update(venue='FRA'),
            lambda r:r['identity'].update(currency='USD')]
        for mutate in mutations:
            f.result=copy.deepcopy(original);mutate(f.result)
            with self.assertRaises(ValueError):f.trade()
        f.result=original;f.cfg['protection']['target_r']='99';f.write(fixture.hourly.CONFIG,f.cfg)
        with self.assertRaises(ValueError):f.trade()
        self.assertEqual(len(fixture.paper.ledger(f.db)['fills']),1)

    def test_legacy_v1_not_retroactively_armed_and_wait_never_buys(self):
        f=self.f;f.cfg.pop('protection');f.write(fixture.hourly.CONFIG,f.cfg);self.s.prepare('BUY')
        fill=self.buy();self.assertNotIn('protection',fill['smart_eligibility'])
        f.cfg['protection']=copy.deepcopy(protection.POLICY);f.write(fixture.hourly.CONFIG,f.cfg)
        self.advance(94);self.assertIsNone(self.command())

    def test_protective_exit_cannot_trade_closed_session_or_tampered_quantity(self):
        f=self.f;self.buy();self.advance(94);f.args=self.command()
        f.args['quantity']=.5
        with self.assertRaisesRegex(ValueError,'SIZE_BLOCKED'):f.trade()
        import pandas as pd
        f.now=pd.Timestamp('2026-11-04T22:05Z').timestamp()-900
        self.advance(94);f.args=self.command()
        with self.assertRaisesRegex(ValueError,'SESSION_CLOSED'):f.trade()
        self.assertEqual(len(fixture.paper.ledger(f.db)['fills']),1)

    def test_risk_plan_supports_all_eleven_tf_without_authorizing_execution(self):
        from timeframes import LABELS
        f=self.f;size=self.s.size();base=f.result['execution_context']['trading_engine']['timeframe_context']['1H']
        for tf in LABELS:
            result=copy.deepcopy(f.result);result['timeframe']=tf
            result['execution_context']['trading_engine']['timeframe_context'][tf]=base
            plan=protection.entry_plan(f.cfg,result,size,f.now)
            self.assertEqual(plan['signal_tf'],tf)
            self.assertLessEqual(float(plan['estimated_stop_risk']),float(size['risk_budget']))
        self.assertEqual(len(fixture.paper.ledger(f.db)['fills']),0)

    def test_runtime_wait_releases_transaction_before_status_write(self):
        f=self.f;self.buy();self.advance(100);f.db.commit();writes={}
        def save(k,v):
            self.assertFalse(f.db.in_transaction);writes[k]=v
        store=SimpleNamespace(path='unused',connect=lambda: f.db,setting=lambda k,d=None:f.cfg if k==fixture.hourly.CONFIG else d,
            set_setting=save,read_cache=SimpleNamespace(token_for=lambda _:{}))
        app=SimpleNamespace(store=store,watchlist=[f.info],closing=False,user_dirty=False,ai_config={},
            root=SimpleNamespace(after=lambda *a:None),compute_trading=SimpleNamespace(calculate=lambda *a:f.result),
            job=lambda work,done,failed=None,**kw:done(work()))
        with patch.object(fixture.hourly.sqlite3,'connect',return_value=f.db),patch.object(fixture.hourly.time,'time',return_value=f.now):
            fixture.hourly.Runtime(app).tick()
        self.assertEqual(writes[fixture.hourly.STATUS+f.isin]['state'],'WAIT')

    def test_runtime_stop_works_on_wait_but_never_buys_for_test(self):
        f=self.f;self.buy();self.advance(94);f.db.commit();writes={}
        store=SimpleNamespace(path='unused',connect=lambda: f.db,setting=lambda k,d=None:f.cfg if k==fixture.hourly.CONFIG else d,
            set_setting=lambda k,v:writes.update({k:v}),read_cache=SimpleNamespace(token_for=lambda _:{}))
        app=SimpleNamespace(store=store,watchlist=[f.info],closing=False,user_dirty=False,ai_config={},
            root=SimpleNamespace(after=lambda *a:None),compute_trading=SimpleNamespace(calculate=lambda *a:f.result),
            job=lambda work,done,failed=None,**kw:done(work()))
        with patch.object(fixture.hourly.sqlite3,'connect',return_value=f.db),patch.object(fixture.hourly.time,'time',return_value=f.now):
            fixture.hourly.Runtime(app).tick()
        report=writes[fixture.hourly.STATUS+f.isin]
        self.assertEqual(report['state'],'COMPLETED',report)
        self.assertEqual(report['protective_exit']['reason'],'STOP_LOSS')
        self.assertEqual(len(fixture.paper.ledger(f.db)['fills']),2)


if __name__=='__main__':unittest.main()
