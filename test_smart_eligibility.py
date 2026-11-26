import sys, unittest
from pathlib import Path
from unittest.mock import patch
import pandas as pd
sys.path.insert(0, str(Path(__file__).parent/'stock_monitor'))
from calculation_window import bounded_features, assess_all


class BoundedEligibility(unittest.TestCase):
    def setUp(self):
        self.now=pd.Timestamp('2026-10-23T21:30Z')
        self.info=dict(isin='TEST',eur_listings=[])
        self.frame=pd.DataFrame(dict(open=10.,high=11.,low=9.,close=10.,volume=100.,source='gettex'),
                                index=pd.bdate_range('2026-01-01',periods=210))
        self.frame.attrs=dict(source='gettex',symbol='TEST',venue='GETTEX',currency='EUR',timezone='Europe/Berlin',timeframe='1D',data_quality={'state':'READY'})
    def ready_audit(self, frame, tf, **kwargs):
        return dict(state='READY',identity={k:frame.attrs.get(k) for k in ('source','symbol','venue','currency','timezone')})
    def run_window(self, frame=None, info=None):
        # Isolates the NEW dependency/seed guard; existing calendar is not mocked
        # by production code. DQ rejection has its own explicit test below.
        with patch('calculation_window.data_quality.audit',side_effect=self.ready_audit):
            return bounded_features(info or self.info, self.frame if frame is None else frame, '1D',self.now)
    def test_exact_reference_and_archive_unchanged(self):
        f=self.frame.copy();f.attrs['data_quality']={'state':'GAP','ranges':[['2020-01-01','2020-01-02']]}
        r=self.run_window(f)
        self.assertTrue(r['analysis_ready'],r)
        self.assertEqual(r['count'],200)
        self.assertEqual(f.attrs['data_quality']['state'],'GAP')
    def test_recursive_seed_difference_blocks(self):
        f=self.frame.copy();f.iloc[0,f.columns.get_loc('close')]=10.5
        r=self.run_window(f)
        self.assertFalse(r['analysis_ready']);self.assertIn('RECURSIVE_INITIALIZATION_SENSITIVE',r['blockers'])
    def test_bad_identity_and_currency(self):
        f=self.frame.copy();f.attrs['currency']='USD'
        self.assertFalse(self.run_window(f)['chart_ready'])
        self.assertIn('IDENTITY_MISMATCH',self.run_window(info={**self.info,'expected_identity':{'venue':'GER'}})['blockers'])
    def test_short_warmup_blocks(self):
        self.assertIn('INDICATOR_WARMUP_OR_OHLCV',self.run_window(self.frame.tail(20))['blockers'])
    def test_actual_invalid_ohlcv_stays_blocked(self):
        f=self.frame.copy();f.iloc[-2,f.columns.get_loc('high')]=1
        self.assertFalse(self.run_window(f)['analysis_ready'])
    def test_unconfirmed_window_and_reference_block(self):
        with patch('calculation_window.data_quality.audit',return_value={'state':'UNCONFIRMED'}):
            r=bounded_features(self.info,self.frame,'1D',self.now)
        self.assertIn('WINDOW_UNCONFIRMED',r['blockers'])
        with patch('calculation_window.data_quality.audit',side_effect=[{'state':'READY'},{'state':'GAP'}]):
            r=bounded_features(self.info,self.frame,'1D',self.now)
        self.assertIn('RECURSIVE_REFERENCE_GAP',r['blockers'])
    def test_derived_provenance_required(self):
        f=self.frame.copy();f.attrs['timeframe']='1W'
        r=bounded_features(self.info,f,'1W',self.now)
        self.assertIn('DERIVED_PROVENANCE_NOT_PROVEN',r['blockers'])
    def test_all_eleven_no_automatic_trade(self):
        class Context:
            def user_state(s,info):return {}
            def source_mode(s,info):return 'gettex'
            def frame_for(s,info,tf):return pd.DataFrame()
        r=assess_all(Context(),self.info,self.now)
        self.assertEqual(len(r['timeframes']),11);self.assertEqual(len(r['candidates']),11)
        self.assertTrue(all(not c['trade_ready'] and not c['signal_ready'] for c in r['candidates']))
    def test_real_calendar_and_quality_path(self):
        import data_quality as dq
        now=pd.Timestamp('2026-10-23T18:00Z')
        idx=dq.expected_index(self.frame.attrs,'1D',pd.Timestamp('2026-01-01'),pd.Timestamp('2026-10-24'),now)
        f=pd.DataFrame(dict(open=10.,high=11.,low=9.,close=10.,volume=100.,source='gettex'),index=idx)
        f.attrs=self.frame.attrs.copy()
        r=bounded_features(self.info,f,'1D',now)
        self.assertTrue(r['analysis_ready'],r)
        f.iloc[-1,f.columns.get_loc('volume')]=-1
        r=bounded_features(self.info,f,'1D',now)
        self.assertFalse(r['chart_ready']);self.assertIn('WINDOW_INVALID',r['blockers'])
    def test_real_stale_reference_does_not_get_ready(self):
        import data_quality as dq
        now=pd.Timestamp('2026-10-23T18:00Z')
        idx=dq.expected_index(self.frame.attrs,'1D',pd.Timestamp('2026-01-01'),pd.Timestamp('2026-10-24'),now)
        f=pd.DataFrame(dict(open=10.,high=11.,low=9.,close=10.,volume=100.,source='gettex'),index=idx[:-3])
        f.attrs=self.frame.attrs.copy()
        r=bounded_features(self.info,f,'1D',now)
        self.assertFalse(r['analysis_ready']);self.assertIn('WINDOW_STALE',r['blockers'])

if __name__=='__main__':unittest.main()
