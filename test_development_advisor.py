import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from contextlib import closing
import os

sys.path.insert(0, str(Path(__file__).parent / 'stock_monitor'))
from development_advisor import analyse, collect, collect_shadow, epoch, render, VALUE_LIMIT


class DevelopmentAdvisorTests(unittest.TestCase):
    def setUp(self):
        self.now = epoch('2026-11-13T18:00:00+00:00')

    def database_path(self):
        archive = Path(__file__).resolve().parent.parent.parent / 'СМ-Архив'
        fd, name = tempfile.mkstemp(prefix='advisor49-test-', suffix='.sqlite3', dir=archive if archive.is_dir() else None)
        os.close(fd)
        path = Path(name)
        self.addCleanup(path.unlink, missing_ok=True)
        return path

    def ids(self, report):
        return {x['id']: x for x in report['advice']}

    def test_saved_current_does_not_override_expiry_or_new_assessment(self):
        records = [('cp_eligibility:A', {'checked_at': self.now-300, 'expires_at': self.now-10, 'publication_state': 'CURRENT'}),
                   ('cp_assessment_request:A', {'requested_at': self.now-900}),
                   ('cp_eligibility:B', {'checked_at': self.now-1000, 'expires_at': self.now+5}),
                   ('cp_assessment_request:B', {'requested_at': self.now-800})]
        r = analyse(records, self.now)
        self.assertEqual(r['facts']['expired_assessments'], 1)
        self.assertIn('ASSESSMENT_AGE', self.ids(r))
        self.assertIn('без более новой сохранённой оценки: 1.', self.ids(r)['ASSESSMENT_QUEUE']['evidence'])

    def test_wait_and_provider_cooldown_are_not_errors(self):
        r = analyse([('paper_auto_status:A', {'at': self.now, 'core': 'READY', 'signal': 'WAIT', 'quote_blocker': None}),
                     ('cp_reconciliation_job:A:1', {'status': 'AWAITING_SOURCE', 'next_check': self.now+3600})], self.now)
        self.assertNotIn('QUOTE_FRESHNESS', self.ids(r))
        self.assertEqual(self.ids(r)['SOURCE_WAIT']['priority'], 3)
        self.assertIn('ещё не наступила у 1', self.ids(r)['SOURCE_WAIT']['evidence'])

    def test_only_recent_quote_failure_is_current_observation(self):
        r = analyse([('paper_auto_status:A', {'at': self.now-301, 'quote_blocker': 'PAPER_STALE_QUOTE'}),
                     ('paper_auto_status:B', {'at': self.now+1, 'quote_blocker': True}),
                     ('paper_auto_status:C', {'at': self.now, 'quote_blocker': True})], self.now)
        self.assertIn('цена заблокирована в 1.', self.ids(r)['QUOTE_FRESHNESS']['evidence'])
        self.assertEqual(r['facts']['old_paper_reports'], 2)

    def test_unknown_and_empty_are_not_zero_problems_claim(self):
        r = analyse([], self.now)
        self.assertIn('не полная проверка', render(r))
        r = analyse([('cp_eligibility:A', {'expires_at': '2026-11-13 18:00:00'})], self.now)
        self.assertIn('срок неизвестен у 1', self.ids(r)['ASSESSMENT_AGE']['evidence'])
        self.assertIsNone(epoch(float('nan')))
        self.assertIsNone(epoch(True))

    def test_saved_dq_is_not_a_stock_count_or_trade_permission(self):
        r = analyse([('data_quality:A:X:15m', {'state': 'UNCONFIRMED'}),
                     ('data_quality:A:Y:15m', {'state': 'INVALID'})], self.now)
        self.assertEqual(r['facts']['saved_dq_reports'], 2)
        self.assertIn('не число заблокированных акций', self.ids(r)['DATA_EVIDENCE']['evidence'])
        self.assertNotIn('trade_ready', r)

    def test_read_only_export_excludes_private_payloads_and_marks_incomplete(self):
        path = self.database_path()
        with closing(sqlite3.connect(path)) as db:
            db.execute('create table settings(key text primary key, value text)')
            db.executemany('insert into settings values (?,?)', [
                ('api_token', 'PRIVATE-SECRET'),
                ('paper_account', json.dumps({'cash': 123456, 'secret': 'PRIVATE-ACCOUNT'})),
                ('data_quality:A:15m', json.dumps({'state': 'INVALID', 'issues': ['PRIVATE-ERROR'], 'api_key': 'PRIVATE-SECRET'})),
                ('cp_reconciliation_job:A:1', json.dumps({'status': 'AWAITING_USER', 'next_check': self.now+1, 'origin': 'PRIVATE-PATH'})),
                ('cp_eligibility:broken', '{broken'),
                ('cp_eligibility:large', json.dumps({'padding': 'x' * VALUE_LIMIT})),
            ])
            original = db.execute('select * from settings order by key').fetchall()
            db.commit()
        r = collect(path, self.now)
        self.assertEqual(r['omitted'], 2)
        self.assertIn('DIAGNOSTIC_COVERAGE', self.ids(r))
        self.assertNotIn('PRIVATE', json.dumps(r))
        self.assertNotIn('PRIVATE', render(r))
        with closing(sqlite3.connect(path)) as db:
            self.assertEqual(original, db.execute('select * from settings order by key').fetchall())

    def test_missing_database_not_created(self):
        path = self.database_path()
        path.unlink()
        with self.assertRaises(sqlite3.OperationalError):
            collect(path, self.now)
        self.assertFalse(path.exists())

    def test_two_numbered_sections_and_missing_shadow_are_explicit(self):
        r = analyse([('data_quality:A', {'state': 'INVALID'}),
                     ('paper_auto_status:A', {'at': self.now, 'core': 'READY', 'signal': 'WAIT'})], self.now)
        programme, trading = render(r, 1), render(r, 2)
        self.assertIn('1.1.', programme)
        self.assertNotIn('2. Развитие торговли', programme)
        self.assertIn('2.1.', trading)
        self.assertIn('2.2.', trading)
        self.assertNotIn('1. Развитие программы', trading)
        self.assertIn('не принят за ноль', trading)
        self.assertNotIn('PRIVATE', render(r))

    def test_shadow_read_only_counts_without_account_details(self):
        path = self.database_path()
        with closing(sqlite3.connect(path)) as db:
            db.execute('create table books(name text primary key,state text)')
            db.executemany('insert into books values (?,?)', [(name,json.dumps({'closed': count, 'cash': 'PRIVATE', 'positions': 'PRIVATE'}))
                                                          for name, count in [('V1',0), ('V2',2)]])
            db.commit()
        s = collect_shadow(path)
        self.assertEqual(s, {'state':'AVAILABLE', 'books':{'V1':0, 'V2':2}})
        r = analyse([], self.now, shadow=s)
        self.assertIn('преимущество новой стратегии ещё не подтверждено', render(r,2))
        self.assertNotIn('PRIVATE', render(r))

    def test_missing_eligibility_is_not_proven_queue_delay(self):
        r = analyse([('cp_assessment_request:A', {'requested_at':self.now-10000})], self.now, omitted=1)
        self.assertNotIn('ASSESSMENT_QUEUE', self.ids(r))
        self.assertIn('DIAGNOSTIC_COVERAGE', self.ids(r))


if __name__ == '__main__':
    unittest.main()
