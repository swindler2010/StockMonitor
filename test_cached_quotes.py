import sys,unittest,tempfile,sqlite3,json
from pathlib import Path
from contextlib import contextmanager
import pandas as pd
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
from storage import Store
from intraday import normalize
import data_quality as q
from timeframes import aggregate

class ClosingStore(Store):
    @contextmanager
    def connect(self):
        connection=sqlite3.connect(self.path)
        try:
            with connection:yield connection
        finally:connection.close()

class CachedQuotesTests(unittest.TestCase):
    now=pd.Timestamp('2026-10-02T20:00Z')
    def frame(self):
        idx=pd.date_range('2026-10-02 09:00',periods=34,freq='15min')
        f=pd.DataFrame({'open':10.,'high':11.,'low':9.,'close':10.,'volume':100.},index=idx)
        f.attrs=dict(source='proxy',venue='GER',currency='EUR',symbol='TEST.DE',timezone='Europe/Berlin')
        return f
    def extra(self,f,stamp,values):
        extra=pd.DataFrame([values],columns=q.FIELDS,index=pd.DatetimeIndex([stamp]))
        out=pd.concat([f,extra]).sort_index();out.attrs=f.attrs.copy();return out
    def norm(self,f):return normalize(f,'15m',now=self.now,cached_quotes_only=True)
    def test_store_read_is_memory_only_and_removes_only_quotes(self):
        f=self.frame();f=self.extra(f,'2026-10-02 10:01:23',[10,10,10,10,0]);f=self.extra(f,'2026-10-02 17:30',[10,10,10,10,0])
        with tempfile.TemporaryDirectory() as d:
            store=ClosingStore(d);store.save_series({'isin':'TEST'},f,'15m')
            before=store.path.read_bytes();out=store.load_series('TEST','15m','proxy','TEST.DE','GER','EUR')
            self.assertEqual(store.path.read_bytes(),before)
            pd.testing.assert_frame_equal(out[list(q.FIELDS)],self.frame(),check_freq=False,check_names=False)
            self.assertEqual(out.attrs['intraday_normalization']['excluded'],{'non_candle_quote':1,'provider_close_quote':1})
            store.read_cache.close()
    def test_genuine_invalid_rows_are_preserved(self):
        for stamp,values in [('2026-10-02 10:01:23',[10,11,9,10,100]),('2026-10-02 17:30',[10,11,9,10,100]),('2026-10-02 08:00',[10,10,10,10,0]),('2026-10-02 10:02',[-1,-1,-1,-1,0])]:
            with self.subTest(stamp=stamp):
                f=self.extra(self.frame(),stamp,values);out=self.norm(f)
                self.assertEqual(len(out),len(f));self.assertEqual(q.audit(out,'15m',now=self.now)['state'],'INVALID')
        for field,value in [('high',1),('volume',-1),('close',float('nan'))]:
            f=self.frame();f.iloc[0,f.columns.get_loc(field)]=value;out=self.norm(f)
            self.assertEqual(len(out),len(f));self.assertEqual(q.audit(out,'15m',now=self.now)['state'],'INVALID')
    def test_duplicates_order_identity_timezone_remain_blocked(self):
        f=self.frame()
        for bad in [pd.concat([f,f.iloc[[0]]]),f.iloc[::-1]]:
            bad.attrs=f.attrs.copy();out=self.norm(bad)
            self.assertEqual(len(out),len(bad));self.assertEqual(q.audit(out,'15m',now=self.now)['state'],'INVALID')
        for key,value in [('venue','UNKNOWN'),('timezone','America/New_York'),('currency','')]:
            bad=f.copy();bad.attrs[key]=value
            with self.assertRaises(q.QualityError):self.norm(bad)
        bad=f.copy();bad['currency']='USD'
        with self.assertRaises(q.QualityError):self.norm(bad)
        bad=f.copy();bad.index=bad.index.tz_localize('UTC')
        with self.assertRaises(q.QualityError):self.norm(bad)
    def test_gap_stale_and_persisted_invalid_are_not_cleared(self):
        f=self.frame().drop(pd.Timestamp('2026-10-02 10:00'));out=self.norm(f)
        self.assertEqual(q.audit(out,'15m',now=self.now)['state'],'GAP')
        f=self.frame();out=self.norm(f)
        self.assertEqual(q.audit(out,'15m',now=pd.Timestamp('2026-10-05T20:00Z'))['state'],'STALE')
        f.attrs['data_quality']={'state':'INVALID','issues':['persisted rejection'],'ranges':[]}
        out=self.norm(f);self.assertEqual(out.attrs['data_quality']['state'],'INVALID')
        self.assertEqual(aggregate(out,'15m','30m',now=self.now).attrs['data_quality']['state'],'INVALID')
    def test_real_flat_bars_and_forming_bars_retained(self):
        f=self.frame();f.iloc[0]=[10,10,10,10,0]
        out=normalize(f,'15m',now=pd.Timestamp('2026-10-02T10:00Z'),cached_quotes_only=True)
        pd.testing.assert_frame_equal(out,f,check_freq=False)
    def test_non_proxy_unchanged(self):
        f=self.frame();f.attrs['source']='gettex'
        pd.testing.assert_frame_equal(self.norm(f),f)
    def test_read_fails_closed_on_bad_coverage_timezone(self):
        with tempfile.TemporaryDirectory() as d:
            store=ClosingStore(d);f=self.frame();store.save_series({'isin':'TEST'},f,'15m')
            store.set_setting('history_coverage:TEST:TEST.DE:15m',{**f.attrs,'timezone':'America/New_York'})
            out=store.load_series('TEST','15m','proxy','TEST.DE','GER','EUR')
            self.assertEqual(len(out),len(f));self.assertEqual(q.audit(out,'15m',now=self.now)['state'],'INVALID')
            store.read_cache.close()

if __name__=='__main__':unittest.main()
