"""One bounded CP scenario: confirmed 5m timestamp relocation, not data deletion."""
import copy
import json
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
from test_cp_tv_legacy_reference import FlatStore


class ExactRelocation(unittest.TestCase):
    def test_closed_conserved_relocation_preserves_original_and_revokes_on_change(self):
        root = Path(os.environ.get('SM_TEST_ARCHIVE', str(Path(__file__).parent.parent.parent / 'СМ-Архив')))
        with tempfile.NamedTemporaryFile(dir=root, suffix='.sqlite3', delete=False) as f:
            path = Path(f.name)
        try:
            store = FlatStore(path)
            isin = 'US11135F1012'
            info = dict(isin=isin, name='Fixture', kind='stock', history_venue_lock='GETTEX')
            store.save_instrument(info)
            meta = dict(source='gettex_chart', venue='GETTEX', currency='EUR', symbol='1YD.GTX', timezone='Europe/Berlin')
            official = {**meta, 'source': 'gettex', 'symbol': isin}
            now = pd.Timestamp('2026-11-14T12:00Z')
            first, end = '2026-11-02 10:30:00', '2026-11-02 11:00:00'
            moved_from, moved_to = '2026-11-02 10:45:00', '2026-11-02 10:40:00'
            trades = [('2026-11-02 10:40:01', 294.25, .5), ('2026-11-02 10:40:02', 294., .5),
                      ('2026-11-02 10:55:01', 294.55, 1.)]
            rows = {'5m': [(moved_to, 294.25, 294.25, 294., 294., 1.),
                           ('2026-11-02 10:55:00', 294.55, 294.55, 294.55, 294.55, 1.)],
                    '15m': [(first, 294.25, 294.25, 294., 294., 1.),
                            (moved_from, 294.55, 294.55, 294.55, 294.55, 1.)]}
            native = {'5m': [(moved_from, *rows['5m'][0][1:]), rows['5m'][1]],
                      '15m': [(moved_from, 294.25, 294.55, 294., 294.55, 2.)]}
            proof = dict(isin=isin, identity=official, basis='complete_published_trade_files', checked_at=now.isoformat(),
                         no_trades=[], trade_intervals=[], zero_quantity_intervals=[], reference={'GETTEX:20261102': {}})
            for at in pd.date_range(first, end, freq='5min', inclusive='left'):
                field = 'trade_intervals' if str(at) in {r[0] for r in rows['5m']} else 'no_trades'
                proof[field].append([str(at), str(at + pd.Timedelta(minutes=5))])
            for tm in ('09.45', '10.00'):
                for board in ('munc', 'mund'):
                    proof['reference']['GETTEX:20261102']['posttrade.20261102.' + tm + '.' + board + '.csv.gz'] = 'a' * 64
            for base in ('5m', '15m'):
                store.set_setting('gettex_chart:' + isin + ':' + base, dict(state='SAVED', identity=meta,
                    listing_proof={**meta, 'isin': isin, 'status': 'CONFIRMED', 'verified_at': now.isoformat()}))
            policy = dict(version=rw.RETIME_VERSION, approved=True)
            store.set_setting(rw.RETIME_POLICY, policy)
            store.set_setting('interval_evidence:' + isin, proof)
            with store.connect() as db:
                db.executemany('INSERT INTO trades VALUES (?,?,?,?)', [(isin, *r) for r in trades])
                for base in rows:
                    db.executemany('INSERT INTO candles VALUES (?,?,?,?,?,?,?,?,?)', [(isin, 'gettex', base, *r) for r in rows[base]])
                    db.executemany('INSERT INTO series VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                                   [(isin, *[meta[k] for k in rw.IDENTITY[:4]], base, *r) for r in native[base]])
                job, _ = rv._enqueue(db, rv._spec(isin, meta, '15m', first, first), {'kind': 'test'}, now)
                inputs = rv._input(db, job)
                plan = rw.retime_plan(db, job, inputs, now)
                self.assertEqual(plan['relocations'], [dict(base_tf='5m', old_at=moved_from, new_at=moved_to,
                                                          ohlcv=list(rows['5m'][0][1:]))])
                # A real change between planning and publication must abort before any write.
                db.execute("UPDATE series SET close=294.1 WHERE base_tf='5m' AND ts=?", (moved_from,))
                with self.assertRaisesRegex(ValueError, 'RETIME_'):
                    rw.publish_retime(db, job, inputs, plan, now)
                db.rollback()
            # Reinsert the initial job/market fixture rolled back with the failed transaction.
            with store.connect() as db:
                db.executemany('INSERT INTO trades VALUES (?,?,?,?)', [(isin, *r) for r in trades])
                for base in rows:
                    db.executemany('INSERT INTO candles VALUES (?,?,?,?,?,?,?,?,?)', [(isin, 'gettex', base, *r) for r in rows[base]])
                    db.executemany('INSERT INTO series VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                                   [(isin, *[meta[k] for k in rw.IDENTITY[:4]], base, *r) for r in native[base]])
                job, _ = rv._enqueue(db, rv._spec(isin, meta, '15m', first, first), {'kind': 'test'}, now)
                # Whole-window OHLCV can still agree while the displaced candle does not.
                db.execute("UPDATE series SET high=294.3 WHERE base_tf='5m' AND ts=?", (moved_from,))
                with self.assertRaisesRegex(ValueError, 'RETIME_RELOCATION_NOT_EXACT'):
                    rw.retime_plan(db, job, rv._input(db, job), now)
                db.execute("UPDATE series SET high=294.25 WHERE base_tf='5m' AND ts=?", (moved_from,))
            bad = copy.deepcopy(proof)
            bad['reference']['GETTEX:20261102'].pop('posttrade.20261102.09.45.mund.csv.gz')
            store.set_setting('interval_evidence:' + isin, bad)
            with store.connect() as db:
                with self.assertRaisesRegex(ValueError, 'PAIR_MISSING'):
                    rw.retime_plan(db, job, rv._input(db, job), now)
            store.set_setting('interval_evidence:' + isin, proof)
            with patch('gettex_history.attach_known_interruptions', return_value=False):
                rv.turn(store, info, now)
            receipt = store.setting(rw.RETIME_PREFIX + isin + ':' + job['id'])
            self.assertEqual((receipt['added'], receipt['changed'], receipt['relocated']), (1, 1, 1))
            with store.connect() as db:
                for base in rows:
                    self.assertEqual(rw._native(db, isin, meta, base, first, end), rows[base])
                history = db.execute('SELECT base_tf,ts,old_ohlcv,new_ohlcv,revision_before,revision_after FROM candle_revision_history').fetchall()
                self.assertEqual(len(history), 4)
                removed = next(r for r in history if r[0] == '5m' and r[1] == moved_from)
                self.assertEqual(json.loads(removed[2]), dict(zip(rw.FIELDS, native['5m'][0][1:])))
                self.assertEqual(json.loads(removed[3]), {})
                self.assertGreater(removed[5], removed[4])
                self.assertIsNone(rw.retimed_rows(db, isin, meta, '5m')[moved_from])
                self.assertEqual(rv._classify(job, rv._input(db, job), now)['state'], 'NATIVE_ROWS_CONFIRMED')
            store.read_cache.close()
            store = FlatStore(path)
            with store.connect() as db:
                self.assertTrue(rw.retime_current(db, receipt, now))
            store.set_setting(rw.RETIME_POLICY, {**policy, 'approved': False})
            with store.connect() as db:
                self.assertFalse(rw.retime_current(db, receipt, now))
                self.assertEqual(rw.retimed_rows(db, isin, meta, '5m'), {})
            store.set_setting(rw.RETIME_POLICY, policy)
            with store.connect() as db:
                db.execute("UPDATE trades SET price=295 WHERE ts=?", (trades[0][0],))
                self.assertFalse(rw.retime_current(db, receipt, now))
                self.assertEqual(rw.retimed_rows(db, isin, meta, '5m'), {})
        finally:
            if 'store' in locals():
                store.read_cache.close()
            for suffix in ('', '-wal', '-shm'):
                Path(str(path) + suffix).unlink(missing_ok=True)


if __name__ == '__main__':
    unittest.main()
