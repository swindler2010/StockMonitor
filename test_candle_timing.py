import sys,sqlite3,tempfile,unittest,json,time
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace
import pandas as pd
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
from storage import Store
import candle_timing as timing

class Timing(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.s=Store(self.tmp.name)
        self.info={'isin':'DE0005557508','name':'Fixture'}
        self.meta=dict(source='proxy',venue='GER',currency='EUR',symbol='TEST.DE',timezone='Europe/Berlin')
        self.f=pd.DataFrame([[10,11,9,10,2]],columns=timing.VALUES,index=pd.to_datetime(['2026-10-21 09:00']))
        self.f.attrs={**self.meta,'timeframe':'5m'}
    def tearDown(self):
        self.s.read_cache.close();self.tmp.cleanup()
    def rows(self):
        with self.s.connect() as db:return db.execute('select * from candle_observations').fetchall()
    def test_new_unchanged_and_correction(self):
        self.s.save_series(self.info,self.f,'5m');before=self.rows()
        self.s.save_series(self.info,self.f,'5m');self.assertEqual(before,self.rows())
        self.f['volume']=3;self.s.save_series(self.info,self.f,'5m');after=self.rows()
        self.assertEqual(before[0][7],after[0][7]);self.assertEqual(after[0][9],2);self.assertEqual(after[0][-1],3)
    def test_direct_sql_update_and_rollback_atomic(self):
        self.s.save_series(self.info,self.f,'5m');before=self.rows()
        with self.assertRaisesRegex(RuntimeError,'rollback'):
            with self.s.connect() as db:
                db.execute('update series set volume=4');raise RuntimeError('rollback')
        self.assertEqual(before,self.rows())
        with self.s.connect() as db:db.execute('update series set volume=4')
        self.assertEqual(self.rows()[0][9],2)
    def test_old_cache_not_backdated(self):
        with self.s.connect() as db:
            db.execute('drop trigger candle_timing_series_insert')
        self.s.save_series(self.info,self.f,'5m')
        with self.s.connect() as db:timing.install(db)
        self.s.save_series(self.info,self.f,'5m');self.assertEqual(self.rows(),[])
        self.f['volume']=3;self.s.save_series(self.info,self.f,'5m')
        self.assertIsNone(self.rows()[0][7])
    def test_gettex_and_other_listing_remain_separate(self):
        self.s.save_series(self.info,self.f,'5m')
        other=self.f.copy();other.attrs.update(venue='FRA',symbol='TEST.F');self.s.save_series(self.info,other,'5m')
        self.s.save_frame(self.info['isin'],'gettex','5m',self.f)
        self.s.save_frame(self.info['isin'],'csv','5m',self.f)
        self.assertEqual(len(self.rows()),3)
        self.assertEqual({r[2] for r in self.rows()},{'GER','FRA','GETTEX'})
    def bar(self):
        return dict(open=10,high=11,low=9,close=10,volume=2,open_at='2026-10-21T07:00:00Z',close_at='2026-10-21T07:05:00Z',closed=True)
    def test_read_exact_values_and_no_invented_provider_time(self):
        self.s.save_series(self.info,self.f,'5m');page=[self.bar()]
        self.assertEqual(timing.annotate_page(self.s,self.info['isin'],'5m',self.meta,page,'2026-10-23T17:00Z'),1)
        t=page[0]['delivery_timing'];self.assertIsNone(t['provider_published_at']);self.assertIsNone(t['application_received_at'])
        self.assertGreater(t['bar_close_to_storage_seconds'],0)
        page=[{**self.bar(),'volume':999}]
        self.assertEqual(timing.annotate_page(self.s,self.info['isin'],'5m',self.meta,page,'2026-10-23T17:00Z'),0)
        page=[self.bar()];self.assertEqual(timing.annotate_page(self.s,self.info['isin'],'4H',self.meta,page,'2026-10-23T17:00Z'),0)
        self.assertEqual(page[0]['delivery_timing']['status'],'NOT_MEASURED_DERIVED')
    def test_read_analytics_existing_path_exports_observation(self):
        import analytical_read as ar
        self.s.save_series(self.info,self.f,'5m')
        context=SimpleNamespace(instrument_cfg=lambda:self.info,user_state=lambda info:{},frame_for=lambda info,tf:self.f.copy())
        with patch.object(ar,'SnapshotContext',return_value=context):
            r=ar.calculate(self.s,{'request':{'isin':self.info['isin'],'timeframe':'5m','limit':10}})
        self.assertEqual(r['delivery_latency']['storage_observed_bars'],1)
        self.assertEqual(r['bars'][0]['delivery_timing']['status'],'PARTIAL_MEASURED')
        self.assertEqual(r['delivery_latency']['provider_latency'],'UNCONFIRMED')
        json.dumps(r,allow_nan=False)
    def test_no_change_does_not_bump_market_revision(self):
        self.s.save_series(self.info,self.f,'5m')
        with self.s.connect() as db:before=db.execute('select * from input_revisions').fetchall()
        self.s.save_series(self.info,self.f,'5m')
        with self.s.connect() as db:after=db.execute('select * from input_revisions').fetchall()
        self.assertEqual(before,after)

if __name__=='__main__':unittest.main()
