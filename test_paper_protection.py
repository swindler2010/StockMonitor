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

    def legacy_v2_buy(self):
        """Seed a position written by the retired V2 for migration tests."""
        import paper_entry_modes
        from paper_strategy_v2 import proposal
        with patch.dict(fixture.hourly.STRATEGY_TIMEFRAMES,{'V2':('1H','4H')}), \
             patch.object(paper_entry_modes,'decision',side_effect=lambda engine,mode,tf,**_:proposal(
                 engine,inherit_core=False,timeframe=tf)):
            return self.buy()

    def advance(self,price=100,side='WAIT'):
        f=self.f;f.now+=900;self.s.prepare(side)
        bar=f.result['execution_quote']['bars'][0]
        from datetime import datetime,timezone
        bar.update(open=price,high=price+1,low=price-1,close=price,
            open_at=datetime.fromtimestamp(f.now-1200,timezone.utc).isoformat(),
            close_at=datetime.fromtimestamp(f.now-300,timezone.utc).isoformat())

    def command(self):
        f=self.f;return order_command(f.db,f.info,f.result,f.now)

    def use_strategy(self,mode):
        from paper_hourly import CONFIG,read,set_trading_mode
        f=self.f
        f.db.commit()
        set_trading_mode(f.db,mode)
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
            target['v5_portfolio_context']={'steady_relative':True}
            quote=self.f.result['execution_quote']['bars'][0]
            target['v5_opening_range']['closed']=quote['close_at']
            if not falling:
                target['signal']='BUY'
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
                entry=self.legacy_v2_buy() if mode=='V2' else self.buy()
                self.assertEqual(entry['initiator'],STRATEGY_CODES[mode])
                self.assertEqual(entry['smart_eligibility']['protection']['strategy_mode'],mode)
                self.assertEqual(fixture.paper.ledger(f.db)['positions'][0]['auto_entry']['strategy_mode'],mode)
                self.use_strategy('V1')
                self.advance(96 if mode in ('V2','AGGRESSIVE','MOMENTUM') else 100)
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
        self.use_strategy('PULLBACK')
        self.s.prepare('WAIT');self.strategy_facts('PULLBACK')
        f.args=self.command()
        self.assertEqual(f.args['side'],'BUY')
        sizing=copy.deepcopy(f.cfg['sizing'])
        self.use_strategy('AGGRESSIVE')
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
        self.use_strategy('V1')
        self.advance(100)
        f.now+=3*3600-900
        self.s.prepare('WAIT')
        bar=f.result['execution_quote']['bars'][0]
        bar.update(open_at=datetime.fromtimestamp(f.now-900,timezone.utc).isoformat(),
                   close_at=datetime.fromtimestamp(f.now,timezone.utc).isoformat())
        self.assertEqual(self.command()['_protection']['reason'],'STRATEGY_TIME_EXIT')
        from paper_v5 import LEGACY_VERSION, MULTI_VERSION, PORTFOLIO_VERSION, PREVIOUS_UNIFIED_VERSION
        legacy=dict(plan,model=LEGACY_VERSION,hold_minutes=300)
        position=fixture.paper.ledger(f.db)['positions'][0]
        from decimal import Decimal
        self.assertIsNone(protection.strategy_trigger(f.db,position,fill,legacy,bar,Decimal(100))[0])
        for historic_model in (MULTI_VERSION,PORTFOLIO_VERSION,PREVIOUS_UNIFIED_VERSION):
            saved=dict(plan,model=historic_model,hold_minutes=180)
            self.assertEqual(protection.strategy_trigger(f.db,position,fill,saved,bar,Decimal(100))[0],
                             'STRATEGY_TIME_EXIT')
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
        entry=self.legacy_v2_buy()
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
        self.use_strategy('MOMENTUM')
        # Build a position under the former interval choice before restoring today's policy.
        eligibility=patch.dict(fixture.hourly.STRATEGY_TIMEFRAMES,{'MOMENTUM':('30m',)})
        eligibility.start()
        self.addCleanup(eligibility.stop)
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
        eligibility.stop()
        self.use_strategy('V1')
        position=fixture.paper.ledger(f.db)['positions'][0]
        self.assertEqual(fixture.hourly.turn_timeframes(f.cfg,position),('30m',))
        close_at=(datetime.fromisoformat(fill['at'])+timedelta(hours=3)).isoformat()
        self.assertEqual(protection.strategy_trigger(f.db,position,fill,bracket,{'close_at':close_at},Decimal(100))[0],
                         'STRATEGY_TIME_EXIT')

    def test_v5_auto_selects_4h_protects_position_and_cannot_enter_twice(self):
        from paper_v5 import UNIFIED_VERSION
        from trading_context import plan
        f=self.f
        self.use_strategy('AGGRESSIVE')
        self.s.prepare('BUY')
        context=f.result['execution_context']['trading_engine']
        contract=plan('4H')
        f.result['timeframe']='4H'
        f.result['hourly'].update(required_tf=contract['required_tf'],timeframe_contract=contract)
        self.v4_context(context,'4H')
        context['v4_selected_context'].update(elapsed=240,range_close=99.25)
        context['v5_portfolio_context']={'near52':True,'relative20':False,'quiet':True}
        one_hour=copy.deepcopy(f.result)
        one_hour['timeframe']='1H'
        before=copy.deepcopy(f.result)
        bar=context['v4_selected_context']
        bar['closed']=(datetime.fromisoformat(bar['closed'])+timedelta(seconds=1)).isoformat()
        with self.assertRaisesRegex(ValueError,'PAPER_QUOTE_BEFORE_STRATEGY_SIGNAL'):
            self.command()
        f.result=before
        price=f.result['execution_quote']['bars'][0]
        price['close']=100.64
        with self.assertRaisesRegex(ValueError,'PAPER_V5_4H_ENTRY_TOO_FAR'):
            self.command()
        price['close']=100
        hourly=fixture.hourly
        calls=[]
        def calculate(kind,values):
            if '_paper_timeframes' in values:
                calls.append(values['_paper_timeframes'])
                return {'results':{tf:one_hour if tf=='1H' else f.result if tf=='4H'
                    else {'_calculation_error':'PAPER_DATA_BLOCKED'} for tf in calls[-1]}}
            calls.append(values['timeframe'])
            return f.result
        store=SimpleNamespace(path='unused',connect=lambda:f.db,set_setting=lambda *_:None)
        app=SimpleNamespace(store=store,compute_trading=SimpleNamespace(calculate=calculate),
            closing=False,user_dirty=False)
        f.db.commit()
        with (patch.object(hourly.sqlite3,'connect',return_value=f.db),
              patch.object(hourly.time,'time',return_value=f.now),
              patch('paper_v5_context_study.capture',return_value='WAIT')):
            status=hourly.run_turn(app,f.info,{},f.cfg)
            hourly.run_turn(app,f.info,{},f.cfg)
        [fill]=fixture.paper.ledger(f.db)['fills']
        self.assertEqual((fill['initiator'],fill['timeframe'],fill['quantity']),('V5','4H','10'))
        self.assertEqual(fill['smart_eligibility']['protection']['model'],UNIFIED_VERSION)
        self.assertEqual(fill['smart_eligibility']['entry_evidence']['daily_context']['near52'],True)
        self.assertEqual(status['timeframes']['1H']['signal'],'WAIT')
        self.assertEqual(calls,[hourly.configured_timeframes(f.cfg),'4H'])

    def test_unified_v5_can_use_original_hourly_reversal_after_core_buy(self):
        from paper_v5 import UNIFIED_VERSION, decide
        f=self.f
        self.use_strategy('AGGRESSIVE')
        self.s.prepare('WAIT')
        self.strategy_facts('AGGRESSIVE')
        engine=f.result['execution_context']['trading_engine']
        bar=engine['v5_opening_range']
        bar.update(number=9,open=98.9,close=100.,high=100.25,low=98.6,
                   prev_high=99.8,daily_down_closes=2,volume=125,
                   closed=f.result['execution_quote']['bars'][0]['close_at'])
        engine['signal']='WAIT'
        self.assertEqual(fixture.hourly.strategy_signal(f.cfg,f.result)[1]['reason'],'V5_SELECTED_CORE_WAIT')
        self.assertIsNone(self.command())
        old_cfg=dict(f.cfg,v5_entry_profile='legacy')
        self.assertEqual(fixture.hourly.strategy_signal(old_cfg,f.result)[1]['reason'],'V5_SELECTED_CORE_WAIT')
        self.assertEqual(decide(engine,'1H')['setup'],'DAILY_REVERSAL')
        f.write(fixture.hourly.CONFIG,old_cfg)
        self.use_strategy('AGGRESSIVE')
        self.assertNotIn('v5_entry_profile',f.cfg)
        f.result['hourly']['config_digest']=fixture.hourly.digest(f.cfg)
        f.result['_input_revisions']=dict(f.db.execute('SELECT scope,revision FROM input_revisions'))
        engine['signal']='BUY'
        engine['v5_portfolio_context']['steady_relative']=False
        self.assertEqual(fixture.hourly.strategy_signal(f.cfg,f.result)[1]['reason'],'V5_STEADY_GROWTH_WAIT')
        self.assertIsNone(self.command())
        engine['v5_portfolio_context']['steady_relative']=True
        fill=self.buy()
        evidence=fill['smart_eligibility']
        self.assertEqual(evidence['strategy_model'],UNIFIED_VERSION)
        self.assertEqual(evidence['entry_evidence']['setup'],'DAILY_REVERSAL')

    def test_unified_v5_daily_rule_requires_verified_relative_strength_or_compression(self):
        from paper_v5 import entry_decision
        candidate=dict(action='BUY',setup='OPEN_RANGE')
        engine={'signal':'BUY'}
        with patch('paper_v5.decide',return_value=candidate):
            self.assertEqual(entry_decision(engine,'1D')['reason'],'V5_PORTFOLIO_DAILY_DATA_WAIT')
            engine['v5_portfolio_context']={'near52':True,'relative20':False,'quiet':False}
            self.assertEqual(entry_decision(engine,'1D')['reason'],'V5_PORTFOLIO_1D_STRENGTH_WAIT')
            engine['v5_portfolio_context']['quiet']=True
            self.assertEqual(entry_decision(engine,'1D')['action'],'BUY')
            engine['v5_portfolio_context'].update(relative20=True,quiet=False)
            self.assertEqual(entry_decision(engine,'1D')['action'],'BUY')

    def test_unified_v5_held_gap_requires_the_exact_quote_and_shares_the_ledger(self):
        import pandas as pd
        from paper_v5 import HELD_GAP, held_gap_window
        from performance_cache import capture_closed_inputs
        from trading_context import plan

        f = self.f
        self.use_strategy('AGGRESSIVE')
        f.db.execute("INSERT INTO series SELECT isin,source,venue,currency,symbol,'1W',"
                     "ts,open,high,low,close,volume FROM series WHERE base_tf='1D'")
        contract = plan('1D')
        signal_at = '2026-11-03 00:00:00'

        def daily_result(close_at):
            at = pd.Timestamp(close_at)
            f.now = at.timestamp()+300
            result = f.result_for('WAIT',signal_at)
            result['timeframe'] = '1D'
            result['execution_context']['trading_engine']['timeframe_context']['1W'] = dict(data_quality=dict(state='READY'))
            specs = [dict(isin=f.isin,identity=f.identity,base_tf=tf,timeframe=tf,through=at.isoformat())
                     for tf in contract['required_tf']]
            result['hourly'].update(required_tf=contract['required_tf'],timeframe_contract=contract,
                proof=capture_closed_inputs(f.db,specs,result['_input_revisions']))
            result['execution_quote']['bars'][0].update(
                open_at=(at-pd.Timedelta(minutes=15)).tz_convert('Europe/Berlin').isoformat(),
                close_at=at.isoformat())
            return result

        f.result = daily_result('2026-11-04T07:00:00Z')
        window = held_gap_window(f.identity,pd.Timestamp(f.now,unit='s',tz='UTC'))
        self.assertEqual(window['exit_at'],'2026-11-09T07:00:00+00:00')
        self.assertIsNone(held_gap_window(f.identity,pd.Timestamp('2026-11-04T07:15:00Z')))
        engine = f.result['execution_context']['trading_engine']
        gap = dict(window,signal_day='2026-11-03',signal_closed='2026-11-03T22:00:00Z',
                   signal_close=100,previous_close=97,atr=2)
        engine['v5_portfolio_context'] = dict(held_gap=gap)
        engine['status'] = 'DATA BLOCKED'
        self.assertIsNone(self.command())
        engine['status'] = 'READY'
        engine['timestamp'] = '2026-11-02 00:00:00'
        self.assertIsNone(self.command())
        engine['timestamp'] = signal_at
        bar = f.result['execution_quote']['bars'][0]
        bar['close'] = 101.1
        self.assertIsNone(self.command())
        bar['close'] = 100
        fill = self.buy()
        receipt = fill['smart_eligibility']
        self.assertEqual((fill['timeframe'],fill['quantity']),('1D','10'))
        self.assertEqual(receipt['entry_evidence']['setup'],HELD_GAP)
        self.assertEqual(receipt['protection']['time_exit_at'],window['exit_at'])
        self.assertEqual(receipt['protection']['target'],'102.50')
        self.assertIsNone(self.command())
        self.assertEqual(len(fixture.paper.ledger(f.db)['fills']),1)

        f.result = daily_result('2026-11-06T07:00:00Z')
        self.assertIsNone(self.command())
        f.result = daily_result(window['exit_at'])
        f.args = self.command()
        self.assertEqual(f.args['_protection']['reason'],'STRATEGY_TIME_EXIT')
        f.trade()
        self.assertEqual(fixture.paper.ledger(f.db)['positions'][0]['quantity'],'0')

    def test_unified_v5_steady_growth_uses_four_peers_and_excludes_recent_sessions(self):
        import pandas as pd
        from paper_v5 import steady_relative_growth

        index = pd.date_range('2026-07-01',periods=62)
        upside = pd.DataFrame({'close':[100*1.001**n for n in range(62)]},index=index)
        flat = pd.DataFrame({'close':100.0},index=index)
        peers = dict.fromkeys(('A','B','C','D'),flat)
        upside.loc[index[-6:],'close'] = 10
        self.assertIs(steady_relative_growth(upside,peers),True)
        self.assertIs(steady_relative_growth(flat,peers),False)
        peers.pop('D')
        self.assertIsNone(steady_relative_growth(upside,peers))

    def test_opening_gap_candidates_get_an_existing_trading_turn(self):
        import pandas as pd
        from paper_hourly import Runtime

        f = self.f
        self.use_strategy('AGGRESSIVE')
        current = {}

        def job(fn, done, *_args, **kwargs):
            self.assertEqual(kwargs['lane'],'trading')
            done(fn())
            return True

        app = SimpleNamespace(job=job,store=SimpleNamespace(path='unused',setting=lambda *_:current))
        runtime = Runtime(app)
        at = lambda time:pd.Timestamp(f'2026-11-04T{time}Z').timestamp()
        ids = list(f.cfg['identities'])
        with patch('paper_v5.opening_gap_candidates',return_value=[f.isin]) as scan:
            runtime.opening_priority(f.cfg,ids,at('06:45'))
            scan.assert_called_once()
        self.assertEqual(runtime.opening_priority(f.cfg,ids,at('06:59')),(None,True))
        self.assertEqual(runtime.opening_priority(f.cfg,ids,at('07:02')),(f.isin,False))
        current['timeframes']={'1H':dict(state='COMPLETED',signal='BUY',at=at('07:02'))}
        self.assertEqual(runtime.opening_priority(f.cfg,ids,at('07:03')),(None,False))
        self.assertEqual(runtime.opening_priority(f.cfg,ids,at('07:15')),(None,False))

    def test_unified_v5_ranks_two_stocks_on_same_quote_and_rechecks_ledger(self):
        from paper_hourly import V5EntryQueue
        from paper_v5 import UNIFIED_VERSION
        f=self.f
        first = f.isin, f.identity, f.info
        second_isin='US5949181045'
        second_identity=dict(f.identity,symbol='MSFT.GTX')
        second_info=dict(f.info,isin=second_isin,name='Microsoft')
        import json
        f.db.execute('INSERT INTO instruments VALUES (?,?)',(second_isin,json.dumps(second_info)))
        f.db.execute("""INSERT INTO series SELECT ?,source,venue,currency,?,base_tf,ts,open,high,low,close,volume
                        FROM series WHERE isin=?""",(second_isin,second_identity['symbol'],first[0]))
        f.cfg['identities'][second_isin]=second_identity
        f.write(fixture.hourly.CONFIG,f.cfg)
        f.write('gettex_chart:'+second_isin+':15m',dict(state='SAVED',identity=second_identity,
            listing_proof=dict(second_identity,status='CONFIRMED',isin=second_isin)))
        self.use_strategy('AGGRESSIVE')
        queue=V5EntryQueue()
        for isin,identity,info in (first,(second_isin,second_identity,second_info)):
            f.isin,f.identity,f.info=isin,identity,info
            f.result=f.result_for('BUY')
            engine=f.result['execution_context']['trading_engine']
            self.strategy_facts('AGGRESSIVE')
            engine['v5_opening_range']['closed']=f.result['execution_quote']['bars'][-1]['close_at']
            engine['v5_portfolio_context']={'near52':isin==second_isin,'relative20':False,'steady_relative':True}
            _,signal=fixture.hourly.strategy_signal(f.cfg,f.result)
            self.assertEqual(signal['model'],UNIFIED_VERSION)
            f.write(fixture.hourly.STATUS+isin,dict(next_check=f.now+60,
                timeframes={'1H':dict(fixture.hourly.compact(f.result),state='WAIT',signal='BUY',timeframe='1H',
                                      execution_blocker='PAPER_V5_RANKING_PENDING')}))
            queue.add(info,f.result,signal,fixture.hourly.configured_timeframes(f.cfg))
        f.isin,f.identity,f.info=first
        f.db.commit()
        app=SimpleNamespace(store=SimpleNamespace(path='unused'),closing=False,user_dirty=False)
        with (patch.object(fixture.hourly.sqlite3,'connect',return_value=f.db),
              patch.object(fixture.hourly.time,'time',return_value=f.now)):
            queue.flush(app)
        fills=[json.loads(row[0]) for row in f.db.execute('SELECT payload FROM paper_fills ORDER BY rowid')]
        self.assertEqual([fill['isin'] for fill in fills],[second_isin,first[0]])
        self.assertEqual([fill['quantity'] for fill in fills],['10','10'])
        self.assertFalse(queue.offers)

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

    def test_v5_weekly_trend_uses_the_last_week_before_the_4h_close(self):
        from contextlib import contextmanager
        import pandas as pd
        import paper_v5_four_hour as model
        from paper_v5 import peer_input_current
        from timeframes import schedule

        f = self.f
        @contextmanager
        def connect():
            yield f.db

        dependencies = set()
        store = SimpleNamespace(connect=connect, read_cache=SimpleNamespace(depend=dependencies.add))
        sessions = schedule({**f.identity, 'timeframe': '1D'}, '2026-01-01', '2026-11-06')
        f.write('gettex_chart:'+f.isin+':1D', dict(identity=f.identity, state='SAVED',
            listing_proof=dict(f.identity, status='CONFIRMED', isin=f.isin)))
        f.db.execute("DELETE FROM series WHERE base_tf='1D'")
        prices = []
        for number, day in enumerate(sessions.index):
            price = 100 + number if day <= pd.Timestamp('2026-10-30') else 10
            prices.append((f.isin, f.identity['source'], f.identity['venue'], 'EUR',
                f.identity['symbol'], '1D', str(day), price, price+1, price-1, price, 20))
        f.db.executemany('INSERT INTO series VALUES (?,?,?,?,?,?,?,?,?,?,?,?)', prices)

        evidence = {}
        at = pd.Timestamp('2026-11-06T22:00:00Z')
        trend = model.weekly_context(store, f.db, f.info, f.identity, at, evidence)
        self.assertTrue(trend['rising'])
        self.assertEqual(trend['closed_at'], '2026-10-30T22:00:00+00:00')
        scope = 'market:'+f.isin+':1D'
        self.assertIn(scope, dependencies)
        self.assertIn('setting:gettex_chart:'+f.isin+':1D', dependencies)
        f.db.execute("UPDATE series SET close=11 WHERE base_tf='1D' AND ts LIKE '2026-11-06%'")
        self.assertTrue(peer_input_current(f.db, evidence[scope]))
        current = model.weekly_context(store, f.db, f.info, f.identity,
                                       at+pd.Timedelta(seconds=1), {}, include_recovery=True)
        self.assertFalse(current['rising'])
        self.assertEqual(current['daily_recovery']['day'], '2026-11-05')
        f.db.execute("DELETE FROM series WHERE base_tf='1D' AND ts LIKE '2026-10-28%'")
        self.assertFalse(peer_input_current(f.db, evidence[scope]))
        with patch('paper_v4.intraday') as intraday:
            now = pd.Timestamp('2026-11-09T06:50:00Z')
            result = model.context(store, f.info, f.identity, '2026-11-06 19:30:00', now, {}, {})
        self.assertEqual(result, {'reason': model.WEEKLY_UNAVAILABLE})
        intraday.assert_not_called()

    def test_v5_recent_listing_uses_ema20_only_with_continuous_verified_history(self):
        import pandas as pd
        import paper_v5_four_hour as model
        from timeframes import schedule

        listing = '2026-06-12'
        meta = dict(self.f.identity, timeframe='1D')
        sessions = schedule(meta, '2026-06-08', '2026-11-20')
        rising = pd.Series(range(100, 100+len(sessions)), index=sessions.index, dtype=float)
        self.assertIsNone(model.weekly_trend(sessions, rising))
        short = model.weekly_trend(sessions, rising, listing)
        self.assertTrue(short['rising'])
        self.assertEqual(short['rule'], model.RECENT_LISTING_RULE)
        self.assertEqual(short['complete_weeks'], 23)
        self.assertFalse(model.weekly_trend(sessions, -rising+500, listing)['rising'])
        missing = rising.drop(pd.Timestamp('2026-08-11'))
        self.assertIsNone(model.weekly_trend(sessions, missing, listing))

        mature = schedule(meta, '2026-04-06', '2026-11-20')
        prices = pd.Series(range(100, 100+len(mature)), index=mature.index, dtype=float)
        self.assertEqual(model.weekly_trend(mature, prices, '2026-04-06')['rule'], model.WEEKLY_RULE)
        self.assertIsNone(model.weekly_trend(mature, rising, '2026-04-06'))

    def test_v5_weekly_direction_needs_two_defined_slow_ema_values(self):
        import pandas as pd
        import paper_v5_four_hour as model
        from timeframes import schedule

        meta = dict(self.f.identity, timeframe='1D')
        sessions = schedule(meta, '2026-04-20', '2026-11-20')
        prices = pd.Series(range(100, 100+len(sessions)), index=sessions.index, dtype=float)
        weeks = sessions.index.to_period('W-FRI')
        only_thirty = sessions.loc[weeks >= weeks.unique()[-30]]
        self.assertIsNone(model.weekly_trend(only_thirty, prices))
        self.assertTrue(model.weekly_trend(sessions, prices)['rising'])

    def test_v5_daily_recovery_requires_a_breakout_and_nonfalling_sma(self):
        import pandas as pd
        import paper_v5_four_hour as model

        dates = pd.bdate_range('2026-10-01', periods=25)
        daily = pd.Series([100.]*20+[99.,100.,100.,101.,106.], index=dates)
        recovery = model.daily_recovery(daily)
        self.assertTrue(recovery['confirmed'])
        self.assertEqual(recovery['previous_five_high'], 101.)
        self.assertIsNone(model.daily_recovery(daily.mask(daily.index==dates[-3])))
        stalled = daily.copy()
        stalled.iloc[-1] = 101.
        self.assertFalse(model.daily_recovery(stalled)['confirmed'])
        falling = pd.Series([110.]*20+[100.,101.,102.,103.,104.], index=dates)
        self.assertFalse(model.daily_recovery(falling)['confirmed'])

    def test_v5_weighted_price_uses_only_previous_eight_trading_quarters(self):
        import pandas as pd
        import paper_v5_four_hour as model
        from timeframes import schedule

        meta = dict(self.f.identity, timeframe='1D')
        cal = schedule(meta, '2026-11-06', '2026-11-09')
        previous = pd.date_range('2026-11-06T20:00Z',periods=8,freq='15min')
        opening = cal.open.iloc[-1]
        raw = pd.DataFrame(dict(close=[100,105,103,101,102,107,100,111],
                                volume=[1,0,2,0,5,1,1,2]), index=previous)
        raw.loc[opening-model.QUARTER] = [999,100]
        raw.loc[opening] = [999,100]
        evidence = model.weighted_entry_context(cal,raw,opening)
        self.assertEqual(evidence['positive_bars'],6)
        self.assertEqual(evidence['price'],'103.75')
        self.assertEqual(evidence['last_open'],'2026-11-06T21:45:00+00:00')
        self.assertIsNone(model.weighted_entry_context(cal,raw.drop(previous[-1]),opening))

    def test_v5_four_hour_screens_wait_but_buy_and_held_positions_use_core(self):
        import pandas as pd
        import paper_v5_four_hour as model

        f,hourly=self.f,fixture.hourly
        self.use_strategy('AGGRESSIVE')
        hourly.activate_v5_four_hour(f.db)
        f.db.commit()
        f.cfg=hourly.read(f.db,hourly.CONFIG)
        clock=pd.Timestamp('2026-11-04T06:50:00Z')
        f.now=clock.timestamp()
        self.s.prepare('WAIT','2026-11-03 19:30:00')
        quote=f.result['execution_quote']
        quote['bars'][0].update(open_at='2026-11-04T06:30:00Z',close_at='2026-11-04T06:45:00Z')
        engine=f.result['execution_context']['trading_engine']
        evidence=dict(setup=model.SETUP,atr=2.,entry_level=99.,relative_volume=1.4,
            signal_closed='2026-11-03T22:00:00Z',opening='2026-11-04T06:30:00Z',
            quote_end='2026-11-04T06:45:00Z',entry_rule=model.ENTRY_RULE,
            weekly_trend=dict(rule=model.WEEKLY_RULE,rising=True),
            weighted_entry=dict(price='100',positive_bars=8))
        store=SimpleNamespace(connect=lambda:f.db,read_cache=SimpleNamespace(depend=lambda _:None),
                              set_setting=lambda *_:None)
        request=dict(watchlist=[f.info],timeframe='4H',_paper_v5_screen=True)
        calculation=dict(engine=engine,proof=f.result['hourly']['proof'],manual=None)

        with (patch.object(hourly,'fresh_quote',return_value=quote),
              patch.object(model,'closed_setup',return_value=evidence),
              patch('trading_context.calculate',return_value=calculation) as core):
            rejected=hourly.calculate(store,request,now=clock)
            self.assertTrue(rejected['_v5_entry_screen'])
            self.assertNotIn('hourly',rejected)
            self.assertEqual(rejected['execution_context']['trading_engine']['reason'],model.WEIGHTED_PRICE_WAIT)
            core.assert_not_called()

            app=SimpleNamespace(store=store,closing=False,user_dirty=False,
                compute_trading=SimpleNamespace(calculate=lambda kind,values:hourly.calculate(store,values,now=clock)))
            with patch.object(hourly,'order_command') as command:
                status=hourly.run_turn(app,f.info,request,f.cfg)
            command.assert_not_called()
            self.assertEqual((status['state'],status['signal'],status['reason']),('WAIT','WAIT',model.WEIGHTED_PRICE_WAIT))

            evidence['weighted_entry']['price']='99.50'
            candidate=hourly.calculate(store,request,now=clock)
            self.assertNotIn('_v5_entry_screen',candidate)
            self.assertEqual(hourly.strategy_signal(f.cfg,candidate)[1]['action'],'BUY')
            core.assert_called_once()

            position=dict(isin=f.isin,quantity='10')
            with (patch.object(fixture.paper,'ledger',return_value={'positions':[position]}),
                  patch.object(hourly,'screen_v5_entry') as screen):
                hourly.calculate(store,request,now=clock)
                screen.assert_not_called()
                self.assertEqual(core.call_args.args[4],position)

    def test_v5_four_hour_is_independent_and_uses_first_quote_and_bound_time_exit(self):
        import pandas as pd
        import paper_v5_four_hour as model
        from trading_context import plan

        f, hourly = self.f, fixture.hourly
        self.use_strategy('AGGRESSIVE')
        hourly.activate_v5_four_hour(f.db)
        f.db.commit()
        f.cfg = hourly.read(f.db, hourly.CONFIG)
        self.assertEqual(hourly.configured_timeframes(f.cfg), ('4H',))
        f.now = pd.Timestamp('2026-11-04T06:50:00Z').timestamp()
        self.s.prepare('WAIT', '2026-11-03 19:30:00')
        f.result['timeframe'] = '4H'
        contract = plan('4H')
        f.result['hourly'].update(required_tf=contract['required_tf'], timeframe_contract=contract)
        engine = f.result['execution_context']['trading_engine']
        engine['v5_four_hour'] = dict(setup=model.SETUP, atr=2.0, entry_level=99., relative_volume=1.4,
            signal_closed='2026-11-03T22:00:00Z', opening='2026-11-04T06:30:00Z',
            quote_end='2026-11-04T06:45:00Z')
        self.assertEqual(hourly.strategy_signal(f.cfg, f.result)[1]['reason'], model.WEEKLY_UNAVAILABLE)
        trend = dict(rule=model.WEEKLY_RULE, rising=True, closed_at='2026-10-30T22:00:00Z',
                     close=101., ema10=100., ema30=99.)
        engine['v5_four_hour']['weekly_trend'] = trend
        engine['v5_four_hour']['entry_rule'] = model.ENTRY_RULE
        quote = f.result['execution_quote']['bars'][-1]
        quote.update(open_at='2026-11-03T21:45:00Z', close_at='2026-11-03T22:00:00Z')
        self.assertEqual(hourly.strategy_signal(f.cfg, f.result)[1]['reason'], 'V5_4H_WAIT_FIRST_QUOTE')
        quote.update(open_at='2026-11-04T06:30:00Z', close_at='2026-11-04T06:45:00Z', close=99.)
        self.assertEqual(hourly.strategy_signal(f.cfg, f.result)[1]['reason'], 'PAPER_V5_ENTRY_LEVEL_LOST')
        quote['close'] = 100.
        self.assertEqual(hourly.strategy_signal(f.cfg, f.result)[1]['reason'], model.WEIGHTED_UNAVAILABLE)
        weight = dict(price='100',positive_bars=8,first_open='2026-11-03T20:00:00Z',
                      last_open='2026-11-03T21:45:00Z')
        engine['v5_four_hour']['weighted_entry'] = weight
        self.assertEqual(hourly.strategy_signal(f.cfg, f.result)[1]['reason'], model.WEIGHTED_PRICE_WAIT)
        trend['rising'] = False
        self.assertEqual(hourly.strategy_signal(f.cfg, f.result)[1]['reason'], model.RECOVERY_UNAVAILABLE)
        trend['daily_recovery'] = dict(confirmed=False, day='2026-11-02')
        self.assertEqual(hourly.strategy_signal(f.cfg, f.result)[1]['reason'], model.WEEKLY_NOT_RISING)
        trend['daily_recovery']['confirmed'] = True
        weight['price'] = '99.50'
        entry = self.buy()
        protection_plan = entry['smart_eligibility']['protection']
        journal = entry['smart_eligibility']['entry_evidence']
        self.assertEqual(journal['weekly_trend'], trend)
        self.assertEqual(journal['weighted_entry'], weight)
        self.assertEqual(journal['entry_rule'], model.ENTRY_RULE)
        self.assertEqual((entry['timeframe'],entry['quantity'],entry['smart_eligibility']['strategy_model']),
                         ('4H', '10', model.MODEL))
        self.assertEqual((float(protection_plan['stop']), float(protection_plan['target']),
                          protection_plan['hold_minutes']), (97.6, 106.0, 720))
        self.assertTrue(f.trade()['idempotent'])
        self.use_strategy('V1')
        for reason, closed, price in (
                ('STOP_LOSS','2026-11-04T07:00:00Z',96.),
                ('TAKE_PROFIT','2026-11-04T07:00:00Z',107.),
                ('STRATEGY_TIME_EXIT','2026-11-04T18:45:00Z',100.)):
            with self.subTest(reason=reason):
                closed = pd.Timestamp(closed)
                f.now = (closed+pd.Timedelta(minutes=2)).timestamp()
                self.s.prepare('WAIT', '2026-11-04 15:30:00')
                f.result['timeframe'] = '4H'
                f.result['hourly'].update(required_tf=contract['required_tf'], timeframe_contract=contract)
                quote = f.result['execution_quote']['bars'][-1]
                quote.update(open_at=(closed-model.QUARTER).isoformat(),close_at=closed.isoformat(),
                             open=price,high=price+1,low=price-1,close=price)
                f.db.execute('SAVEPOINT v5_exit')
                try:
                    f.args = self.command()
                    self.assertEqual(f.args['_protection']['reason'],reason)
                    close = f.trade()['fill']
                    self.assertEqual((close['side'],close['quantity'],close['timeframe'],close['initiator']),
                                     ('SELL','10','4H','V5'))
                    self.assertTrue(f.trade()['idempotent'])
                    self.assertEqual(fixture.paper.ledger(f.db)['positions'][0]['quantity'],'0')
                finally:
                    f.db.execute('ROLLBACK TO v5_exit')
                    f.db.execute('RELEASE v5_exit')

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
