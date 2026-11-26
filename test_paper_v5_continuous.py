"""Scope-aware priority and a read-only V5 direction outside order windows."""
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch
import json
import sqlite3
import unittest

import pandas as pd

import paper_hourly as hourly
import paper_preparation as preparation
import paper_v5_four_hour as model
from performance_cache import install_input_revisions


class ContinuousV5Tests(unittest.TestCase):
    def setUp(self):
        self.db=sqlite3.connect(':memory:')
        self.addCleanup(self.db.close)
        self.db.execute('CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT)')
        for table in ('candles','series'):
            self.db.execute(f'CREATE TABLE {table}(isin TEXT,base_tf TEXT,ts TEXT,close REAL,'
                            'PRIMARY KEY(isin,base_tf,ts))')
        self.db.execute('CREATE TABLE instruments(isin TEXT PRIMARY KEY,payload TEXT)')
        install_input_revisions(self.db)
        self.ids=tuple(f'US{n:010}' for n in range(200))
        identities={i:dict(source='gettex_chart',quote_currency='EUR',currency='EUR',
                           symbol=i+'.GTX',venue='GETTEX',timezone='Europe/Berlin') for i in self.ids}
        self.cfg=dict(enabled=True, strategy_mode='AGGRESSIVE', identities=identities,
                      **{model.PROFILE_KEY:model.MODEL})
        self.now=pd.Timestamp('2026-11-04T18:50Z')
        self.db.execute('INSERT INTO settings VALUES (?,?)',(hourly.CONFIG,json.dumps(self.cfg)))
        for n,i in enumerate(self.ids):
            self.record(i,retry=hourly.v5_revisions(self.db,hourly.v5_retry_scopes(i,identities[i])),at=n)
        self.db.commit()

    def record(self, isin, *, retry, at=0, next_check=None):
        values=dict(retry_inputs=retry,at=at,next_check=next_check or self.now.timestamp()+600,
                    strategy_mode='AGGRESSIVE',v5_rule_version=hourly.v5_rule_version(self.cfg),
                    timeframe_policy=hourly.TIMEFRAME_POLICY,candidate_timeframes=['4H'])
        self.db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)',
                        (hourly.STATUS+isin,json.dumps(values)))

    def save(self, isin, base, close=100):
        self.db.execute('INSERT OR REPLACE INTO series VALUES (?,?,?,?)',(isin,base,'recent',close))

    def test_revision_wakes_waiting_stock_immediately_for_53_and_200(self):
        for size in (53,200):
            with self.subTest(size=size):
                ids=self.ids[:size]
                self.assertIsNone(hourly.v5_due_stock(self.db,self.cfg,ids,self.now.timestamp()))
                target=ids[-1]
                self.save('UNRELATED','15m')
                self.assertIsNone(hourly.v5_due_stock(self.db,self.cfg,ids,self.now.timestamp()))
                self.save(target,'1D')
                self.assertEqual(hourly.v5_due_stock(self.db,self.cfg,ids,self.now.timestamp()),target)
                self.db.rollback()

    def test_stock_with_changed_intraday_data_precedes_timer_and_copy_is_ignored(self):
        first,last=self.ids[0],self.ids[-1]
        self.record(first,retry=hourly.v5_revisions(self.db,hourly.v5_retry_scopes(first,self.cfg['identities'][first])),
                    next_check=self.now.timestamp()-30)
        self.save(last,'15m')
        self.assertEqual(hourly.v5_due_stock(self.db,self.cfg,self.ids,self.now.timestamp()),last)
        token=hourly.v5_revisions(self.db,hourly.v5_retry_scopes(last,self.cfg['identities'][last]))
        self.record(last,retry=token,at=self.now.timestamp())
        self.save(last,'15m')
        self.assertEqual(hourly.v5_due_stock(self.db,self.cfg,self.ids,self.now.timestamp()),first)

    def test_runtime_rechecks_changed_stock_and_defers_admission_during_quote_window(self):
        target=self.ids[-1]
        self.save(target,'15m')
        self.cfg['automatic_admission']={'enabled':True}
        self.db.execute('UPDATE settings SET value=? WHERE key=?',(json.dumps(self.cfg),hourly.CONFIG))
        for n,i in enumerate(self.ids):
            token=hourly.v5_revisions(self.db,hourly.v5_retry_scopes(i,self.cfg['identities'][i]))
            if i==target:
                token['market:'+i+':15m']=0
            self.record(i,retry=token,at=n)
        self.db.commit()
        store=SimpleNamespace(connect=lambda:self.db,setting=lambda key,default=None:self.cfg if key==hourly.CONFIG else default,
                              read_cache=SimpleNamespace(token_for=lambda scopes:{}))
        def job(work,done,failed=None,**options):
            self.assertEqual((options['lane'],options['key']),('trading','paper_auto_v5'))
            done(work())
            return True
        app=SimpleNamespace(store=store,root=SimpleNamespace(after=lambda *_:None),
                            watchlist=[dict(isin=i,name=i) for i in self.ids],ai_config={},
                            user_dirty=False,closing=False,job=job)
        runtime=hourly.Runtime(app)
        runtime.operational_turns=2
        with (patch.object(hourly.time,'time',return_value=self.now.timestamp()),
              patch.object(hourly,'run_turn') as turn,patch.object(preparation,'turn') as admit):
            runtime.tick()
        admit.assert_not_called()
        self.assertEqual(turn.call_args.args[1]['isin'],target)
        self.assertTrue(turn.call_args.args[2]['_paper_v5_watch'])

    def test_new_direction_is_visible_even_when_outside_order_window(self):
        closed=pd.Timestamp('2026-11-04T12:32Z')
        identity=self.cfg['identities'][self.ids[0]]
        store=SimpleNamespace(connect=lambda:self.db)
        weak=dict(rule=model.WEEKLY_RULE,rising=False,daily_recovery={'confirmed':False,'day':'2026-11-03'})
        ready=dict(weak,rising=True)
        with (patch.object(model,'weekly_context',side_effect=[weak,ready]),
              patch.object(hourly,'fresh_quote',side_effect=ValueError('PAPER_STALE_QUOTE'))):
            reports=[hourly.screen_v5_entry(store,{'isin':self.ids[0]},identity,closed,watch=True)
                     for _ in range(2)]
        engines=[r['execution_context']['trading_engine'] for r in reports]
        self.assertEqual([e['v5_four_hour']['trend_watch']['state'] for e in engines],
                         [model.WEEKLY_NOT_RISING,'WEEKLY_RISING'])
        self.assertEqual([e['signal'] for e in engines],['WAIT','WAIT'])
        self.assertTrue(all(r['_v5_entry_screen'] for r in reports))
        self.assertEqual([e['reason'] for e in engines],['V5_4H_OUTSIDE_OPENING_WINDOW']*2)

    def test_only_eligible_closed_setup_requests_a_missing_execution_quote(self):
        identity=self.cfg['identities'][self.ids[0]]
        info={'isin':self.ids[0]}
        clock=pd.Timestamp('2026-11-23T06:51Z')
        window=(clock-pd.Timedelta(hours=13),clock-pd.Timedelta(hours=9),
                pd.Timestamp('2026-11-23T06:30Z'),pd.Timestamp('2026-11-23T06:45Z'))
        evidence=dict(quote_end=window[-1].isoformat(),opening=window[-2].isoformat(),
                      weekly_trend=dict(rule=model.WEEKLY_RULE,rising=True))
        with (patch.object(model,'entry_window',return_value=(None,window)),
              patch.object(model,'closed_setup',side_effect=[{'reason':model.WEEKLY_NOT_RISING},evidence]),
              patch.object(hourly,'fresh_quote',side_effect=ValueError('PAPER_STALE_QUOTE')) as quote):
            failed=hourly.screen_v5_entry(None,info,identity,clock)
            pending=hourly.screen_v5_entry(None,info,identity,clock)
        self.assertEqual(quote.call_count,1)
        self.assertNotIn('quote_request',failed)
        self.assertEqual(pending['quote_request'],dict(identity=identity,close_at=window[-1].isoformat()))
        self.assertEqual(pending['execution_context']['trading_engine']['signal'],'WAIT')
        self.assertNotIn('execution_quote',pending)

    def test_daily_setup_is_evaluated_before_requesting_the_second_quote(self):
        import paper_v5_daily_pullback as daily
        identity=self.cfg['identities'][self.ids[0]]
        info={'isin':self.ids[0]}
        now=pd.Timestamp('2026-11-23T07:03Z')
        window=daily.opening_window(identity,now)
        eligible=dict(quote_end=window[-1].isoformat(),signal_day='2026-11-20')
        with (patch.object(daily,'context',side_effect=[{'reason':daily.WAIT_PATTERN},eligible]),
              patch.object(hourly,'fresh_quote',side_effect=ValueError('PAPER_STALE_QUOTE')) as quote):
            rejected,skip=hourly.screen_daily_pullback(None,info,identity,self.cfg,now,{})
            pending,request=hourly.screen_daily_pullback(None,info,identity,self.cfg,now,{})
        self.assertEqual(rejected['reason'],daily.WAIT_PATTERN)
        self.assertEqual(rejected['quote_end'],window[-1].isoformat())
        self.assertEqual(skip,{})
        self.assertEqual(quote.call_count,1)
        self.assertEqual(pending['reason'],daily.WAIT_QUOTE)
        self.assertEqual(request['quote_request']['close_at'],window[-1].isoformat())



    def test_morning_runtime_checks_all_49_once_before_other_entries(self):
        from paper_account import AUTO_PORTFOLIOS_SETTING, entry_status_key
        import paper_v5_daily_pullback as daily

        at=pd.Timestamp('2026-11-25T08:00:33.821568251+01:00')
        cohort=tuple(sorted(daily.COHORT))
        ids=(*self.ids[:20],*cohort)
        identity=self.cfg['identities'][self.ids[0]]
        self.cfg['identities']={i:{**identity,'symbol':i+'.GTX'} for i in ids}
        self.cfg[daily.PROFILE_KEY]=daily.MODEL
        self.db.execute('UPDATE settings SET value=? WHERE key=?',(json.dumps(self.cfg),hourly.CONFIG))
        self.db.execute('INSERT INTO settings VALUES (?,?)',
                        (AUTO_PORTFOLIOS_SETTING,json.dumps(dict(version=2,ready=['V1','V2','V4','V5']))))
        self.db.commit()

        @contextmanager
        def connect():
            yield self.db
            self.db.rollback()

        calls=[]
        def turn(app,info,values,cfg,**options):
            self.assertEqual(options['entry_portfolio'],'V5')
            self.assertTrue(options['v5_daily_only'])
            isin=info['isin']
            calls.append(isin)
            report=dict(timeframes={'1D':dict(at=at.timestamp(),reason=daily.WAIT_PATTERN)})
            self.db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)',
                            (entry_status_key('V5',isin),json.dumps(report)))
            self.db.commit()
            return report

        def job(work,done,failed=None,**options):
            self.assertEqual(options['lane'],'trading')
            done(work())
            return True

        app=SimpleNamespace(store=SimpleNamespace(connect=connect,read_cache=SimpleNamespace(token_for=lambda _:{})),
                            watchlist=[dict(isin=i,name=i) for i in ids],ai_config={},closing=False,user_dirty=False,job=job)
        runtime=hourly.Runtime(app)
        with (patch.object(hourly.time,'time',return_value=at.timestamp()),
              patch.object(hourly,'maintained_positions',return_value=[]),
              patch.object(model,'entry_window',side_effect=ValueError('4H calendar unavailable')) as four_hour,
              patch.object(hourly,'run_turn',side_effect=turn)):
            for _ in cohort:
                runtime.tick_independent(app,ids)
        four_hour.assert_not_called()
        self.assertEqual(calls,list(cohort))
        self.assertEqual(hourly.v5_daily_pending(self.db,ids,daily.opening_window(identity,at)),())
        tomorrow=at+pd.Timedelta(days=1)
        self.assertEqual(hourly.v5_daily_pending(self.db,ids,daily.opening_window(identity,tomorrow)),cohort)

    def test_4h_calendar_preserves_nanosecond_window_boundaries(self):
        identity=self.cfg['identities'][self.ids[0]]
        boundary=pd.Timestamp('2026-11-25T08:00:00+01:00')
        before=boundary-pd.Timedelta(nanoseconds=1)
        after=boundary+pd.Timedelta(nanoseconds=1)
        _,previous=model.entry_window(identity,before,reclaim=True)
        _,current=model.entry_window(identity,after,reclaim=True)
        _,exact=model.entry_window(identity,boundary,reclaim=True)
        self.assertEqual(previous[-1],boundary-model.QUARTER)
        self.assertEqual(current[-1],boundary)
        self.assertEqual(current,exact)

    def test_official_4h_requires_a_complete_single_source_and_current_pair_evidence(self):
        conn=sqlite3.connect(':memory:')
        self.addCleanup(conn.close)
        conn.execute('CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT)')
        conn.execute('CREATE TABLE candles(isin TEXT,source TEXT,base_tf TEXT,ts TEXT,'
                     'open REAL,high REAL,low REAL,close REAL,volume REAL)')
        store=SimpleNamespace(connect=lambda:conn)
        isin=self.ids[0]
        info={'isin':isin}
        identity=self.cfg['identities'][isin]
        now=pd.Timestamp('2026-11-24T11:50+01:00')
        cal,window=model.entry_window(identity,now)
        start,end,_,_=window
        expected=pd.date_range(start,end,freq=model.QUARTER,inclusive='left')

        def local(t):
            return str(t.tz_convert(identity['timezone']).tz_localize(None))

        receipts={}
        for t in expected:
            conn.execute('INSERT INTO candles VALUES (?,?,?,?,?,?,?,?,?)',
                         (isin,'gettex','15m',local(t),100.,102.,99.,101.,10.))
            receipts[(t+model.QUARTER).strftime('%Y%m%d.%H.%M')]=dict(
                state='ACCUMULATED',sha256={'munc':'a'*64,'mund':'b'*64},errors={},existing_value_conflicts=0)

        def persist():
            conn.execute('INSERT OR REPLACE INTO settings VALUES (?,?)',
                         ('gettex_cached_history',json.dumps({'pairs':receipts})))

        label=next(iter(receipts))
        info_proof=self.cfg['identities'][isin]
        weekly=dict(rule=model.WEEKLY_RULE,rising=True)
        native=pd.DataFrame([[100.,102.,99.,101.,10.]]*len(expected),
               columns=['open','high','low','close','volume'],index=expected)
        with (patch('eur_listings.stored_identity',return_value=info_proof),
              patch('paper_v4._series',return_value=native.iloc[1:]) as main,
              patch.object(model,'weekly_context',return_value=weekly)):
            persist()
            evidence=model.context(store,info,identity,str(start-pd.Timedelta(hours=4)),now,{}, {})
            self.assertEqual(evidence['signal_source']['source'],'gettex')
            self.assertEqual(evidence['signal_source']['signal_at'],local(start))
            self.assertEqual(evidence['reason'],'V5_4H_NO_COMPRESSION_BREAKOUT')
            proof=evidence['_official_input']
            self.assertTrue(model.official_input_current(conn,proof))

            receipts[label]['sha256']['munc']='c'*64
            persist()
            self.assertFalse(model.official_input_current(conn,proof))
            receipts[label]['errors'][isin]='EXISTING_TRADE_COVERAGE_DIFFERS'
            persist()
            blocked=model.closed_setup(store,info,identity,cal,window,{}, {})
            self.assertEqual(blocked['reason'],'V5_4H_SIGNAL_UNAVAILABLE')
            self.assertNotIn('signal_source',blocked)

            main.return_value=native
            result=model.closed_setup(store,info,identity,cal,window,{}, {})
            self.assertEqual(result['reason'],'V5_4H_NO_COMPRESSION_BREAKOUT')
            self.assertNotIn('signal_source',result)


    def test_official_signal_cannot_execute_after_its_revision_changes(self):
        from test_paper_v5_reclaim_four_hour import ReclaimFourHourTests
        from paper_account import ledger
        fixture=ReclaimFourHourTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        seed=fixture.f
        setup=fixture.setup_evidence(15)
        result=fixture.prepare(15,setup)
        stamp=str(fixture.signal_open.tz_convert('Europe/Berlin').tz_localize(None))
        label=(fixture.signal_open+model.QUARTER).strftime('%Y%m%d.%H.%M')
        seed.db.execute('INSERT INTO candles(isin,source,base_tf,ts,open,high,low,close,volume) '
                        'VALUES (?,?,?,?,?,?,?,?,?)',
                        (seed.isin,'gettex','15m',stamp,99.,102.,98.,101.,100.))
        pair=dict(state='ACCUMULATED',sha256={'munc':'a'*64,'mund':'b'*64},
                  errors={},existing_value_conflicts=0)
        def save_pair():
            seed.db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)',
                            ('gettex_cached_history',json.dumps(dict(pairs={label:pair}))))
        save_pair()
        proof=dict(isin=seed.isin,first=stamp,last=stamp,digest=hourly.digest(
                   model.official_input(seed.db,seed.isin,stamp,stamp)))
        setup['signal_source']=dict(source='gettex',venue='GETTEX',currency='EUR',
                                    through=fixture.signal.isoformat(),signal_at=stamp,digest=proof['digest'])
        result['hourly']['v5_official_input']=proof
        seed.args=fixture.runner.command()
        pair['sha256']['mund']='c'*64
        save_pair()
        with self.assertRaisesRegex(ValueError,'PAPER_V5_OFFICIAL_SIGNAL_CHANGED'):
            seed.trade()
        self.assertEqual(ledger(seed.db)['fills'],[])
        pair['sha256']['mund']='b'*64
        save_pair()
        fill=seed.trade()['fill']
        self.assertEqual(fill['identity'],seed.identity)
        self.assertEqual(fill['smart_eligibility']['entry_evidence']['signal_source']['source'],'gettex')
        self.assertEqual(fill['smart_eligibility']['protection']['hold_minutes'],model.HOLD_MINUTES)
