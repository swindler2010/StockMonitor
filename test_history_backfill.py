import sys,tempfile,unittest
from pathlib import Path
import pandas as pd
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
from storage import Store
from history_backfill import sync,key

class Tests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.store=Store(self.temp.name);self.info={'isin':'TEST','history_symbol':'TEST'}
        self.now=pd.Timestamp('2026-10-04',tz='UTC');self.calls=[]
    def tearDown(self):
        self.store.read_cache.close();self.temp.cleanup()
    def frame(self,first='2006-10-04',last='2026-10-02',listed='2006-10-04',base='1D'):
        from data_quality import expected_index
        meta={'venue':'NMS','timezone':'America/New_York'}
        index=expected_index(meta,base,first,last if base=='1D' else last+' 16:00',self.now)
        f=pd.DataFrame({'open':10.,'high':12.,'low':9.,'close':11.,'volume':100.},index=index)
        f.attrs={'source':'proxy','symbol':'TEST','venue':'NMS','currency':'USD','timezone':'America/New_York','first_trade_date':int(pd.Timestamp(listed,tz='UTC').timestamp())}
        return f
    def load(self,frame):
        def download(symbol,interval,start):
            self.calls.append(start);return frame.copy()
        return sync(self.info,self.store,'TEST','1D',download,self.now)
    def test_full_then_incremental(self):
        result=self.load(self.frame());self.assertTrue(result['backfill_complete'])
        self.assertEqual(self.calls[0],pd.Timestamp('1900-01-01',tz='UTC'))
        self.load(self.frame('2026-10-02'))
        self.assertEqual(self.calls[-1].date(),pd.Timestamp('2026-10-03').date())
        self.assertEqual(self.store.load_series('TEST','1D','proxy','TEST').index[0],pd.Timestamp('2006-10-04'))
    def test_young_is_not_rebackfilled(self):
        result=self.load(self.frame('2020-09-16',listed='2020-09-16'));self.assertEqual(result['status'],'COMPLETE_FROM_INCEPTION')
        self.load(self.frame('2026-10-02',listed='2020-09-16'));self.assertEqual(self.calls[-1].year,2026)
    def test_incomplete_not_ready(self):
        result=self.load(self.frame('2020-01-01'));self.assertEqual(result['status'],'INCOMPLETE')
    def test_invalid_does_not_certify_or_overwrite(self):
        f=self.frame();f.loc[f.index[0],'high']=1
        with self.assertRaises(ValueError):self.load(f)
        self.assertEqual(self.store.setting(key(self.info,'TEST','1D'),{})['status'],'ERROR')
        self.assertTrue(self.store.load_series('TEST','1D','proxy','TEST').empty)
    def test_identity_change_preserves_history(self):
        self.load(self.frame());f=self.frame('2026-10-02');f.attrs['currency']='EUR'
        with self.assertRaises(ValueError):self.load(f)
        self.assertEqual(self.store.load_series('TEST','1D','proxy','TEST').attrs['currency'],'USD')
    def test_restart_uses_persisted_cursor(self):
        self.load(self.frame());self.store.read_cache.close();self.store=Store(self.temp.name)
        self.load(self.frame('2026-10-02'));self.assertEqual(self.calls[-1].year,2026)
    def test_empty_incremental_is_not_identity_failure(self):
        from history_service import recover
        from unittest.mock import patch
        self.load(self.frame())
        with patch('history_service.candidates',return_value=['TEST']),patch('history_service.download',return_value=pd.DataFrame()):
            meta,errors=recover(self.info,self.store)
        self.assertEqual(meta['status'],'COMPLETE')
        self.assertFalse(any('не совпадает' in error for error in errors))
    def test_source_limited_restart_is_incremental(self):
        calls=[]
        frame=self.frame('2026-08-06',base='15m')
        def download(symbol,interval,start):calls.append(start);return frame
        first=sync(self.info,self.store,'TEST','15m',download,self.now)
        self.assertEqual(first['status'],'SOURCE_LIMITED')
        self.store.read_cache.close();self.store=Store(self.temp.name)
        sync(self.info,self.store,'TEST','15m',download,self.now)
        self.assertGreater(calls[-1],self.now-pd.Timedelta(days=3))
    def test_hourly_maximum_is_separate_from_15m(self):
        calls=[]
        def download(symbol,interval,start):calls.append((interval,start));return self.frame('2024-10-04',base='1H')
        state=sync(self.info,self.store,'TEST','1H',download,self.now)
        self.assertEqual(calls[0][0],'60m');self.assertEqual(state['status'],'SOURCE_LIMITED')
        self.assertIn('730',state['limitation']);self.assertTrue(self.store.load_series('TEST','15m','proxy','TEST').empty)
    def test_indicators_weekly_monthly_use_full_sqlite_history(self):
        from analytics import resample,analyse,bar_ends
        self.load(self.frame());raw=self.store.load_series('TEST','1D','proxy','TEST')
        raw.attrs['timezone']='America/New_York'
        for tf in ('1D','1W','1M'):
            frame=resample(raw,'1D',tf);frame.attrs['timeframe']=tf
            enriched,result=analyse(frame,now=self.now)
            self.assertEqual(result['bar_count'],len(frame));self.assertGreater(len(frame),200)
            self.assertTrue(result['ohlcv_valid']);self.assertTrue(enriched.ema30.notna().any())
        self.assertEqual(bar_ends(frame)[0].tz_convert('America/New_York').date(),pd.Timestamp('2006-10-31').date())
    def test_transaction_rollback(self):
        self.load(self.frame());before=self.store.setting(key(self.info,'TEST','1D'))
        with self.store.connect() as db:db.execute("CREATE TRIGGER fail_history BEFORE INSERT ON series BEGIN SELECT RAISE(ABORT,'test failure'); END")
        with self.assertRaises(Exception):self.load(self.frame('2026-10-02'))
        after=self.store.setting(key(self.info,'TEST','1D'))
        self.assertEqual(after['status'],'ERROR');self.assertEqual(after['count'],before['count'])

if __name__=='__main__':unittest.main()
