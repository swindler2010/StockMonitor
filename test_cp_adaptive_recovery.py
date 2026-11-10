import json,unittest
from unittest.mock import Mock,patch
import pandas as pd
from test_p1_parent_recovery import ParentRecovery
from storage import Store
import cp_experience as cp
import history_service as hs
import data_quality as dq


class IntervalMemory(unittest.TestCase):
    setUp=ParentRecovery.setUp
    tearDown=ParentRecovery.tearDown
    repair=ParentRecovery.repair
    rows=ParentRecovery.rows

    def local(self,callback,now=None,meta=None,at='2026-10-26 10:00'):
        return cp.local_recovery(self.info,self.store,meta or self.meta,'1H',at,at,now or self.now,callback)

    def test_same_interval_reused_but_expiry_and_clock_rollback_recheck(self):
        fn=Mock(return_value=None)
        self.assertEqual(self.local(fn),(None,False))
        self.assertEqual(self.local(fn,self.now+pd.Timedelta(minutes=1)),(None,True))
        self.assertEqual(self.local(fn,self.now+pd.Timedelta(minutes=5)),(None,False))
        self.assertEqual(self.local(fn,self.now-pd.Timedelta(minutes=1)),(None,False))
        self.assertEqual(fn.call_count,3)

    def test_unrelated_append_reuses_but_in_range_change_rechecks(self):
        fn=Mock(return_value=None);self.local(fn)
        later=self.child.iloc[-1:].copy();later.index=pd.to_datetime(['2026-10-26 12:00'])
        self.store.save_series(self.info,later,'5m')
        self.assertTrue(self.local(fn)[1])
        changed=self.child.loc[['2026-10-26 10:05']].copy();changed['volume']=5
        self.store.save_series(self.info,changed,'5m')
        self.assertFalse(self.local(fn)[1]);self.assertEqual(fn.call_count,2)

    def test_evidence_identity_dq_and_validator_change_recheck(self):
        fn=Mock(return_value=None);self.local(fn)
        self.store.set_setting('interval_evidence:TEST',{'new_evidence':'unconfirmed does not grant readiness'})
        self.assertFalse(self.local(fn)[1])
        self.store.set_setting('data_quality:TEST:TEST.DE:5m',{'state':'INVALID','issues':['bad'],'ranges':[]})
        self.assertFalse(self.local(fn)[1])
        self.assertFalse(self.local(fn,meta={**self.meta,'venue':'FRA'})[1])
        with patch.object(cp,'recovery_policy_digest',return_value='new validator'):
            self.assertFalse(self.local(fn)[1])
        self.assertEqual(fn.call_count,5)

    def test_race_does_not_save_negative_and_positive_is_never_memoized(self):
        def race():
            self.store.save_instrument({**self.info,'changed':True})
        self.local(race)
        fn=Mock(return_value=self.child)
        self.assertIs(self.local(fn)[0],self.child)
        self.assertIs(self.local(fn)[0],self.child)
        self.assertEqual(fn.call_count,2)

    def test_memory_survives_store_restart_without_market_writes(self):
        before=self.rows().copy();fn=Mock(return_value=None);self.local(fn)
        self.store.read_cache.close();self.store=Store(self.tmp.name)
        self.assertTrue(self.local(fn)[1]);self.assertEqual(fn.call_count,1)
        pd.testing.assert_frame_equal(before,self.rows())

    def test_real_repair_skips_unchanged_local_check_during_provider_wait(self):
        with self.store.connect() as db:db.execute("DELETE FROM series WHERE base_tf='5m' AND ts='2026-10-26 10:05:00'")
        wait=(self.now+pd.Timedelta(hours=1)).isoformat()
        def turn():return dq.repair(self.info,self.store,'TEST.DE','1H',self.meta,lambda *a,**k:self.fail('HTTP during cooldown'),self.now,network_not_before=wait)
        first=turn()
        later=self.child.iloc[-1:].copy();later.index=pd.to_datetime(['2026-10-26 12:00'])
        self.store.save_series(self.info,later,'5m')
        with patch.object(hs,'recover_from_children',side_effect=AssertionError('unchanged target search repeated')):second=turn()
        self.assertEqual(second['next_retry'],wait);self.assertNotEqual(second['state'],'READY')
        self.assertEqual(len(self.rows()),2)
        self.store.save_series(self.info,self.child.loc[['2026-10-26 10:05']],'5m')
        turn();self.assertEqual(len(self.rows()),3)

    def test_53_and_200_jobs_bound_memory_without_false_success(self):
        for n in (53,200):
            fn=Mock(return_value=None)
            for i in range(n):
                at=str(pd.Timestamp('2026-01-01')+pd.Timedelta(days=i))
                self.local(fn,at=at)
            with self.store.connect() as db:
                entries=[json.loads(v) for v, in db.execute("SELECT value FROM settings WHERE key LIKE 'cp_local_recovery:%'")]
            self.assertTrue(all(len(x)<=64 for x in entries))
            self.assertEqual(len(self.rows()),2)

    def test_new_evidence_reopens_local_recovery_without_market_revision(self):
        with self.store.connect() as db:db.execute("DELETE FROM series WHERE base_tf='5m' AND ts='2026-10-26 10:05:00'")
        wait=(self.now+pd.Timedelta(hours=1)).isoformat()
        def turn():return dq.repair(self.info,self.store,'TEST.DE','1H',self.meta,lambda *a,**k:self.fail('HTTP during cooldown'),self.now,network_not_before=wait)
        turn()
        self.store.set_setting('interval_evidence:TEST',dict(isin='TEST',identity=self.meta,basis='complete_published_trade_files',reference='isolated fixture',checked_at=self.now.isoformat(),no_trades=[['2026-10-26 10:05','2026-10-26 10:10']]))
        turn()
        self.assertEqual(len(self.rows()),3)
        self.assertEqual(self.rows().loc['2026-10-26 10:00','volume'],22.)

    def test_optional_memo_failure_keeps_original_validation_path(self):
        fn=Mock(return_value=self.child)
        with patch.object(cp,'local_failure_token',side_effect=ValueError('unreadable optional evidence')):
            self.assertIs(self.local(fn)[0],self.child)
        fn.assert_called_once()


class IndependentBatch(unittest.TestCase):
    setUp=ParentRecovery.setUp
    tearDown=ParentRecovery.tearDown
    rows=ParentRecovery.rows

    def prepare(self,bad_child='12:05'):
        from timeframes import aggregate
        self.now=pd.Timestamp('2026-10-26T15:00Z')
        frame=pd.DataFrame(dict(open=100.,high=102.,low=99.,close=101.,volume=2.),index=pd.date_range('2026-10-26 09:00','2026-10-26 13:55',freq='5min'))
        frame.attrs=self.meta.copy();parent=aggregate(frame,'5m','1H',self.now)
        parent.loc[pd.to_datetime(['2026-10-26 10:00','2026-10-26 12:00']),'high']=0
        self.store.save_series(self.info,parent,'1H');self.store.save_series(self.info,frame,'5m')
        with self.store.connect() as db:db.execute("DELETE FROM series WHERE base_tf='5m' AND ts=?",('2026-10-26 '+bad_child+':00',))
        return frame

    def run_batch(self,fetch=None,max_ranges=2):
        with patch.object(hs,'download',return_value=pd.DataFrame()):
            return dq.repair(self.info,self.store,'TEST.DE','1H',self.meta,fetch or (lambda *a,**kw:pd.DataFrame()),self.now,max_ranges=max_ranges)

    def test_bad_range_does_not_postpone_independent_real_local_repair(self):
        self.prepare();fetch=Mock(return_value=pd.DataFrame());report=self.run_batch(fetch)
        self.assertEqual(fetch.call_count,1)
        self.assertEqual(self.rows().loc['2026-10-26 10:00','high'],102)
        self.assertEqual(self.rows().loc['2026-10-26 12:00','high'],0)
        self.assertEqual(report['state'],'INVALID')
        self.assertEqual(len(report['recovery_completed_ranges']),1)
        self.assertEqual(len(report['recovery_failed_ranges']),1)
        self.assertEqual(report['recovery_cursor'],'2026-10-26 10:00:00')

    def test_429_stops_batch_and_never_calls_auxiliary_endpoint(self):
        from urllib.error import HTTPError
        self.prepare();error=HTTPError('https://example.invalid',429,'limited',{'Retry-After':'60'},None)
        with patch.object(hs,'download',side_effect=AssertionError('auxiliary bypassed429')):
            report=dq.repair(self.info,self.store,'TEST.DE','1H',self.meta,Mock(side_effect=error),self.now,max_ranges=2)
        self.assertEqual(report['recovery_failed_ranges'][0]['code'],'RATE_LIMIT_RESPONSE')
        self.assertEqual(len(report['recovery_ranges']),1)
        self.assertEqual(self.rows().loc['2026-10-26 10:00','high'],0)

    def test_identity_and_input_change_abort_following_ranges(self):
        self.prepare()
        for message in ('HISTORY_RESPONSE_IDENTITY_MISMATCH','Recovery input changed; no stale OHLCV write'):
            self.store.set_setting(dq.key(self.info,'TEST.DE','1H'),{})
            report=self.run_batch(Mock(side_effect=ValueError(message)))
            self.assertEqual(len(report['recovery_ranges']),1)
            self.assertEqual(self.rows().loc['2026-10-26 10:00','high'],0)

    def test_batch_budget_and_cursor_remain_bounded(self):
        self.prepare();report=self.run_batch(max_ranges=1)
        self.assertEqual(len(report['recovery_ranges']),1)
        self.assertEqual(report['recovery_cursor'],'2026-10-26 12:00:00')
        self.assertEqual(self.rows().loc['2026-10-26 10:00','high'],0)

    def test_committed_success_survives_later_failure_and_restart(self):
        self.prepare(bad_child='10:05');report=self.run_batch()
        self.assertEqual(len(report['recovery_completed_ranges']),1)
        self.store.read_cache.close();self.store=Store(self.tmp.name)
        self.assertEqual(self.rows().loc['2026-10-26 12:00','high'],102)
        self.assertEqual(self.rows().loc['2026-10-26 10:00','high'],0)
        self.assertEqual(report['state'],'INVALID')
        with self.store.connect() as db:
            events=cp.read(db)['recent']
        self.assertEqual(sum(x['outcome']=='SUCCESS' for x in events),1)
        self.assertEqual(sum(x['outcome']=='FAILED' for x in events),1)

    def test_pre_http_wait_is_not_a_failed_range_and_respects_source_deadline(self):
        self.prepare();wait=(self.now+pd.Timedelta(hours=2)).isoformat()
        self.store.set_setting('cp_source_retry:proxy',wait)
        report=self.run_batch(Mock(side_effect=ValueError('Proxy source cooldown')))
        self.assertEqual(report['recovery_failed_ranges'],[])
        self.assertEqual(report['next_retry'],wait)
        with self.store.connect() as db:self.assertEqual(cp.read(db)['recent'],[])


class MeasuredMethods(unittest.TestCase):
    setUp=ParentRecovery.setUp
    tearDown=ParentRecovery.tearDown
    repair=ParentRecovery.repair
    rows=ParentRecovery.rows

    def event(self,tf,ms,n,outcome='SUCCESS',result='CANDIDATE',ago=0,parents=1):
        at=self.now-pd.Timedelta(days=ago)+pd.Timedelta(microseconds=n)
        observations=[dict(tf=tf,elapsed_ms=ms,parents=parents,result=result)]
        reason=cp.measured_reason('ok' if outcome=='SUCCESS' else dict(version=1,code='INPUT_CHANGED'),observations)
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            cp.record(db,self.info,self.meta,'1H','2026-10-26 10:00','2026-10-26 10:00',{},at,'children:'+tf,outcome,reason)

    def order(self,meta=None,base='1H'):
        return cp.child_order(self.store,self.info,meta or self.meta,base,('15m','5m','1m'),self.now+pd.Timedelta(seconds=1))

    def test_three_measured_commits_each_choose_faster_without_removing_other_methods(self):
        for n in range(3):self.event('15m',40,n);self.event('5m',8,n)
        self.assertEqual(self.order(),('5m','15m','1m'))
        self.assertEqual(self.order(meta={**self.meta,'currency':'USD'}),('15m','5m','1m'))
        self.assertEqual(self.order(base='15m'),('15m','5m','1m'))

    def test_sparse_evidence_does_not_reorder_and_latency_is_per_parent(self):
        for n in range(2):self.event('15m',40,n);self.event('5m',8,n)
        self.assertEqual(self.order(),('15m','5m','1m'))
        self.event('15m',40,2);self.event('5m',8,2)
        with self.store.connect() as db:stats=cp.method_stats(db,self.info,self.meta,'1H',self.now+pd.Timedelta(seconds=1))
        self.assertEqual(stats['5m']['median_ms_per_parent'],8)

    def test_recent_rejections_outweigh_faster_but_less_reliable_method(self):
        for n in range(3):self.event('15m',40,n);self.event('5m',1,n)
        for n in range(3,6):self.event('5m',1,n,'FAILED','REJECTED')
        self.assertEqual(self.order(),('15m','5m','1m'))

    def test_revision_failed_candidate_is_not_a_committed_success(self):
        for n in range(3):self.event('5m',1,n,'FAILED')
        with self.store.connect() as db:stats=cp.method_stats(db,self.info,self.meta,'1H',self.now+pd.Timedelta(seconds=1))
        self.assertNotIn('5m',stats)

    def test_old_future_and_malformed_measurements_are_not_used(self):
        for n in range(3):self.event('5m',1,n,ago=31);self.event('15m',2,n,ago=-1)
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            cp.record(db,self.info,self.meta,'1H','a','b',{},self.now,'children:1m','FAILED',dict(version=1,code='x',method_observations=[{'tf':'1m','parents':0,'elapsed_ms':-1}]))
            stats=cp.method_stats(db,self.info,self.meta,'1H',self.now)
        self.assertEqual(stats,{})

    def test_natural_measurements_committed_atomically_and_read_after_restart(self):
        self.repair()
        self.store.read_cache.close();self.store=Store(self.tmp.name)
        with self.store.connect() as db:history=cp.read(db)
        event=next(x for x in history['recent'] if x['outcome']=='SUCCESS')
        self.assertIsNone(event['failure_evidence'])
        measured=event['method_observations'][0]
        self.assertEqual(measured['tf'],'5m');self.assertEqual(measured['parents'],1)
        self.assertGreaterEqual(measured['elapsed_ms'],0)
        self.assertEqual(measured['result'],'CANDIDATE')
        self.assertEqual(len(self.rows()),3)

    def test_repeated_method_observations_are_one_outcome_with_total_cost(self):
        reason=cp.measured_reason('ok',[dict(tf='5m',elapsed_ms=2,parents=1,result='REJECTED'),dict(tf='5m',elapsed_ms=3,parents=1,result='CANDIDATE')])
        self.assertEqual(len(reason['method_observations']),1)
        self.assertEqual(reason['method_observations'][0]['elapsed_ms'],5)

    def test_measurements_are_whitelisted_and_cannot_overflow_event(self):
        observations=[dict(tf=tf,elapsed_ms=5,parents=1,result='REJECTED',secret='must not persist') for tf in ('1m','5m','15m','1H')]
        self.assertNotIn('secret',json.dumps(cp.measured_reason('ok',observations)))
        reason=dict(version=1,code='x',explanation_ru='a'*1800)
        self.assertEqual(cp.measured_reason(reason,observations),reason)


if __name__=='__main__':unittest.main()
