import unittest,json,tempfile
from unittest.mock import patch
import pandas as pd
import test_p1_parent_recovery as fixture
from storage import Store
import history_service as hs
import data_quality as dq


class AutomaticRecovery(unittest.TestCase):
    setUp=fixture.ParentRecovery.setUp
    tearDown=fixture.ParentRecovery.tearDown
    rows=fixture.ParentRecovery.rows

    def test_cold_unconfirmed_retries_only_after_due_and_survives_restart(self):
        with self.store.connect() as db:db.execute("DELETE FROM series WHERE base_tf='5m'")
        with patch.object(hs,'download',return_value=pd.DataFrame()) as fetch:
            first=hs.inspect_saved_archive(self.info,self.store,self.now)
            self.assertEqual(first['recovery'],'UNCONFIRMED')
            count=fetch.call_count
            self.assertGreater(count,0)
            retry=pd.Timestamp(self.store.setting(dq.key(self.info,'TEST.DE','1H'))['next_retry'])
            self.store.read_cache.close();self.store=Store(self.tmp.name)
            before=self.rows().copy()
            hs.inspect_saved_archive(self.info,self.store,retry-pd.Timedelta(seconds=1))
            self.assertEqual(fetch.call_count,count)
            due=hs.inspect_saved_archive(self.info,self.store,retry)
            self.assertEqual(due['state'],'AUDITED')
            self.assertEqual(due['recovery'],'UNCONFIRMED')
            self.assertGreater(fetch.call_count,count)
            count=fetch.call_count
            hs.inspect_saved_archive(self.info,self.store,retry+pd.Timedelta(seconds=1))
            self.assertEqual(fetch.call_count,count)
            pd.testing.assert_frame_equal(before,self.rows())

    def test_large_gap_repairs_one_complete_session_and_keeps_unknown_remainder(self):
        with self.store.connect() as db:db.execute('DELETE FROM series')
        # Saved complete older child sessions; no HTTP, no invented grid fill.
        days=pd.bdate_range('2026-10-05','2026-10-19')
        child=pd.concat([self.child.set_axis(self.child.index+(day-pd.Timestamp('2026-10-26'))) for day in days])
        child.attrs=self.meta.copy();self.store.save_series(self.info,child,'5m')
        parent=self.parent.iloc[[0,-1]].copy()
        parent.index=pd.to_datetime(['2026-10-05 09:00','2026-10-19 11:00'])
        parent.attrs=self.meta.copy();self.store.save_series(self.info,parent,'1H')
        with patch.object(hs,'download',side_effect=AssertionError('Unnecessary network')):
            report=dq.repair(self.info,self.store,'TEST.DE','1H',self.meta,hs.download,self.now,max_ranges=1)
        actual=self.rows()
        self.assertGreater(len(actual),2)
        self.assertLessEqual(len(actual),11)
        self.assertNotEqual(report['state'],'READY')
        pd.testing.assert_frame_equal(actual.loc[parent.index,list(dq.FIELDS)],parent[list(dq.FIELDS)],check_dtype=False)

    def test_failed_recent_piece_does_not_starve_older_complete_piece_after_cooldown(self):
        from timeframes import aggregate
        with self.store.connect() as db:db.execute('DELETE FROM series')
        idx=dq.expected_index(self.meta,'5m','2026-10-05','2026-10-19 18:00',self.now)
        child=pd.DataFrame(dict(open=100.,high=102.,low=99.,close=101.,volume=2.),index=idx);child.attrs=self.meta.copy()
        full=aggregate(child,'5m','1H',self.now)
        self.store.save_series(self.info,full.iloc[[0,-1]],'1H')
        child=child.loc[child.index.normalize()!=pd.Timestamp('2026-10-19')].copy();self.store.save_series(self.info,child,'5m')
        def unavailable(*a,**k):return pd.DataFrame()
        with patch.object(hs,'download',side_effect=unavailable):
            first=dq.repair(self.info,self.store,'TEST.DE','1H',self.meta,unavailable,self.now,max_ranges=1)
            self.assertEqual(len(self.rows()),2)
            self.assertIn('next_retry',first)
            self.store.read_cache.close();self.store=Store(self.tmp.name)
            second=dq.repair(self.info,self.store,'TEST.DE','1H',self.meta,unavailable,pd.Timestamp(first['next_retry']),max_ranges=1)
        actual=self.rows();self.assertGreater(len(actual),2)
        self.assertIn(pd.Timestamp('2026-10-16 09:00'),actual.index)
        self.assertNotIn(pd.Timestamp('2026-10-19 09:00'),actual.index)
        self.assertNotEqual(second['state'],'READY')


class LegacyProof(unittest.TestCase):
    setUp=fixture.ParentRecovery.setUp
    tearDown=fixture.ParentRecovery.tearDown

    def legacy(self):
        self.info={**self.info,'eur_listings':[], 'history_symbol':'TEST.DE','source_mode':'proxy','eur_history_auto':False}
        self.store.save_instrument(self.info);self.store.set_setting('history_identity:TEST',self.meta)
        return {**self.meta,'isin':'TEST','status':'CONFIRMED','figi':'fixture','evidence':{'basis':'isolated resolver fixture'}}

    def test_existing_feed_resolves_selected_listing_once_without_source_or_watchlist_change(self):
        import eur_listings as eur
        from feed_worker import FeedContext
        proof=self.legacy();original=self.store.instruments()
        ctx=FeedContext(self.store)
        def feed(info):ctx.events.put((None,(info,[])))
        with patch.object(ctx,'_run_feed',side_effect=feed),patch.object(eur,'discover',return_value=[proof]) as resolve,patch('calculation_window.eligibility_due',return_value=[]),patch('calculation_window.eligibility_turn'):
            ctx.calculate(self.info);ctx.calculate(self.info)
            resolve.assert_called_once_with('TEST')
        self.assertEqual(original,self.store.instruments())
        stored=self.store.setting('history_identity:TEST');self.assertEqual({k:stored[k] for k in dq.IDENTITY},self.meta)
        self.assertTrue(eur.confirmed_listing(self.info,stored))
        self.store.read_cache.close();self.store=Store(self.tmp.name)
        with patch.object(eur,'discover') as resolve:
            self.assertEqual(eur.ensure_selected_proof(self.info,self.store,self.now)['state'],'CONFIRMED');resolve.assert_not_called()
        altered={**stored,'venue':'FRA'};self.assertFalse(eur.confirmed_listing(self.info,altered))

    def test_no_match_and_rate_limit_keep_proof_unconfirmed_without_retry_loop(self):
        import eur_listings as eur,requests
        proof=self.legacy()
        with patch.object(eur,'discover',return_value=[{**proof,'currency':'USD'}]) as resolve:
            self.assertEqual(eur.ensure_selected_proof(self.info,self.store,self.now)['state'],'UNCONFIRMED')
            eur.ensure_selected_proof(self.info,self.store,self.now+pd.Timedelta(hours=1));self.assertEqual(resolve.call_count,1)
        self.assertNotIn('listing_proof',self.store.setting('history_identity:TEST'))
        response=requests.Response();response.status_code=429;response.headers['Retry-After']='7200'
        error=requests.HTTPError(response=response)
        later=self.now+pd.Timedelta(days=1)
        with patch.object(eur,'discover',side_effect=error) as resolve:
            eur.ensure_selected_proof(self.info,self.store,later)
            self.assertEqual(eur.ensure_selected_proof(self.info,self.store,later+pd.Timedelta(hours=1))['state'],'COOLDOWN')
            self.assertEqual(resolve.call_count,1)
        self.assertEqual(pd.Timestamp(self.store.setting('cp_source_retry:listing')),later+pd.Timedelta(hours=2))

    def test_eligibility_only_queue_can_resolve_missing_proof_without_waiting_for_feed(self):
        import eur_listings as eur
        from feed_worker import FeedContext
        proof=self.legacy();ctx=FeedContext(self.store)
        with patch.object(eur,'discover',return_value=[proof]) as resolve,patch('calculation_window.eligibility_turn') as assess:
            ctx.calculate({**self.info,'_eligibility_only':True})
            resolve.assert_called_once_with('TEST');assess.assert_called_once()
        self.assertTrue(eur.confirmed_listing(self.info,self.store.setting('history_identity:TEST')))

    def test_source_change_during_resolution_discards_receipt(self):
        import eur_listings as eur
        proof=self.legacy()
        def resolve(isin):self.store.set_setting('history_identity:TEST',{**self.meta,'symbol':'OTHER.DE'});return [proof]
        with patch.object(eur,'discover',side_effect=resolve):result=eur.ensure_selected_proof(self.info,self.store,self.now)
        self.assertIn('INPUT_CHANGED',result['reason']);self.assertNotIn('listing_proof',self.store.setting('history_identity:TEST'))

    def test_proof_is_used_by_analysis_but_does_not_bypass_dq_and_revocation(self):
        import eur_listings as eur
        from calculation_window import bounded_features
        proof=self.legacy();meta={**self.meta,'listing_proof':proof};self.store.set_setting('history_identity:TEST',meta)
        frame=self.child.copy();frame.attrs={**meta,'timeframe':'5m'}
        row=bounded_features(self.info,frame,'5m',self.now)
        self.assertNotIn('LISTING_UNCONFIRMED',row['blockers']);self.assertFalse(row['analysis_ready'])
        parent=hs.recover_from_children(self.info,self.store,meta,'1H','2026-10-26 10:00','2026-10-26 10:00',self.now)
        self.assertIsNotNone(parent)
        self.store.set_setting('history_identity:TEST',self.meta)
        self.assertIsNone(hs.recover_from_children(self.info,self.store,meta,'1H','2026-10-26 10:00','2026-10-26 10:00',self.now))

    def test_refresh_preserves_matching_proof_and_never_attaches_it_to_other_venue(self):
        import eur_listings as eur,candle_schedule
        proof=self.legacy();self.store.set_setting('history_identity:TEST',{**self.meta,'listing_proof':proof})
        with patch.object(candle_schedule,'decision',return_value={'due':False}):hs._recover(self.info,self.store)
        self.assertEqual(self.store.setting('history_identity:TEST')['listing_proof'],proof)
        wrong={**self.meta,'venue':'FRA','listing_proof':proof}
        self.assertFalse(eur.confirmed_listing(self.info,wrong))

    def test_explicit_selected_symbol_is_not_overridden_by_old_cached_listing(self):
        import candle_schedule,history_backfill
        proof=self.legacy();self.store.set_setting('history_identity:TEST',{**self.meta,'listing_proof':proof})
        selected={**self.info,'history_symbol':'TEST.F'}
        fresh={**self.meta,'symbol':'TEST.F','venue':'FRA','backfill_complete':True,'first':'2026-10-01'}
        with patch.object(candle_schedule,'decision',return_value={'due':True,'key':'fixture'}),patch.object(candle_schedule,'finish'),patch.object(history_backfill,'sync',return_value=fresh) as sync:
            result,_=hs._recover(selected,self.store)
        self.assertTrue(all(call.args[2]=='TEST.F' for call in sync.call_args_list))
        self.assertNotIn('listing_proof',result)
        self.assertEqual(self.store.setting('history_identity:TEST')['symbol'],'TEST.F')
        self.assertEqual(self.store.instruments()[0],self.info)

if __name__=='__main__':unittest.main()
