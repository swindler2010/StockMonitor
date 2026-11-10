import json,sys,unittest
from pathlib import Path
from unittest.mock import Mock,patch
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
import market

class ResolverTests(unittest.TestCase):
    def response(self,symbol='RANDOM',isin='US1912161007',venue='NYQ',currency='USD',category='Stocks'):
        chart=Mock();chart.json.return_value={'chart':{'result':[{'meta':{
            'symbol':symbol,'instrumentType':'EQUITY','exchangeName':venue,'currency':currency}}]}}
        listing=Mock();listing.text='new Array('+','.join(json.dumps(v) for v in ('Any company',category,symbol+'|'+isin+'|'+symbol+'||'+symbol,'75','','id'))+')'
        return chart,listing
    def resolve(self,**kwargs):
        with patch('market.requests.get',side_effect=self.response(**kwargs)) as get:
            result=market.resolve_symbol('RANDOM',None,{'symbol':'RANDOM','quoteType':'EQUITY','exchange':'NYQ'})
        return result,get
    def test_exact_complete_identity(self):
        result,get=self.resolve();self.assertEqual(result['status'],'CONFIRMED')
        self.assertEqual((result['isin'],result['symbol'],result['venue'],result['currency']),('US1912161007','RANDOM','NYQ','USD'))
        self.assertEqual(get.call_args_list[1].kwargs['params']['query'],'RANDOM')
    def test_invalid_checksum(self):
        result,_=self.resolve(isin='US1912161008');self.assertEqual(result['status'],'BLOCKED')
    def test_provider_changes_symbol(self):
        result,get=self.resolve(symbol='OTHER');self.assertEqual(result['status'],'BLOCKED');self.assertEqual(get.call_count,1)
    def test_provider_changes_venue(self):
        result,_=self.resolve(venue='NMS');self.assertEqual(result['status'],'BLOCKED')
    def test_missing_currency(self):
        result,_=self.resolve(currency=None);self.assertEqual(result['status'],'BLOCKED');self.assertNotIn('currency',result)
    def test_currency_case_preserves_pence(self):
        result,_=self.resolve(currency='GBp');self.assertEqual(result['currency'],'GBp')
    def test_name_or_partial_symbol_never_matches(self):
        _,listing=self.response(symbol='RANDOM.EXTRA')
        with self.assertRaises(ValueError):market._exact_isins(listing.text,'RANDOM','EQUITY')
    def test_bond_with_same_symbol_never_matches(self):
        _,listing=self.response(category='Bonds')
        with self.assertRaises(ValueError):market._exact_isins(listing.text,'RANDOM','EQUITY')
    def test_ambiguous_exact_symbols_blocked(self):
        _,one=self.response();_,two=self.response(isin='US17275R1023')
        with self.assertRaises(ValueError):market._exact_isins(one.text+two.text,'RANDOM','EQUITY')
    def test_http_failure_is_blocked(self):
        with patch('market.requests.get',side_effect=TimeoutError('source timeout')):
            result=market.resolve_symbol('RANDOM',None,{'symbol':'RANDOM','quoteType':'EQUITY','exchange':'NYQ'})
        self.assertEqual(result['status'],'BLOCKED');self.assertIn('timeout',result['error'])
    def test_search_mismatch_blocks_without_network(self):
        with patch('market.requests.get') as get:
            result=market.resolve_symbol('RANDOM',None,{'symbol':'OTHER','quoteType':'EQUITY'})
        self.assertEqual(result['status'],'BLOCKED');get.assert_not_called()
    def test_duplicate_same_identity_is_not_ambiguous(self):
        _,listing=self.response();self.assertEqual(market._exact_isins(listing.text+listing.text,'RANDOM','EQUITY'),'US1912161007')

if __name__=='__main__':unittest.main()
