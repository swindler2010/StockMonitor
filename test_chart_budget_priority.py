"""Shared provider budget: operational quotes cannot lose every free slot."""
import copy,unittest
from unittest.mock import patch
import test_trading_quote_priority as fixture
import candle_schedule as schedule
import gettex_chart as chart
import pandas as pd


class BudgetTests(unittest.TestCase):
    def setUp(self):
        self.x=fixture.QuotePriority();self.x.setUp();self.addCleanup(self.x.doCleanups)
        self.f=self.x.f;self.store=self.x.store;self.now=self.x.now
        self.store.set_setting=self.f.write
        for base in ('5m','15m'):
            self.f.write('gettex_chart:'+self.f.isin+':'+base,dict(state='SAVED',identity=self.f.identity,
                received_at='2026-11-04T08:00Z',last='2026-11-04 09:00:00',
                listing_proof=dict(self.f.identity,status='CONFIRMED',isin=self.f.isin)))

    def test_background_yields_free_budget_to_real_configured_quote(self):
        due=schedule.operational_chart_due(self.f.db,self.now)
        self.assertEqual({b for _,i,b in due},{'15m'})
        self.assertFalse(chart.reserve_request(self.f.db,self.now,'BACKGROUND','15m'))
        self.assertIsNone(self.store.setting('gettex_chart_next_request'))
        self.assertFalse(chart.reserve_request(self.f.db,self.now,self.f.isin,'5m'))
        self.assertTrue(chart.reserve_request(self.f.db,self.now,self.f.isin,'15m'))
        self.assertEqual(self.store.setting('gettex_chart_next_request'),(self.now+pd.Timedelta(minutes=1)).isoformat())

    def test_pending_candidate_reserves_alternating_ordinary_http_slots(self):
        now=self.now
        with patch.object(schedule,'operational_chart_due',return_value=[('',self.f.isin,'15m')]),patch.object(schedule,'preparation_chart_due',return_value=[('','CANDIDATE','15m')]):
            for ordinary in ('CANDIDATE','BACKGROUND','CANDIDATE'):
                for _ in range(2):
                    self.assertFalse(chart.reserve_request(self.f.db,now,'CANDIDATE','15m'))
                    self.assertTrue(chart.reserve_request(self.f.db,now,self.f.isin,'15m'))
                    now+=pd.Timedelta(minutes=1)
                if ordinary=='CANDIDATE':
                    previous=self.store.setting('gettex_chart_next_request')
                    self.assertFalse(chart.reserve_request(self.f.db,now,'BACKGROUND','15m',recovery=True))
                    self.assertFalse(chart.reserve_request(self.f.db,now,self.f.isin,'15m'))
                    self.assertEqual(self.store.setting('gettex_chart_next_request'),previous)
                self.assertTrue(chart.reserve_request(self.f.db,now,ordinary,'15m'))
                self.assertFalse(chart.reserve_request(self.f.db,now,ordinary,'15m'))
                now+=pd.Timedelta(minutes=1)

    def test_live_v5_quote_priority_and_bounded_publication_retry(self):
        from paper_hourly import CONFIG,STATUS
        import paper_v5_four_hour as model
        f=self.f;now=pd.Timestamp('2026-11-04T10:01Z')
        f.cfg.update(strategy_mode='AGGRESSIVE',**{model.PROFILE_KEY:model.MODEL})
        f.write(CONFIG,f.cfg)
        fresh=schedule.decision(self.store,f.isin,f.identity,'15m',now)
        request=dict(identity=f.identity,close_at=fresh['close_at'])
        status=dict(strategy_mode='AGGRESSIVE',timeframes={'4H':{'quote_request':request}})
        f.write(STATUS+f.isin,status)
        others=[('',f.isin,'15m'),('','OTHER','15m')]
        with patch.object(schedule,'operational_chart_due',return_value=others):
            self.assertFalse(chart.reserve_request(f.db,now,'OTHER','15m'))
            self.assertTrue(chart.reserve_request(f.db,now,f.isin,'15m'))
        schedule.finish(self.store,f.isin,f.identity,'15m',fresh,now=now)
        self.assertFalse(schedule.decision(self.store,f.isin,f.identity,'15m',now+pd.Timedelta(minutes=1))['due'])
        after=now+pd.Timedelta(minutes=2)
        retry=schedule.decision(self.store,f.isin,f.identity,'15m',after)
        self.assertEqual(retry['reason'],'V5_QUOTE_PUBLICATION_RECHECK')
        schedule.finish(self.store,f.isin,f.identity,'15m',retry,now=after)
        self.assertFalse(schedule.decision(self.store,f.isin,f.identity,'15m',after+pd.Timedelta(minutes=4))['due'])
        self.assertEqual(schedule.urgent_v5_quotes(f.db,now+pd.Timedelta(minutes=15),others),set())
        request['identity']=dict(f.identity,currency='USD')
        f.write(STATUS+f.isin,status)
        self.assertEqual(schedule.urgent_v5_quotes(f.db,now,others),set())

    def test_fresh_planner_picks_candidate_ahead_of_older_feed_receipts(self):
        import json
        from paper_hourly import CONFIG,STATUS
        import paper_v5_four_hour as model
        f=self.f;now=pd.Timestamp('2026-11-04T10:03Z')
        second=dict(isin='US67066G1040',name='NVIDIA',kind='stock')
        other=dict(f.identity,symbol='NVDA.GTX')
        f.db.execute('INSERT INTO instruments VALUES (?,?)',(second['isin'],json.dumps(second)))
        f.cfg.update(strategy_mode='AGGRESSIVE',**{model.PROFILE_KEY:model.MODEL})
        f.cfg['identities'][second['isin']]=other
        f.write(CONFIG,f.cfg)
        f.write('gettex_chart:'+second['isin']+':15m',dict(
            state='SAVED',identity=other,received_at=now.isoformat(),last='2026-11-04 10:00:00',
            listing_proof=dict(other,status='CONFIRMED',isin=second['isin'])))
        target=schedule.boundary(other,'15m',now)['close_at']
        f.write(STATUS+second['isin'],dict(strategy_mode='AGGRESSIVE',
            timeframes={'4H':{'quote_request':dict(identity=other,close_at=target)}}))
        f.write('gettex_chart_request_rotation',dict(priority_streak=0))
        info,_=schedule.fresh_candidate(self.store,[f.info,second],now)
        self.assertEqual(info['isin'],second['isin'])
        self.assertEqual(info['_cp_focus']['timeframe'],'15m')
        self.assertFalse(info['_cp_focus']['execution_permission'])
        holding=dict(isin=f.isin,identity=f.identity,quantity='1')
        with patch('paper_account.ledger',return_value={'positions':[holding]}):
            info,_=schedule.fresh_candidate(self.store,[f.info,second],now)
        self.assertEqual(info['isin'],f.isin)

    def test_two_priority_then_background_gets_time_under_53_and_200_jobs(self):
        for size in (53,200):
            self.f.db.execute("DELETE FROM settings WHERE key IN ('gettex_chart_next_request','gettex_chart_request_rotation')")
            with patch.object(schedule,'operational_chart_due',return_value=[('',self.f.isin,'15m')]):
                now=self.now;history=0;priority=0
                while history<size:
                    allowed=chart.reserve_request(self.f.db,now,'BACKGROUND','15m')
                    if allowed:history+=1
                    else:
                        self.assertTrue(chart.reserve_request(self.f.db,now,self.f.isin,'15m'));priority+=1
                    now+=pd.Timedelta(minutes=1)
                self.assertEqual(priority,2*size)

    def test_priority_never_crosses_source_cooldown_or_wrong_identity(self):
        self.f.write('cp_source_retry:gettex_chart',(self.now+pd.Timedelta(hours=1)).isoformat())
        self.assertEqual(schedule.operational_chart_due(self.f.db,self.now),[])
        self.f.db.execute("DELETE FROM settings WHERE key='cp_source_retry:gettex_chart'")
        for base in ('5m','15m'):
            saved=self.store.setting('gettex_chart:'+self.f.isin+':'+base)
            saved['identity']=dict(saved['identity'],venue='FRA')
            self.f.write('gettex_chart:'+self.f.isin+':'+base,saved)
        self.assertEqual(schedule.operational_chart_due(self.f.db,self.now),[])

    def test_recovery_defers_without_burning_an_http_attempt(self):
        wait=chart.recovery_wait(self.store,self.f.info,self.f.identity,'15m',self.now)
        self.assertEqual(pd.Timestamp(wait),self.now+pd.Timedelta(seconds=30))
        self.assertFalse(chart.reserve_request(self.f.db,self.now,self.f.isin,'15m',recovery=True))
        self.assertIsNone(self.store.setting('gettex_chart_next_request'))

    def test_closed_daily_filter_gets_priority_without_forming_or_complete_day(self):
        key='gettex_chart:'+self.f.isin+':1D'
        saved=copy.deepcopy(self.store.setting('gettex_chart:'+self.f.isin+':15m'))
        saved.update(last='2026-11-09 00:00:00',received_at='2026-11-10T22:02Z')
        self.f.write(key,saved)
        now=pd.Timestamp('2026-11-10T22:47Z')
        due=schedule.operational_chart_due(self.f.db,now)
        self.assertIn(('',self.f.isin,'1D'),[('',i,b) for _,i,b in due])
        self.assertFalse(chart.reserve_request(self.f.db,now,'BACKGROUND','1D'))
        self.assertTrue(chart.reserve_request(self.f.db,now,self.f.isin,'1D'))
        # Before today's close, yesterday's verified candle is sufficient.
        before=pd.Timestamp('2026-11-10T20:00Z')
        self.assertNotIn('1D',[b for _,i,b in schedule.operational_chart_due(self.f.db,before)])
        saved['last']='2026-11-10 00:00:00';self.f.write(key,saved)
        self.assertNotIn('1D',[b for _,i,b in schedule.operational_chart_due(self.f.db,now)])

    def tail(self):
        now=pd.Timestamp('2026-11-10T22:20Z')
        saved=self.store.setting('gettex_chart:'+self.f.isin+':15m')
        saved['last']='2026-11-10 22:30:00';self.f.write('gettex_chart:'+self.f.isin+':15m',saved)
        return now

    def test_last_bar_has_four_bounded_postclose_checks_and_restart_progress(self):
        now=self.tail();meta=self.f.identity
        for count in range(1,5):
            p=schedule.decision(self.store,self.f.isin,meta,'15m',now)
            self.assertTrue(p['due'],p);self.assertEqual(p['intraday_publication_rechecks'],count)
            self.assertTrue(schedule.fresh_window(p,'15m',now))
            schedule.finish(self.store,self.f.isin,meta,'15m',p,now=now)
            pending=schedule.decision(self.store,self.f.isin,meta,'15m',now)
            self.assertFalse(pending['due']);self.assertEqual(pending['publication_delay_status'],'UNKNOWN')
            now=pd.Timestamp(pending['next_publication_check'])
        self.assertEqual(pending['reason'],'INTRADAY_PUBLICATION_UNCONFIRMED')
        self.assertFalse(schedule.decision(self.store,self.f.isin,meta,'15m',now+pd.Timedelta(minutes=1))['due'])
        # Same persisted receipts, new scheduler call: no reset of the budget.
        self.assertEqual(self.store.setting(p['key'])['intraday_publication_rechecks'],4)

    def test_arrived_tail_stops_rechecking_without_quality_or_price_permission(self):
        now=self.tail();p=schedule.decision(self.store,self.f.isin,self.f.identity,'15m',now)
        schedule.finish(self.store,self.f.isin,self.f.identity,'15m',p,now=now)
        saved=self.store.setting('gettex_chart:'+self.f.isin+':15m');saved['last']='2026-11-10 22:45:00'
        self.f.write('gettex_chart:'+self.f.isin+':15m',saved)
        result=schedule.decision(self.store,self.f.isin,self.f.identity,'15m',now+pd.Timedelta(minutes=6))
        self.assertFalse(result['due']);self.assertEqual(result['reason'],'UNCHANGED')
        self.assertNotIn('execution_permission',result)

    def test_tail_cooldown_and_weekend_are_not_bypassed(self):
        now=self.tail();self.f.write('cp_source_retry:gettex_chart',(now+pd.Timedelta(hours=1)).isoformat())
        result=schedule.decision(self.store,self.f.isin,self.f.identity,'15m',now)
        self.assertFalse(result['due']);self.assertEqual(result['reason'],'SOURCE_COOLDOWN')
        self.f.db.execute("DELETE FROM settings WHERE key='cp_source_retry:gettex_chart'")
        # A real Friday catch-up has spent its time budget before Saturday.
        friday=pd.Timestamp('2026-11-13T22:05Z')
        p=schedule.decision(self.store,self.f.isin,self.f.identity,'15m',friday)
        schedule.finish(self.store,self.f.isin,self.f.identity,'15m',p,now=friday)
        result=schedule.decision(self.store,self.f.isin,self.f.identity,'15m',pd.Timestamp('2026-11-14T12:00Z'))
        self.assertFalse(result['due'])

    def test_weekend_first_catchup_uses_persisted_bounded_budget(self):
        now=pd.Timestamp('2026-11-14T09:30Z');meta=self.f.identity
        key='gettex_chart:'+self.f.isin+':15m'
        saved=self.store.setting(key);saved['last']='2026-11-13 12:00:00';self.f.write(key,saved)
        before=copy.deepcopy(saved)
        for count in range(1,5):
            p=schedule.decision(self.store,self.f.isin,meta,'15m',now)
            self.assertTrue(p['due'],p);self.assertTrue(schedule.fresh_window(p,'15m',now))
            self.assertEqual(p['intraday_publication_rechecks'],count)
            self.assertEqual(p['publication_retry_until'],'2026-11-14T11:30:00+00:00')
            schedule.finish(self.store,self.f.isin,meta,'15m',p,now=now)
            pending=schedule.decision(self.store,self.f.isin,meta,'15m',now)
            self.assertFalse(pending['due'])
            now=pd.Timestamp(pending['next_publication_check'])
        self.assertEqual(self.store.setting(key),before) # no data or permission writes
        for later in ('2026-11-14T10:31Z','2026-11-15T10:00Z'):
            self.assertFalse(schedule.decision(self.store,self.f.isin,meta,'15m',pd.Timestamp(later))['due'])
        # Neither a later ordinary finish nor an error may reset progress.
        schedule.finish(self.store,self.f.isin,meta,'15m',dict(key=p['key'],close_at=p['close_at']),RuntimeError('offline'),now)
        kept=self.store.setting(p['key']);self.assertEqual(kept['intraday_publication_rechecks'],4)
        self.assertEqual(kept['intraday_catchup_started_at'],'2026-11-14T09:30:00+00:00')
        later=pd.Timestamp('2026-11-16T22:10Z')
        self.assertEqual(schedule.decision(self.store,self.f.isin,meta,'15m',later)['intraday_publication_rechecks'],1)


if __name__=='__main__':unittest.main()
