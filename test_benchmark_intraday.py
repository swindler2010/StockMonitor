"""Native index intraday integration; no working database or account writes."""
import copy,json,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import Mock,patch
from types import SimpleNamespace
import pandas as pd
import requests
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
import benchmarks as bm
import history_service as history
import data_quality as quality
from storage import Store
from intraday import normalize
from benchmark_ui import BenchmarkMixin
from benchmark_display import select_frame

ARCHIVE=Path(__file__).resolve().parents[2]/'СМ-Архив'
TEMP_ROOT=ARCHIVE if ARCHIVE.is_dir() else None

class IntradayIndexTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(prefix='index-intraday-',dir=TEMP_ROOT)
        self.store=Store(self.temp.name)
        self.addCleanup(self.temp.cleanup);self.addCleanup(self.store.read_cache.close)
        self.info=bm.CATALOG['INDEX:SP500'];self.now=pd.Timestamp('2026-11-25T16:00Z')
    def bars(self,info=None):
        info=info or self.info;meta=bm.data_identity(info,'5m')
        f=pd.DataFrame({'open':[100.,101.],'high':[102.,103.],'low':[99.,100.],
                        'close':[101.,102.],'volume':[0.,0.]},index=pd.to_datetime(['2026-11-25 09:30','2026-11-25 09:35']))
        f.attrs=meta;return f
    def test_every_index_has_an_exact_intraday_identity_and_vix_daily_stays_cboe(self):
        for info in bm.CATALOG.values():
            meta=bm.data_identity(info,'5m')
            self.assertEqual(meta,bm.proxy_identity(meta['symbol']))
            self.assertEqual(meta['source'],'proxy')
            self.assertNotEqual(meta['symbol'],'SPY')
        vix=bm.CATALOG['INDEX:VIX']
        self.assertEqual(bm.data_identity(vix)['source'],'cboe')
        self.assertEqual(bm.data_identity(vix,'5m')['symbol'],'^VIX')
        wrong={**vix,'currency':'EUR'}
        with self.assertRaisesRegex(ValueError,'IDENTITY'):bm.data_identity(wrong,'5m')
    def test_verified_index_parser_accepts_na_volume_but_stocks_and_wrong_instrument_type_stay_rejected(self):
        meta=bm.data_identity(self.info,'5m');stamps=list(pd.to_datetime(['2026-11-25T14:30Z','2026-11-25T14:35Z']).as_unit('ns').asi8//10**9)
        payload={'chart':{'error':None,'result':[{'meta':{'symbol':meta['symbol'],'currency':'USD','exchangeName':'SNP','exchangeTimezoneName':'America/New_York','instrumentType':'INDEX'},
            'timestamp':stamps,'indicators':{'quote':[{'open':[100.,101.],'high':[102.,103.],'low':[99.,100.],'close':[101.,102.],'volume':[None,None]}]}}]}}
        response=Mock();response.json.return_value=payload
        with patch.object(history.requests,'get',return_value=response) as http:
            f=history.download('^GSPC','5m',start=pd.Timestamp('2026-11-25T14:30Z'),end=pd.Timestamp('2026-11-25T14:40Z'))
            self.assertEqual(len(f),2);self.assertTrue((f.volume==0).all())
            self.assertEqual(f.attrs['symbol'],'^GSPC')
            payload['chart']['result'][0]['meta']['instrumentType']='ETF'
            with self.assertRaisesRegex(ValueError,'INDEX_TYPE'):history.download('^GSPC','5m',start=pd.Timestamp('2026-11-25T14:30Z'))
            count=http.call_count
            with self.assertRaisesRegex(ValueError,'EUR_HISTORY_IDENTITY'):history.download('AAPL','5m')
            with self.assertRaisesRegex(ValueError,'EUR_HISTORY_ONLY'):history.download('AAPL','5m',expected={**meta,'symbol':'AAPL'})
            self.assertEqual(http.call_count,count)
    def test_regular_writer_saves_one_intraday_base_and_never_replaces_daily_archive(self):
        daily=self.bars().copy();daily.index=pd.to_datetime(['2026-11-24','2026-11-25']);daily.attrs=bm.identity(self.info)
        self.store.save_series(self.info,daily,'1D');before=bm.load(self.store,self.info).copy()
        result=bm.sync_intraday(self.store,self.info,self.now,downloader=lambda *_:self.bars())
        self.assertEqual(result['status'],'SAVED');self.assertEqual(result['count'],2)
        pd.testing.assert_frame_equal(bm.load(self.store,self.info),before)
        self.assertEqual(len(bm.load(self.store,self.info,'5m')),2)
        with self.store.connect() as db:
            self.assertFalse(db.execute("SELECT key FROM settings WHERE key LIKE 'paper_%' OR key LIKE 'real_%'").fetchall())
    def test_changed_currency_or_corrupt_price_does_not_replace_previous_cache(self):
        original=self.bars();self.store.save_series(self.info,original,'5m')
        for column in ('currency','price'):
            incoming=self.bars();incoming['close']=999
            if column=='currency':incoming.attrs['currency']='EUR'
            else:incoming['high']=1
            plan={'due':True,'key':'test-index-negative-'+column,'close_at':None}
            result=bm.sync_intraday(self.store,self.info,self.now,downloader=lambda *_:incoming,plan=plan)
            self.assertEqual(result['quality'],'INVALID')
            self.assertEqual(bm.load(self.store,self.info,'5m').close.tolist(),[101.,102.])
    def test_429_retry_after_is_durable_and_new_plan_cannot_bypass_it(self):
        response=requests.Response();response.status_code=429;response.headers['Retry-After']='7200'
        error=requests.HTTPError('429',response=response)
        def fail(*_):raise error
        bm.sync_intraday(self.store,self.info,self.now,downloader=fail)
        self.assertEqual(self.store.setting('cp_source_retry:proxy'),(self.now+pd.Timedelta(hours=2)).isoformat())
        self.assertFalse(bm.feed_plan(self.store,self.info,self.now+pd.Timedelta(minutes=30))['due'])
    def test_closed_market_does_not_repeat_identical_interval_after_restart(self):
        clock=pd.Timestamp('2026-11-29T12:00Z');self.store.save_series(self.info,self.bars(),'5m')
        self.store.set_setting(bm.history_key(self.info),{'attempted_at':clock.isoformat()})
        bm.sync_intraday(self.store,self.info,clock,downloader=lambda *_:self.bars())
        self.assertFalse(bm.feed_plan(self.store,self.info,clock+pd.Timedelta(minutes=5))['due'])
    def test_empty_weekend_source_retries_are_bounded_and_resume_daily_without_no_trades(self):
        clock=pd.Timestamp('2026-11-29T12:00Z')
        self.store.set_setting(bm.history_key(self.info),{'attempted_at':clock.isoformat()})
        for n in range(4):
            at=clock+pd.Timedelta(minutes=16*n)
            result=bm.sync_intraday(self.store,self.info,at,downloader=lambda *_:pd.DataFrame())
            self.assertEqual(result['quality'],'SOURCE_UNAVAILABLE')
            self.assertTrue(bm.load(self.store,self.info,'5m').empty)
        self.assertEqual(result['publication_wait'],'UNCONFIRMED_AFTER_BOUNDED_RECHECKS')
        paused=bm.feed_plan(self.store,self.info,clock+pd.Timedelta(hours=2))
        self.assertFalse(paused['due'] and paused['base']=='5m')
        plan=bm.feed_plan(self.store,self.info,clock+pd.Timedelta(hours=25))
        self.assertTrue(plan['due']);self.assertEqual(plan['base'],'5m')
        self.assertNotIn('NO_TRADES',json.dumps(result))

    def test_late_last_closed_bar_is_queued_again_instead_of_frozen_overnight(self):
        clock=pd.Timestamp('2026-11-29T12:00Z')
        self.store.set_setting(bm.history_key(self.info),{'attempted_at':clock.isoformat()})
        result=bm.sync_intraday(self.store,self.info,clock,downloader=lambda *_:self.bars())
        self.assertEqual(result['publication_wait'],'LATE_CLOSED_TAIL')
        retry=bm.feed_plan(self.store,self.info,clock+pd.Timedelta(minutes=16))
        self.assertTrue(retry['due']);self.assertEqual(retry['base'],'5m')

    def test_candlepilot_turn_invokes_only_one_existing_base_loader(self):
        with patch.object(bm,'sync_intraday',return_value={'quality':'READY'}) as intra,patch.object(bm,'sync') as daily:
            bm.sync_history(self.store,self.info,self.now)
            intra.assert_called_once();daily.assert_not_called()
    def test_overview_names_both_vix_providers_and_keeps_their_dates_separate(self):
        from benchmark_display import information_text
        facts={'archive':{'status':'INCOMPLETE'},'quality':'STALE','count':1006,
               'first':'2023-01-03','last':'2026-11-24','close':26.36,
               'intraday':{'count':300,'last':'2026-11-26 09:05','quality':'STALE'}}
        text=information_text(bm.CATALOG['INDEX:VIX'],facts)
        for value in ('Cboe','Yahoo Finance','^VIX','1D','5m','26.11','24.11','300'):self.assertIn(value,text)

    def test_vix_intraday_receipt_never_becomes_official_cboe_history(self):
        info=bm.CATALOG['INDEX:VIX'];incoming=self.bars(info);incoming.index=pd.to_datetime(['2026-11-25 08:30','2026-11-25 08:35'])
        result=bm.sync_intraday(self.store,info,self.now,downloader=lambda *_:incoming)
        self.assertEqual(result['identity']['source'],'proxy');self.assertEqual(result['identity']['symbol'],'^VIX')
        self.assertTrue(bm.load(self.store,info).empty)
        self.assertEqual(len(bm.load(self.store,info,'5m')),2)
        wrong=incoming.copy();wrong.attrs=bm.identity(info)
        rejected=select_frame(info,'5m',wrong,self.store,self.now)
        self.assertTrue(rejected.empty);self.assertEqual(rejected.attrs['data_quality']['state'],'INVALID')

class RecordedSourceIndexTests(unittest.TestCase):
    def test_six_recorded_native_indices_render_all_eight_intraday_timeframes(self):
        probe=ARCHIVE/'Index intraday source probe.json'
        if not probe.exists():self.skipTest('Addressed source observation file not installed')
        observations=json.loads(probe.read_text(encoding='utf-8'))['observations']
        with tempfile.TemporaryDirectory(prefix='index-source-',dir=TEMP_ROOT) as folder:
            store=Store(folder)
            try:
                for info in bm.CATALOG.values():
                    meta=bm.data_identity(info,'5m');obs=next(o for o in observations if o.get('summary',{}).get('symbol')==meta['symbol'])
                    d=obs['raw']['chart']['result'][0];q=d['indicators']['quote'][0];clock=pd.Timestamp(obs['checked_at'])
                    f=pd.DataFrame({k:q[k] for k in quality.FIELDS},index=pd.to_datetime(d['timestamp'],unit='s',utc=True).tz_convert(meta['timezone']))
                    f['volume']=pd.to_numeric(f.volume,errors='coerce').fillna(0);f.attrs=meta;f=normalize(f,'5m',now=clock)
                    store.save_series(info,f,'5m');context=SimpleNamespace(store=store,analysis_at=clock)
                    for tf in ('5m','10m','15m','30m','1H','2H','3H','4H'):
                        with self.subTest(index=info['isin'],tf=tf):
                            analytical=BenchmarkMixin._frame_uncached(context,info,tf)
                            shown=select_frame(info,tf,analytical,store,clock)
                            self.assertGreater(len(shown),0)
                            for k,v in meta.items():self.assertEqual(shown.attrs[k],v)
                            self.assertNotIn('accepted_risk',shown.attrs)
                            if info['isin']=='INDEX:STOXX600':
                                self.assertTrue(shown.attrs.get('partial_chart',{}).get('display_only'))
                                self.assertNotEqual(shown.attrs['data_quality']['state'],'READY')
            finally:store.read_cache.close()

if __name__=='__main__':unittest.main()
