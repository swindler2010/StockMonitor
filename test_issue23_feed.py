import tempfile
import unittest
from unittest.mock import patch

import pandas as pd

import candle_schedule
import cp_assessment
import gettex_chart
import performance_cache
import revision_diagnostics
from storage import Store


ISIN = 'US0378331005'
META = gettex_chart.identity(ISIN, 'AAPL.GTX')
NOW = pd.Timestamp('2026-11-23T16:51:00Z')


class Issue23FeedTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix='issue23-feed-')
        self.addCleanup(directory.cleanup)
        self.store = Store(directory.name)
        self.addCleanup(self.store.read_cache.close)

    def test_reserved_live_turn_skips_archive_preparation_even_when_rate_limited(self):
        with patch('gettex_chart._fetch_turn', return_value={'state':'RATE_WAIT'}), \
             patch('gettex_chart.confirm_native_parents') as parents, \
             patch('gettex_chart.prepare_daily') as daily:
            result = gettex_chart.turn({'isin':ISIN,'_gettex_poll_only':True}, self.store)
        self.assertEqual(result, {'state':'RATE_WAIT'})
        parents.assert_not_called()
        daily.assert_not_called()

    def test_old_assessment_cannot_clear_a_new_market_request(self):
        scope = 'market:'+ISIN+':15m'
        with self.store.connect() as db:
            first = cp_assessment.request_from_db(db, ISIN, market_revisions={scope:20})
        with self.store.connect() as db:
            cp_assessment.request_from_db(db, ISIN, market_revisions={scope:21})
            cp_assessment.request_from_db(db, ISIN, market_revisions={scope:19})
        cp_assessment.finish(self.store, first, error='INPUT_CHANGED')
        with self.store.connect() as db:
            pending = cp_assessment.choose(db)
        self.assertEqual(pending['requested_at'], first['requested_at'])
        self.assertEqual(pending['market_revisions'], {scope:21})

    def test_late_tail_records_lag_and_has_only_one_early_recheck(self):
        key = gettex_chart.state_key(ISIN, '15m')
        self.store.set_setting(key, dict(state='UNCHANGED', identity=META, last='2026-11-23 15:45:00'))
        first = NOW-pd.Timedelta(minutes=5)
        initial = candle_schedule.decision(self.store, ISIN, META, '15m', first)
        candle_schedule.finish(self.store, ISIN, META, '15m', initial, now=first)
        plan = candle_schedule.decision(self.store, ISIN, META, '15m', NOW)
        self.assertEqual(plan['reason'], 'LATE_CLOSED_TAIL_RECHECK')
        candle_schedule.finish(self.store, ISIN, META, '15m', plan, now=NOW)
        self.assertEqual(self.store.setting(plan['key'])['closed_tail']['lag_seconds'], 6300)
        self.assertFalse(candle_schedule.decision(self.store, ISIN, META, '15m',
                                                 NOW+pd.Timedelta(minutes=1))['due'])

    def test_comparison_uses_a_whole_separate_official_series(self):
        official = candle_schedule.gettex_meta(ISIN)

        def frame(meta, start, count):
            index = pd.date_range(start, periods=count, freq='15min')
            data = pd.DataFrame(dict(open=100., high=102., low=99., close=101., volume=10.,
                                     source=meta['source']), index=index)
            data.attrs = {**meta,'timeframe':'15m'}
            return data

        longer = frame(META, '2026-11-23 07:30', 36)
        longer.attrs['listing_proof'] = {**META,'isin':ISIN,'status':'CONFIRMED'}
        fresh = frame(official, '2026-11-23 15:30', 8)
        result, base = gettex_chart.prefer_live_official({'isin':ISIN}, '1H', longer, '15m', fresh, '15m', NOW)
        self.assertEqual((result.attrs['source'], base), ('gettex','15m'))
        self.assertEqual(len(result), len(fresh))
        self.assertTrue(result.index.equals(fresh.index))
        self.assertIs(gettex_chart.prefer_live_official(
            {'isin':ISIN}, '1H', longer, '15m', fresh.iloc[:2], '15m', NOW)[0], longer)
        partial = frame(META, '2026-11-23 07:30', 39)
        partial.attrs['listing_proof'] = longer.attrs['listing_proof']
        result, _ = gettex_chart.prefer_live_official({'isin':ISIN}, '1H', partial, '15m', fresh, '15m', NOW)
        self.assertEqual(result.attrs['source'], 'gettex')

    def test_revision_observation_requires_source_range_and_captured_revision(self):
        scope = 'market:'+ISIN+':15m'
        event = dict(id=1, isin=ISIN, identity=META, ts='2026-11-23 08:30:00',
                     base_tf='15m', revision_after=20)
        spec = dict(isin=ISIN, identity=META, base_tf='15m', through='2026-11-23T08:30:00Z')
        guard = dict(revisions={scope:20}, series=[dict(spec=spec, scope=scope, count=20)])
        record = dict(publication_state='CURRENT', checked_at='2026-11-23T08:35:00Z',
                      published_at=NOW.timestamp(), candidates=[dict(timeframe='1H',closed_inputs=guard)])
        self.assertIsNotNone(revision_diagnostics._saved_trace(event, record))
        self.assertIsNone(revision_diagnostics._saved_trace({**event,'revision_after':21}, record))
        self.assertIsNone(revision_diagnostics._saved_trace({**event,'ts':'2026-11-23 09:30:00'}, record))
        self.assertIsNone(revision_diagnostics._saved_trace(
            {**event,'identity':{**META,'symbol':'OTHER.GTX'}}, record))

    def test_assessment_memo_reuses_only_the_same_range_and_revision(self):
        spec=dict(isin=ISIN,identity=META,base_tf='15m',through='2026-11-23T08:30:00Z')
        scope='market:'+ISIN+':15m'
        with self.store.connect() as db, patch('performance_cache._closed_rows', return_value=[]) as read_rows:
            memo={}
            first=performance_cache.capture_closed_inputs(db,[spec],{},memo=memo)
            self.assertEqual(first,performance_cache.capture_closed_inputs(db,[spec],{},memo=memo))
            self.assertEqual(read_rows.call_count,1)
            performance_cache.capture_closed_inputs(db,[{**spec,'through':'2026-11-23T08:45:00Z'}],{},memo=memo)
            db.execute('INSERT INTO input_revisions VALUES (?,?)',(scope,1))
            performance_cache.capture_closed_inputs(db,[spec],{},memo=memo)
            self.assertEqual(read_rows.call_count,3)

    def test_assessment_memo_reuses_validated_setting_only_inside_snapshot(self):
        key='setting:cp_manual_risk:'+ISIN
        with self.store.connect() as db, patch('performance_cache._setting_value', return_value={}) as read_setting:
            memo={}
            first=performance_cache.capture_closed_inputs(db,[],{key:1},memo=memo)
            self.assertEqual(first,performance_cache.capture_closed_inputs(db,[],{key:1},memo=memo))
            self.assertEqual(read_setting.call_count,1)
            performance_cache.capture_closed_inputs(db,[],{key:2},memo=memo)
            performance_cache.capture_closed_inputs(db,[],{key:1},memo={})
            self.assertEqual(read_setting.call_count,3)
