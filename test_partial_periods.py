import sys,json,copy,unittest,hashlib
from pathlib import Path
from unittest.mock import Mock
import pandas as pd
import numpy as np
from matplotlib.figure import Figure
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
import partial_chart as pc
import data_quality as q
from timeframes import schedule
from chart_modes import draw_prices

NOW=pd.Timestamp('2026-10-19T00:00:00Z')
class Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cases=[]
        for entry in json.loads(pc.REGISTRY.read_text())['archives']:
            data=json.loads((pc.REGISTRY.parent/entry['file']).read_text(encoding='utf8'))
            raw=pd.DataFrame(data['ohlcv'],columns=list(q.FIELDS),index=pd.to_datetime(data['timestamps']),dtype=float)
            raw.attrs=data['attrs'];daily=pc.split_archive(raw,entry['identity'],NOW)
            for tf in ('1W','1M'):
                cls.cases.append((entry,raw,daily,tf,pc.complete_periods(daily,tf,NOW)))

    def test_four_real_series_have_all_sessions_and_exact_ohlcv(self):
        evidence=[]
        for entry,raw,daily,tf,out in self.cases:
            self.assertIsNotNone(out);self.assertTrue(out.bar_complete.all())
            sched=schedule(raw.attrs,raw.index[0]-pd.Timedelta(days=40),raw.index[-1]+pd.Timedelta(days=40))
            for stamp,row in out.iterrows():
                start=stamp-pd.Timedelta(days=6) if tf=='1W' else stamp
                end=stamp if tf=='1W' else stamp+pd.offsets.MonthBegin(1)-pd.Timedelta(days=1)
                sessions=sched.loc[str(start.date()):str(end.date())]
                group=daily.loc[(daily.index>=start)&(daily.index<=end)]
                self.assertTrue(group.index.equals(sessions.index))
                self.assertLessEqual(sessions.close.iloc[-1],NOW)
                expected=[group.open.iloc[0],group.high.max(),group.low.min(),group.close.iloc[-1],group.volume.sum()]
                np.testing.assert_allclose(row[list(q.FIELDS)].to_numpy(dtype=float),expected,rtol=1e-12)
                for a,b in daily.attrs['partial_chart']['ranges']:
                    self.assertFalse(start<=pd.Timestamp(b) and end>=pd.Timestamp(a))
            self.assertEqual(out.attrs['data_quality']['state'],'INVALID')
            for key in q.IDENTITY:self.assertEqual(out.attrs[key],entry['identity'][key])
            evidence.append(dict(symbol=entry['identity']['symbol'],tf=tf,count=len(out),first=str(out.index[0]),last=str(out.index[-1]),**out.attrs['partial_chart']))
        Path('history64-evidence/partial-periods-result.json').write_text(json.dumps(evidence,ensure_ascii=False,indent=2),encoding='utf8')

    def test_retention_edge_and_forming_month_excluded(self):
        for entry,raw,daily,tf,out in self.cases:
            if tf=='1M':self.assertLess(out.index[-1],pd.Timestamp('2026-10-01'))
            early=pc.complete_periods(daily,tf,pd.Timestamp('2012-06-12T12:00:00Z'))
            self.assertTrue(all(pd.Timestamp(x)<pd.Timestamp('2012-06-12') for x in early.index))
            self.assertTrue(any(x['reason']=='FORMING_PERIOD' for x in early.attrs['partial_chart']['excluded_periods']))

    def test_missing_day_inside_bucket_blocks_entire_bucket_without_mutation(self):
        _,_,daily,_,_=self.cases[0];original=daily.copy(deep=True)
        damaged=daily.drop(pd.Timestamp('2026-09-15'))
        for tf,label in [('1W','2026-09-18'),('1M','2026-09-01')]:
            out=pc.complete_periods(damaged,tf,NOW)
            self.assertNotIn(pd.Timestamp(label),out.index)
            self.assertTrue(out.attrs['partial_chart']['break_before'])
        pd.testing.assert_frame_equal(original,daily)

    def test_renderer_breaks_and_blocked_binding_for_four_series(self):
        for _,_,_,_,out in self.cases:
            ax=Figure().subplots();draw_prices(ax,out,'Line')
            line=next(x for x in ax.lines if x.get_label()=='Close')
            self.assertEqual(np.isnan(line.get_ydata()).sum(),len(out.attrs['partial_chart']['break_before']))
            bound=pc.bind({'trading_engine':{'signal':'BUY','status':'READY'}},out)
            self.assertEqual(bound['trading_engine']['status'],'DATA BLOCKED')
            self.assertEqual(bound['trading_engine']['signal'],'WAIT')
            self.assertFalse(bound['analysis'][1]['ohlcv_valid'])

    def test_loader_derivatives_read_only_and_unknown_calendar_fails(self):
        for entry,raw,daily,tf,_ in self.cases:
            info=dict(isin=entry['isin'],eur_history_auto=True,eur_listings=[dict(entry['identity'],isin=entry['isin'],status='CONFIRMED')])
            store=Mock();store.setting.return_value={'state':'INVALID','identity':entry['identity']}
            out=pc.load(info,tf,pd.DataFrame(),store)
            self.assertIsNotNone(out);self.assertEqual(out.attrs['timeframe'],tf)
            self.assertTrue(pc.current(out.attrs['partial_chart']['files']))
            self.assertTrue(all(call[0]=='setting' for call in store.mock_calls))
        bad=daily.copy();bad.attrs=copy.deepcopy(daily.attrs);bad.attrs['venue']='UNKNOWN'
        with self.assertRaises(ValueError):pc.complete_periods(bad,'1M',NOW)

if __name__=='__main__':unittest.main()
