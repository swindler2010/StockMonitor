"""Targeted frozen V2 contracts and the real isolated PAPER transaction path."""
import copy
import hashlib
import json
import sqlite3
import unittest
from decimal import Decimal
from unittest.mock import patch
import test_paper_hourly as fixtures
import pandas as pd
import numpy as np
import paper_account as paper
import paper_hourly as hourly
import paper_v2_30m as v2
import paper_v2_30m_features as features
from performance_cache import capture_closed_inputs
from trading_context import plan
from trading_engine import profile


def artifact(code,now):
    week=pd.Timestamp(now,unit='s',tz='UTC').tz_convert('Europe/Berlin').tz_localize(None).to_period('W-SUN').start_time
    fit=dict(center=[0.]*25,scale=[1.]*25,coeff=[6.]+[0.]*54,
        groups=[dict(isin=code,entry=x,values=[[0.,0.,0.,0.,1.],[0.,0.,0.,0.,1.]]) for x in (*features.ENTRIES,'false_break')],
        serialization_parity=True,training_rows=120)
    model=dict(schema=1,model=v2.MODEL,week_start=str(week.date()),training_end=str((week-pd.Timedelta(days=3)).date()),
        features=list(features.FEATURES),interim=list(features.INTERIM),entries=list(features.ENTRIES),
        fee=2.,half_spread=.001,models=[dict(step30=fit,lock75=fit)]*2)
    model['artifact_sha256']=hashlib.sha256(v2.canonical(model).encode()).hexdigest()
    return model


class V2Transactions(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.HourlyTests();self.f.setUp();self.addCleanup(self.f.tearDown)
        f=self.f;f.now=pd.Timestamp('2026-11-04T10:05:00Z').timestamp()
        paper.initialize_portfolios(f.db);paper.initialize_independent_automation(f.db)
        f.cfg.update(strategy_mode='V2',target_equity='20000.00',
            sizing=dict(version=hourly.SIZING,allocation_fraction='0.45',risk_fraction='0.01'))
        f.cfg.pop('quantity');f.cfg.pop('max_position')
        f.write(hourly.CONFIG,f.cfg)
        for ts in pd.date_range('2026-09-01 07:30',periods=40,freq='D'):
            f.db.execute('INSERT INTO series VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                (f.isin,'gettex_chart','GETTEX','EUR','AAPL.GTX','30m',str(ts),100,102,99,101,20))
        self.model=artifact(f.isin,f.now)
        v2.activate(f.db,self.model,dict(passed=True,model=v2.MODEL,artifact_sha256=self.model['artifact_sha256']),pd.Timestamp(f.now,unit='s',tz='UTC'))
        self.refresh()

    def refresh(self,price=100.,quote_end='2026-11-04T10:00:00+00:00',action='BUY'):
        f=self.f
        f.result=f.result_for('WAIT')
        f.result.update(timeframe='30m',paper_portfolio='V2')
        engine=f.result['execution_context']['trading_engine']
        engine['timeframe_context']['30m']=dict(data_quality=dict(state='READY'))
        engine['v2_30m']=dict(action=action,reason='V2_30M_FROZEN_ENTRY',model=v2.MODEL,
            artifact_sha256=self.model['artifact_sha256'],cohort=[f.isin],setup='recovery',
            exit_rule='lock75',atr=2.,anchor=99.4,stop=97.7,target=108.625,quote=price,
            quote_end=quote_end,signal_closed='2026-11-04T09:30:00+00:00',score=6.,personal_days=2,
            hold_minutes=75)
        engine['timestamp']='2026-11-04T09:30:00+00:00'
        h=f.result['hourly'];h['config_digest']=hourly.digest(paper.portfolio_config(f.cfg,'V2'))
        h['ledger_token']=hourly.ledger_token(f.db,'V2');h['timeframe_contract']=plan('30m')
        h['required_tf']=list(profile('30m'))
        token=dict(f.db.execute('SELECT scope,revision FROM input_revisions'))
        token.setdefault('setting:'+paper.portfolio_epoch_key('V2'),0)
        f.result['_input_revisions']=token
        specs=[dict(isin=f.isin,identity=f.identity,base_tf=t,timeframe=t,through='2026-11-04T09:30:00Z') for t in profile('30m')]
        h['proof']=capture_closed_inputs(f.db,specs,token)
        bar=f.result['execution_quote']['bars'][0]
        bar.update(open=price,high=price+1,low=price-1,close=price,volume=20,
            open_at=(pd.Timestamp(quote_end)-pd.Timedelta(minutes=15)).isoformat(),close_at=quote_end)

    def buy(self):
        f=self.f;args=hourly.order_command(f.db,f.info,f.result,f.now,manage=True)
        self.assertIsNotNone(args)
        return paper.execute(f.db,f.info,f.result,args,f.now),args

    def test_actual_buy_sell_costs_idempotence_and_restart_isolation(self):
        f=self.f
        before=paper.ledger(f.db,'V5')
        answer,args=self.buy();fill=answer['fill']
        self.assertEqual((fill['side'],fill['paper_portfolio'],fill['fee'],fill['spread_bps']),('BUY','V2','2.00','20'))
        self.assertTrue(fill['smart_eligibility']['protection']['stop'])
        self.assertTrue(paper.execute(f.db,f.info,f.result,args,f.now)['idempotent'])
        f.db.commit()
        clone=sqlite3.connect(':memory:');f.db.backup(clone)
        self.assertEqual(paper.ledger(clone,'V2')['fills'],paper.ledger(f.db,'V2')['fills']);clone.close()
        self.assertEqual(paper.ledger(f.db,'V5'),before)
        f.now+=900;self.refresh(96.,'2026-11-04T10:15:00+00:00',action='HOLD')
        args=hourly.order_command(f.db,f.info,f.result,f.now,manage=True)
        self.assertEqual(args['side'],'SELL')
        sell=paper.execute(f.db,f.info,f.result,args,f.now)['fill']
        self.assertEqual(sell['smart_eligibility']['protective_exit']['reason'],'STOP_LOSS')
        self.assertEqual(sell['fee'],'2.00')
        self.assertEqual(paper.ledger(f.db,'V2')['positions'][0]['quantity'],'0')
        self.assertEqual(paper.ledger(f.db,'V5'),before)

    def test_wait_never_generates_order(self):
        self.refresh(action='WAIT')
        self.assertIsNone(hourly.order_command(self.f.db,self.f.info,self.f.result,self.f.now,manage=True))

    def official(self):
        import paper_v2_quotes as sources
        f=self.f;bar=f.result['execution_quote']['bars'][0]
        stamp=str(pd.Timestamp(bar['open_at']).tz_convert('Europe/Berlin').tz_localize(None))
        f.db.execute('INSERT INTO candles VALUES (?,?,?,?,?,?,?,?,?)',
            (f.isin,'gettex','15m',stamp,*(bar[k] for k in ('open','high','low','close','volume'))))
        q=f.result['execution_quote'];q['identity']=sources.identity(f.info)
        q['data_quality']['identity']=q['identity'].copy()
        f.result['_input_revisions'].update(dict(f.db.execute('SELECT scope,revision FROM input_revisions')))

    def test_official_quote_actual_buy_sell_retains_origin_and_canonical_position(self):
        f=self.f;before=paper.ledger(f.db,'V5');self.official()
        fill,_=self.buy()
        self.assertEqual(fill['fill']['execution_identity']['source'],'gettex')
        self.assertEqual(fill['fill']['identity'],f.identity)
        self.assertEqual(paper.ledger(f.db,'V2')['positions'][0]['identity'],f.identity)
        f.now+=900;self.refresh(96.,'2026-11-04T10:15:00+00:00',action='HOLD');self.official()
        args=hourly.order_command(f.db,f.info,f.result,f.now,manage=True)
        sold=paper.execute(f.db,f.info,f.result,args,f.now)['fill']
        self.assertEqual(sold['execution_identity']['source'],'gettex')
        self.assertEqual(sold['smart_eligibility']['protective_exit']['reason'],'STOP_LOSS')
        self.assertEqual(paper.ledger(f.db,'V2')['positions'][0]['quantity'],'0')
        self.assertEqual(paper.ledger(f.db,'V5'),before)

    def test_official_quote_forgery_identity_and_other_portfolio_rejected(self):
        from paper_v2_quotes import bound_identity
        f=self.f;self.official();original=copy.deepcopy(f.result)
        for key,value in [('currency','USD'),('venue','FRA'),('symbol','OTHER'),('source','csv')]:
            f.result=copy.deepcopy(original);f.result['execution_quote']['identity'][key]=value
            with self.assertRaisesRegex(ValueError,'IDENTITY_BLOCKED'):bound_identity(f.db,f.info,f.result)
        f.result=copy.deepcopy(original);f.result['paper_portfolio']='V5'
        with self.assertRaisesRegex(ValueError,'IDENTITY_BLOCKED'):bound_identity(f.db,f.info,f.result)
        f.result=copy.deepcopy(original);f.result['execution_quote']['bars'][0]['close']+=.01
        with self.assertRaisesRegex(ValueError,'QUOTE_CHANGED'):hourly.order_command(f.db,f.info,f.result,f.now,manage=True)
        self.assertFalse(paper.ledger(f.db,'V2')['fills'])

    def test_official_closed_window_rejects_changes_without_reading_future(self):
        import paper_v2_quotes as sources
        f=self.f;self.official();meta=sources.identity(f.info)
        start=end='2026-11-04 10:45:00'
        proof=(f.isin,meta,start,end,hourly.digest(sources.rows(f.db,f.isin,meta,start,end)))
        f.result['hourly']['v2_inputs']={'market:'+f.isin+':15m':proof}
        f.db.execute('INSERT INTO candles VALUES (?,?,?,?,?,?,?,?,?)',
            (f.isin,'gettex','15m','2026-11-04 11:00:00',100,102,99,101,20))
        sources.validate_inputs(f.db,f.result)
        f.db.execute('UPDATE candles SET high=104 WHERE ts=?',(start,))
        with self.assertRaisesRegex(ValueError,'MARKET_INPUT_CHANGED'):sources.validate_inputs(f.db,f.result)

    def test_new_official_slot_precedes_stale_universe_once_without_changing_v5(self):
        from paper_v2_quotes import new_slots
        f=self.f;self.official();other='DE0000000001'
        cfg=paper.portfolio_config(f.cfg,'V2');cfg['identities'][other]=f.identity.copy()
        status=dict(at=f.now-60,strategy_mode='V2',timeframe_policy=hourly.TIMEFRAME_POLICY,
                    candidate_timeframes=['30m'],next_check=f.now-1)
        f.write(paper.entry_status_key('V2',f.isin),dict(status,at=f.now-600))
        f.write(paper.entry_status_key('V2',other),dict(status,at=f.now-3600))
        self.assertEqual(hourly.due_stock(f.db,cfg,[other,f.isin],f.now,'V2'),f.isin)
        f.write(paper.entry_status_key('V2',f.isin),status)
        self.assertEqual(hourly.due_stock(f.db,cfg,[other,f.isin],f.now,'V2'),other)
        self.assertFalse(new_slots(f.db,[f.isin],{f.isin:status},f.now))
        self.assertFalse(new_slots(f.db,[f.isin],{},f.now+901))
        self.assertEqual(hourly.due_stock(f.db,cfg,[other,f.isin],f.now,'V5'),other)

    def test_official_fresh_price_is_selected_and_missing_data_never_fabricated(self):
        from contextlib import contextmanager
        from types import SimpleNamespace
        import paper_v2_quotes as sources
        f=self.f;self.official()
        @contextmanager
        def connect():yield f.db
        store=SimpleNamespace(connect=connect,read_cache=SimpleNamespace(depend=lambda _:None))
        q=sources.fresh_quote(store,f.info,f.identity,pd.Timestamp(f.now,unit='s',tz='UTC'))
        self.assertEqual(q['identity']['source'],'gettex')
        for key in ('open','high','low','close','volume','closed'):
            self.assertEqual(q['bars'][0][key],f.result['execution_quote']['bars'][0][key])
        for key in ('open_at','close_at'):
            self.assertEqual(pd.Timestamp(q['bars'][0][key]),pd.Timestamp(f.result['execution_quote']['bars'][0][key]))
        f.db.execute('UPDATE candles SET volume=0')
        with patch.object(hourly,'fresh_quote',side_effect=ValueError('PAPER_NO_CLOSED_BAR')):
            with self.assertRaisesRegex(ValueError,'NO_CLOSED_BAR'):
                sources.fresh_quote(store,f.info,f.identity,pd.Timestamp(f.now,unit='s',tz='UTC'))

    def test_target_sale_locks_profitable_day_and_prevents_second_position(self):
        f=self.f;self.buy()
        with self.assertRaisesRegex(ValueError,'ONE_POSITION'):
            v2.size(f.db,f.info,f.result,f.result['execution_context']['trading_engine']['v2_30m'],f.now)
        f.now+=900;self.refresh(112.,'2026-11-04T10:15:00+00:00',action='HOLD')
        args=hourly.order_command(f.db,f.info,f.result,f.now,manage=True)
        fill=paper.execute(f.db,f.info,f.result,args,f.now)['fill']
        self.assertEqual(fill['smart_eligibility']['protective_exit']['reason'],'TAKE_PROFIT')
        f.now+=900;self.refresh(100.,'2026-11-04T10:30:00+00:00')
        f.result['execution_context']['trading_engine']['timestamp']='2026-11-04T10:00:00+00:00'
        with self.assertRaisesRegex(ValueError,'DAILY_NET_PROFIT_LOCK'):
            hourly.order_command(f.db,f.info,f.result,f.now,manage=True)

    def test_stale_quote_rejected_by_common_executor(self):
        f=self.f;f.now+=901
        with self.assertRaisesRegex(ValueError,'STALE_QUOTE'):
            hourly.order_command(f.db,f.info,f.result,f.now,manage=True)
        self.assertFalse(paper.ledger(f.db,'V2')['fills'])

    def test_bad_core_revisions_and_admission_remain_blocking(self):
        f=self.f
        f.result['execution_context']['trading_engine']['status']='DATA BLOCKED'
        self.assertIsNone(hourly.order_command(f.db,f.info,f.result,f.now,manage=True))
        self.refresh();args=hourly.order_command(f.db,f.info,f.result,f.now,manage=True)
        f.write(v2.ADMISSION_KEY,dict(enabled=False))
        with self.assertRaisesRegex(ValueError,'STALE_INPUTS'):
            paper.execute(f.db,f.info,f.result,args,f.now)

    def test_registration_change_invalidates_ranked_basket(self):
        f=self.f
        f.db.execute('INSERT INTO instruments VALUES (?,?)',('XX0000000001',json.dumps(dict(isin='XX0000000001',kind='stock'))))
        args=hourly.order_command(f.db,f.info,f.result,f.now,manage=True)
        with self.assertRaisesRegex(ValueError,'REGISTERED_UNIVERSE_CHANGED'):
            paper.execute(f.db,f.info,f.result,args,f.now)

    def test53_and200_cached_basket_reads_reject_changed_closed_inputs(self):
        from contextlib import contextmanager
        from types import SimpleNamespace
        f=self.f
        for number in range(13):
            code=f.isin if number==0 else 'XX'+str(number).zfill(10)
            info=dict(isin=code,kind='stock',name='Synthetic'+str(number))
            f.db.execute('INSERT OR REPLACE INTO instruments VALUES (?,?)',(code,json.dumps(info)))
            f.write('gettex_chart:'+code+':15m',dict(identity=f.identity))
            f.db.execute('INSERT INTO series VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                (code,'gettex_chart','GETTEX','EUR','AAPL.GTX','15m','2026-11-04 10:45:00',100,102,99,101,20))
        @contextmanager
        def connect():yield f.db
        store=SimpleNamespace(connect=connect,read_cache=SimpleNamespace(depend=lambda _:None))
        now=pd.Timestamp(f.now,unit='s',tz='UTC');quote=dict(bars=[dict(close_at='2026-11-04T10:00:00Z')])
        v2._CACHE.clear()
        with patch.object(v2,'authority',return_value=self.model),patch('eur_listings.stored_identity',return_value=True), \
                patch.object(v2,'candidates',return_value=([],{f.isin:'TEST_WAIT'})) as build:
            for count in (53,200):
                for _ in range(count):self.assertEqual(v2.context(store,f.info,f.identity,now,quote)['reason'],'TEST_WAIT')
            self.assertEqual(build.call_count,1)
            f.db.execute('UPDATE series SET high=103 WHERE base_tf=?',('15m',))
            v2.context(store,f.info,f.identity,now,quote);self.assertEqual(build.call_count,2)
            f.db.execute('INSERT INTO series VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                (f.isin,'gettex_chart','GETTEX','EUR','AAPL.GTX','15m','2026-11-04 11:00:00',100,102,99,101,20))
            v2.context(store,f.info,f.identity,now,quote);self.assertEqual(build.call_count,2)
            f.db.execute('UPDATE series SET volume=NULL WHERE base_tf=?',('15m',))
            result=v2.context(store,f.info,f.identity,now,quote)
            self.assertEqual(result['reason'],'V2_PEER_OR_PRICE_COVERAGE_INSUFFICIENT')
            self.assertEqual(build.call_count,2)
        v2._CACHE.clear()


class FrozenRules(unittest.TestCase):
    def test_v2_presentation_reports_half_percent_risk_in_selected_language(self):
        import i18n
        from paper_view import automation_summary
        state=dict(automation=dict(strategy_mode='V2',enabled=True,independent=True),
                   automatic_portfolios=['V2'],portfolios={})
        old=i18n._language
        try:
            for language,phrase in [('ru','0,5%'),('de','0,5%'),('en','0.5%')]:
                i18n._language=language
                self.assertIn(phrase,automation_summary(state))
        finally:i18n._language=old

    def test_screen_stale_price_never_runs_basket_or_authorizes_order(self):
        with patch('paper_v2_quotes.fresh_quote',side_effect=ValueError('PAPER_STALE_QUOTE')),patch.object(v2,'context') as basket:
            result=v2.screen(None,dict(isin='TEST'),{},pd.Timestamp('2026-11-24T14:00:00Z'))
        basket.assert_not_called()
        self.assertEqual(result['quote_blocker'],'PAPER_STALE_QUOTE')
        self.assertEqual(result['execution_context']['trading_engine']['status'],'NOT_EVALUATED')
        self.assertNotIn('hourly',result)

    def test_screen_possible_buy_cannot_skip_full_core(self):
        with patch('paper_v2_quotes.fresh_quote',return_value={'bars':[]}), \
                patch.object(v2,'context',return_value=dict(action='BUY',reason='UNIT_TEST')):
            self.assertIsNone(v2.screen(None,dict(isin='TEST'),{},pd.Timestamp('2026-11-24T14:00:00Z')))
    def market(self,reverse=False,extended=False):
        from types import SimpleNamespace
        g=v2.grid('2026-11-19','2026-11-24')
        when=pd.Timestamp('2026-11-24T14:00:00Z')
        if not extended:g=g.loc[g.index<=when]
        tapes={}
        for code in ['TEST']+['P'+str(i) for i in range(12)]:
            close=100.+(np.arange(len(g))*.08 if code=='TEST' else np.zeros(len(g)))
            raw=pd.DataFrame(dict(open=close-.015,high=close+.6,low=close-1.4,close=close,volume=1000.),index=g.index)
            if extended:raw.loc[raw.index>when,['open','high','low','close']]=9999.
            if code=='TEST' and reverse:
                raw.iloc[-1,raw.columns.get_loc('close')]-=2.
                raw.iloc[-1,raw.columns.get_loc('low')]-=2.
            native,bars=features.half_hours(raw,g)
            tapes[code]=SimpleNamespace(close=native.close.to_numpy(),volume=native.volume.to_numpy(),bars=bars)
        return tapes,g,when

    def test_natural_continuation_buy_and_falling_price_wait(self):
        tapes,g,when=self.market();model=artifact('TEST',when.timestamp())
        offers,waits=v2.candidates(tapes,g,when,model)
        self.assertTrue(offers)
        self.assertEqual(offers[0]['isin'],'TEST')
        self.assertGreaterEqual(offers[0]['score'],4.)
        tapes,g,when=self.market(reverse=True)
        offers,waits=v2.candidates(tapes,g,when,model)
        self.assertFalse(offers)

    def test_future_rows_cannot_change_captured_inputs(self):
        tapes,g,when=self.market();model=artifact('TEST',when.timestamp())
        before=v2.candidates(tapes,g,when,model)
        # The public adapter truncates to the observed quote before feature construction.
        self.assertTrue(all(t.bars.index.max()<=when for t in tapes.values()))
        future,g,when=self.market(extended=True)
        self.assertEqual(v2.candidates(future,g,when,model),before)

    def test_failed_breakdown_requires_recovery_and_rejects_second_sharp_fall(self):
        tapes,g,when=self.market();model=artifact('TEST',when.timestamp())
        bars=tapes['TEST'].bars;at=when-pd.Timedelta(minutes=30)
        floor=bars.low.shift().rolling(4).min().loc[at]
        bars.loc[at,'low']=floor-.20*bars.loc[at,'atr']
        offers,_=v2.candidates(tapes,g,when,model)
        self.assertTrue(any(o['setup']=='false_break' for o in offers))
        tapes['TEST'].close=tapes['TEST'].close.copy()
        tapes['TEST'].close[-1]-=3.
        offers,_=v2.candidates(tapes,g,when,model)
        self.assertFalse(offers)

    def test_model_changed_future_training_and_week_expiry_rejected(self):
        now=pd.Timestamp('2026-11-24T14:00:00Z');m=artifact('TEST',now.timestamp())
        v2.validate_model(m,now)
        changed=copy.deepcopy(m);changed['fee']=0
        with self.assertRaisesRegex(ValueError,'MODEL_CHANGED'):v2.validate_model(changed,now)
        with self.assertRaisesRegex(ValueError,'WEEK_EXPIRED'):v2.validate_model(m,now+pd.Timedelta(days=7))
        changed=copy.deepcopy(m);changed['training_end']=changed['week_start']
        changed['artifact_sha256']=hashlib.sha256(v2.canonical({k:v for k,v in changed.items() if k!='artifact_sha256'}).encode()).hexdigest()
        with self.assertRaisesRegex(ValueError,'WEEK_EXPIRED'):v2.validate_model(changed,now)

    def test_bounded53_and200_predictions_use_exact_immutable_model(self):
        now=pd.Timestamp('2026-11-24T14:00:00Z');m=artifact('TEST',now.timestamp());before=copy.deepcopy(m)
        for count in (53,200):
            for _ in range(count):
                score,n=v2.forecast(m,0,'TEST','recovery','lock75',np.zeros(25))
                self.assertEqual((score,n),(6.,2))
        self.assertEqual(m,before)

    def test_size_cash_volume_and_cost_limits(self):
        qty,why=v2.sized(10000.,100.1,20.,97.7,108.625)
        self.assertEqual((qty,why),(10,None))
        self.assertEqual(v2.sized(10000.,100.1,0.,97.7,108.625)[0],0)
        self.assertEqual(v2.sized(5.,100.1,20.,97.7,108.625)[0],0)
        self.assertEqual(v2.sized(10000.,100.1,20.,97.7,100.2)[0],0)

    def test_same_quote_cannot_trigger_new_trail_and_time_stop(self):
        db=sqlite3.connect(':memory:');self.addCleanup(db.close)
        db.execute('CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT)')
        entry=dict(request_id='unit-entry',bar_close_at='2026-11-24T14:00:00Z')
        position=dict(quantity='10',cost='1003.00')
        plan=dict(exit_rule='lock75',stop='97',target='112',initial_quote='100',atr='2')
        bar=dict(close_at='2026-11-24T14:15:00Z')
        reason,_,_,state=v2.protection_trigger(db,position,entry,plan,bar,Decimal('104'))
        self.assertIsNone(reason);self.assertGreater(Decimal(state['stop']),97)
        from paper_protection import management_key
        db.execute('INSERT INTO settings VALUES (?,?)',(management_key(entry['request_id']),json.dumps(state)))
        self.assertIsNone(v2.protection_trigger(db,position,entry,plan,bar,Decimal('101'))[0])
        bar['close_at']='2026-11-24T15:15:00Z'
        self.assertEqual(v2.protection_trigger(db,position,entry,plan,bar,Decimal('105'))[0],'V2_TIME_EXIT')


if __name__=='__main__':unittest.main()
