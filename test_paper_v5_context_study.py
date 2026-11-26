"""Observe V5 context in isolation from the PAPER order ledger."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import sqlite3
import sys
from pathlib import Path
import unittest

sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
import paper_v5_context_study as study
from paper_hourly import CONFIG


class V5ContextStudyTests(unittest.TestCase):
    def setUp(self):
        self.db=sqlite3.connect(':memory:')
        self.addCleanup(self.db.close)
        self.db.execute('CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT)')
        self.isin='US67066G1040'
        self.identity=dict(venue='GETTEX',currency='EUR',source='gettex_chart',
                           symbol='NVDA.GTX',timezone='Europe/Berlin')
        self.info={'isin':self.isin}
        self.cfg=dict(epoch='one',identities={self.isin:self.identity})
        self.when=datetime.fromisoformat('2026-11-18T18:00:00+00:00')
        self.engine=dict(timestamp='2026-11-18 17:30:00',v5_opening_range=dict(prev_high=99,open_high=97))
        self.result=dict(identity=self.identity,execution_context=dict(trading_engine=self.engine))
        self.signal=dict(action='BUY',setup='DAILY_REVERSAL',model='opening_range_and_reversal_1h_v2',
                         relative_volume=1.3,atr=2,hold_minutes=180,breadth_positive=3,breadth_available=4)

    def observe(self, minutes=0,price=100,volume=20,*,buy=True):
        clock=self.when+timedelta(minutes=minutes)
        bar=dict(close_at=(clock-timedelta(minutes=5)).isoformat(),close=price,volume=volume)
        period=study._calendar(self.identity,clock) if buy else None
        return study.observe(self.db,self.info,self.result,self.cfg,self.signal if buy else None,
                             bar,clock.timestamp(),period)

    def test_single_observation_closes_at_target_and_reports_comparable_subset(self):
        self.assertEqual(self.observe(),'OPEN')
        self.assertIsNone(self.observe())
        self.assertEqual(self.observe(15,106,buy=False),'CLOSED')
        row=study.summary(self.db)
        self.assertEqual(row['states'],{'CLOSED':1})
        self.assertEqual(row['setups']['DAILY_REVERSAL']['net'],Decimal('56.97'))
        self.assertEqual(row['variants']['skip_near_macro']['accepted'],0)
        self.assertEqual(row['variants']['skip_near_macro']['baseline'],Decimal('56.97'))
        self.assertEqual(row['variants']['reversal_level_held']['known'],1)
        self.assertEqual(row['variants']['skip_near_earnings']['accepted'],1)
        self.assertIsNone(self.db.execute("SELECT name FROM sqlite_master WHERE name='paper_fills'").fetchone())

    def test_gap_and_thin_trade_are_not_reported_as_wins_or_losses(self):
        self.assertEqual(self.observe(),'OPEN')
        self.assertEqual(self.observe(30,106,buy=False),'INCOMPLETE')
        self.engine['timestamp']='2026-11-18 18:30:00'
        self.assertEqual(self.observe(45,100,volume=2),'BLOCKED')
        row=study.summary(self.db)
        self.assertEqual(row['states'],{'BLOCKED':1,'INCOMPLETE':1})
        self.assertEqual(row['variants'],{})

    def test_hourly_core_confirmation_compares_only_known_closed_signals(self):
        for minute,core in ((0,None),(30,'WAIT'),(60,'BUY')):
            self.engine.update(timestamp=f'replay-{minute}',signal=core)
            self.assertEqual(self.observe(minute),'OPEN')
            self.assertEqual(self.observe(minute+15,106,buy=False),'CLOSED')
        summary=study.summary(self.db)
        self.assertEqual(summary['states'],{'CLOSED':3})
        self.assertEqual(summary['variants']['core_confirms_1h'],
            dict(known=2,accepted=1,baseline=Decimal('113.94'),net=Decimal('56.97')))
        self.assertIsNone(self.db.execute("SELECT name FROM sqlite_master WHERE name='paper_fills'").fetchone())

    def test_dst_and_holiday_and_article_publication_bound(self):
        summer=datetime.fromisoformat('2026-10-26T13:30:00+00:00')
        winter=datetime.fromisoformat('2026-11-02T14:30:00+00:00')
        self.assertEqual(study._calendar(self.identity,summer)['minutes_from_us_open'],0)
        self.assertEqual(study._calendar(self.identity,winter)['minutes_from_us_open'],0)
        self.assertIsNone(study._calendar(self.identity,datetime(2026,11,26,18,tzinfo=timezone.utc))['minutes_from_us_open'])
        value=dict(status='PARTIAL',fetched_at=(self.when-timedelta(minutes=5)).isoformat(),
            articles=[dict(published=(self.when+timedelta(minutes=10)).isoformat(),url='https://example.org/future'),
                      dict(published=(self.when-timedelta(minutes=10)).isoformat(),url='https://example.org/earlier')])
        self.db.execute('INSERT INTO settings VALUES (?,?)',('news:market',json.dumps(value)))
        self.assertEqual([r['source'] for r in study._news(self.db,'market',self.when)['items']],
                         ['https://example.org/earlier'])

    def test_only_selected_events_block_buy_and_calendar_expiry_is_explicit(self):
        from paper_v5 import macro_release_day
        at=lambda iso:macro_release_day(datetime.fromisoformat(iso))
        self.assertEqual(at('2026-12-09T07:30:00+01:00')['kind'],'FOMC')
        self.assertEqual(at('2026-12-10T22:00:00+01:00')['kind'],'CPI')
        self.assertEqual(at('2026-11-18T20:00:00+01:00')['state'],'CLEAR')
        self.assertEqual(at('2027-01-04T09:00:00+01:00')['state'],'CALENDAR_UNCONFIRMED')
