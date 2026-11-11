"""Recovered parents depend on their own proven empty slots, not a whole manifest."""
import copy
import unittest
import pandas as pd
from test_p1_parent_recovery import ParentRecovery as _Fixture
import history_service as hs
import data_quality as dq


class ParentAbsenceScope(unittest.TestCase):
    setUp=_Fixture.setUp
    tearDown=_Fixture.tearDown
    rows=_Fixture.rows
    repair=_Fixture.repair
    def prepare_absence(self):
        self.proof=dict(isin='TEST',identity=self.meta,basis='complete_published_trade_files',
            reference='isolated fixture',checked_at=self.now.isoformat(),
            no_trades=[['2026-10-26 10:05','2026-10-26 10:10']])
        self.store.set_setting('interval_evidence:TEST',self.proof)
        with self.store.connect() as db:
            db.execute("DELETE FROM series WHERE base_tf='5m' AND ts='2026-10-26 10:05:00'")
        self.repair()
        self.assertFalse(self.rows().attrs['invalid_parent_provenance'])

    def test_manifest_refresh_and_unrelated_ranges_preserve_parent(self):
        self.prepare_absence();before=self.rows()[list(dq.FIELDS)].copy()
        updated=copy.deepcopy(self.proof)
        updated.update(checked_at=(self.now+pd.Timedelta(minutes=1)).isoformat(),
                       manifest='new files from a later session')
        updated['no_trades'].append(['2026-10-26 11:05','2026-10-26 11:10'])
        updated['trade_intervals']=[['2026-10-26 12:00','2026-10-26 12:05']]
        self.store.set_setting('interval_evidence:TEST',updated)
        out=self.rows();self.assertFalse(out.attrs['invalid_parent_provenance'])
        pd.testing.assert_frame_equal(before,out[list(dq.FIELDS)])

    def test_absence_revocation_conflict_identity_and_future_still_block(self):
        self.prepare_absence()
        variants=[{},dict(self.proof,no_trades=[]),
            dict(self.proof,trade_intervals=[['2026-10-26 10:05','2026-10-26 10:10']]),
            dict(self.proof,zero_quantity_intervals=[['2026-10-26 10:05','2026-10-26 10:10']]),
            dict(self.proof,identity={**self.meta,'venue':'FRA'}),
            dict(self.proof,checked_at='2099-01-01T00:00:00+00:00')]
        for value in variants:
            with self.subTest(value=value):
                self.store.set_setting('interval_evidence:TEST',value)
                self.assertIn('2026-10-26 10:00:00',self.rows().attrs['invalid_parent_provenance'])

    def test_legacy_stays_blocked_until_normal_local_recovery(self):
        self.prepare_absence();key=hs.parent_provenance_key('TEST',self.meta,'1H')
        proofs=self.store.setting(key)
        for proof in proofs.values():proof.pop('absence_scope',None)
        self.store.set_setting(key,proofs)
        self.store.set_setting('interval_evidence:TEST',dict(self.proof,checked_at=(self.now+pd.Timedelta(seconds=1)).isoformat()))
        self.assertTrue(self.rows().attrs['invalid_parent_provenance'])
        self.now+=pd.Timedelta(minutes=1);self.repair()
        self.assertFalse(self.rows().attrs['invalid_parent_provenance'])
        self.assertIn('absence_scope',self.store.setting(key)['2026-10-26 10:00:00'])

    def test_no_empty_slots_do_not_depend_on_other_parent_absence(self):
        self.prepare_absence()
        self.store.set_setting('interval_evidence:TEST',{})
        spec=dict(identity=self.meta,base_tf='5m',**{'from':'2026-10-26 11:00:00','through':'2026-10-26T11:00:00+00:00'})
        proof=dict(inputs=[spec],absence_scope=dict(version=1,base='5m',stamps=[]))
        self.assertTrue(hs._parent_absence_valid(proof,self.meta))
        proof['absence_scope']['stamps']=['2026-10-26 10:05']
        self.assertFalse(hs._parent_absence_valid(proof,self.meta))


if __name__=='__main__':unittest.main()
