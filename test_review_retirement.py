"""Retired permissions are not a second market-history work queue."""
import copy, unittest
from unittest.mock import patch
import test_cp_autonomy02 as fixture
from cp_controls import historical_review_turn, historical_review_due, _review_receipts_retired
import cp_controls as cp


class RetirementTests(unittest.TestCase):
    def setUp(self):
        self.f=fixture.Review();self.f.setUp();self.addCleanup(self.f.tearDown)
        self.f.begin();self.key='cp_manual_risk:'+self.f.isin

    def completed(self, keep_original=False):
        g=self.f.store.setting(self.key);r=g['historical_review']
        r['results']={pid:dict(state='UNCONFIRMED',next_retry='2026-10-26T00:00Z') for pid in r['targets']}
        r.update(state='RUNNING',execution_suspended=True,summary={'UNCONFIRMED':len(r['targets'])})
        if not keep_original:g['accepted']={}
        self.f.store.set_setting(self.key,g);return g

    def test_completed_revocation_closes_once_without_audit_or_new_grant(self):
        before=self.completed();s=self.f.store
        with patch.object(fixture.dq,'audit',side_effect=AssertionError('retired review audited')):
            result=historical_review_turn(s,self.f.info,self.f.now)
            self.assertEqual(result['state'],'COMPLETE')
            after=s.setting(self.key)
            self.assertFalse(after['historical_review']['execution_suspended'])
            self.assertEqual(after['accepted'],{})
            self.assertEqual(after['historical_review']['results'],before['historical_review']['results'])
            with s.connect() as db:rev=cp._revisions(db,['setting:'+self.key])
            # New market data still receives ordinary DQ; it does not revive
            # the old permission review or invalidate an independent context.
            s.save_frame(self.f.isin,'gettex','5m',self.f.frame)
            for _ in range(20):
                self.assertEqual(historical_review_turn(s,self.f.info,self.f.now)['state'],'UNCHANGED')
                self.assertEqual(historical_review_due(s,[self.f.info],self.f.now),[])
            with s.connect() as db:self.assertEqual(cp._revisions(db,rev),rev)
            self.assertEqual(s.setting(self.f.qkey),self.f.q)

    def test_original_receipt_or_unreviewed_target_prevents_retirement(self):
        g=self.completed(keep_original=True)
        self.assertFalse(_review_receipts_retired(g))
        g['accepted']={};g['historical_review']['results']={'other':dict(state='RESOLVED')}
        self.assertFalse(_review_receipts_retired(g))
        g['historical_review']['receipts_retired']=True
        self.assertFalse(_review_receipts_retired(g))
        with self.assertRaisesRegex(ValueError,'REVIEW_PENDING'):self.f.gate()

    def test_independent_new_authority_preserved_not_issued_by_retirement(self):
        g=self.completed();pid,old=next(iter(g['historical_review']['targets'].items()))
        new=copy.deepcopy(old);new.update(accepted_at='2026-10-27T09:00Z',actor=cp.REVIEWED_ACTOR)
        g['accepted'][pid]=new;self.f.store.set_setting(self.key,g)
        historical_review_turn(self.f.store,self.f.info,self.f.now)
        self.assertEqual(self.f.store.setting(self.key)['accepted'],{pid:new})
        with self.f.store.connect() as db:
            # A policy-less fabricated test receipt is still rejected.
            self.assertEqual(cp.effective_accepted(db,self.f.isin,self.f.store.setting(self.key)),{})

    def test_retirement_persists_after_restart(self):
        self.completed();historical_review_turn(self.f.store,self.f.info,self.f.now)
        self.f.store.read_cache.close();self.f.store=fixture.Store(self.f.tmp.name)
        self.assertEqual(historical_review_due(self.f.store,[self.f.info],self.f.now),[])
        self.assertEqual(historical_review_turn(self.f.store,self.f.info,self.f.now)['state'],'UNCHANGED')


if __name__=='__main__':unittest.main()
