"""A completed quote acquisition must release CP's serial writer promptly."""
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import Mock, patch

from feed_worker import FeedContext
import test_cp_fresh_fair_share as fixtures
import candle_schedule as schedule


class FreshCompletion(unittest.TestCase):
    def context(self, result=None):
        store = fixtures.Memory()
        self.addCleanup(store.db.close)
        store.set_setting(schedule.FRESH_ROTATION, {'consecutive': 2})
        return SimpleNamespace(store=store, _calculate=Mock(return_value=result or {'errors': []}))

    def stages(self, stack, requested=False):
        methods = {}
        for name in ('cp_controls.automatic_turn', 'cp_controls.tv_risk_turn',
                     'cp_controls.reviewed_risk_turn', 'cp_reconciliation.turn',
                     'calculation_window.eligibility_turn', 'cp_reconciliation.complete_analysis_request'):
            value = {'analysis_requested_at': '2026-11-04T13:00Z'} if requested and name == 'cp_controls.reviewed_risk_turn' else {}
            methods[name] = stack.enter_context(patch(name, return_value=value))
        return methods

    def test_fresh_acquisition_never_enters_archive_or_full_assessment(self):
        ctx = self.context()
        with ExitStack() as stack:
            calls = self.stages(stack, requested=True)
            out = FeedContext.calculate(ctx, {'isin': 'A', '_gettex_poll_only': True})
        ctx._calculate.assert_called_once()
        for name in ('cp_controls.automatic_turn','cp_controls.tv_risk_turn','cp_controls.reviewed_risk_turn'):
            calls[name].assert_called_once()
        for name in ('cp_reconciliation.turn','calculation_window.eligibility_turn','cp_reconciliation.complete_analysis_request'):
            calls[name].assert_not_called()
        self.assertEqual(out['errors'], [])
        self.assertEqual(ctx.store.setting(schedule.FRESH_ROTATION)['consecutive'], 2)
        self.assertEqual(ctx.store.setting('cp_reconciliation_progress:A')['analysis_requested_at'],
                         '2026-11-04T13:00Z')

    def test_history_routes_still_process_review_and_requested_assessment(self):
        for route in ({}, {'_eligibility_only': True}, {'_review_only': True}):
            ctx = self.context()
            with ExitStack() as stack:
                calls = self.stages(stack, requested=True)
                FeedContext.calculate(ctx, {'isin': 'A', **route})
            for method in calls.values(): method.assert_called_once()
            self.assertEqual(ctx.store.setting(schedule.FRESH_ROTATION)['consecutive'], 0)

    def test_failed_or_rate_limited_fresh_result_does_not_clear_diagnostics(self):
        ctx = self.context({'errors': ['SOURCE_COOLDOWN'], 'other': {'dq': 'INVALID'}})
        ctx.store.set_setting('cp_eligibility:A', {'error': 'INPUT_CHANGED'})
        with ExitStack() as stack:
            calls = self.stages(stack)
            out = FeedContext.calculate(ctx, {'isin': 'A', '_gettex_poll_only': True})
        self.assertEqual(out['errors'], ['SOURCE_COOLDOWN'])
        self.assertEqual(out['other']['dq'], 'INVALID')
        self.assertEqual(ctx.store.setting('cp_eligibility:A')['error'], 'INPUT_CHANGED')
        calls['cp_reconciliation.turn'].assert_not_called()
        calls['calculation_window.eligibility_turn'].assert_not_called()

    def test_acquisition_exception_is_not_a_success(self):
        ctx = self.context(); ctx._calculate.side_effect = ValueError('INPUT_CHANGED')
        with self.assertRaisesRegex(ValueError, 'INPUT_CHANGED'):
            FeedContext.calculate(ctx, {'isin': 'A', '_gettex_poll_only': True})
        self.assertEqual(ctx.store.setting(schedule.FRESH_ROTATION)['consecutive'], 2)

    def test_deferred_request_survives_reload_and_history_finishes_it(self):
        ctx = self.context()
        with ExitStack() as stack:
            self.stages(stack, requested=True)
            FeedContext.calculate(ctx, {'isin': 'A', '_gettex_poll_only': True})
        loaded = fixtures.Memory(); self.addCleanup(loaded.db.close)
        for key, value in ctx.store.db.execute('SELECT * FROM settings'):
            import json
            loaded.set_setting(key, json.loads(value))
        request = loaded.setting('cp_reconciliation_progress:A')['analysis_requested_at']
        # The ordinary persisted-request consumer, after a simulated restart.
        with patch('cp_controls.automatic_turn', return_value={}), \
             patch('cp_controls.tv_risk_turn', return_value={}), \
             patch('cp_controls.reviewed_risk_turn', return_value={}), \
             patch('cp_reconciliation.turn', return_value={'analysis_requested_at': request}), \
             patch('calculation_window.eligibility_turn', return_value={'checked_at': request, 'error': 'INPUT_CHANGED'}):
            FeedContext.calculate(SimpleNamespace(store=loaded, _calculate=Mock(return_value={'errors': []})), {'isin': 'A'})
        progress = loaded.setting('cp_reconciliation_progress:A')
        self.assertNotIn('analysis_requested_at', progress)
        self.assertEqual(progress['analysis_receipt']['error'], 'INPUT_CHANGED')

    def test_continuous_53_and_200_stock_load_retains_history_and_background(self):
        for n in (53, 200):
            fixture = fixtures.FairLiveTurn(); fixture.setup_universe(n)
            # Exercise the changed worker boundary, not only planner rotation.
            seen = set(); historical = 0
            with ExitStack() as stack:
                calls = self.stages(stack)
                for _ in range(4*(n-2)):
                    item = fixture.plan()
                    if item is None:
                        historical += 1
                        FeedContext.calculate(SimpleNamespace(store=fixture.store, _calculate=Mock(return_value={'errors': []})),
                                              {'isin': 'HISTORY', '_eligibility_only': True})
                    else:
                        seen.add(item['isin']); fixture.complete(item)
                        FeedContext.calculate(SimpleNamespace(store=fixture.store, _calculate=Mock(return_value={'errors': []})), item)
                self.assertEqual(calls['cp_reconciliation.turn'].call_count, historical)
            self.assertEqual(len(seen), n)
            self.assertEqual(historical, n-2)
            fixture.doCleanups()


if __name__ == '__main__': unittest.main()
