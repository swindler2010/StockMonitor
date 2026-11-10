import copy
import json
import tempfile
import unittest
from pathlib import Path

import pandas as pd

import gettex_chart as chart
import data_quality as quality
from storage import Store
from timeframes import endpoints, aggregate

ISIN='US0378331005'
META=chart.identity(ISIN,'AAPL.GTX')
NOW=pd.Timestamp('2026-10-28T12:00:00Z')
FIX=Path(__file__).resolve().parents[1]/'history64-evidence/proxy-auto28/source-probes'


def payload(base='15m', rows=None):
    if rows is None:
        rows=[{'TIME':str(int(pd.Timestamp('2026-10-27 07:44:00',tz='Europe/Berlin').timestamp())),
               'OPEN':'100','HIGH':'102','LOW':'99','CLOSE':'101','VOLUME':'20'}]
    return {'status':'OK','request':{'ric':'AAPL.GTX','interval':15 if base=='15m' else 5,'intervalUnit':'m'},'data':rows}


class FakeClient:
    def __init__(self, data=None, meta=None, callback=None):
        self.data=data or payload();self.meta=meta or META;self.callback=callback;self.calls=0
    def fetch(self,*args):
        self.calls+=1
        if self.callback:self.callback()
        return copy.deepcopy(self.data),dict(self.meta)


class ChartTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(prefix='gettex-chart-new-')
        self.addCleanup(self.tmp.cleanup)
        self.store=Store(self.tmp.name);self.info={'isin':ISIN,'name':'Apple','kind':'stock'}
        self.addCleanup(self.store.read_cache.close)
        self.store.save_watchlist([self.info])
    def decode(self,p=None,base='15m',meta=None,now=NOW):
        return chart.decode(p or payload(),meta or META,ISIN,base,NOW-pd.Timedelta(days=90),now,now)
    def isolate_base(self,base='15m'):
        for other in chart.BASES:
            if other!=base:self.store.set_setting(chart.state_key(ISIN,other),{'retry_after':(NOW+pd.Timedelta(days=2)).isoformat()})

    def test_real_saved_provider_rows_and_same_provider_aggregation(self):
        for base in ('5m','15m'):
            p=json.loads((FIX/('gettex-apple-overlap-'+base+'.json')).read_text())
            f=self.decode(p,base);self.assertEqual(len(f),124 if base=='5m' else 58)
            self.assertEqual(f.attrs['source'],'gettex_chart');self.assertEqual(f.volume.sum(),3994)
        f=self.decode(json.loads((FIX/'gettex-apple-old-15m.json').read_text()))
        self.assertEqual(len(f),308)
        out=aggregate(f,'15m','4H',now=NOW)
        self.assertGreater(len(out),0);self.assertEqual(out.attrs['source'],'gettex_chart')
        self.assertNotEqual(out.attrs['data_quality']['state'],'READY') # stale/gaps not erased

    def test_wrong_listing_currency_and_interval_rejected(self):
        for meta in (dict(META,currency='USD'),dict(META,venue='GER'),dict(META,symbol='APC.DE')):
            with self.subTest(meta=meta),self.assertRaises(ValueError):self.decode(meta=meta)
        p=payload();p['request']['interval']=60
        with self.assertRaises(ValueError):self.decode(p)
        p=payload();p['request']['ric']='SAPG.GTX'
        with self.assertRaises(ValueError):self.decode(p)

    def test_invalid_duplicates_and_off_session_never_silently_filtered(self):
        p=payload();p['data'][0]['HIGH']='90'
        with self.assertRaises(ValueError):self.decode(p)
        p=payload();p['data']*=2
        with self.assertRaises(ValueError):self.decode(p)
        p=payload();p['data'][0]['TIME']=str(int(pd.Timestamp('2026-10-27 05:00',tz='Europe/Berlin').timestamp()))
        with self.assertRaises(ValueError):self.decode(p)

    def test_forming_bar_not_saved_and_no_synthetic_gaps(self):
        p=payload();p['data'][0]['TIME']=str(int(pd.Timestamp('2026-10-28 12:59',tz='Europe/Berlin').timestamp()))
        f=self.decode(p,now=pd.Timestamp('2026-10-28T11:59:30Z'));self.assertTrue(f.empty)
        p=payload();row=dict(p['data'][0]);row['TIME']=str(int(row['TIME'])+1800);p['data'].append(row)
        f=self.decode(p);r=quality.audit(f,'15m',now=NOW)
        self.assertEqual(len(f),2);self.assertEqual(r['state'],'UNCONFIRMED');self.assertGreater(r['missing_count'],0)

    def test_commit_is_separate_durable_and_idempotent(self):
        self.isolate_base();client=FakeClient()
        first=chart.turn(self.info,self.store,NOW,client);self.assertEqual(first['added'],1)
        reopened=Store(self.tmp.name)
        self.addCleanup(reopened.read_cache.close)
        frame=chart.load(reopened,self.info,'15m');self.assertEqual(len(frame),1)
        again=chart.turn(self.info,reopened,NOW+pd.Timedelta(minutes=16),client)
        self.assertEqual(again['state'],'UNCHANGED');self.assertEqual(again['added'],0)
        with reopened.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM candles').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT count(*) FROM candle_revision_history').fetchone()[0],0)
        self.assertEqual(first['input_revisions_after'],again['input_revisions_after'])

    def test_changed_same_provider_bar_uses_existing_revision_history(self):
        self.isolate_base();chart.turn(self.info,self.store,NOW,FakeClient())
        p=payload();p['data'][0]['CLOSE']='102'
        result=chart.turn(self.info,self.store,NOW+pd.Timedelta(minutes=16),FakeClient(p))
        self.assertEqual(result['changed'],1)
        with self.store.connect() as db:
            row=db.execute('SELECT old_ohlcv,new_ohlcv,revision_before,revision_after FROM candle_revision_history').fetchone()
        self.assertEqual(json.loads(row[0])['close'],101);self.assertEqual(json.loads(row[1])['close'],102)
        self.assertGreater(row[3],row[2])

    def test_input_change_rejects_entire_commit(self):
        client=FakeClient(callback=lambda:self.store.save_watchlist([dict(self.info,name='Changed')]))
        result=chart.turn(self.info,self.store,NOW,client)
        self.assertEqual(result['state'],'ERROR');self.assertIn('INPUT_CHANGED',result['reason'])
        with self.store.connect() as db:self.assertEqual(db.execute('SELECT count(*) FROM series').fetchone()[0],0)

    def test_empty_or_invalid_response_keeps_accepted_rows(self):
        self.isolate_base();chart.turn(self.info,self.store,NOW,FakeClient())
        empty=payload(rows=[])
        result=chart.turn(self.info,self.store,NOW+pd.Timedelta(minutes=16),FakeClient(empty))
        self.assertEqual(result['state'],'SOURCE_UNAVAILABLE');self.assertEqual(len(chart.load(self.store,self.info,'15m')),1)

    def test_cooldown_and_unchanged_interval_prevent_requests(self):
        self.isolate_base();client=FakeClient();chart.turn(self.info,self.store,NOW,client)
        out=chart.turn(self.info,self.store,NOW+pd.Timedelta(seconds=10),client)
        self.assertEqual(out['state'],'NOT_DUE');self.assertEqual(client.calls,1)
        self.store.set_setting('cp_source_retry:gettex_chart',(NOW+pd.Timedelta(hours=1)).isoformat())
        self.assertEqual(chart.turn(self.info,self.store,NOW+pd.Timedelta(minutes=20),client)['state'],'SOURCE_COOLDOWN')
        self.assertEqual(client.calls,1)

    def test_calendar_padding_does_not_reject_real_january_or_certify_2025(self):
        f=pd.DataFrame([[100,101,99,100,1]],columns=chart.FIELDS,index=pd.to_datetime(['2026-01-02']))
        f.attrs=dict(META,timeframe='1D');self.assertFalse(endpoints(f).isna().any())
        f.index=pd.to_datetime(['2025-12-30'])
        with self.assertRaises(ValueError):endpoints(f)

    def test_retry_after_is_obeyed_without_retry_loop(self):
        import requests
        class RateClient:
            def fetch(self,*args):
                response=requests.Response();response.status_code=429;response.headers['Retry-After']='7200'
                raise requests.HTTPError('rate limited',response=response)
        self.assertEqual(chart.turn(self.info,self.store,NOW,RateClient())['state'],'ERROR')
        self.assertGreaterEqual(pd.Timestamp(self.store.setting('cp_source_retry:gettex_chart')),NOW+pd.Timedelta(hours=2))
        client=FakeClient();self.assertEqual(chart.turn(self.info,self.store,NOW+pd.Timedelta(hours=1),client)['state'],'SOURCE_COOLDOWN')
        self.assertEqual(client.calls,0)

    def test_saved_source_selection_and_missing_daily_remain_safe(self):
        self.isolate_base();chart.turn(self.info,self.store,NOW,FakeClient())
        from snapshot_worker import SnapshotContext,Value
        ctx=SnapshotContext();ctx.store=self.store;ctx.analysis_at=NOW
        ctx.watchlist=[self.info];ctx.drafts={};ctx.symbol=Value('Apple');ctx.timeframe=Value('15m');ctx.period=Value('Max')
        from desktop014 import Desktop014
        f=Desktop014._frame_uncached(ctx,self.info,'15m')
        self.assertEqual(f.attrs['source'],'gettex_chart')
        self.assertNotEqual(f.attrs['data_quality']['state'],'READY')
        daily=Desktop014._frame_uncached(ctx,self.info,'1D',required_identity=META)
        self.assertFalse(daily.bar_complete.any())
        self.assertNotEqual(daily.attrs['data_quality']['state'],'READY')

    def test_deeper_base_cannot_replace_fresher_valid_requested_aggregate(self):
        from unittest.mock import patch
        idx=pd.date_range('2026-10-27 07:30',periods=16,freq='15min')
        current=pd.DataFrame([[100,102,99,101,20]]*len(idx),columns=chart.FIELDS,index=idx)
        current.attrs=dict(META,source='gettex',symbol=ISIN)
        older=current.copy();older.index=older.index-pd.Timedelta(days=1)
        proposed=pd.concat([older,current.iloc[[0,-1]]]);proposed.attrs=dict(META,timeframe='15m',classify_absence=True)
        with patch.object(chart,'load',return_value=proposed):
            selected,base=chart.candidate(self.store,self.info,'4H',current,'15m',NOW)
        self.assertIs(selected,current);self.assertEqual(base,'15m')

    def test_rejected_raw_payload_preserved_without_database_candles(self):
        self.isolate_base();p=payload();p['data'][0]['HIGH']='80'
        r=chart.turn(self.info,self.store,NOW,FakeClient(p));self.assertEqual(r['state'],'ERROR')
        saved=self.store.setting(chart.state_key(ISIN,'15m'));self.assertTrue(Path(saved['rejected_receipt']).exists())
        self.assertEqual(json.loads(Path(saved['rejected_receipt']).read_text())['data'],p['data'])
        with self.store.connect() as db:self.assertEqual(db.execute('SELECT count(*) FROM series').fetchone()[0],0)


if __name__=='__main__':unittest.main()
