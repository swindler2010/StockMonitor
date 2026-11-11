import sys,tempfile,unittest,json
from pathlib import Path
import pandas as pd
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
import data_quality as q
from storage import Store

META={'source':'proxy','venue':'NMS','currency':'USD','symbol':'AAPL','timezone':'America/New_York'}
NOW=pd.Timestamp('2026-10-04',tz='UTC')
def frame(base='1D',first='2026-09-01',last='2026-10-02 16:00'):
    idx=q.expected_index(META,base,first,last,NOW)
    f=pd.DataFrame({'open':10.,'high':12.,'low':9.,'close':11.,'volume':100.},index=idx)
    f.attrs=META.copy();return f

class Tests(unittest.TestCase):
    def test_ready_weekend_no_false_stale(self):self.assertEqual(q.audit(frame(),'1D',now=NOW)['state'],'READY')
    def test_gaps_only_missing_ranges(self):
        f=frame();gone=f.index[[3,4,10]];r=q.audit(f.drop(gone),'1D',now=NOW)
        self.assertEqual(r['state'],'GAP');self.assertEqual(r['missing_count'],3);self.assertEqual(len(r['ranges']),2)
    def test_duplicates(self):
        f=frame();g=pd.concat([f,f.iloc[[-1]]]);g.attrs=META
        self.assertEqual(q.audit(g,'1D',now=NOW)['state'],'INVALID')
    def test_order(self):self.assertEqual(q.audit(frame().iloc[::-1],'1D',now=NOW)['state'],'INVALID')
    def test_stale(self):self.assertEqual(q.audit(frame().iloc[:-2],'1D',now=NOW)['state'],'STALE')
    def test_impossible_ohlc(self):
        f=frame();f.iloc[2,f.columns.get_loc('high')]=1
        r=q.audit(f,'1D',now=NOW);self.assertEqual(r['state'],'INVALID');self.assertTrue(r['repairable'])
    def test_prices_volume_nan(self):
        for col,value in [('open',-1),('volume',-1),('close',float('inf')),('volume',float('nan'))]:
            f=frame();f.loc[f.index[3],col]=value;self.assertEqual(q.audit(f,'1D',now=NOW)['state'],'INVALID')
    def test_zero_and_outlier_are_visible(self):
        f=frame();f.iloc[-1,f.columns.get_loc('volume')]=0
        self.assertTrue(any('ZERO_VOLUME' in x for x in q.audit(f,'1D',now=NOW)['warnings']))
        f.iloc[-1,f.columns.get_loc('volume')]=100000
        self.assertTrue(any('OUTLIER' in x for x in q.audit(f,'1D',now=NOW)['warnings']))
    def test_timezone_listing_mismatch(self):
        f=frame();f.attrs['timezone']='Europe/Berlin';self.assertEqual(q.audit(f,'1D',now=NOW)['state'],'INVALID')
    def test_dst_nonexistent(self):
        f=frame().iloc[:1];f.index=pd.DatetimeIndex(['2026-03-08 02:30'])
        self.assertEqual(q.audit(f,'15m',now=NOW)['state'],'INVALID')
    def test_dst_sessions(self):
        for day,utc_hour in [('2026-03-06',14),('2026-03-09',13),('2026-11-02',14)]:
            idx=q.expected_index(META,'15m',day,day+' 23:00',pd.Timestamp('2026-12-01',tz='UTC'))
            self.assertEqual(idx[0].hour,9);self.assertEqual(idx.tz_localize(META['timezone']).tz_convert('UTC')[0].hour,utc_hour)
    def test_holiday_short_session(self):
        idx=q.expected_index(META,'15m','2025-11-27','2025-11-28 23:00',NOW)
        self.assertEqual(len(idx),14);self.assertEqual(idx[-1].hour,12)
    def test_off_session(self):
        f=frame('15m').iloc[:1];f.index=pd.DatetimeIndex(['2026-09-01 08:00']);self.assertEqual(q.audit(f,'15m',now=NOW)['state'],'INVALID')
    def test_identity_and_mixed_rows(self):
        for field,value in [('currency','EUR'),('venue','GER'),('symbol','MSFT'),('source','other')]:
            f=frame();f.attrs[field]=value;self.assertEqual(q.audit(f,'1D',META,NOW)['state'],'INVALID')
            f=frame();f[field]=META[field];f.loc[f.index[3],field]=value;self.assertEqual(q.audit(f,'1D',META,NOW)['state'],'INVALID')
    def test_no_calendar_not_ready(self):
        f=frame();f.attrs['venue']='UNKNOWN';self.assertEqual(q.audit(f,'1D',now=NOW)['state'],'INVALID')
    def test_gettex_published_calendar(self):
        meta={**META,'venue':'GETTEX','timezone':'Europe/Berlin','source':'gettex'}
        idx=q.expected_index(meta,'15m','2026-05-25','2026-05-25 23:00',NOW)
        self.assertEqual(len(idx),62);self.assertEqual(idx[0].strftime('%H:%M'),'07:30')
        self.assertEqual(len(q.expected_index(meta,'15m','2026-04-03','2026-04-03 23:00',NOW)),0)
        with self.assertRaises(ValueError):q.expected_index(meta,'15m','2025-01-01','2026-01-01',NOW)
    def test_raw_duplicate_recovery_is_bounded(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as tmp:
            store=Store(tmp);full=frame();raw=pd.concat([full,full.iloc[[-1]]]);raw.attrs=META
            calls=[]
            def download(symbol,interval,start,end=None):
                calls.append((start,end));return full.loc[(full.index>=start.tz_localize(None))&(full.index<end.tz_localize(None))].copy()
            fixed=q.repair_incoming({'isin':'A'},store,'AAPL','1D',raw,download,q.audit(raw,'1D',now=NOW),NOW)
            self.assertEqual(q.audit(fixed,'1D',now=NOW)['state'],'READY');self.assertEqual(len(calls),1)
            pd.testing.assert_frame_equal(full,fixed,check_freq=False);store.read_cache.close()
    def test_retry_cooldown(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as tmp:
            store=Store(tmp);full=frame();store.save_series({'isin':'A'},full.drop(full.index[3]),'1D');calls=[]
            def download(*args,**kwargs):calls.append(1);raise OSError('offline')
            for _ in range(2):q.repair({'isin':'A'},store,'AAPL','1D',META,download,NOW)
            self.assertEqual(len(calls),1);store.read_cache.close()
    def test_quality_blocks_signals(self):
        from analytics import analyse
        full=frame(first='2026-01-02')
        full['source']='proxy'
        for state in ['GAP','STALE','INVALID','RECOVERING','SOURCE_UNAVAILABLE']:
            full.attrs['data_quality']={'state':state}
            _,result=analyse(full,now=NOW);self.assertEqual(result['decision'],'WAIT');self.assertFalse(result['sufficient_history'])
    def test_gettex_recovery_updates_only_official_table(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as tmp:
            store=Store(tmp);info={'isin':'A'};meta={'source':'gettex','venue':'GETTEX','symbol':'A','currency':'EUR','timezone':'Europe/Berlin'}
            idx=q.expected_index(meta,'15m','2026-09-01','2026-09-01 23:00',NOW)
            full=pd.DataFrame({'open':10.,'high':12.,'low':9.,'close':11.,'volume':100.},index=idx);full.attrs=meta
            store.save_frame('A','gettex','15m',full.drop(idx[3]));calls=[]
            def download(symbol,interval,start,end=None):
                calls.append((start,end));return full.loc[(full.index>=start.tz_localize(None))&(full.index<end.tz_localize(None))].copy()
            r=q.repair(info,store,'A','15m',meta,download,NOW)
            self.assertEqual(r['missing_count'],0);self.assertEqual(len(calls),1)
            self.assertTrue(store.load_series('A','15m','gettex').empty)
            actual=store.load_official('A')[list(q.FIELDS)];store.read_cache.close()
            pd.testing.assert_frame_equal(actual,full[list(q.FIELDS)].rename_axis('ts'),check_freq=False)
    def test_gettex_recovery_download_filters_file_ranges(self):
        import gettex_history,gzip
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as tmp:
            store=Store(tmp);calls=[]
            names=['posttrade.20260901.08.00.munc.csv.gz','posttrade.20260920.08.00.munc.csv.gz']
            html=''.join('<a href="https://erdk.bayerische-boerse.de/'+n+'">x</a>' for n in names).encode()
            def http(url):
                calls.append(url);return html if url==gettex_history.PAGE else gzip.compress(b'test')
            def parse(raw,isin,file_time=None):return pd.DataFrame({'price':[10.],'volume':[1.]},index=[pd.Timestamp('2026-09-01 10:00')])
            gettex_history.download('A',http,parse,store,pd.Timestamp('2026-09-01 10:00',tz='Europe/Berlin'),pd.Timestamp('2026-09-01 10:15',tz='Europe/Berlin'))
            self.assertEqual(len(calls),2);self.assertFalse(any('20260920' in x for x in calls));store.read_cache.close()
    def test_failed_new_source_blocks_healthy_cached_signals(self):
        from desktop014 import Desktop014
        from analytics import analyse
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as tmp:
            store=Store(tmp);info={'isin':'A'};full=frame(first='2026-01-02');store.save_series(info,full,'1D')
            store.set_setting('history_identity:A',META)
            monitor=Desktop014();monitor.store=store;monitor.source_mode=lambda info:'proxy'
            for state in ['INVALID','RECOVERING','SOURCE_UNAVAILABLE']:
                q.persist(store,info,'AAPL','1D',{'state':state,'issues':['test failed fetch']})
                f=monitor._frame_uncached(info,'1D');_,result=analyse(f,now=NOW)
                self.assertEqual(f.attrs['data_quality']['state'],state);self.assertEqual(result['decision'],'WAIT');self.assertFalse(result['sufficient_history'])
            store.read_cache.close()
    def test_empty(self):self.assertEqual(q.audit(pd.DataFrame(),'1D')['state'],'SOURCE_UNAVAILABLE')
    def test_atomic_bounded_recovery_preserves_healthy(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as tmp:
            store=Store(tmp);info={'isin':'A'};full=frame();gone=full.index[[3,4]]
            store.save_series(info,full.drop(gone),'1D');calls=[]
            before=store.load_series('A','1D','proxy','AAPL')
            def download(symbol,interval,start,end=None):
                calls.append((start,end,store.setting(q.key(info,symbol,'1D'))['state']))
                return full.loc[(full.index>=start.tz_localize(None))&(full.index<end.tz_localize(None))].copy()
            r=q.repair(info,store,'AAPL','1D',META,download,NOW)
            self.assertEqual(r['state'],'READY');self.assertEqual(len(calls),1);self.assertEqual(calls[0][2],'RECOVERING')
            after=store.load_series('A','1D','proxy','AAPL');pd.testing.assert_frame_equal(before,after.loc[before.index],check_freq=False)
            store.read_cache.close();store=Store(tmp);self.assertEqual(store.setting(q.key(info,'AAPL','1D'))['state'],'READY');store.read_cache.close()
    def test_failed_recovery_preserves_cache(self):
        for kind in ['outage','currency','partial']:
            with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as tmp:
                store=Store(tmp);info={'isin':'A'};full=frame();store.save_series(info,full.drop(full.index[3]),'1D')
                before=store.load_series('A','1D','proxy','AAPL')
                def download(*args,**kwargs):
                    if kind=='outage':raise OSError('offline')
                    f=full.iloc[:1].copy()
                    if kind=='currency':f.attrs['currency']='EUR'
                    return f
                r=q.repair(info,store,'AAPL','1D',META,download,NOW)
                self.assertIn(r['state'],['INVALID','SOURCE_UNAVAILABLE'])
                pd.testing.assert_frame_equal(before,store.load_series('A','1D','proxy','AAPL'));store.read_cache.close()

if __name__=='__main__':unittest.main()
