import sys,unittest
from pathlib import Path
from unittest.mock import Mock,patch
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
import eur_listings as eur

ISIN='US0378331005'
class EURTests(unittest.TestCase):
    def test_selection_prefers_quality_then_depth_not_invalid_volume(self):
        def reports(q,n):return {'1D':dict(quality=q,count=n,target=3000)}
        self.assertGreater(eur.rank_reports(reports('READY',100)),eur.rank_reports(reports('GAP',10000)))
        self.assertGreater(eur.rank_reports(reports('GAP',100)),eur.rank_reports(reports('INVALID',100000)))
        self.assertGreater(eur.rank_reports(reports('GAP',200)),eur.rank_reports(reports('GAP',100)))
    def setUp(self):eur._cache.clear();eur._calls.clear();eur._cooldown=0
    def mapping(self,**changes):
        row=dict(ticker='APC',exchCode='GY',marketSector='Equity',figi='BBG000DH0LL7');row.update(changes)
        return Mock(status_code=200,json=Mock(return_value=[{'data':[row]},{'warning':'No identifier found.'}]))
    def chart(self,**changes):
        meta=dict(symbol='APC.DE',exchangeName='GER',currency='EUR',exchangeTimezoneName='Europe/Berlin',instrumentType='EQUITY');meta.update(changes)
        return Mock(status_code=200,json=Mock(return_value={'chart':{'result':[{'meta':meta}]}}))
    def test_exact_identity_and_filters_cached_without_duplicate_requests(self):
        with patch.object(eur.requests,'post',return_value=self.mapping()) as post,patch.object(eur.requests,'get',return_value=self.chart()) as get:
            result=eur.discover(ISIN);self.assertEqual(result,eur.discover(ISIN))
        self.assertEqual((result[0]['isin'],result[0]['symbol'],result[0]['currency'],result[0]['mic']),(ISIN,'APC.DE','EUR','XETR'))
        self.assertEqual(post.call_count,1);self.assertEqual(get.call_count,1)
        self.assertEqual(post.call_args.kwargs['json'][0]['idValue'],ISIN)
        self.assertEqual(post.call_args.kwargs['json'][0]['currency'],'EUR')
    def test_wrong_currency_venue_symbol_timezone_rejected(self):
        for changes in [dict(currency='USD'),dict(exchangeName='FRA'),dict(symbol='AAPL'),dict(exchangeTimezoneName='UTC')]:
            eur._cache.clear()
            with patch.object(eur.requests,'post',return_value=self.mapping()),patch.object(eur.requests,'get',return_value=self.chart(**changes)):
                self.assertEqual(eur.discover(ISIN),[])
    def test_wrong_mapping_exchange_rejected_before_chart(self):
        with patch.object(eur.requests,'post',return_value=self.mapping(exchCode='US')),patch.object(eur.requests,'get') as get:
            self.assertEqual(eur.discover(ISIN),[]);get.assert_not_called()
    def test_ambiguous_mapping_not_chosen(self):
        response=self.mapping();payload=response.json();payload[0]['data']*=2
        with patch.object(eur.requests,'post',return_value=response),patch.object(eur.requests,'get') as get:
            self.assertEqual(eur.discover(ISIN),[]);get.assert_not_called()
    def test_invalid_isin_rejected_without_network(self):
        with patch.object(eur.requests,'post') as post:
            with self.assertRaises(ValueError):eur.discover('US0378331006')
            post.assert_not_called()
    def test_rate_limit_cooldown_no_retries(self):
        response=Mock(status_code=429);response.raise_for_status.side_effect=ValueError('429')
        with patch.object(eur.requests,'post',return_value=response) as post:
            for _ in range(2):
                with self.assertRaises(ValueError):eur.discover(ISIN)
            self.assertEqual(post.call_count,1)
    def test_isin_search_uses_exact_isin_not_legacy_name_matching(self):
        with patch.object(eur,'discover',return_value=[{'status':'CONFIRMED','isin':ISIN,'symbol':'APC.DE','currency':'EUR'}]) as discover,patch('market.resolve_symbol') as legacy:
            result=eur.resolve_for_add('AAPL',None,{},ISIN)
        legacy.assert_not_called();discover.assert_called_once_with(ISIN);self.assertEqual(result['symbol'],'APC.DE')
    def test_existing_history_pipeline_receives_confirmed_listing(self):
        import history_service
        identity=dict(source='proxy',symbol='APC.DE',venue='GER',currency='EUR',timezone='Europe/Berlin',backfill_complete=True)
        store=Mock();store.setting.return_value={}
        info=dict(isin=ISIN,history_symbol='APC.DE',history_currency='EUR',history_venue='GER')
        with patch('history_backfill.sync',return_value=identity) as sync:
            history_service.recover(info,store)
        self.assertEqual([c.args[3] for c in sync.call_args_list],['1D','5m','15m','1H'])
        self.assertTrue(all(c.args[2]=='APC.DE' for c in sync.call_args_list))
    def test_selection_is_pinned_on_refresh_and_rejects_usd(self):
        import history_service,pandas as pd
        euro=dict(status='CONFIRMED',isin=ISIN,source='proxy',symbol='APC.DE',venue='GER',currency='EUR',timezone='Europe/Berlin')
        usd={**euro,'symbol':'AAPL','venue':'NMS','currency':'USD'}
        store=Mock();store.setting.side_effect=lambda k,default=None: {'symbol':'APC.DE'} if k.startswith('eur_history_selection:') else default
        store.load_series.return_value=pd.DataFrame()
        with patch.object(history_service,'_recover',return_value=(euro,[])) as recover:
            meta,_=history_service.recover(dict(isin=ISIN,eur_history_auto=True,eur_listings=[usd,euro]),store)
        self.assertEqual(meta['currency'],'EUR');self.assertEqual(recover.call_count,1)
        self.assertEqual(recover.call_args.args[0]['history_symbol'],'APC.DE')
    def test_no_eur_candidate_never_downloads_usd(self):
        import history_service
        store=Mock();store.setting.return_value={}
        with patch.object(history_service,'_recover') as recover:
            with self.assertRaises(ValueError):history_service.recover(dict(isin=ISIN,eur_history_auto=True,eur_listings=[dict(isin=ISIN,status='CONFIRMED',currency='USD')]),store)
            recover.assert_not_called()
    def test_depth_excludes_incomplete_candles(self):
        import pandas as pd
        frame=pd.DataFrame({'bar_complete':[True]*99+[False]},index=pd.date_range('2026-01-01',periods=100,freq='5min'))
        with patch('analytics.bar_ends',return_value=pd.date_range('2026-01-01',periods=100,freq='5min',tz='UTC')):
            report=eur.depth(frame,'5m')
        self.assertEqual(report['count'],99);self.assertEqual(report['status'],'SOURCE_LIMITED')
    def test_target_met_does_not_mask_quality(self):
        import pandas as pd
        frame=pd.DataFrame(index=pd.date_range('2020-01-01',periods=101,freq='5min'))
        frame['close']=1.
        ends=pd.date_range('2020-01-01',periods=101,freq='5min',tz='UTC')
        for quality in ('READY','SOURCE_GAP','GAP','INVALID','STALE','RECOVERING','SOURCE_UNAVAILABLE',None):
            with self.subTest(quality=quality),patch('analytics.bar_ends',return_value=ends):
                frame.attrs['data_quality']={'state':quality}
                report=eur.depth(frame,'5m')
            expected='COMPLETE' if quality=='READY' else quality or 'INCOMPLETE'
            self.assertEqual(report['status'],expected)
            self.assertTrue(report['target_met']);self.assertEqual(report['depth_status'],'COMPLETE')
            self.assertEqual(report['count'],101)
    def test_short_gapped_history_keeps_depth_limitation_separate(self):
        import pandas as pd
        frame=pd.DataFrame({'close':[1.,2.]},index=pd.date_range('2020-01-01',periods=2,freq='5min'))
        frame.attrs['data_quality']={'state':'SOURCE_GAP'}
        with patch('analytics.bar_ends',return_value=pd.date_range('2020-01-01',periods=2,freq='5min',tz='UTC')):
            report=eur.depth(frame,'5m')
        self.assertEqual(report['status'],'SOURCE_GAP')
        self.assertEqual(report['depth_status'],'SOURCE_LIMITED');self.assertFalse(report['target_met'])
    def test_empty_depth_is_honestly_limited_for_all_tf(self):
        import pandas as pd
        for tf in eur.TARGETS:
            report=eur.depth(pd.DataFrame(),tf)
            self.assertEqual(report['status'],'SOURCE_LIMITED');self.assertIsNone(report['first'])

if __name__=='__main__':unittest.main()
