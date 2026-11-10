"""New quantity policy only; no live account or source requests."""
import copy,json,unittest
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch
from test_paper_hourly import HourlyTests, hourly, paper


class SizingTests(unittest.TestCase):
    def setUp(self):
        self.f=HourlyTests();self.f.setUp();f=self.f
        f.cfg.pop('quantity');f.cfg.pop('max_position')
        f.cfg.update(sizing=dict(version=hourly.SIZING,risk_fraction='0.01',allocation_fraction='0.45'),target_equity='20000.00')
        f.write(hourly.CONFIG,f.cfg)
        self.prepare('BUY')

    def tearDown(self):self.f.tearDown()

    def prepare(self,side,stamp='2026-11-04 09:30:00'):
        f=self.f;f.result=f.result_for(side,stamp);f.args=f.args_for(side)
        f.result['execution_context']['trading_engine']['timeframe_context']['1H']['facts']={'atr':2,'support':99}

    def size(self,side='BUY'):
        f=self.f
        return hourly.order_size(f.db,f.info,f.result,side,f.now)

    def test_dynamic_buy_full_sell_and_roundtrip_costs(self):
        f=self.f;plan=self.size();self.assertEqual(plan['quantity'],23)
        self.assertLessEqual(Decimal(plan['estimated_risk']),Decimal('100'))
        f.args['quantity']=23;fill=f.trade()['fill'];self.assertEqual(paper.ledger(f.db)['cash'],'7697.85')
        self.assertEqual(fill['smart_eligibility']['sizing'],plan)
        self.assertTrue(f.trade()['idempotent'])
        f.now+=3600;self.prepare('SELL','2026-11-04 10:30:00')
        f.result['execution_quote']['bars'][0]['close_at']='2026-11-04T10:30:00Z'
        del f.result['execution_context']['trading_engine']['timeframe_context']['1H']['facts']
        f.args['quantity']=self.size('SELL')['quantity'];self.assertEqual(f.args['quantity'],23)
        f.trade();state=paper.ledger(f.db)
        self.assertEqual(state['cash'],'9995.70');self.assertEqual(state['realized'],'-4.30')
        self.assertEqual(state['positions'][0]['quantity'],'0')

    def test_allocation_commission_and_no_averaging(self):
        f=self.f;facts=f.result['execution_context']['trading_engine']['timeframe_context']['1H']['facts']
        facts.update(atr=.01,support=100)
        plan=self.size();self.assertEqual(plan['quantity'],44)
        self.assertLessEqual(Decimal(plan['gross_and_fee']),Decimal('4500'))
        f.args['quantity']=plan['quantity'];f.trade()
        self.prepare('BUY','2026-11-04 10:30:00')
        with self.assertRaisesRegex(ValueError,'POSITION_LIMIT'):self.size()

    def test_bad_volatility_and_unaffordable_quote(self):
        f=self.f;facts=f.result['execution_context']['trading_engine']['timeframe_context']['1H']['facts']
        for bad in (0,-1,float('nan'),float('inf'),None):
            with self.subTest(bad=bad):
                facts['atr']=bad
                with self.assertRaisesRegex(ValueError,'SIZING_UNCONFIRMED'):self.size()
        facts['atr']=2
        f.result['execution_quote']['bars'][0].update(open=100000,high=100002,low=99999,close=100000)
        with self.assertRaisesRegex(ValueError,'SIZE_BELOW_ONE'):self.size()

    def test_quantity_tampering_and_signal_guards(self):
        f=self.f
        for bad in (1,24,23.5):
            f.args['quantity']=bad;f.blocked('SIZE_BLOCKED')
        f.args['quantity']=23
        f.result['execution_context']['trading_engine']['signal']='WAIT';f.blocked('SIGNAL_MISMATCH')
        f.result['execution_context']['trading_engine']['signal']='BUY'
        f.result['execution_context']['trading_engine']['status']='DATA BLOCKED';f.blocked('ENGINE_DATA_BLOCKED')

    def test_stale_invalid_identity_and_changed_revision_block(self):
        f=self.f;f.args['quantity']=23;bar=f.result['execution_quote']['bars'][0]
        bar['close_at']='2026-11-04T08:30:00Z';f.blocked('STALE_QUOTE')
        bar['close_at']='2026-11-04T09:30:00Z';bar['volume']=0;f.blocked('BAD_OHLCV')
        bar['volume']=20;f.result['execution_quote']['identity']['venue']='FRA';f.blocked('IDENTITY_BLOCKED')
        f.result['execution_quote']['identity']['venue']='GETTEX'
        f.db.execute("UPDATE series SET close=100 WHERE base_tf='1H'");f.blocked('STALE_INPUTS')

    def test_unauthorized_sizing_policy_and_sell_without_holding(self):
        f=self.f
        with self.assertRaisesRegex(ValueError,'INSUFFICIENT_POSITION'):self.size('SELL')
        f.cfg['sizing']['risk_fraction']='1.0';f.write(hourly.CONFIG,f.cfg)
        with self.assertRaisesRegex(ValueError,'NOT_AUTHORIZED'):self.size()

    def test_runtime_uses_variable_quantity_and_deduplicates(self):
        f=self.f;writes={}
        f.db.commit()  # Runtime opens its own write transaction, as in the application.
        store=SimpleNamespace(path='unused',setting=lambda k,d=None:f.cfg if k==hourly.CONFIG else writes.get(k,d),
            set_setting=lambda k,v:writes.update({k:v}),read_cache=SimpleNamespace(token_for=lambda _:{}))
        app=SimpleNamespace(store=store,watchlist=[f.info],closing=False,user_dirty=False,ai_config={},
            root=SimpleNamespace(after=lambda *args:None),compute_feed=SimpleNamespace(calculate=lambda *args:copy.deepcopy(f.result)))
        app.job=lambda work,done,failed=None,**kwargs:done(work())
        with patch.object(hourly.sqlite3,'connect',return_value=f.db),patch.object(hourly.time,'time',return_value=f.now):
            runtime=hourly.Runtime(app);runtime.tick()
            report=writes[hourly.STATUS+f.isin];self.assertEqual(report['state'],'COMPLETED',report)
            self.assertEqual(report['quantity'],23)
            writes[hourly.STATUS+f.isin]['next_check']=0;runtime.tick()
        self.assertTrue(writes[hourly.STATUS+f.isin]['idempotent'])
        self.assertEqual(len(paper.ledger(f.db)['fills']),1)

    def test_target_and_size_display_without_promise(self):
        from paper_view import automation_summary
        text=automation_summary(dict(automation=self.f.cfg,equity='10000.00'))
        self.assertIn('20000.00',text);self.assertIn('количество автоматически',text)
        self.assertIn('не гарантируются',text);self.assertNotIn('максимум по 1',text)
        text=automation_summary(dict(automation=self.f.cfg,equity=None))
        self.assertIn('оценка счёта недоступна',text)


if __name__=='__main__':unittest.main(verbosity=2)
