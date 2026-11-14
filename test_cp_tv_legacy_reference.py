"""One targeted v3 full-hash reconstruction regression; isolated files, no market I/O."""
import copy
import hashlib
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent / 'stock_monitor'))
import cp_controls as ctl
import cp_reconciliation as rv
from storage import Store


class FlatStore(Store):
    """Keep the isolated SQLite in an existing directory, not a new tree."""
    def __init__(self, filename):
        self.filename = filename
        super().__init__(filename.parent)

    @property
    def path(self):
        return self.filename

    @path.setter
    def path(self, value):
        pass


class LegacyReference(unittest.TestCase):
    def test_complete_v3_hash_survives_added_lock_and_rechecks_before_publish(self):
        files = []
        def temporary(suffix):
            with tempfile.NamedTemporaryFile(prefix='sm-tv-legacy-', suffix=suffix,
                    dir=os.environ.get('SM_TEST_TMP'), delete=False) as handle:
                path = Path(handle.name)
            files.append(path)
            return path
        dbfile, reference = temporary('.sqlite3'), temporary('.json')
        store = FlatStore(dbfile)
        try:
            info = dict(isin='US0378331005', name='Fixture', kind='stock')
            meta = dict(source='gettex', symbol=info['isin'], venue='GETTEX',
                        currency='EUR', timezone='Europe/Berlin')
            store.save_instrument(info)
            first = '2026-10-28 10:15:00'
            now = pd.Timestamp('2026-11-02T14:00Z')
            spec = rv._spec(info['isin'], meta, '15m', first, first)
            with store.connect() as db:
                for at, close in [('2026-10-28 10:00:00', 100.), ('2026-10-28 10:30:00', 101.)]:
                    db.execute('INSERT INTO candles VALUES (?,?,?,?,?,?,?,?,?)',
                        (info['isin'], 'gettex', '15m', at, close, close+1, close-1, close, 10.))
                native = rv._input(db, spec)
            native['fingerprint'] = rv._fingerprint(native, version=3)
            raw = json.dumps(dict(records=[dict(spec=spec, native=native)])).encode()
            reference.write_bytes(raw)
            origin = dict(kind='prior_human_diagnosis', native_reference=str(reference),
                          native_sha256=hashlib.sha256(raw).hexdigest())
            original = dict(result='ABSENT', tv_symbol='GETTEX:APC', identity_confirmed=True,
                at='2026-11-01T12:00:00+00:00', input_fingerprint=native['fingerprint'],
                fingerprint_version=3, actor='human_assisted', evidence=[{'sha256':'visual-proof'}])
            with store.connect() as db:
                job, _ = rv._enqueue(db, spec, origin, now)
                prior = dict(job)
                job.update(observation=original, status='AWAITING_SOURCE')
                rv._save_job(db, job, prior)
            store.save_instrument(dict(info, history_venue_lock='GETTEX'))
            store.set_setting(ctl.TV_POLICY, dict(version=ctl.TV_VERSION, enabled=True, approved=True))
            store.set_setting('data_quality:'+info['isin']+':'+meta['symbol']+':15m',
                dict(state='UNCONFIRMED', identity=meta, base_tf='15m', missing_count=1,
                     ranges=[[first, first]], reasons=['Fixture gap']))
            with store.connect() as db:
                current = rv._input(db, spec)
                before = {t: db.execute('SELECT * FROM '+t+' ORDER BY 1').fetchall()
                          for t in ('instruments', 'candles', 'series')}
            self.assertNotEqual(rv._fingerprint(current, version=3), original['input_fingerprint'])
            self.assertTrue(rv._observation_current(job, current))
            self.assertEqual(original['fingerprint_version'], 3)
            for change in ('price', 'proof', 'identity', 'unproven_name', 'kind'):
                with self.subTest(reject=change):
                    changed = copy.deepcopy(current)
                    if change == 'price':
                        row = list(changed['neighbors']['before']); row[4] += .01
                        changed['neighbors']['before'] = row
                    elif change == 'proof':
                        changed['proof']['trade_intervals'] = [[first, '2026-10-28 10:30:00']]
                    elif change == 'identity':
                        changed['listing']['currency'] = 'USD'
                    elif change == 'kind':
                        changed['info']['kind'] = 'benchmark'
                    else:
                        changed['info']['name'] = 'Unproven previous name'
                    changed['fingerprint'] = rv._fingerprint(changed)
                    self.assertFalse(rv._observation_current(job, changed))
            other = copy.deepcopy(job); other['observation']['input_fingerprint'] = 'unproven'
            self.assertFalse(rv._observation_current(other, current))
            other = copy.deepcopy(job); other['observation']['fingerprint_version'] = 4
            self.assertFalse(rv._observation_current(other, current))
            # The original full hash proves the complete old market inputs.
            reference.unlink()
            self.assertTrue(rv._observation_current(job, current))

            with patch('gettex_history.attach_known_interruptions', return_value=False):
                result = rv.turn(store, info, now)
            self.assertEqual(result['checked'], 1)
            saved = store.setting(rv.key(job))
            self.assertEqual(saved['observation']['at'], original['at'])
            self.assertEqual(saved['observation']['fingerprint_version'], 4)
            self.assertTrue(saved['finding']['observation_current'])
            self.assertEqual(saved['finding']['discrepancy'], 'MISSING_IN_BOTH')
            self.assertFalse(saved['finding']['can_affect_dq'])
            self.assertIsNone(store.setting('cp_manual_risk:'+info['isin']))
            self.assertEqual(ctl.tv_risk_turn(store, info, now)['accepted'], 1)
            self.assertIsNotNone(store.setting(rv.PROGRESS+info['isin'])['analysis_requested_at'])
            with store.connect() as db:
                after = {t: db.execute('SELECT * FROM '+t+' ORDER BY 1').fetchall() for t in before}
            self.assertEqual(before, after)
            self.assertEqual(store.setting('data_quality:'+info['isin']+':'+meta['symbol']+':15m')['state'], 'UNCONFIRMED')
            store.read_cache.close(); store = FlatStore(dbfile)
            self.assertEqual(store.setting(rv.key(job))['observation'], saved['observation'])

            # The restored observation must still lose a race to new market data.
            with store.connect() as db:
                latest = ctl.read(db, rv.key(job)); reset = dict(latest,
                    observation=original, next_check=now.isoformat(), status='AWAITING_SOURCE')
                rv._save_job(db, reset, latest)
            classify = rv._classify
            def race(candidate, inputs, clock):
                result = classify(candidate, inputs, clock)
                with store.connect() as db:
                    db.execute('UPDATE candles SET close=close+.01 WHERE ts=?', ('2026-10-28 10:00:00',))
                return result
            with patch('gettex_history.attach_known_interruptions', return_value=False), \
                    patch.object(rv, '_classify', side_effect=race):
                result = rv.turn(store, info, now+pd.Timedelta(minutes=1))
            self.assertEqual(result['checked'], 0)
            self.assertEqual(store.setting(rv.key(job))['observation'], original)
            with store.connect() as db:
                self.assertFalse(ctl.effective_accepted(db, info['isin'],
                    ctl.read(db, 'cp_manual_risk:'+info['isin'], {})))
        finally:
            store.read_cache.close()
            for path in files:
                for suffix in ('', '-wal', '-shm'):
                    Path(str(path)+suffix).unlink(missing_ok=True)


if __name__ == '__main__':
    unittest.main()
