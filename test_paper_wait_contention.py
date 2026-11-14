"""One affected WAL-contention scenario; no live account or network."""
import copy
import gc
import os
import sqlite3
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import test_paper_protection as fixture
import paper_hourly as hourly


class WaitContentionTests(unittest.TestCase):
    def test_wait_during_cp_writer_and_protective_revalidation(self):
        p = fixture.ProtectionTests(); p.setUp()
        self.addCleanup(p.doCleanups)
        f = p.f
        root = Path(os.environ.get('SM_TEST_ARCHIVE', tempfile.gettempdir()))
        fd, name = tempfile.mkstemp(prefix='paper-wait-', suffix='.sqlite3', dir=root)
        os.close(fd)
        def cleanup():
            gc.collect()
            for suffix in ('', '-wal', '-shm'):
                Path(name + suffix).unlink(missing_ok=True)
        self.addCleanup(cleanup)
        def snapshot():
            f.db.commit()
            with connect() as db:
                f.db.backup(db)
                db.execute('pragma journal_mode=wal')
        @contextmanager
        def connect():
            db = sqlite3.connect(name, timeout=.2)
            try:
                with db: yield db
            finally: db.close()
        def compute(_kind, values):
            result = copy.deepcopy(f.result)
            result['timeframe'] = values['timeframe']
            return result
        writes = {}
        app = SimpleNamespace(closing=False, user_dirty=False,
            store=SimpleNamespace(path=name, connect=connect,
                set_setting=lambda k,v: writes.update({k:v})),
            compute_trading=SimpleNamespace(calculate=compute))
        def run():
            with patch.object(hourly.time, 'time', return_value=f.now):
                return hourly.run_turn(app, f.info, {}, f.cfg)
        def count():
            with connect() as db: return db.execute('select count(*) from paper_fills').fetchone()[0]

        # Real concurrent WAL writer: WAIT without a position needs no write lock.
        p.s.prepare('WAIT'); snapshot()
        with connect() as writer:
            writer.execute('begin immediate')
            result = run()
            self.assertTrue(writer.in_transaction)
            self.assertEqual([r['state'] for r in result['timeframes'].values()], ['WAIT','WAIT'])
            self.assertFalse(any(r.get('execution_blocker') for r in result['timeframes'].values()))
        self.assertEqual(count(), 0)

        # Existing bracket is still checked on WAIT; a real exit must acquire
        # the write lock, then re-evaluate its trigger and all execution guards.
        p.s.prepare('BUY'); p.buy(); p.advance(94); snapshot()
        with connect() as writer:
            writer.execute('begin immediate')
            result = run()
            self.assertIn('database is locked', result['timeframes']['1H']['execution_blocker'])
        self.assertEqual(count(), 1)
        f.result['quote_blocker'] = 'PAPER_STALE_QUOTE'
        self.assertIn('PAPER_STALE_QUOTE', run()['timeframes']['1H']['execution_blocker'])
        self.assertEqual(count(), 1)
        f.result.pop('quote_blocker')

        original = fixture.protection.decision
        calls = []
        connections = []
        def changed_trigger(db, info, result, now):
            calls.append(True)
            connections.append(db)
            if len(calls) == 2:
                result['execution_quote']['bars'][0].update(open=100,high=101,low=99,close=100)
            return original(db, info, result, now)
        with patch.object(fixture.protection, 'decision', side_effect=changed_trigger):
            result = run()
        for db in connections: db.close()
        self.assertGreaterEqual(len(calls), 2)
        self.assertEqual(result['timeframes']['1H']['state'], 'WAIT', result)
        self.assertEqual(count(), 1)

        result = run()
        self.assertEqual(result['timeframes']['1H']['state'], 'COMPLETED')
        self.assertEqual(result['timeframes']['1H']['protective_exit']['reason'], 'STOP_LOSS')
        self.assertEqual(count(), 2)


if __name__ == '__main__': unittest.main()
