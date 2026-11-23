"""Batch geometry must match the retained scalar proof on every input."""
import copy,random,sys,unittest
from pathlib import Path
from unittest.mock import patch
import pandas as pd
import numpy as np
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
import data_quality as q
import gettex_first as gf
NOW=pd.Timestamp('2026-11-24T18:00Z')
def meta():
    m=dict(source='gettex',venue='GETTEX',currency='EUR',symbol='TEST',timezone='Europe/Berlin',timeframe='5m',classify_absence=True)
    m['absence_evidence']=dict(isin='TEST',identity={k:m[k] for k in q.IDENTITY},basis='complete_published_trade_files',reference='isolated complete publication',checked_at=NOW.isoformat(),no_trades=[],trade_intervals=[],zero_quantity_intervals=[])
    return m
class Batch(unittest.TestCase):
    def same(self,m,stamps,base='5m',now=NOW,closed_only=False):
        with patch.object(q,'_plain_empty_mask',return_value=None):expected=q.covered_empty(m,stamps,base,now,closed_only=closed_only)
        actual=q.covered_empty(m,stamps,base,now,closed_only=closed_only)
        self.assertTrue(actual.equals(expected),(base,len(actual),len(expected)));return actual
    def test_53_200_random_overlap_nested_holes_trade_zero_and_duplicate_order(self):
        rng=random.Random(47);start=pd.Timestamp('2026-11-09 07:30');stamps=pd.date_range(start,periods=186,freq='5min')
        for count in (53,200):
            for _ in range(count):
                m=meta();p=m['absence_evidence']
                for field in ('no_trades','trade_intervals','zero_quantity_intervals'):
                    p[field]=[[str(start+pd.Timedelta(minutes=(a:=rng.randrange(0,900)))),str(start+pd.Timedelta(minutes=a+rng.randrange(1,160)))] for _ in range(rng.randrange(0,12))]
                selected=stamps if rng.randrange(2) else stamps.take([rng.randrange(len(stamps)) for _ in range(80)])
                self.same(m,selected)
    def test_all_supported_tf_session_close_dst_and_daily(self):
        for tf in q.STEPS:
            for first in ('2026-03-27','2026-10-23','2026-11-09'):
                m=meta();m['absence_evidence']['no_trades']=[[first+' 00:00',str(pd.Timestamp(first)+pd.Timedelta(days=8))]]
                stamps=q.expected_index(m,tf,first,pd.Timestamp(first)+pd.Timedelta(days=6),NOW)
                if len(stamps)<16:stamps=stamps.repeat(16)
                self.same(m,stamps,tf)
    def test_header_clock_and_in_place_revision_revalidated(self):
        stamps=pd.date_range('2026-11-09 07:30',periods=53,freq='5min');m=meta();p=m['absence_evidence'];p['no_trades']=[['2026-11-09 07:30','2026-11-09 23:00']]
        self.assertEqual(len(self.same(m,stamps)),53)
        for field,value in [('basis','empty_http_response'),('reference',''),('checked_at','2026-11-25T00:00Z'),('checked_at','bad'),('checked_at','2026-11-09T07:00Z')]:
            other=copy.deepcopy(m);other['absence_evidence'][field]=value;self.same(other,stamps)
        p['trade_intervals'].append(['2026-11-09 07:31','2026-11-09 07:32']);self.assertEqual(len(self.same(m,stamps)),52)
        p['identity']['currency']='USD';self.assertEqual(len(self.same(m,stamps)),0)
    def test_malformed_and_unsupported_shapes_fall_back(self):
        stamps=pd.date_range('2026-11-09 07:30',periods=53,freq='5min')
        for ranges in ([['bad','time']],[['NaT','NaT']],[['2026-11-09 08:00','2026-11-09 07:30']],[['2026-11-09 08:00','2026-11-09 08:00']],[['2026-11-09T08:00Z','2026-11-09T09:00Z']]):
            for field in ('no_trades','trade_intervals','zero_quantity_intervals'):
                m=meta();m['absence_evidence'][field]=ranges;self.same(m,stamps)
        m=meta();self.same(m,pd.date_range('2026-11-09 22:00',periods=53,freq='5min'))
        self.same(m,stamps,now=None)
    def test_exchange_notice_and_official_correction_scopes_unchanged(self):
        stamps=pd.date_range('2026-10-26 07:30',periods=53,freq='5min');m=meta();m['absence_evidence'].update(exchange_closures=[q.GETTEX_EQUITY_INTERRUPTION],closure_checked_at=NOW.isoformat())
        self.same(m,stamps);self.same(m,stamps,closed_only=True)
        m['absence_evidence']['zero_quantity_intervals']=[['2026-10-26 08:10','2026-10-26 08:15']];self.same(m,stamps)
        authority=meta();authority['absence_evidence']['no_trades']=[['2026-10-26 07:30','2026-10-26 23:00']]
        m['official_retiming_empty']=[dict(first='2026-10-26 07:30',end='2026-10-26 23:00',authority=authority)]
        self.same(m,stamps)
    def test_entire_dq_report_keeps_real_gaps_invalid_and_publication_conflicts(self):
        stamps=pd.date_range('2026-11-09 07:30',periods=80,freq='5min')
        m=meta();missing=stamps[1::3];m['absence_evidence']['no_trades']=[[str(t),str(t+q.STEPS['5m'])] for t in missing]
        f=pd.DataFrame(dict(open=10.,high=11.,low=9.,close=10.,volume=1.),index=stamps.difference(missing));f.attrs=m
        cases=[f]
        broken=f.copy();broken.iloc[0,broken.columns.get_loc('high')]=1.;cases.append(broken)
        gap=f.copy();gap.attrs['absence_evidence']['no_trades']=[];cases.append(gap)
        incomplete=f.copy();incomplete.attrs['absence_evidence']['incomplete_publication_intervals']=[[str(stamps[0]),str(stamps[2])]];cases.append(incomplete)
        for data in cases:
            with patch.object(q,'_plain_empty_mask',return_value=None):expected=q.audit(data,'5m',now=NOW,check_stale=False)
            actual=q.audit(data,'5m',now=NOW,check_stale=False)
            actual.pop('checked_at',None);expected.pop('checked_at',None) # wall-clock telemetry only
            self.assertEqual(actual,expected)
        self.assertEqual(q.audit(broken,'5m',now=NOW,check_stale=False)['state'],'INVALID')
        self.assertIn('source_incomplete_count',q.audit(incomplete,'5m',now=NOW,check_stale=False))

    def test_lineage_reuses_only_header_from_its_validated_read_view(self):
        import sqlite3
        from contextlib import contextmanager
        from types import SimpleNamespace
        from unittest.mock import Mock
        from history_service import attach_parent_provenance,parent_provenance_key
        identity=dict(source='gettex_chart',symbol='TEST.GTX',venue='GETTEX',currency='EUR',timezone='Europe/Berlin')
        stamps=pd.date_range('2026-11-09 07:30',periods=53,freq='15min')
        f=pd.DataFrame(dict(open=10.,high=11.,low=9.,close=10.,volume=1.),index=stamps);f.attrs=identity.copy()
        current=dict(identity={**identity,'source':'gettex','symbol':'TEST'},checked_at=NOW.isoformat())
        records={str(t):{'official_timebase':'receipt:'+str(n)} for n,t in enumerate(stamps)}
        receipts={'receipt:'+str(n):dict(first=str(t),end=str(t+pd.Timedelta(minutes=15)),mode='VERIFIED_NATIVE_EMPTY',source={'proof':{'identity':current['identity']} }) for n,t in enumerate(stamps)}
        db=sqlite3.connect(':memory:');db.execute('PRAGMA query_only=ON');db.execute('BEGIN')
        @contextmanager
        def connect():yield db
        key=parent_provenance_key('TEST',identity,'15m');mapping={key:records,'interval_evidence:TEST':current,**receipts}
        try:
            for use_view,seed in ((True,True),(True,False),(False,False)):
                store=SimpleNamespace(connect=connect,setting=Mock(side_effect=lambda k,default=None:mapping.get(k,default)),read_cache=Mock())
                if use_view:store._lineage_read_view=(db,NOW,{('official_proof','TEST'):current} if seed else {})
                def valid(*args,**kw):
                    return True
                with patch('revision_watcher.retime_current',side_effect=valid):out=attach_parent_provenance(store,f.copy(),'TEST','15m')
                self.assertEqual(len(out.attrs['official_retiming_empty']),53)
                self.assertTrue(all(x['authority']['absence_evidence']['checked_at']==NOW.isoformat() for x in out.attrs['official_retiming_empty']))
                reads=sum(c.args[0]=='interval_evidence:TEST' for c in store.setting.call_args_list)
                self.assertEqual(reads,(0 if seed else 1) if use_view else 53)
                self.assertIn(('setting:interval_evidence:TEST',),[c.args for c in store.read_cache.depend.call_args_list])
        finally:db.close()

    def test_contract_binding_does_not_copy_discarded_proof_or_change_rows(self):
        class NoCopy:
            def __deepcopy__(self,memo):raise AssertionError('Discarded metadata copied')
        f=pd.DataFrame(dict(open=[10.],high=[11.],low=[9.],close=[10.],volume=[1.],source=['gettex']),index=pd.to_datetime(['2026-11-09 07:30']))
        f.attrs=meta();f.attrs['large_proof']=NoCopy();before=f.to_numpy(copy=True)
        with patch.object(gf,'source_contract',return_value=True):
            for n in range(253):
                out=gf.bind(f,{'isin':'TEST'});self.assertIsNot(out,f);self.assertTrue(gf.bound(out))
                self.assertEqual(out.attrs['gettex_first_contract']['identity'],{k:f.attrs[k] for k in q.IDENTITY})
        np.testing.assert_array_equal(f.to_numpy(),before);self.assertNotIn('gettex_first_contract',f.attrs)
        out.attrs['new']='value';self.assertNotIn('new',f.attrs)
if __name__=='__main__':unittest.main()
