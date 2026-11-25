import sys,unittest
from unittest.mock import patch
from types import SimpleNamespace
from desktop014 import Desktop014
import eur_listings as eur
class LegacyTests(unittest.TestCase):
 def setUp(self):
  from test_frankfurt_cached import Tests as Fixtures
  f=Fixtures();f.setUp();self.f=f;f.info.pop('eur_history_auto')
 def test_daily_legacy_uses_cached_listing_without_changing_preferences(self):
  f=self.f;f.info.update(source_mode='gettex',history_symbol='TEST.DE')
  before=repr(f.info)
  out=Desktop014._frame_uncached(SimpleNamespace(store=f.store,source_mode=lambda i:i['source_mode']),f.info,'1D')
  self.assertEqual(out.attrs['symbol'],'TEST.F');self.assertEqual(repr(f.info),before)
 def test_explicit_optout_preserved(self):
  f=self.f;f.info['eur_history_auto']=False
  with patch.object(eur,'cached_daily_history',side_effect=AssertionError('optout')):
   out=Desktop014._frame_uncached(SimpleNamespace(store=f.store,source_mode=lambda i:'gettex'),f.info,'1D')
  self.assertTrue(out.empty)
 def test_intraday_does_not_enable_daily_selection(self):
  f=self.f
  with patch.object(eur,'cached_daily_history',side_effect=AssertionError('intraday changed')):
   out=Desktop014._frame_uncached(SimpleNamespace(store=f.store,source_mode=lambda i:'gettex'),f.info,'1H')
  self.assertTrue(out.empty)
if __name__=='__main__':unittest.main()
