import unittest
from unittest.mock import patch
import pandas as pd
from test_p1_parent_recovery import ParentRecovery
import history_service as hs
import data_quality as dq


class RecoveryPreflight(unittest.TestCase):
    setUp=ParentRecovery.setUp
    tearDown=ParentRecovery.tearDown
    repair=ParentRecovery.repair
    rows=ParentRecovery.rows

    def recover(self, **kw):
        return hs.recover_from_children(self.info,self.store,self.meta,'1H','2026-10-26 10:00','2026-10-26 10:00',self.now,**kw)

    def test_empty_target_does_not_materialize_unrelated_archives(self):
        with self.store.connect() as db:
            db.execute("UPDATE series SET ts=replace(ts,'2026-10-26','2026-10-23') WHERE base_tf='5m'")
        with patch.object(self.store,'load_series',wraps=self.store.load_series) as loads:
            with self.store.read_cache.track_inputs() as tracked:
                self.assertIsNone(self.recover())
            loads.assert_not_called()
        self.assertTrue({'market:TEST:'+tf for tf in ('1H','15m','5m','1m')}<=set(tracked))
        self.store.save_series(self.info,self.child,'5m')
        self.assertFalse(self.store.read_cache.current(tracked))

    def test_parent_only_loaded_after_complete_child_and_rechecked_on_next_call(self):
        missing=self.child.loc[['2026-10-26 10:05']].copy()
        with self.store.connect() as db:
            db.execute("DELETE FROM series WHERE base_tf='5m' AND ts='2026-10-26 10:05:00'")
        with patch.object(self.store,'load_series',wraps=self.store.load_series) as loads:
            self.assertIsNone(self.recover())
            self.assertEqual([c.args[1] for c in loads.call_args_list],['5m'])
        self.store.save_series(self.info,missing,'5m')
        with patch.object(self.store,'load_series',wraps=self.store.load_series) as loads:
            result=self.recover()
            self.assertEqual([c.args[1] for c in loads.call_args_list],['5m','1H'])
        self.assertEqual(result.attrs['recovery_method'],'children:5m')
        self.assertTrue(result.attrs['parent_recovery_proofs'])
        self.assertEqual(result.volume.iloc[0],24.)

    def test_other_currency_venue_symbol_or_source_cannot_satisfy_preflight(self):
        with self.store.connect() as db:db.execute("DELETE FROM series WHERE base_tf='5m'")
        for field,value in [('currency','USD'),('venue','FRA'),('source','other'),('symbol','OTHER')]:
            f=self.child.copy();f.attrs={**self.meta,field:value}
            self.store.save_series(self.info,f,'5m')
        with patch.object(self.store,'load_series',wraps=self.store.load_series) as loads:
            self.assertIsNone(self.recover());loads.assert_not_called()

    def test_invalid_child_does_not_load_parent_or_create_ready(self):
        bad=self.child.loc[['2026-10-26 10:05']].copy();bad['high']=0
        self.store.save_series(self.info,bad,'5m')
        with patch.object(self.store,'load_series',wraps=self.store.load_series) as loads:
            self.assertIsNone(self.recover())
            self.assertEqual([c.args[1] for c in loads.call_args_list],['5m'])
        self.assertEqual(len(self.rows()),2)

    def test_download_fallback_survives_empty_local_preflight(self):
        with self.store.connect() as db:db.execute("DELETE FROM series WHERE base_tf='5m'")
        minute=pd.DataFrame(dict(open=100.,high=102.,low=99.,close=101.,volume=2.),index=pd.date_range('2026-10-26 10:00','2026-10-26 10:59',freq='1min'))
        minute.attrs=self.meta.copy()
        with patch.object(hs,'download',return_value=minute) as network:
            result=self.recover(allow_download=True)
        self.assertEqual(network.call_count,1)
        self.assertEqual(result.attrs['recovery_method'],'children:1m')
        self.assertEqual(result.volume.iloc[0],120.)
        self.assertEqual(len(self.rows()),2)  # Candidate alone never writes.

    def test_official_gettex_empty_range_skips_materialization(self):
        meta=dict(source='gettex',symbol=self.info['isin'],venue='GETTEX',currency='EUR',timezone='Europe/Berlin')
        with self.store.connect() as db:
            db.execute('INSERT INTO candles VALUES (?,?,?,?,?,?,?,?,?)',('TEST','gettex','5m','2026-10-23 10:00:00',100,102,99,101,2))
        with patch.object(self.store,'load_official',wraps=self.store.load_official) as loads:
            self.assertIsNone(hs.recover_from_children(self.info,self.store,meta,'15m','2026-10-26 10:00','2026-10-26 10:00',self.now))
            loads.assert_not_called()


if __name__=='__main__':unittest.main()
