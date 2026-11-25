"""Read-only, point-in-time news for the long-term V1 portfolio."""
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
import sqlite3
import unittest

import test_paper_v1_long_term as long_term
from news_engine import normalize
from news_translation import fingerprint, setting as translation_key
from paper_v1_news import CALENDAR_SETTING, context, evidence


NOW = datetime(2026, 11, 23, 13, tzinfo=timezone.utc)
INFO = dict(isin='US0378331005', name='Apple', profile_symbol='AAPL')


class NewsTests(unittest.TestCase):
    def setUp(self):
        self.db = self.enterContext(closing(sqlite3.connect(':memory:')))
        self.db.execute('CREATE TABLE settings (key TEXT PRIMARY KEY,value TEXT)')

    def save(self, key, value):
        self.db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)', (key, json.dumps(value)))

    def article(self, code, **changes):
        row = dict(title='Apple earnings '+code, url='https://example.org/'+code,
                   summary='Original text', provider='Issuer', scope='company',
                   published=(NOW-timedelta(hours=2)).isoformat(),
                   fetched_at=(NOW-timedelta(hours=1)).isoformat(), isin=INFO['isin'], symbol='AAPL')
        row.update(changes)
        return row

    def company(self, rows, **changes):
        fetched = (NOW-timedelta(hours=1)).isoformat()
        packet = dict(status='AVAILABLE', identity=dict(isin=INFO['isin'], name='Apple', symbol='AAPL'),
                      fetched_at=fetched, attempted_at=fetched, articles=rows)
        packet.update(changes)
        self.save('news:'+INFO['isin'], packet)

    def test_selects_only_news_known_at_decision_for_exact_company(self):
        good = self.article('known')
        tomorrow = (NOW+timedelta(days=1)).isoformat()
        self.company([good, self.article('duplicate', url=good['url']+'?utm_source=foo'),
                      self.article('future-publication', published=tomorrow),
                      self.article('future-fetch', fetched_at=tomorrow),
                      self.article('unknown-time', published=None),
                      self.article('too-old', published=(NOW-timedelta(days=8)).isoformat()),
                      self.article('wrong-instrument', isin='US67066G1040'),
                      self.article('wrong-symbol', symbol='NVDA'),
                      self.article('corrupt', url={})])
        self.save('news:market', dict(status='PARTIAL', fetched_at=good['fetched_at'],
            articles=[good, self.article('market', scope='market')]))
        snapshot = evidence(self.db, INFO, NOW)
        self.assertEqual(snapshot['company']['recent'], 1)
        self.assertEqual(snapshot['market']['recent'], 1)
        self.assertEqual(snapshot['market']['status'], 'PARTIAL')
        self.assertEqual(snapshot['company']['articles'][0]['title'], good['title'])
        self.assertNotIn('summary', snapshot['company']['articles'][0])
        self.assertEqual(context(self.db, {**INFO,'profile_symbol':'OTHER'}, NOW)['company']['status'],
                         'IDENTITY_CHANGED')
        self.assertEqual(context(self.db, INFO, NOW-timedelta(hours=2))['company']['status'],
                         'AFTER_CUTOFF')

    def test_stale_legitimate_references_and_nearby_official_dates(self):
        self.company([self.article('known')], fetched_at=(NOW-timedelta(hours=4)).isoformat())
        self.save(CALENDAR_SETTING, {'sources':{'FED':{'status':'AVAILABLE',
            'fetched_at':(NOW-timedelta(days=3)).isoformat(), 'rows':[
                dict(kind='FOMC', date='2026-12-01', end_date='2026-12-02', original='Official dates'),
                dict(kind='FOMC', date='2026-12-30', end_date='2026-12-30'),
                dict(kind='FOMC', date='2026-11-21', end_date='2026-11-21')]}}})
        snapshot = context(self.db, INFO, NOW)
        self.assertEqual(snapshot['company']['status'], 'STALE_CACHE')
        self.assertEqual(snapshot['company']['recent'], 1)
        self.assertEqual(snapshot['calendar']['sources']['FED'], 'STALE_CACHE')
        self.assertEqual([row['date'] for row in snapshot['calendar']['events']], ['2026-12-01'])
        self.assertTrue(snapshot['calendar']['events'][0]['stale'])

    def test_translation_is_only_a_display_projection(self):
        row = self.article('known')
        self.company([row])
        translation = dict(target='ru', status='TRANSLATED', title='Translated title',
                           summary='Translated summary')
        article_hash = fingerprint(normalize([row], NOW.timestamp())[0])
        self.save(translation_key(INFO['isin'], 'ru'), {'items':{article_hash:translation}})
        translated = context(self.db, INFO, NOW, language='ru')['company']['articles'][0]
        self.assertEqual(translated['title'], 'Translated title')
        self.assertEqual(translated['original_title'], row['title'])
        self.assertEqual(evidence(self.db, INFO, NOW)['company']['articles'][0]['title'], row['title'])

    def test_unavailable_news_preserves_buy_and_is_saved_in_receipt(self):
        fixture = long_term.LongTermTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        f = fixture.f
        baseline = fixture.command()
        background = evidence(self.db, INFO, NOW)
        f.result['news_background'] = background
        f.args = fixture.command()
        self.assertEqual(f.args, baseline)
        fill = f.trade()['fill']
        self.assertEqual(fill['side'], 'BUY')
        self.assertEqual(fill['smart_eligibility']['news_background'], background)

    def test_company_events_separate_earnings_dates_from_report_periods(self):
        from company_events import PREFIX
        timestamp=(NOW-timedelta(hours=3)).isoformat()
        future=(NOW+timedelta(hours=1)).isoformat()
        report=dict(metric='TotalRevenue',period_end='2026-09-30',value=10,currency='USD',
                    updated_at=timestamp,frequency='quarterly')
        self.save(PREFIX+INFO['isin'],dict(isin=INFO['isin'],symbol='AAPL',calendar_fetched_at=timestamp,
            calendar={'Earnings Date':['2026-12-01','2026-12-02'],'Dividend Date':'2026-11-27'},
            reports=[report,{**report,'value':11,'period_end':'2026-11-20','updated_at':future}]))
        self.save('fundamentals:'+INFO['isin'],dict(isin=INFO['isin'],symbol='AAPL',updated_at=future,
            calendar={'Earnings Date':['2026-12-09']},metrics=[]))
        packet=evidence(self.db,INFO,NOW)['company_events']
        self.assertEqual([(r['kind'],r['date'],r['end_date']) for r in packet['dates']],
            [('EARNINGS_DATE','2026-12-01','2026-12-02'),('DIVIDEND_PAYMENT','2026-11-27','2026-11-27')])
        self.assertEqual([(r['period_end'],r['value'],r['currency']) for r in packet['reports']],
            [('2026-09-30',10,'USD')])
        self.assertEqual(context(self.db,{**INFO,'profile_symbol':'NVDA'},NOW)['company_events']['reports'],[])
        from paper_v1_news_view import company_rows
        self.assertEqual(company_rows(packet)[-1]['kind'],'QUARTERLY_REPORT')

    def test_provider_failure_keeps_stale_dates_and_reports_without_redating(self):
        from company_events import fetch
        from unittest.mock import Mock
        ticker=Mock()
        ticker.get_calendar.side_effect=RuntimeError('offline')
        stamp=datetime.now(timezone.utc).isoformat()
        old=dict(isin=INFO['isin'],symbol='AAPL',calendar_fetched_at=stamp,reports_attempted_at=stamp,
                 calendar={'Earnings Date':['2026-12-01']},reports=[dict(value=10)])
        packet=fetch(INFO,old,'.',ticker=ticker)
        self.assertEqual(packet['calendar_status'],'STALE_CACHE')
        self.assertEqual(packet['calendar_fetched_at'],stamp)
        self.assertEqual(packet['reports'],old['reports'])
        ticker.get_income_stmt.assert_not_called()


if __name__ == '__main__':
    unittest.main()
