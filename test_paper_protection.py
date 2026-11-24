"""BUY/protective SELL use the real executor over isolated in-memory fixtures."""
import copy,sqlite3,unittest
from datetime import datetime, timedelta, timezone
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

    def use_strategy(self,mode,timeframe='1H'):
        from paper_hourly import CONFIG,read,set_trading_mode
        f=self.f
        f.db.commit()
        set_trading_mode(f.db,mode,timeframe)
        f.db.commit()
        f.cfg=read(f.db,CONFIG)

    def strategy_facts(self,mode, *, falling=False):
        from test_paper_strategy_v2 import engine
        e=engine()
        for context in e['timeframe_context'].values():
            context['data_quality']={'state':'READY'}
        h=e['timeframe_context']['1H']['facts']
        h.update(ema10=99.,candle_up=True,previous_rsi=40.)
        if mode=='PULLBACK':
            h.update(breakout=False,reclaim=True,histogram=-.1)
            e['timeframe_context']['4H']['facts']['regime']='BULLISH'
        elif mode=='MOMENTUM':
            h.update(breakout=False,relative_volume=1.3)
        elif mode=='AGGRESSIVE':
            h.update(breakout=False,ema10=104.,support=99.,histogram=-.3,rsi=46.)
        if falling:
            h.update(breakdown=True,histogram=-1.,histogram_delta=-.5,rsi=40.)
        target=self.f.result['execution_context']['trading_engine']
        target.update(timeframe_context=e['timeframe_context'])
        if mode=='AGGRESSIVE':
            from test_paper_strategy_v2 import opening_range
            opening_range(target)
        elif mode=='MOMENTUM' and not falling:
            self.v4_context(target)

    def v4_context(self, engine, timeframe='1H'):
        instant=datetime.fromtimestamp(self.f.now,timezone.utc)
        closed=datetime.fromisoformat(self.f.result['execution_quote']['bars'][0]['close_at'])
        engine['v4_selected_context']=dict(signal_at=engine['timestamp'],timeframe=timeframe,
            checked_at=instant.isoformat(),closed=closed.isoformat(),session_end=(instant+timedelta(hours=5)).isoformat(),
            elapsed=120,complete=True,open=99.,high=100.25,low=98.9,close=100.,atr=1.5,
            volume=150.,vol_med=100.,prev_close=99.,prev_high=99.8,open_high=99.5,open_low=98.5,
            breadth_available=4,breadth_positive=3,breadth_required=3,breadth_inputs=['A','B','C','D'])

    def test_selected_strategies_use_main_ledger_and_pin_exit_across_switch(self):
        from paper_entry_modes import STRATEGY_CODES
        f=self.f
        start=f.now
        for mode in ('V2','PULLBACK','MOMENTUM','AGGRESSIVE'):
            with self.subTest(mode=mode):
                f.now=start
                f.db.execute('DELETE FROM paper_fills')
                self.use_strategy(mode)
                self.s.prepare('WAIT')
                self.strategy_facts(mode)
                entry=self.buy()
                self.assertEqual(entry['initiator'],STRATEGY_CODES[mode])
                self.assertEqual(entry['smart_eligibility']['protection']['strategy_mode'],mode)
                self.assertEqual(fixture.paper.ledger(f.db)['positions'][0]['auto_entry']['strategy_mode'],mode)
                self.use_strategy('V1','4H')
                self.advance(96 if mode in ('AGGRESSIVE','MOMENTUM') else 100)
                self.strategy_facts(mode,falling=True)
                f.result['execution_context']['trading_engine']['timestamp']='2026-11-04 10:30:00'
                f.args=self.command()
                sell=f.trade()['fill']
                self.assertEqual(sell['initiator'],STRATEGY_CODES[mode])
                self.assertEqual(sell['timeframe'],'1H')
                self.assertEqual(len(fixture.paper.ledger(f.db)['fills']),2)
                self.assertEqual(fixture.paper.ledger(f.db)['positions'][0]['quantity'],'0')

    def test_switch_rejects_unfilled_order_based_on_previous_pair(self):
        f=self.f
        self.use_strategy('V2')
        self.s.prepare('WAIT');self.strategy_facts('V2')
        f.args=self.command()
        self.assertEqual(f.args['side'],'BUY')
        sizing=copy.deepcopy(f.cfg['sizing'])
        self.use_strategy('AGGRESSIVE','1H')
        with self.assertRaisesRegex(ValueError,'PAPER_STALE_INPUTS'):
            f.trade()
        self.assertEqual(f.cfg['sizing'],sizing)
        self.assertFalse(fixture.paper.ledger(f.db)['fills'])

    def test_v5_rejects_weak_breadth_and_thin_quote_before_buy(self):
        f=self.f
        self.use_strategy('AGGRESSIVE')
        self.s.prepare('WAIT')
        self.strategy_facts('AGGRESSIVE')
        engine=f.result['execution_context']['trading_engine']
        opening=engine['v5_opening_range']
        opening['breadth_positive']=2
        self.assertEqual(fixture.hourly.strategy_signal(f.cfg,f.result)[1]['reason'],'V5_BREADTH_TOO_WEAK')
        self.assertIsNone(self.command())
        opening['breadth_positive']=3
        f.result['execution_quote']['bars'][0]['volume']=9
        with self.assertRaisesRegex(ValueError,'V5_QUOTE_VOLUME_BELOW_SIZE'):
            self.command()
        f.result['execution_quote']['bars'][0]['volume']=20
        self.assertEqual(self.buy()['quantity'],'10')

    def test_v5_exit_at_target_or_three_hours_and_preserve_five_hour_plan(self):
        from datetime import datetime, timezone
        f=self.f
        self.use_strategy('AGGRESSIVE')
        self.s.prepare('WAIT')
        self.strategy_facts('AGGRESSIVE')
        fill=self.buy()
        receipt=fill['smart_eligibility']
        plan=receipt['protection']
        self.assertEqual((plan['hold_minutes'],receipt['entry_evidence']['breadth_positive']),(180,3))
        self.assertEqual((plan['stop'],plan['target']),('98.20','103.45'))
        self.use_strategy('V1','4H')
        self.advance(100)
        f.now+=3*3600-900
        self.s.prepare('WAIT')
        bar=f.result['execution_quote']['bars'][0]
        bar.update(open_at=datetime.fromtimestamp(f.now-900,timezone.utc).isoformat(),
                   close_at=datetime.fromtimestamp(f.now,timezone.utc).isoformat())
        self.assertEqual(self.command()['_protection']['reason'],'STRATEGY_TIME_EXIT')
        from paper_v5 import LEGACY_VERSION
        legacy=dict(plan,model=LEGACY_VERSION,hold_minutes=300)
        position=fixture.paper.ledger(f.db)['positions'][0]
        from decimal import Decimal
        self.assertIsNone(protection.strategy_trigger(f.db,position,fill,legacy,bar,Decimal(100))[0])
        bar.update(open=104,high=105,low=103,close=104)
        self.assertEqual(self.command()['_protection']['reason'],'TAKE_PROFIT')
        f.args=self.command()
        self.assertEqual(f.trade()['fill']['initiator'],'V5')

    def test_v5_buy_reversal_requires_confirmed_prior_days_and_records_reason(self):
        f=self.f
        self.use_strategy('AGGRESSIVE')
        self.s.prepare('WAIT')
        self.strategy_facts('AGGRESSIVE')
        engine=f.result['execution_context']['trading_engine']
        candle=engine['v5_opening_range']
        candle.update(number=9,open=98.9,close=100.,high=100.25,low=98.6,prev_high=99.8,
                      daily_down_closes=None)
        self.assertEqual(fixture.hourly.strategy_signal(f.cfg,f.result)[1]['reason'],'V5_REVERSAL_DAILY_UNCONFIRMED')
        candle['daily_down_closes']=1
        self.assertIsNone(self.command())
        candle['daily_down_closes']=2
        candle['volume']=105
        self.assertIsNone(self.command())
        candle['volume']=115
        quote=f.result['execution_quote']['bars'][0]
        quote['close']=99.8
        with self.assertRaisesRegex(ValueError,'PAPER_V5_REVERSAL_LEVEL_LOST'):
            self.command()
        quote['close']=100
        fill=self.buy()
        evidence=fill['smart_eligibility']['entry_evidence']
        self.assertEqual((evidence['setup'],evidence['daily_down_closes'],fill['quantity']),('DAILY_REVERSAL',2,'10'))
        self.assertEqual(evidence['entry_level'],99.8)

    def test_v5_macro_day_blocks_new_orders_but_preserves_protective_sell(self):
        from unittest.mock import patch
        from paper_v5 import MACRO_DATES
        f=self.f
        self.use_strategy('AGGRESSIVE')
        self.s.prepare('WAIT')
        self.strategy_facts('AGGRESSIVE')
        with patch.dict(MACRO_DATES,{'2026-11-04':('CPI','08:30','https://www.bls.gov/')}):
            with self.assertRaisesRegex(ValueError,'PAPER_V5_MACRO_RELEASE_DAY:CPI'):
                self.command()
            self.assertEqual(fixture.paper.ledger(f.db)['fills'],[])
        self.buy()
        self.advance(96)
        with patch.dict(MACRO_DATES,{'2026-11-04':('CPI','08:30','https://www.bls.gov/')}):
            f.args=self.command()
            fill=f.trade()['fill']
            self.assertEqual(fill['side'],'SELL')
            self.assertEqual(fill['smart_eligibility']['protective_exit']['reason'],'STOP_LOSS')

    def test_v2_partial_then_stop_share_main_ledger(self):
        f=self.f
        self.use_strategy('V2')
        self.s.prepare('WAIT');self.strategy_facts('V2')
        entry=self.buy()
        self.advance(107);self.strategy_facts('V2')
        f.args=self.command()
        self.assertEqual(f.args['_protection']['reason'],'PARTIAL_2R')
        first=f.trade()['fill']
        self.assertGreater(float(entry['quantity']),float(first['quantity']))
        self.assertTrue(f.trade()['idempotent'])
        self.assertNotEqual((self.command() or {}).get('_protection',{}).get('reason'),'PARTIAL_2R')
        self.advance(96);self.strategy_facts('V2')
        f.args=self.command();self.assertEqual(f.args['_protection']['reason'],'STOP_LOSS')
        f.trade()
        ledger=fixture.paper.ledger(f.db)
        self.assertEqual(ledger['positions'][0]['quantity'],'0')
        self.assertEqual(len(ledger['fills']),3)

    def test_v4_trailing_stop_persists_and_does_not_trigger_against_same_quote(self):
        f=self.f
        self.use_strategy('MOMENTUM')
        self.s.prepare('WAIT');self.strategy_facts('MOMENTUM')
        # Seed a fill with the previous V4 logic, then exercise its live legacy exits.
        from paper_entry_modes import decision as previous_decision
        with patch('paper_v4.decide',side_effect=lambda e,tf:previous_decision(e,'MOMENTUM',tf)):
            self.buy()
        self.advance(104);self.strategy_facts('MOMENTUM')
        e=f.result['execution_context']['trading_engine'];e['timestamp']='2026-11-04 10:30:00'
        self.assertIsNone(order_command(f.db,f.info,f.result,f.now,manage=True))
        self.assertIsNone(order_command(f.db,f.info,f.result,f.now,manage=True))
        self.assertEqual(fixture.paper.ledger(f.db)['positions'][0]['active_stop'],'102.50')
        self.advance(102);self.strategy_facts('MOMENTUM')
        f.args=self.command()
        self.assertEqual(f.args['_protection']['reason'],'STOP_LOSS')
        f.trade()
        self.assertEqual(fixture.paper.ledger(f.db)['positions'][0]['quantity'],'0')

    def test_v4_selected_30m_executes_fresh_quote_and_retains_bracket_after_switch(self):
        from decimal import Decimal
        from trading_context import plan
        from paper_v4 import VERSION
        f=self.f
        self.use_strategy('MOMENTUM','30m')
        self.s.prepare('WAIT')
        e=f.result['execution_context']['trading_engine']
        e['timeframe_context']['30m']=copy.deepcopy(e['timeframe_context']['1H'])
        contract=plan('30m')
        f.result['timeframe']='30m'
        f.result['hourly'].update(required_tf=contract['required_tf'],timeframe_contract=contract)
        for spec in f.result['hourly']['proof']['series']:
            if spec['spec']['timeframe']=='1H':
                spec['spec']['timeframe']='30m'
        self.v4_context(e,'30m')
        bar=e['v4_selected_context']
        initial=bar['closed']
        bar['closed']=(datetime.fromisoformat(initial)+timedelta(seconds=1)).isoformat()
        with self.assertRaisesRegex(ValueError,'PAPER_V4_QUOTE_BEFORE_SIGNAL'):
            self.command()
        bar['closed']=initial
        fill=self.buy()
        self.assertEqual((fill['initiator'],fill['timeframe'],fill['quantity']),('V4','30m','10'))
        bracket=fill['smart_eligibility']['protection']
        self.assertEqual((bracket['model'],bracket['hold_minutes']),(VERSION,180))
        self.use_strategy('V1','4H')
        position=fixture.paper.ledger(f.db)['positions'][0]
        self.assertEqual(fixture.hourly.turn_timeframes(f.cfg,position),('30m',))
        close_at=(datetime.fromisoformat(fill['at'])+timedelta(hours=3)).isoformat()
        self.assertEqual(protection.strategy_trigger(f.db,position,fill,bracket,{'close_at':close_at},Decimal(100))[0],
                         'STRATEGY_TIME_EXIT')

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

