"""Real calendar boundaries, no permission/price fabrication."""
import sys,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
import pandas as pd
import data_quality as dq
from trading_context import closed_parent_input
from analytics import resample

class ClosedParentInput(unittest.TestCase):
    def setUp(self):
        self.meta=dict(source='gettex_chart',symbol='AAPL.GTX',venue='GETTEX',currency='EUR',timezone='Europe/Berlin')
        self.now=pd.Timestamp('2026-11-11T07:44Z')
        idx=dq.expected_index(self.meta,'15m','2026-11-10','2026-11-11 08:30',self.now)
        self.raw=pd.DataFrame(dict(open=100.,high=101.,low=99.,close=100.,volume=10.),index=idx)
        self.raw.loc[pd.Timestamp('2026-11-11 08:15')]=[100.,101.,99.,100.,10.]
        self.raw.attrs=dict(self.meta,timeframe='15m',classify_absence=True)

    def test_forming_4h_children_do_not_damage_yesterdays_closed_input(self):
        raw=self.raw.drop(pd.Timestamp('2026-11-11 08:00'))
        before=raw.copy();out,at=closed_parent_input(raw,'15m','4H',self.now)
        self.assertEqual(at,self.now)
        self.assertEqual(out.index.max(),pd.Timestamp('2026-11-10 22:45'))
        self.assertEqual(dq.audit(out,'15m',now=at,check_stale=False)['state'],'READY')
        parent=resample(out,'15m','4H',self.now)
        self.assertEqual(dq.audit(parent,'4H',now=self.now)['state'],'READY')
        self.assertEqual(parent.attrs['data_quality']['state'],'READY')
        pd.testing.assert_frame_equal(raw,before)

    def test_gap_in_closed_1h_still_blocks(self):
        raw=self.raw.drop(pd.Timestamp('2026-11-11 08:00'))
        out,at=closed_parent_input(raw,'15m','1H',self.now)
        self.assertEqual(at,self.now)
        self.assertIn(['2026-11-11 08:00:00','2026-11-11 08:00:00'],dq.audit(out,'15m',now=at)['ranges'])
        self.assertNotEqual(dq.audit(out,'15m',now=at,check_stale=False)['state'],'READY')

    def test_missing_required_tail_remains_blocked(self):
        raw=self.raw.loc[self.raw.index<pd.Timestamp('2026-11-11 08:00')]
        out,at=closed_parent_input(raw,'15m','1H',self.now)
        q=dq.audit(out,'15m',now=at)
        self.assertGreater(q.get('missing_count',0),0)
        self.assertNotEqual(q['state'],'READY')

    def test_invalid_closed_ohlcv_is_not_removed(self):
        self.raw.loc[pd.Timestamp('2026-11-10 12:00'),'high']=1
        out,at=closed_parent_input(self.raw,'15m','4H',self.now)
        self.assertEqual(dq.audit(out,'15m',now=at)['state'],'INVALID')

    def test_all_intraday_targets_use_calendar_closed_boundary(self):
        from timeframes import MINUTES,endpoints
        for tf in MINUTES:
            if tf=='1m':continue
            out,at=closed_parent_input(self.raw,'15m',tf,self.now)
            self.assertLessEqual(at,self.now)
            self.assertTrue((endpoints(out)<=pd.Timestamp(out.attrs['closed_parent_input']['through'])).all())
        for tf in ('1W','1M'):
            out,at=closed_parent_input(self.raw,'15m',tf,self.now)
            self.assertIs(out,self.raw) # outside this hourly integration, no invented policy

    def test_daily_and_dst_boundary_keep_exchange_timezone(self):
        daily=pd.DataFrame(dict(open=[100.,100.],high=101.,low=99.,close=100.,volume=10.),index=pd.to_datetime(['2026-11-10','2026-11-11']))
        daily.attrs=dict(self.meta,timeframe='1D')
        out,at=closed_parent_input(daily,'1D','1D',self.now)
        self.assertEqual(list(out.index),[pd.Timestamp('2026-11-10')])
        for now,expected in [('2026-10-23T06:44Z','2026-10-23T06:30Z'),('2026-10-26T07:44Z','2026-10-26T07:30Z')]:
            out,at=closed_parent_input(self.raw,'15m','1H',pd.Timestamp(now))
            self.assertEqual(pd.Timestamp(out.attrs['closed_parent_input']['through']),pd.Timestamp(expected))
            self.assertEqual(at,pd.Timestamp(now))


class ClosedParentAcceptedTail(unittest.TestCase):
    from test_tv_accepted_display import AcceptedDisplay as _fixture
    setUp=_fixture.setUp
    tearDown=_fixture.tearDown
    prepare=_fixture.prepare

    def build(self,extra=False):
        import cp_controls as c
        self.prepare(granted=[pd.Timestamp('2026-11-02 19:15')],
                     other=[pd.Timestamp('2026-11-02 19:00')] if extra else [])
        raw,clock=closed_parent_input(self.raw,'15m','4H',self.now)
        raw.attrs['data_quality']=dq.audit(raw,'15m',now=clock,check_stale=False)
        raw=c.accepted_gap_input(self.s,self.i,raw,'15m',clock)
        return c.accepted_gap_output(resample(raw,'15m','4H',self.now),'4H',self.now)

    def test_reviewed_last_child_allows_closed_parent_without_invented_bar(self):
        import cp_controls as c
        out=self.build()
        self.assertTrue(out.loc[pd.Timestamp('2026-11-02 15:30')].bar_complete)
        self.assertTrue(c.analysis_quality_allowed(out.attrs['data_quality'],out.attrs.get('accepted_risk')))
        self.assertNotIn(pd.Timestamp('2026-11-02 19:15'),self.raw.index)
        self.assertEqual(out.iloc[-1].volume,self.raw.loc['2026-11-02 15:30':'2026-11-02 19:15'].volume.sum())
        self.assertEqual(out.attrs['accepted_risk']['price_estimates'],[])

    def test_unreviewed_neighbour_cannot_borrow_accepted_tail(self):
        out=self.build(extra=True)
        self.assertNotIn('accepted_risk',out.attrs)
        self.assertNotIn(pd.Timestamp('2026-11-02 15:30'),out.index)

if __name__=='__main__':unittest.main()
