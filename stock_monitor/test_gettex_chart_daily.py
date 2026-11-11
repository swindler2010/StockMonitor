import json
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd
import gettex_chart as chart
import data_quality as quality
from storage import Store
from analytics import resample
from timeframes import endpoints

ISIN='US0378331005'
META=chart.identity(ISIN,'AAPL.GTX')
NOW=pd.Timestamp('2026-10-27T22:05:00Z')


class DailyTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='sm-chart-day-')
        self.addCleanup(self.tmp.cleanup)
        self.store=Store(self.tmp.name);self.addCleanup(self.store.read_cache.close)
        self.info={'isin':ISIN,'name':'Apple','kind':'stock'}
        self.store.save_watchlist([self.info])
        idx=pd.date_range('2026-10-27 07:30', '2026-10-27 22:45',freq='15min')
        self.raw=pd.DataFrame([[100+n,102+n,99+n,101+n,5+n] for n in range(len(idx))],index=idx,columns=chart.FIELDS)
        self.raw.attrs=dict(META,timeframe='15m')
        self.store.save_series(self.info,self.raw,'15m')
        self.store.set_setting(chart.state_key(ISIN,'15m'),dict(state='SAVED',identity=META,
            listing_proof=dict(META,isin=ISIN,status='CONFIRMED'),received_at=NOW.isoformat()))
    def ctx(self):
        from snapshot_worker import SnapshotContext,Value
        c=SnapshotContext();c.store=self.store;c.analysis_at=NOW;c.watchlist=[self.info];c.drafts={}
        c.symbol=Value('Apple');c.timeframe=Value('15m');c.period=Value('Max')
        return c

    def test_complete_day_existing_aggregation_and_no_new_raw_rows(self):
        result=chart.prepare_daily(self.store,self.info,NOW)
        self.assertEqual(result['complete_closed'],1);self.assertEqual(result['state'],'READY')
        out=chart.load(self.store,self.info,'1D',NOW)
        self.assertEqual(list(out.loc[out.index[0],list(chart.FIELDS)]),[100,163,99,162,2201])
        self.assertEqual(out.attrs['aggregation_base'],'15m')
        self.assertEqual(out.attrs['source'],'gettex_chart')
        self.assertEqual(endpoints(out)[0],pd.Timestamp('2026-10-27T22:00:00Z'))
        with self.store.connect() as db:
            self.assertEqual(db.execute("select count(*) from series where base_tf='1D'").fetchone()[0],0)
            self.assertEqual(db.execute('select count(*) from series').fetchone()[0],62)
        from desktop014 import Desktop014
        selected=Desktop014._frame_uncached(self.ctx(),self.info,'1D',required_identity=META)
        self.assertEqual(selected.attrs['aggregation_base'],'15m')
        self.assertEqual(selected.attrs['data_quality']['state'],'READY')

    def test_gap_forming_day_and_invalid_stay_blocked(self):
        with self.store.connect() as db:db.execute('delete from series where ts=?',(str(self.raw.index[5]),))
        out=chart.derived_daily(self.store,self.info,NOW)
        self.assertFalse(out.bar_complete.any());self.assertEqual(out.attrs['data_quality']['state'],'UNCONFIRMED')
        self.store.save_series(self.info,self.raw,'15m')
        early=NOW-pd.Timedelta(hours=1)
        self.assertEqual(chart.prepare_daily(self.store,self.info,early)['complete_closed'],0)
        with self.store.connect() as db:db.execute('update series set high=1 where ts=?',(str(self.raw.index[5]),))
        from desktop014 import Desktop014
        invalid=Desktop014._frame_uncached(self.ctx(),self.info,'1D',required_identity=META)
        self.assertEqual(invalid.attrs['data_quality']['state'],'INVALID')

    def test_confirmed_no_trades_does_not_invent_price_or_volume(self):
        stamp=self.raw.index[5];end=stamp+pd.Timedelta(minutes=15)
        with self.store.connect() as db:db.execute('delete from series where ts=?',(str(stamp),))
        self.store.set_setting('interval_evidence:'+ISIN,dict(isin=ISIN,identity=META,
            basis='complete_published_trade_files',reference='isolated test fixture',checked_at=NOW.isoformat(),
            no_trades=[[str(stamp),str(end)]],trade_intervals=[]))
        out=chart.derived_daily(self.store,self.info,NOW)
        self.assertTrue(out.bar_complete.all());self.assertEqual(out.volume.iloc[0],2191)
        self.assertEqual(out.attrs['data_quality']['state'],'READY')

    def test_identity_and_unconfirmed_receipt_rejected(self):
        k=chart.state_key(ISIN,'15m');s=self.store.setting(k)
        for change in ({'currency':'USD'},{'venue':'GER'},{'symbol':'APC.DE'}):
            self.store.set_setting(k,dict(s,identity=dict(META,**change)))
            self.assertTrue(chart.derived_daily(self.store,self.info,NOW).empty)
        self.store.set_setting(k,dict(s,listing_proof=dict(s['listing_proof'],status='UNCONFIRMED')))
        self.assertTrue(chart.derived_daily(self.store,self.info,NOW).empty)

    def test_revisions_revoke_and_restart_progress_avoids_rework(self):
        first=chart.prepare_daily(self.store,self.info,NOW)
        reopened=Store(self.tmp.name);self.addCleanup(reopened.read_cache.close)
        with patch.object(chart,'derived_daily',side_effect=AssertionError('unchanged archive recalculated')):
            self.assertTrue(chart.prepare_daily(reopened,self.info,NOW)['skipped_unchanged'])
        with self.store.connect() as db:db.execute('update series set volume=volume+7 where ts=?',(str(self.raw.index[5]),))
        changed=chart.prepare_daily(reopened,self.info,NOW)
        self.assertNotEqual(first['input_revisions'],changed['input_revisions'])
        self.assertEqual(chart.load(reopened,self.info,'1D',NOW).volume.iloc[0],2208)
        with self.store.connect() as db:db.execute('delete from series where ts=?',(str(self.raw.index[5]),))
        self.assertEqual(chart.prepare_daily(reopened,self.info,NOW)['complete_closed'],0)

    def test_atomic_preparation_rejects_concurrent_child_change(self):
        calculate=chart.derived_daily
        def race(*args):
            out=calculate(*args)
            with self.store.connect() as db:db.execute('update series set volume=volume+1 where ts=?',(str(self.raw.index[5]),))
            return out
        with patch.object(chart,'derived_daily',side_effect=race),self.assertRaisesRegex(ValueError,'INPUT_CHANGED'):
            chart.prepare_daily(self.store,self.info,NOW)
        self.assertIsNone(self.store.setting('gettex_chart_daily:'+ISIN))

    def test_existing_cp_turn_prepares_offline_despite_network_cooldown(self):
        self.store.set_setting('cp_source_retry:gettex_chart',(NOW+pd.Timedelta(days=1)).isoformat())
        result=chart.turn(self.info,self.store,NOW)
        self.assertEqual(result['state'],'SOURCE_COOLDOWN')
        self.assertEqual(result['daily_aggregation']['complete_closed'],1)
        self.assertEqual(self.store.setting('gettex_chart_daily:'+ISIN)['base'],'15m')

    def test_native_daily_kept_separate_and_all_intraday_targets_usable(self):
        native=resample(self.raw,'15m','1D',NOW)
        native.loc[:,'close']=150
        self.store.save_series(self.info,native,'1D')
        self.store.set_setting(chart.state_key(ISIN,'1D'),self.store.setting(chart.state_key(ISIN,'15m')))
        self.assertEqual(chart.load(self.store,self.info,'1D',NOW).close.iloc[0],150)
        self.assertEqual(chart.derived_daily(self.store,self.info,NOW).close.iloc[0],162)
        for tf,n in [('30m',31),('1H',16),('2H',8),('3H',6),('4H',4)]:
            out=resample(self.raw,'15m',tf,NOW)
            self.assertEqual(len(out),n);self.assertTrue(out.bar_complete.all())

    def test_hourly_context_pins_derived_daily_and_guards_real_children(self):
        from snapshot_worker import Value
        from performance_cache import frame_dependency,capture_closed_inputs,validate_closed_inputs,InputRevisionChanged
        c=self.ctx();c.timeframe=Value('4H');c.set_analysis_scope(NOW)
        def frame_for(info,tf):
            self.assertNotEqual(tf,'1D','daily must be pinned to the entry provider')
            f=resample(self.raw,'15m',tf,NOW)
            f.attrs['source_selection']={'policy':'proxy_extended_eur_v1','selected':'GETTEX_CHART_EUR'}
            return f
        with patch.object(c,'frame_for',side_effect=frame_for),patch.object(c,'user_state',return_value={'benchmark_id':None}),patch('snapshot_worker.analytics.analyse_multiframe',return_value={}) as engine:
            c._trading_analysis(self.info)
        day=engine.call_args.args[0]['1D']
        self.assertEqual(day.attrs['source'],'gettex_chart');self.assertEqual(day.attrs['aggregation_base'],'15m')
        spec=frame_dependency(ISIN,day,'1D',NOW)
        self.assertEqual(spec['base_tf'],'15m')
        with self.store.connect() as db:guard=capture_closed_inputs(db,[spec],{})
        with self.store.connect() as db:db.execute('update series set volume=volume+1 where ts=?',(str(self.raw.index[5]),))
        with self.store.connect() as db,self.assertRaises(InputRevisionChanged):validate_closed_inputs(db,guard)


if __name__=='__main__':unittest.main()
