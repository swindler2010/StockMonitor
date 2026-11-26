"""Bounded exact GETTEX listing selection; no live source or account access."""
import sys,unittest
from pathlib import Path
import pandas as pd
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
import gettex_chart as chart
class ExactNativeListingTests(unittest.TestCase):
    def fetch(self,search,quotes):
        calls=[]
        class Reply:
            def __init__(self,data=None,text=''):self.data=data;self.text=text
            def json(self):return self.data
        class Client(chart.Client):
            def request(self,method,path,**kw):
                calls.append((path,kw))
                if path.startswith('https://www.gettex.de/'):return Reply(text='const samlRequest=`public-test`;')
                if '/samllogin' in path:return Reply(dict(token='public-session-fixture'))
                if path.endswith('/find/securities'):return Reply(dict(status='OK',data=search))
                if path.endswith('/quote/info'):return Reply(dict(status='OK',data=quotes))
                return Reply(dict(status='OK',request={'ric':kw['params']['ric']},data=[]))
        self.calls=calls
        return Client().fetch('US30231G1022','1m',pd.Timestamp('2026-11-25T07:00Z'),pd.Timestamp('2026-11-25T10:00Z'))
    def test_exact_metadata_resolves_multiple_search_rows_with_one_quote_call(self):
        _,meta=self.fetch([{'x.RIC':'WRONG.GTX'},{'x.RIC':'XOM.GTX'}],[{'x.RIC':'WRONG.GTX','x._ISIN':'US0000000001','x._CURRENCY':'EUR'},{'x.RIC':'XOM.GTX','x._ISIN':'US30231G1022','x._CURRENCY':'EUR','q._CURRENCY':'EUR'}])
        self.assertEqual(meta['symbol'],'XOM.GTX');self.assertEqual(len(self.calls),5)
        self.assertEqual(sum(path.endswith('/quote/info') for path,_ in self.calls),1)
    def test_duplicate_search_rows_do_not_make_one_exact_listing_ambiguous(self):
        _,meta=self.fetch([{'x.RIC':'XOM.GTX'},{'x.RIC':'XOM.GTX'}],[{'x._ISIN':'US30231G1022','x._CURRENCY':'EUR'}])
        self.assertEqual(meta['symbol'],'XOM.GTX');self.assertEqual(len(self.calls),5)
    def test_two_real_matches_wrong_currency_or_unmapped_ric_stay_rejected(self):
        candidates=[{'x.RIC':'XOM.GTX'},{'x.RIC':'EXX.GTX'}]
        for quotes in ([{'x.RIC':ric,'x._ISIN':'US30231G1022','x._CURRENCY':'EUR'} for ric in ('XOM.GTX','EXX.GTX')],
                       [{'x.RIC':'XOM.GTX','x._ISIN':'US30231G1022','x._CURRENCY':'USD'}],
                       [{'x.RIC':'UNKNOWN.GTX','x._ISIN':'US30231G1022','x._CURRENCY':'EUR'}]):
            with self.subTest(quotes=quotes),self.assertRaises(ValueError):self.fetch(candidates,quotes)
            self.assertFalse(any('/timeseries/' in path for path,_ in self.calls))
if __name__=='__main__':unittest.main()
