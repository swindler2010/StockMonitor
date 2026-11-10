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
        self.assertEqual({b for _,i,b in due},{'5m','15m'})
        self.assertFalse(chart.reserve_request(self.f.db,self.now,'BACKGROUND','15m'))
        self.assertIsNone(self.store.setting('gettex_chart_next_request'))
        self.assertTrue(chart.reserve_request(self.f.db,self.now,self.f.isin,'5m'))
        self.assertFalse(chart.reserve_request(self.f.db,self.now,self.f.isin,'15m'))
        self.assertEqual(self.store.setting('gettex_chart_next_request'),(self.now+pd.Timedelta(minutes=1)).isoformat())

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
        result=schedule.decision(self.store,self.f.isin,self.f.identity,'15m',pd.Timestamp('2026-11-14T12:00Z'))
        self.assertFalse(result['due'])


if __name__=='__main__':unittest.main()
