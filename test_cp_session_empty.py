"""Verified empty session tail, including unchanged published price-only rows."""
import copy
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import pandas as pd
sys.path.insert(0, str(Path(__file__).parent / 'stock_monitor'))
import cp_reconciliation as rv
import revision_watcher as rw
import data_quality as dq
from test_cp_tv_legacy_reference import FlatStore


class SessionEmpty(unittest.TestCase):
    def test_ordinary_tail_proof_preserves_zero_row_and_rejects_missing_evidence(self):
        root = Path(os.environ.get('SM_TEST_ARCHIVE', str(Path(__file__).parent.parent.parent / 'СМ-Архив')))
        with tempfile.NamedTemporaryFile(dir=root, suffix='.sqlite3', delete=False) as f:
            path = Path(f.name)
        try:
            store = FlatStore(path)
            isin = 'US69608A1088'
            info = dict(isin=isin, name='Fixture', kind='stock', history_venue_lock='GETTEX')
            store.save_instrument(info)
            meta = dict(source='gettex_chart', venue='GETTEX', currency='EUR', symbol='PTXG.GTX', timezone='Europe/Berlin')
            official = {**meta, 'source': 'gettex', 'symbol': isin}
            now = pd.Timestamp('2026-11-14T12:00Z')
            first, end = '2026-11-13 22:30:00', '2026-11-13 23:00:00'
            gap = '2026-11-13 22:45:00'
            trades = [('2026-11-13 22:31:00', 151.16, 9.), ('2026-11-13 22:36:00', 150.4, 200.),
                      ('2026-11-13 22:39:00', 151.46, 196.), ('2026-11-13 22:43:00', 151.08, 0.)]
            rows = {'5m': [(first, 151.16, 151.16, 151.16, 151.16, 9.),
                           ('2026-11-13 22:35:00', 150.4, 151.46, 150.4, 151.46, 396.),
                           ('2026-11-13 22:40:00', 151.08, 151.08, 151.08, 151.08, 0.)],
                    '15m': [(first, 151.16, 151.46, 150.4, 151.08, 405.)]}
            proof = dict(isin=isin, identity=official, basis='complete_published_trade_files', checked_at=now.isoformat(),
                         no_trades=[[gap, end]], trade_intervals=[[first, '2026-11-13 22:35:00'],
                         ['2026-11-13 22:35:00', '2026-11-13 22:40:00']],
                         zero_quantity_intervals=[['2026-11-13 22:40:00', gap]], reference={'GETTEX:20261113': {}})
            for tm in ('21.45', '22.00'):
                for board in ('munc', 'mund'):
                    proof['reference']['GETTEX:20261113']['posttrade.20261113.' + tm + '.' + board + '.csv.gz'] = 'a' * 64
            policy = dict(version=rw.RETIME_VERSION, approved=True)
            store.set_setting(rw.RETIME_POLICY, policy)
            store.set_setting('interval_evidence:' + isin, proof)
            store.set_setting('data_quality:' + isin + ':' + meta['symbol'] + ':15m',
                              dict(state='STALE', identity=meta, ranges=[], stale_range=[gap, gap], stale_count=1))
            for base in rows:
                store.set_setting('gettex_chart:' + isin + ':' + base, dict(state='SAVED', identity=meta,
                    listing_proof={**meta, 'isin': isin, 'status': 'CONFIRMED', 'verified_at': now.isoformat()}))
            with store.connect() as db:
                db.executemany('INSERT INTO trades VALUES (?,?,?,?)', [(isin, *r) for r in trades])
                for base in rows:
                    db.executemany('INSERT INTO candles VALUES (?,?,?,?,?,?,?,?,?)', [(isin, 'gettex', base, *r) for r in rows[base]])
                    db.executemany('INSERT INTO series VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                                   [(isin, *[meta[k] for k in rw.IDENTITY[:4]], base, *r) for r in rows[base]])
                spec = rv._spec(isin, meta, '15m', gap, gap)
                inputs = rv._input(db, spec)
                plan = rw.retime_plan(db, spec, inputs, now)
                self.assertIsNotNone(plan)
                self.assertEqual(plan['mode'], 'VERIFIED_NATIVE_EMPTY')
                # The zero-quantity row is never itself certified empty.
                self.assertFalse(dq.no_trades_proof({**official, 'absence_evidence': proof},
                                                   pd.Timestamp('2026-11-13 22:40'), pd.Timestamp(gap), now))
                with self.assertRaisesRegex(ValueError, 'INCOMPLETE_PUBLICATION'):
                    rw._official_window(db, isin, first, end, now)
                saved = db.execute('SELECT * FROM series ORDER BY base_tf,ts').fetchall()
                # The same missing final observed bar at midday is not a session boundary.
                unrelated = rv._spec(isin, meta, '15m', '2026-11-13 12:45:00', '2026-11-13 12:45:00')
                other_inputs = dict(inputs, neighbors={'before': ('2026-11-13 12:30:00', *rows['15m'][0][1:]), 'after': None})
                self.assertIsNone(rw.empty_native_span(unrelated, other_inputs))
            bad = copy.deepcopy(proof)
            bad['reference']['GETTEX:20261113'].pop('posttrade.20261113.22.00.mund.csv.gz')
            store.set_setting('interval_evidence:' + isin, bad)
            with store.connect() as db:
                with self.assertRaisesRegex(ValueError, 'PAIR_MISSING'):
                    rw.retime_plan(db, spec, rv._input(db, spec), now)
            bad = copy.deepcopy(proof)
            bad['incomplete_publication_intervals'] = [[gap, end]]
            store.set_setting('interval_evidence:' + isin, bad)
            with store.connect() as db:
                with self.assertRaisesRegex(ValueError, 'INCOMPLETE_PUBLICATION'):
                    rw.retime_plan(db, spec, rv._input(db, spec), now)
            store.set_setting('interval_evidence:' + isin, proof)
            with store.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                db.execute("UPDATE series SET high=152 WHERE base_tf='5m' AND ts='2026-11-13 22:40:00'")
                self.assertIsNone(rw.retime_plan(db, spec, rv._input(db, spec), now))
                db.rollback()
            # No manual enqueue or OHLCV change: saved STALE report discovers the tail.
            with patch('gettex_history.attach_known_interruptions', return_value=False):
                rv.turn(store, info, now)
            receipt = store.setting(rw.RETIME_PREFIX + isin + ':' + spec['id'])
            self.assertEqual((receipt['added'], receipt['changed'], receipt['relocated']), (0, 0, 0))
            self.assertEqual(store.setting(rv.key(spec))['status'], 'RESOLVED_NATIVE')
            frame = store.load_series(isin, '15m', **{k: meta[k] for k in ('source', 'symbol', 'venue', 'currency')})
            self.assertEqual(dq.audit(frame, '15m', frame.attrs, now)['state'], 'READY')
            self.assertEqual(list(frame.index), [pd.Timestamp(first)])
            with store.connect() as db:
                self.assertEqual(db.execute('SELECT * FROM series ORDER BY base_tf,ts').fetchall(), saved)
                self.assertEqual(db.execute('SELECT COUNT(*) FROM candle_revision_history').fetchone()[0], 0)
                self.assertTrue(rw.retime_current(db, receipt, now))
                self.assertEqual(rw.retimed_rows(db, isin, meta, '15m'), {})
                # Connecting a stock after close is separate from executing at
                # its old price. The actual quote timestamp is never advanced.
                import paper_preparation as prep
                import paper_account as paper
                result = dict(_input_revisions=dict(db.execute('SELECT scope,revision FROM input_revisions')),
                    hourly={'proof': {'series': [{'spec': dict(isin=isin, identity=meta, timeframe='1D',
                                                              through='2026-11-13T22:00Z')}]}})
                report = prep.closed_session_archive(db, info, meta, result, now.timestamp())
                self.assertEqual(report['tail_at'], first)
                self.assertEqual(report['confirmed_empty_tail']['first'], gap)
                self.assertEqual(report['execution_blocker'], 'PAPER_SESSION_CLOSED')
                bad_result = copy.deepcopy(result)
                bad_result['_input_revisions'].pop('setting:' + rw.RETIME_PREFIX + isin + ':' + spec['id'])
                with self.assertRaisesRegex(ValueError, 'SESSION_TAIL_MISSING'):
                    prep.closed_session_archive(db, info, meta, bad_result, now.timestamp())
                with self.assertRaisesRegex(ValueError, 'PAPER_SESSION_CLOSED'):
                    paper.validate_session(meta, now.timestamp())
                with self.assertRaisesRegex(ValueError, 'REQUIRES_CLOSED_SESSION'):
                    prep.closed_session_archive(db, info, meta, result, pd.Timestamp('2026-11-16T10:00Z').timestamp())
            store.set_setting(rw.RETIME_POLICY, {**policy, 'approved': False})
            with store.connect() as db:
                with self.assertRaisesRegex(ValueError, 'SESSION_TAIL_MISSING'):
                    prep.closed_session_archive(db, info, meta, result, now.timestamp())
            frame = store.load_series(isin, '15m', **{k: meta[k] for k in ('source', 'symbol', 'venue', 'currency')})
            self.assertEqual(dq.audit(frame, '15m', frame.attrs, now)['state'], 'STALE')
        finally:
            if 'store' in locals():
                store.read_cache.close()
            for suffix in ('', '-wal', '-shm'):
                Path(str(path) + suffix).unlink(missing_ok=True)


if __name__ == '__main__':
    unittest.main()
