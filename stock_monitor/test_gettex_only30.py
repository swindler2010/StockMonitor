import copy
import unittest
from unittest.mock import patch
import pandas as pd
from test_gettex_chart_daily import DailyTests, ISIN, META, NOW
from eur_listings import confirmed_listing, ensure_selected_proof
from paper_account import quote
import history_service, revision_watcher

FRA=dict(source='proxy',symbol='APC.F',venue='FRA',currency='EUR',timezone='Europe/Berlin')

class VenueLockTests(unittest.TestCase):
    def setUp(self):
        DailyTests.setUp(self)
        self.info.update(history_venue_lock='GETTEX',eur_listings=[dict(FRA,isin=ISIN,status='CONFIRMED')])
        self.store.save_watchlist([self.info])
        self.store.set_setting('history_identity:'+ISIN,FRA)

    def test_all_11_tf_never_load_reserve_and_keep_quality(self):
        ctx=DailyTests.ctx(self)
        from desktop014 import Desktop014
        original=self.store.load_series
        def guarded(isin,base,source,*args,**kw):
            self.assertNotEqual(source,'proxy')
            return original(isin,base,source,*args,**kw)
        with patch.object(self.store,'load_series',side_effect=guarded):
            for tf in ('5m','10m','15m','30m','1H','2H','3H','4H','1D','1W','1M'):
                f=Desktop014._frame_uncached(ctx,self.info,tf)
                if f.empty:self.assertEqual(f.attrs['data_quality']['state'],'SOURCE_UNAVAILABLE')
                else:
                    self.assertEqual(f.attrs['venue'],'GETTEX');self.assertEqual(f.attrs['currency'],'EUR')
                self.assertEqual(f.attrs['source_selection']['venue_lock'],'GETTEX')

    def test_required_foreign_venue_and_currency_rejected(self):
        ctx=DailyTests.ctx(self)
        from desktop014 import Desktop014
        for identity in (FRA,dict(META,currency='USD')):
            with self.assertRaisesRegex(ValueError,'VENUE_EXCLUDED'):
                Desktop014._frame_uncached(ctx,self.info,'15m',required_identity=identity)
        # Same-provider GETTEX pin remains available to CORE.
        f=Desktop014._frame_uncached(ctx,self.info,'15m',required_identity=META)
        self.assertEqual(len(f),62);self.assertEqual(f.attrs['venue'],'GETTEX')

    def test_unlocked_instrument_receipts_unchanged_and_paper_blocks_old_reserve(self):
        unlocked={k:v for k,v in self.info.items() if k!='history_venue_lock'}
        self.assertTrue(confirmed_listing(unlocked,FRA))
        self.assertFalse(confirmed_listing(self.info,FRA))
        self.assertTrue(confirmed_listing(self.info,dict(META,listing_proof=dict(META,isin=ISIN,status='CONFIRMED'))))
        with self.assertRaisesRegex(ValueError,'PAPER_IDENTITY_BLOCKED'):
            quote(self.info,dict(isin=ISIN,identity=FRA,timeframe='15m'),NOW.timestamp())

    def test_cp_does_not_request_reserve_or_resolve_its_proof(self):
        with patch('requests.get',side_effect=AssertionError('Unexpected HTTP')):
            self.assertEqual(history_service.recover(self.info,self.store)[0]['state'],'VENUE_EXCLUDED')
            self.assertEqual(ensure_selected_proof(self.info,self.store)['state'],'VENUE_EXCLUDED')
            result=revision_watcher.check(self.info,self.store,lambda *a,**k:self.fail('reserve revision HTTP'),NOW)
            self.assertNotEqual(result.get('state'),'ERROR')

    def test_archive_queue_skips_reserve_without_deleting_rows(self):
        reserve=self.raw.copy();reserve.attrs=FRA
        self.store.save_series(self.info,reserve,'15m')
        original=self.store.load_series
        def guarded(isin,base,source,*args,**kw):
            self.assertNotEqual(source,'proxy')
            return original(isin,base,source,*args,**kw)
        with patch.object(self.store,'load_series',side_effect=guarded),patch('requests.get',side_effect=AssertionError('HTTP')):
            history_service.inspect_saved_archive(self.info,self.store,NOW)
        self.assertEqual(len(original(ISIN,'15m','proxy','APC.F','FRA','EUR')),62)

    def test_policy_change_invalidates_instrument_dependency_without_market_writes(self):
        before=self.store.read_cache.token_for(['instrument:'+ISIN,'market:'+ISIN+':15m'])
        self.info['history_venue_lock']=None;self.store.save_watchlist([self.info])
        after=self.store.read_cache.token_for(before)
        self.assertNotEqual(before['instrument:'+ISIN],after['instrument:'+ISIN])
        self.assertEqual(before['market:'+ISIN+':15m'],after['market:'+ISIN+':15m'])

if __name__=='__main__':unittest.main()
