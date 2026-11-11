import sys,unittest,tempfile
from pathlib import Path
from unittest.mock import patch,Mock
import pandas as pd
import requests
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
import history_service as service
from history_backfill import sync,key
from storage import Store

class DepthTests(unittest.TestCase):
    def test_gettex_display_never_borrows_proxy_complete_history(self):
        from history_backfill import display_coverage
        store=Mock();store.setting.return_value={'status':'COMPLETE','symbol':'KO','count':16302,'first':'1962-01-02','last':'2026-10-09'}
        for tf in ('5m','10m','15m','30m','1H','2H','3H','4H','1D','1W','1M'):
            status,coverage=display_coverage(store,{'isin':'US1912161007'},'gettex',tf,'15m')
            self.assertIn('SOURCE_LIMITED',status);self.assertIn('20 лет не подтверждены',status)
            self.assertNotIn('COMPLETE',status);self.assertEqual(coverage,'')
        store.setting.assert_not_called()
    def test_proxy_display_preserves_its_own_daily_and_intraday_coverage(self):
        from history_backfill import display_coverage
        info={'isin':'ANY'};values={'history_identity:ANY':{'symbol':'ANY'},key(info,'ANY','1D'):{'status':'COMPLETE','count':100,'first':'2000-01-01','last':'2026-10-09'},key(info,'ANY','1H'):{'status':'SOURCE_LIMITED','limitation':'730 days'}}
        store=Mock();store.setting.side_effect=lambda k,default:values.get(k,default)
        status,coverage=display_coverage(store,info,'proxy','1D','1D')
        self.assertEqual(status,'COMPLETE');self.assertIn('2000-01-01',coverage)
        status,_=display_coverage(store,info,'proxy','2H','1H')
        self.assertEqual(status,'SOURCE_LIMITED • 730 days')
    def test_unknown_source_display_does_not_claim_proxy_coverage(self):
        from history_backfill import display_coverage
        store=Mock()
        status,coverage=display_coverage(store,{'isin':'ANY'},None,'1D','1D')
        self.assertNotIn('COMPLETE',status);self.assertEqual(coverage,'');store.setting.assert_not_called()
    def response(self):
        value=Mock();value.json.return_value={'chart':{'error':None,'result':[{'meta':{'symbol':'ANY','currency':'USD','exchangeName':'NYQ'},'timestamp':[]}]}}
        return value
    def test_daily_default_has_no_twenty_year_cutoff(self):
        with patch.object(service.requests,'get',return_value=self.response()) as call:
            service.download('ANY')
        self.assertEqual(call.call_args.kwargs['params']['period1'],int(pd.Timestamp('1900-01-01',tz='UTC').timestamp()))
    def test_intraday_defaults_request_full_retention(self):
        for tf,days in [('5m',60),('15m',60),('60m',730)]:
            with self.subTest(tf=tf),patch.object(service.requests,'get',return_value=self.response()) as call:
                service.download('ANY',tf)
                params=call.call_args.kwargs['params']
                self.assertAlmostEqual(params['period2']-params['period1'],days*86400-60,delta=2)
    def test_explicit_repair_range_is_not_expanded(self):
        start=pd.Timestamp('2026-10-01',tz='UTC');end=start+pd.Timedelta(days=1)
        with patch.object(service.requests,'get',return_value=self.response()) as call:
            service.download('ANY','5m',start,end)
        p=call.call_args.kwargs['params'];self.assertEqual(p['period1'],int(start.timestamp()));self.assertEqual(p['period2'],int(end.timestamp()))
    def test_rate_limit_has_no_busy_retry(self):
        reply=self.response();reply.raise_for_status.side_effect=requests.HTTPError('429 Too Many Requests')
        with patch.object(service.requests,'get',return_value=reply) as call:
            with self.assertRaises(requests.HTTPError):service.download('ANY')
        self.assertEqual(call.call_count,1)
    def test_empty_initial_download_is_not_complete_and_cools_down(self):
        with tempfile.TemporaryDirectory() as folder:
            store=Store(folder);info={'isin':'ANY'};download=Mock(return_value=pd.DataFrame());now=pd.Timestamp.now(tz='UTC')
            try:
                for _ in range(2):
                    with self.assertRaises(Exception):sync(info,store,'ANY','1D',download,now)
                self.assertEqual(download.call_count,1)
                self.assertEqual(store.setting(key(info,'ANY','1D'))['status'],'ERROR')
            finally:store.read_cache.close()
    def test_hourly_derivatives_use_deepest_compatible_base(self):
        from timeframes import base_for
        for tf in ('1H','2H','3H','4H'):self.assertEqual(base_for(tf),'1H')
        self.assertEqual(base_for('2H','gettex'),'15m')
    def test_hourly_aggregation_dst_early_close_and_ohlcv(self):
        import test_timeframes
        from analytics import resample,bar_ends
        fixture=test_timeframes.Tests()
        for first,last,venue in [('2026-03-06','2026-03-09 16:00','NMS'),('2026-03-27','2026-03-30 17:30','GER'),('2025-11-26','2025-11-28 16:00','NMS')]:
            raw=fixture.frame(first,last,base='1H',venue=venue)
            for tf in ('2H','3H','4H'):
                with self.subTest(first=first,tf=tf):
                    out=resample(raw,'1H',tf,now=pd.Timestamp('2026-10-04',tz='UTC'))
                    self.assertGreater(len(out),0)
                    self.assertNotEqual(out.attrs['data_quality']['state'],'INVALID')
                    self.assertTrue(out.bar_complete.all());self.assertEqual(out.volume.sum(),raw.volume.sum())
                    self.assertEqual(out.iloc[0].open,raw.iloc[0].open);self.assertEqual(out.iloc[-1].close,raw.iloc[-1].close)
                    self.assertFalse(bar_ends(out).hasnans)
                    if first=='2025-11-26':self.assertEqual(bar_ends(out)[-1].tz_convert('America/New_York').strftime('%H:%M'),'13:00')
    def test_more_than_twenty_years_is_preserved(self):
        with tempfile.TemporaryDirectory() as folder:
            store=Store(folder);info={'isin':'ANY'};now=pd.Timestamp('2026-10-09',tz='UTC')
            frame=pd.DataFrame({'open':[10.,11.],'high':[12.,13.],'low':[9.,10.],'close':[11.,12.],'volume':[100.,200.]},index=pd.to_datetime(['1962-01-02','2026-10-08']))
            frame.attrs=dict(source='proxy',symbol='ANY',venue='NYQ',currency='USD',timezone='America/New_York',first_trade_date=int(pd.Timestamp('1962-01-02',tz='UTC').timestamp()))
            try:
                # This test isolates depth/storage; real DQ is exercised by the live probe and regression suite.
                with patch('data_quality.audit',return_value={'state':'READY','issues':[]}),patch('data_quality.repair',return_value={'state':'READY'}):
                    state=sync(info,store,'ANY','1D',Mock(return_value=frame),now)
                self.assertEqual(state['status'],'COMPLETE');self.assertEqual(state['depth_policy'],'provider_maximum')
                self.assertEqual(store.load_series('ANY','1D','proxy','ANY').index[0],pd.Timestamp('1962-01-02'))
            finally:store.read_cache.close()
if __name__=='__main__':unittest.main()
