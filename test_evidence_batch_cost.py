"""Evidence lookup cost and strict per-call revalidation; no provider/network use."""
import copy
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
import pandas as pd
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
import data_quality as q

NOW=pd.Timestamp('2026-11-10T17:00:00Z')
START=pd.Timestamp('2026-11-09 07:30')

def metadata():
    m=dict(source='gettex',venue='GETTEX',currency='EUR',symbol='TEST',
           timezone='Europe/Berlin',timeframe='5m',classify_absence=True)
    m['absence_evidence']=dict(isin='TEST',identity={k:m[k] for k in q.IDENTITY},
        basis='complete_published_trade_files',reference='isolated complete file',
        checked_at=NOW.isoformat(),no_trades=[[str(START),str(START+pd.Timedelta(hours=15,minutes=30))]],
        trade_intervals=[],zero_quantity_intervals=[])
    return m

class EvidenceBatchTests(unittest.TestCase):
    def test_200_intervals_prepare_each_evidence_list_once(self):
        m=metadata();stamps=q.expected_index(m,'5m',START,'2026-11-10 10:00',NOW)[:200]
        with patch.object(q,'_proof_index',wraps=q._proof_index) as index:
            actual=q.covered_empty(m,stamps,'5m',NOW)
        self.assertEqual(len(actual),186)
        self.assertLessEqual(index.call_count,3,'Evidence must not be rebuilt for each candle')

    def test_overlap_nested_adjacent_and_real_holes(self):
        m=metadata();p=m['absence_evidence']
        p['no_trades']=[['2026-11-09 08:00','2026-11-09 08:10'],
            ['2026-11-09 08:00','2026-11-09 09:00'],['2026-11-09 08:15','2026-11-09 08:30'],
            ['2026-11-09 09:00','2026-11-09 09:15'],['2026-11-09 09:20','2026-11-09 09:30']]
        self.assertTrue(q.no_trades_proof(m,'2026-11-09 08:05','2026-11-09 09:15',NOW))
        self.assertIsNone(q.no_trades_proof(m,'2026-11-09 09:10','2026-11-09 09:25',NOW))

    def test_trade_and_zero_records_override_empty_proof(self):
        for field in ('trade_intervals','zero_quantity_intervals'):
            m=metadata();m['absence_evidence'][field]=[['2026-11-09 08:02','2026-11-09 08:03']]
            self.assertIsNone(q.no_trades_proof(m,'2026-11-09 08:00','2026-11-09 08:05',NOW))
            self.assertTrue(q.no_trades_proof(m,'2026-11-09 08:03','2026-11-09 08:05',NOW))

    def test_mutation_of_evidence_and_identity_rechecked_next_batch(self):
        m=metadata();stamps=pd.DatetimeIndex([START])
        self.assertEqual(len(q.covered_empty(m,stamps,'5m',NOW)),1)
        m['absence_evidence']['trade_intervals'].append([str(START),str(START+pd.Timedelta(minutes=1))])
        self.assertEqual(len(q.covered_empty(m,stamps,'5m',NOW)),0)
        m['absence_evidence']['trade_intervals'].clear()
        self.assertEqual(len(q.covered_empty(m,stamps,'5m',NOW)),1)
        m['absence_evidence']['identity']['currency']='USD'
        self.assertEqual(len(q.covered_empty(m,stamps,'5m',NOW)),0)

    def test_future_check_incomplete_basis_and_invalid_time_remain_unconfirmed(self):
        for change in ({'checked_at':'2026-11-11T17:00Z'}, {'checked_at':'invalid'},
                       {'checked_at':'2026-11-09 07:40'}, {'basis':'empty_api_response'}, {'reference':''}):
            m=metadata();m['absence_evidence'].update(change)
            self.assertEqual(len(q.covered_empty(m,pd.DatetimeIndex([START]),'5m',NOW)),0)
        m=metadata();m['absence_evidence']['checked_at']='2026-11-09T06:34:59Z'
        self.assertIsNone(q.no_trades_proof(m,START,START+pd.Timedelta(minutes=5),NOW))
        m['absence_evidence']['checked_at']='2026-11-09T06:35:00Z'
        self.assertTrue(q.no_trades_proof(m,START,START+pd.Timedelta(minutes=5),NOW))

    def test_malformed_evidence_fails_closed_without_masking_other_fields(self):
        m=metadata();m['absence_evidence']['zero_quantity_intervals']=[['bad','time']]
        self.assertIsNone(q.no_trades_proof(m,START,START+pd.Timedelta(minutes=5),NOW))
        self.assertTrue(q.closure_conflict(m,START,START+pd.Timedelta(minutes=5),NOW))
        m['absence_evidence']['trade_intervals']=[[str(START),str(START+pd.Timedelta(minutes=1))]]
        self.assertTrue(q.known_trade(m,START,START+pd.Timedelta(minutes=5),NOW))

    def test_known_trade_requires_full_containment_and_publication(self):
        m=metadata();p=m['absence_evidence']
        p['trade_intervals']=[['2026-11-09 07:29','2026-11-09 07:31']]
        self.assertFalse(q.known_trade(m,START,START+pd.Timedelta(minutes=5),NOW))
        self.assertTrue(q.closure_conflict(m,START,START+pd.Timedelta(minutes=5),NOW))
        p['trade_intervals']=[[str(START),str(START+pd.Timedelta(minutes=1))]]
        self.assertTrue(q.known_trade(m,START,START+pd.Timedelta(minutes=5),NOW))
        p['checked_at']='2026-11-09T06:30:59Z'
        self.assertFalse(q.known_trade(m,START,START+pd.Timedelta(minutes=5),NOW))

    def test_nat_ranges_cannot_hide_trade_conflict(self):
        m=metadata();p=m['absence_evidence']
        p['trade_intervals']=[['NaT','NaT'],[str(START),str(START+pd.Timedelta(minutes=1))]]
        self.assertIsNone(q.no_trades_proof(m,START,START+pd.Timedelta(minutes=5),NOW))
        self.assertTrue(q.closure_conflict(m,START,START+pd.Timedelta(minutes=5),NOW))

    def test_closure_notice_does_not_override_conflicting_publication(self):
        m=metadata();p=m['absence_evidence'];p.update(exchange_closures=[q.GETTEX_EQUITY_INTERRUPTION],
            closure_checked_at=NOW.isoformat(),no_trades=[])
        stamp=pd.Timestamp('2026-10-26 08:00')
        self.assertEqual(len(q.covered_empty(m,pd.DatetimeIndex([stamp]),'5m',NOW,closed_only=True)),1)
        p['zero_quantity_intervals']=[[str(stamp),str(stamp+pd.Timedelta(minutes=1))]]
        self.assertEqual(len(q.covered_empty(m,pd.DatetimeIndex([stamp]),'5m',NOW,closed_only=True)),0)

    def test_53_identities_do_not_share_authorization(self):
        for n in range(53):
            m=metadata();m['symbol']='TEST'+str(n)
            if n%2==0:m['absence_evidence']['identity']['symbol']=m['symbol']
            self.assertEqual(len(q.covered_empty(m,pd.DatetimeIndex([START]),'5m',NOW)),int(n%2==0))

    def test_ohlcv_invalid_and_real_gap_still_block(self):
        m=metadata();idx=pd.date_range(START,periods=3,freq='5min')
        f=pd.DataFrame(dict(open=100.,high=101.,low=99.,close=100.,volume=10.),index=idx)
        f.attrs=m;f.attrs['absence_evidence']['no_trades']=[]
        hole=f.drop(idx[1]);before=copy.deepcopy(hole.attrs)
        report=q.audit(hole,'5m',now=NOW,check_stale=False)
        self.assertEqual(report['state'],'UNCONFIRMED')
        self.assertEqual(report['missing_count'],1)
        hole.attrs['absence_evidence']['trade_intervals']=[[str(idx[1]),str(idx[2])]]
        self.assertEqual(q.audit(hole,'5m',now=NOW,check_stale=False)['interval_classification']['GAP'],1)
        f.iloc[0,f.columns.get_loc('high')]=1
        self.assertEqual(q.audit(f,'5m',now=NOW,check_stale=False)['state'],'INVALID')
        self.assertEqual(before['absence_evidence']['no_trades'],[])

if __name__=='__main__':unittest.main()
