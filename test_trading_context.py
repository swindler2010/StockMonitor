"""Universal calculation plans; executable owner profile stays 1H."""
import copy,unittest
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch
import test_trading_engine as fixture
import test_paper_hourly as hourly_fixture
import analytics
import trading_context as context
from timeframes import LABELS
from trading_engine import profile


class ContextTests(unittest.TestCase):
    def test_all_eleven_plans_preserve_existing_profiles_and_no_permission(self):
        for tf in LABELS:
            p=context.plan(tf)
            self.assertEqual(tuple(p['required_tf']),profile(tf))
            self.assertEqual(p['execution_tf'],['5m','15m'])
            self.assertFalse(p['execution_authorized'])
        self.assertNotIn('5m',context.plan('1H')['required_tf'])
        self.assertNotIn('10m',context.plan('1H')['required_tf'])

    def test_explicit_custom_profile_never_drops_required_filter_on_missing_data(self):
        # Reuse the old deterministic OHLCV fixture, not market downloads.
        cls=next(c for c in vars(fixture).values() if isinstance(c,type) and hasattr(c,'market_frames'))
        frames=cls().market_frames();frames['2H']=frames.pop('4H');frames['2H'].attrs['timeframe']='2H'
        plan=context.plan('1H',('1H','2H','1D'))
        result=analytics.analyse_multiframe(frames,'1H',now='2026-02-01T00:00Z',required_tf=plan['required_tf'])
        self.assertEqual(set(result['timeframe_context']),{'1H','2H','1D'})
        self.assertEqual(result['status'],'READY')
        frames.pop('2H')
        result=analytics.analyse_multiframe(frames,'1H',now='2026-02-01T00:00Z',required_tf=plan['required_tf'])
        self.assertEqual(result['status'],'DATA BLOCKED');self.assertEqual(result['signal'],'WAIT')

    def test_default_profile_keeps_exact_v1_output_and_ignores_non_dependencies(self):
        cls=next(c for c in vars(fixture).values() if isinstance(c,type) and hasattr(c,'market_frames'))
        frames=cls().market_frames()
        baseline=analytics.analyse_multiframe(frames,'1H',now='2026-02-01T00:00Z')
        frames['5m']=frames['1H'].copy();frames['5m'].attrs['data_quality']={'state':'INVALID'}
        actual=analytics.analyse_multiframe(frames,'1H',now='2026-02-01T00:00Z',required_tf=context.plan('1H')['required_tf'])
        self.assertEqual(actual,baseline)

    def test_reject_incomplete_duplicate_unknown_and_unapproved_execution_tf(self):
        for tf,required,execution in [('1H',('1H','1D'),('5m',)),('1H',('1H','1H','1D'),('15m',)),
                ('7m',('1H','4H','1D'),('15m',)),('1H',('1H','4H','1D'),('1m',))]:
            with self.assertRaises(ValueError):context.plan(tf,required,execution)

    def test_context_pins_selection_and_native_listing_without_touching_ui(self):
        f=hourly_fixture.HourlyTests();f.setUp();self.addCleanup(f.tearDown)
        @contextmanager
        def connect():yield f.db
        store=SimpleNamespace(connect=connect,read_cache=SimpleNamespace(dependencies=lambda:{}))
        values=dict(watchlist=[f.info],symbol='Apple',timeframe='5m',period='Week')
        seen={}
        def calculate(ctx,*args,**kwargs):
            seen.update(tf=ctx.timeframe.get(),period=ctx.period.get(),required=ctx.trading_required_tf)
            return {'status':'READY'}
        with patch('snapshot_worker.SnapshotContext.trading_analysis',calculate),patch('calculation_window._candidate_proof',return_value={'proof':1}),patch('cp_controls.manual_context',return_value=None):
            result=context.calculate(store,values,f.identity,context.plan('1H'),{},fixture.pd.Timestamp.now(tz='UTC'))
        self.assertEqual(seen,dict(tf='1H',period='Max',required=('1H','4H','1D')))
        self.assertEqual(values['timeframe'],'5m');self.assertEqual(values['period'],'Week')
        self.assertEqual(result['proof'],{'proof':1})
        wrong=copy.deepcopy(f.identity);wrong['venue']='FRA'
        with self.assertRaisesRegex(ValueError,'LISTING_UNCONFIRMED'):
            context.calculate(store,values,wrong,context.plan('1H'),{},fixture.pd.Timestamp.now(tz='UTC'))


if __name__=='__main__':unittest.main()
