"""Exact owner-approved missing ranges; no price fabrication or global waiver."""
import sqlite3
import unittest
from unittest.mock import patch

import cp_controls as cp


class ReviewedRanges(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.addCleanup(self.db.close)
        self.db.executescript('''CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT);
            CREATE TABLE series(isin,source,venue,currency,symbol,base_tf,ts,open,high,low,close,volume);''')
        self.info = {'isin': 'TEST'}
        self.meta = dict(source='gettex_chart', symbol='TEST.GTX', venue='GETTEX',
                         currency='EUR', timezone='Europe/Berlin')
        self.problem = dict(isin='TEST', kind='archive', identity=self.meta, tf='15m',
            range=['2026-10-26 07:45:00','2026-10-26 09:30:00'], report_key='dq')
        cp.write(self.db, 'dq', dict(state='UNCONFIRMED', identity=self.meta))
        self.add_row('2026-10-26 07:30:00')
        self.add_row('2026-10-26 09:45:00')
        self.proof = patch('eur_listings.stored_identity', return_value=True)
        self.proof.start(); self.addCleanup(self.proof.stop)

    def add_row(self, ts):
        self.db.execute('INSERT INTO series VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
            ('TEST','gettex_chart','GETTEX','EUR','TEST.GTX','15m',ts,100,101,99,100,10))

    def binding(self, problem=None):
        return cp._reviewed_binding(self.db,self.info,problem or self.problem)

    def test_consecutive_missing_range_binds_without_writing_prices(self):
        before = self.db.execute('SELECT * FROM series').fetchall()
        self.assertEqual(len(self.binding()),64)
        self.assertEqual(before,self.db.execute('SELECT * FROM series').fetchall())

    def test_any_restored_bar_within_range_invalidates_scope(self):
        self.binding()
        self.add_row('2026-10-26 08:30:00')
        with self.assertRaisesRegex(ValueError,'REVIEW_NO_LONGER_MISSING'):self.binding()

    def test_neighbour_revision_changes_binding_unrelated_tail_does_not(self):
        old = self.binding()
        self.add_row('2026-11-04 10:00:00')
        self.assertEqual(old,self.binding())
        self.db.execute("UPDATE series SET volume=11 WHERE ts='2026-10-26 09:45:00'")
        self.assertNotEqual(old,self.binding())

    def test_reversed_timezone_or_unaligned_scope_rejected(self):
        for span in (self.problem['range'][::-1],['2026-10-26 07:46:00','2026-10-26 09:30:00'],
                     ['2026-10-26 07:45:00+01:00','2026-10-26 09:30:00+01:00']):
            with self.subTest(span=span),self.assertRaisesRegex(ValueError,'REVIEW_SCOPE_UNSUPPORTED'):
                self.binding(dict(self.problem,range=span))

    def test_identity_and_invalid_quality_remain_blocked(self):
        for field,value in [('currency','USD'),('venue','FRA'),('source','proxy')]:
            with self.subTest(field=field),self.assertRaisesRegex(ValueError,'REVIEW_SCOPE_UNSUPPORTED'):
                self.binding(dict(self.problem,identity=dict(self.meta,**{field:value})))
        cp.write(self.db,'dq',dict(state='INVALID',identity=self.meta))
        with self.assertRaisesRegex(ValueError,'REVIEW_DQ_BLOCKED'):self.binding()

    def test_unconfirmed_listing_rejected(self):
        with patch('eur_listings.stored_identity',return_value=False):
            with self.assertRaisesRegex(ValueError,'REVIEW_IDENTITY_UNCONFIRMED'):self.binding()

    def test_following_session_neighbour_is_part_of_binding(self):
        self.db.execute('DELETE FROM series')
        self.add_row('2026-10-16 22:30:00')
        self.add_row('2026-10-19 07:30:00')
        self.problem['range']=['2026-10-16 22:45:00']*2
        old=self.binding()
        self.db.execute("UPDATE series SET volume=11 WHERE ts='2026-10-19 07:30:00'")
        self.assertNotEqual(old,self.binding())


if __name__ == '__main__':unittest.main()
