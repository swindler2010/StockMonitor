"""Only the new disjoint-tail branch: isolated SQLite and controlled OHLCV."""
import copy
import json
import pathlib
import sys
import tempfile
import unittest
from unittest.mock import Mock
import pandas as pd

ROOT=pathlib.Path(__file__).parent
sys.path.insert(0,str(ROOT/'stock_monitor'))
from storage import Store
import data_quality as dq
import history_backfill as hb
from performance_cache import InputRevisionChanged


class FreshDailyTail(unittest.TestCase):
    def setUp(self):
        saved=json.loads((ROOT/'history64-evidence/cp-autonomy27/all-tf27-inputs.json').read_text(encoding='utf8'))
        self.info=saved['info']['DE0006231004'];self.symbol='IFX.DE'
        self.meta=saved['settings']['history_identity:'+self.info['isin']]
        self.old=saved['settings'][dq.key(self.info,self.symbol,'1D')]
        self.coverage=saved['settings'][hb.key(self.info,self.symbol,'1D')]
        self.now=pd.Timestamp('2026-10-27T06:47:08Z')
        self.tmp=tempfile.TemporaryDirectory();self.store=Store(self.tmp.name)
        self.store.save_instrument(self.info)
        self.store.set_setting('history_identity:'+self.info['isin'],self.meta)
        self.store.set_setting(dq.key(self.info,self.symbol,'1D'),self.old)
        self.store.set_setting(hb.key(self.info,self.symbol,'1D'),self.coverage)
        self.cached=self.rows(pd.to_datetime(['2000-07-18','2026-10-01','2026-10-02']))
        self.cached.iloc[0,self.cached.columns.get_loc('high')]=1 # existing archived defect stays unchanged
        self.store.save_series(self.info,self.cached,'1D')
        dates=dq.expected_index(self.meta,'1D','2026-10-03','2026-10-27',self.now)
        self.reply=self.rows(dates)
        self.fetch=Mock(side_effect=self.download)

    def tearDown(self):
        self.store.read_cache.close();self.tmp.cleanup()

    def rows(self,index):
        frame=pd.DataFrame(dict(open=10.,high=12.,low=9.,close=11.,volume=100.),index=index)
        frame.attrs={k:self.meta[k] for k in ('source','venue','currency','symbol','timezone')}
        return frame

    def download(self,symbol,interval,start,**kwargs):
        self.assertEqual((symbol,interval),(self.symbol,'1d'))
        self.assertEqual(pd.Timestamp(start),pd.Timestamp('2026-10-03',tz='Europe/Berlin'))
        return self.reply.copy()

    def read(self):
        return self.store.load_series(self.info['isin'],'1D','proxy',self.symbol,'GER','EUR')

    def test_disjoint_fresh_rows_persist_without_clearing_archive_or_retry(self):
        before=self.read();result=hb.sync(self.info,self.store,self.symbol,'1D',self.fetch,self.now)
        self.assertEqual(self.fetch.call_count,1)
        self.assertEqual(result['quality_state'],'INVALID');self.assertFalse(result['backfill_complete'])
        self.assertEqual(result['fresh_tail']['count'],16)
        after=self.read()
        pd.testing.assert_frame_equal(before,after.loc[before.index],check_freq=False)
        pd.testing.assert_frame_equal(after.loc[self.reply.index,list(dq.FIELDS)],self.reply[list(dq.FIELDS)],check_freq=False,check_index_type=False,check_names=False)
        self.assertEqual(self.store.setting(dq.key(self.info,self.symbol,'1D')),self.old)
        self.store.read_cache.close();self.store=Store(self.tmp.name)
        self.assertEqual(len(self.read()),len(before)+16)
        hb.sync(self.info,self.store,self.symbol,'1D',self.fetch,self.now)
        self.assertEqual(self.fetch.call_count,1) # no new closed session / no duplicate request
        self.assertEqual(self.store.setting(dq.key(self.info,self.symbol,'1D')),self.old)

    def test_unknown_or_overlapping_invalid_scope_cannot_bypass_cooldown(self):
        for ranges in ([],[['2026-10-05','2026-10-05']]):
            with self.subTest(ranges=ranges):
                self.store.set_setting(dq.key(self.info,self.symbol,'1D'),dict(self.old,ranges=ranges))
                with self.assertRaises(dq.QualityError):hb.sync(self.info,self.store,self.symbol,'1D',self.fetch,self.now)
                self.assertEqual(self.fetch.call_count,0)

    def test_wrong_identity_corruption_gaps_or_empty_response_write_nothing(self):
        before=self.read()
        for kind in ('currency','venue','negative_volume','gap','empty'):
            with self.subTest(kind=kind):
                reply=self.reply.copy()
                if kind=='currency':reply.attrs['currency']='USD'
                if kind=='venue':reply.attrs['venue']='FRA'
                if kind=='negative_volume':reply.iloc[0,reply.columns.get_loc('volume')]=-1
                if kind=='gap':reply=reply.drop(reply.index[3])
                if kind=='empty':reply=reply.iloc[:0]
                with self.assertRaises(dq.QualityError):hb.sync(self.info,self.store,self.symbol,'1D',lambda *a:reply,self.now)
                pd.testing.assert_frame_equal(self.read(),before)
                self.assertEqual(self.store.setting(dq.key(self.info,self.symbol,'1D')),self.old)

    def test_identity_revision_race_aborts_before_candle_commit(self):
        before=self.read()
        def racing(*args):
            self.store.save_instrument(dict(self.info,history_symbol='IFX.F'))
            return self.reply.copy()
        with self.assertRaises(InputRevisionChanged):hb.sync(self.info,self.store,self.symbol,'1D',racing,self.now)
        pd.testing.assert_frame_equal(self.read(),before)
        self.assertEqual(self.store.setting(dq.key(self.info,self.symbol,'1D')),self.old)

    def test_provider_retry_after_and_user_listing_pin_remain_binding(self):
        self.store.set_setting('cp_source_retry:proxy',(self.now+pd.Timedelta(hours=1)).isoformat())
        with self.assertRaises(dq.QualityError):hb.sync(self.info,self.store,self.symbol,'1D',self.fetch,self.now)
        self.assertEqual(self.fetch.call_count,0)
        self.store.set_setting('cp_source_retry:proxy',None)
        self.store.save_instrument(dict(self.info,history_symbol='IFX.F'))
        with self.assertRaises(dq.QualityError):hb.sync(self.info,self.store,self.symbol,'1D',self.fetch,self.now)
        self.assertEqual(self.fetch.call_count,0)

    def test_unclosed_response_row_is_not_saved(self):
        self.reply=self.rows(pd.bdate_range('2026-10-05','2026-10-27'))
        result=hb.sync(self.info,self.store,self.symbol,'1D',self.fetch,self.now)
        self.assertEqual(result['fresh_tail']['count'],16)
        self.assertNotIn(pd.Timestamp('2026-10-27'),self.read().index)

    def test_real_provider_nan_close_keeps_verified_prefix_and_error(self):
        saved=json.loads((ROOT/'history64-evidence/cp-autonomy27/fresh-tail-provider-evidence.json').read_text(encoding='utf8'))
        self.reply=pd.DataFrame(saved['rows'],columns=['ts',*dq.FIELDS])
        self.reply.index=pd.to_datetime(self.reply.pop('ts'));self.reply.attrs=saved['identity']
        before=self.read()
        with self.assertRaises(dq.QualityError) as failure:
            hb.sync(self.info,self.store,self.symbol,'1D',self.fetch,self.now)
        self.assertEqual(failure.exception.report['ranges'],[['2026-10-26 00:00:00','2026-10-26 00:00:00']])
        after=self.read();self.assertEqual(len(after),len(before)+15)
        pd.testing.assert_frame_equal(before,after.loc[before.index],check_freq=False)
        pd.testing.assert_frame_equal(after.loc[self.reply.index[:-1],list(dq.FIELDS)],self.reply.iloc[:-1][list(dq.FIELDS)],check_freq=False,check_index_type=False,check_names=False)
        self.assertNotIn(self.reply.index[-1],after.index)
        self.assertEqual(self.store.setting(dq.key(self.info,self.symbol,'1D')),self.old)
        state=self.store.setting(hb.key(self.info,self.symbol,'1D'))
        self.assertFalse(state['backfill_complete']);self.assertEqual(state['fresh_tail']['rejected_response']['state'],'INVALID')
        recent=after.loc[after.index>pd.Timestamp('2026-10-01')].copy();recent.attrs=saved['identity']
        self.assertEqual(dq.audit(recent,'1D',saved['identity'],self.now)['state'],'STALE')
        self.store.read_cache.close();self.store=Store(self.tmp.name)
        self.assertEqual(len(self.read()),len(after))
        # Same defective last bar: fail closed again without rewriting old rows.
        with self.assertRaises(dq.QualityError):
            hb.sync(self.info,self.store,self.symbol,'1D',lambda *a:self.reply.iloc[-1:].copy(),self.now)
        pd.testing.assert_frame_equal(self.read(),after)


if __name__=='__main__':unittest.main()
