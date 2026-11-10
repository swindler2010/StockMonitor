import sys,unittest,copy
from pathlib import Path
from unittest.mock import Mock,patch
import pandas as pd
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
import eur_listings as eur
import data_quality as q
from desktop014 import Desktop014
from types import SimpleNamespace
NOW=pd.Timestamp('2026-10-18',tz='UTC')
def listing(symbol,venue):
    return dict(isin='TEST',status='CONFIRMED',source='proxy',symbol=symbol,venue=venue,currency='EUR',timezone='Europe/Berlin')
def frame(meta,n=20):
    idx=q.expected_index(meta,'1D','2026-08-01','2026-10-16',NOW)[-n:]
    f=pd.DataFrame(dict(open=10.,high=12.,low=9.,close=11.,volume=100.),index=idx)
    f.attrs={k:meta[k] for k in q.IDENTITY};return f
class Tests(unittest.TestCase):
    def setUp(self):
        self.de=listing('TEST.DE','GER');self.fr=listing('TEST.F','FRA')
        self.info=dict(isin='TEST',eur_history_auto=True,eur_listings=[self.de,self.fr])
        self.frames={'TEST.DE':frame(self.de,10),'TEST.F':frame(self.fr)};self.reports={}
        self.store=Mock();self.store.load_series.side_effect=lambda isin,base,source,symbol,venue,currency:self.frames.get(symbol,pd.DataFrame()).copy()
        self.store.setting.side_effect=lambda key,default=None:self.de if key.startswith('history_identity:') else self.reports.get(key,default)
        self.store.load_official.return_value=pd.DataFrame()
    def test_deeper_confirmed_eur_selected_without_mutation_or_network(self):
        before=self.frames['TEST.F'].copy(deep=True)
        with patch.object(eur,'discover',side_effect=AssertionError('network forbidden')):
            result=eur.cached_daily_history(self.store,self.info,'1D',self.de)
        pd.testing.assert_frame_equal(result,before,check_flags=False)
        self.assertEqual(result.attrs['symbol'],'TEST.F');self.store.set_setting.assert_not_called()
    def test_wrong_isin_currency_and_row_identity_not_selected(self):
        for key,value in [('isin','OTHER'),('currency','USD'),('source','other')]:
            info=copy.deepcopy(self.info);info['eur_listings'][1][key]=value
            self.assertEqual(eur.cached_daily_history(self.store,info,'1D',self.de).attrs['symbol'],'TEST.DE')
        self.frames['TEST.F'].attrs['venue']='GER'
        self.assertEqual(eur.cached_daily_history(self.store,self.info,'1D',self.de).attrs['symbol'],'TEST.DE')
    def test_persisted_invalid_not_replaced_by_clean_cache(self):
        self.reports[q.key(self.info,'TEST.F','1D')]={'state':'INVALID','issues':['rejected source archive']}
        self.assertEqual(eur.cached_daily_history(self.store,self.info,'1D',self.de).attrs['symbol'],'TEST.DE')
    def test_gap_display_uses_own_listing_and_all_real_daily_rows(self):
        self.frames['TEST.DE']=pd.DataFrame()
        self.reports[q.key(self.info,'TEST.F','1D')]={'state':'SOURCE_GAP','issues':['provider gap'],'ranges':[]}
        context=SimpleNamespace(store=self.store,source_mode=lambda info:'gettex')
        result=Desktop014._frame_uncached(context,self.info,'1D')
        self.assertEqual(result.attrs['symbol'],'TEST.F');self.assertEqual(result.attrs['currency'],'EUR')
        self.assertEqual(result.attrs['data_quality']['state'],'SOURCE_GAP')
        self.assertEqual(result.attrs['eur_history_depth']['status'],'SOURCE_GAP')
        pd.testing.assert_frame_equal(result[list(q.FIELDS)],self.frames['TEST.F'][list(q.FIELDS)],check_flags=False)
    def test_good_gettex_retains_precedence(self):
        meta=dict(source='gettex',symbol='TEST',venue='GETTEX',currency='EUR',timezone='Europe/Berlin')
        idx=q.expected_index(meta,'5m','2026-10-15','2026-10-16 23:00',NOW)
        f=pd.DataFrame(dict(open=10.,high=12.,low=9.,close=11.,volume=100.),index=idx);f.attrs=meta
        f.attrs['data_quality']=q.audit(f,'5m',meta,NOW)
        self.store.load_official.return_value=f
        context=SimpleNamespace(store=self.store,source_mode=lambda info:'gettex')
        with patch.object(eur,'cached_daily_history',side_effect=AssertionError('unneeded fallback')):
            result=Desktop014._frame_uncached(context,self.info,'5m')
        self.assertEqual(result.attrs['source'],'gettex')
        self.assertEqual(result.attrs['source_selection']['selected'],'GETTEX_EUR')
        # The new automatic policy checks the saved proxy date before selecting
        # the whole GETTEX row. It does not concatenate either source.
        self.assertTrue(result.close.eq(11.).all());self.store.set_setting.assert_not_called()
if __name__=='__main__':unittest.main()
