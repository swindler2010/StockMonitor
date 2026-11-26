import copy, hashlib, json, pathlib, sys, tempfile, unittest
import pandas as pd
sys.path.insert(0, str(pathlib.Path(__file__).parent / 'stock_monitor'))
from storage import Store
import onvista_archive as ov
from performance_cache import InputRevisionChanged
import data_quality as quality
from analytics import resample

ROOT = pathlib.Path(__file__).parent / 'history64-evidence/onvista28'

class OnvistaArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(self.temp.name)
        self.addCleanup(self.store.read_cache.close)
        self.info = dict(isin='DE0005810055', name='DEUTSCHE BOERSE AG', source_mode='proxy')
        self.store.save_watchlist([self.info])
        self.csv = ROOT/'DE0005810055-gettex-1D-table.csv'
        self.meta = json.loads((ROOT/'manifest.json').read_text(encoding='utf-8'))
        self.now = '2026-10-28T20:41:54Z'

    def prepare(self, **kwargs):
        return ov.prepare(self.store, self.info, kwargs.get('path', self.csv), kwargs.get('meta', self.meta), kwargs.get('now', self.now))

    def changed_file(self, change):
        path = pathlib.Path(self.temp.name)/'modified.csv'
        path.write_text(change(self.csv.read_text(encoding='utf-8')), encoding='utf-8')
        meta = copy.deepcopy(self.meta); meta['sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
        meta['observed_rows'] = len(path.read_text(encoding='utf-8').splitlines())-1
        return path, meta

    def test_actual_export_roundtrip_idempotency_and_long_archive(self):
        old = pd.DataFrame([[10,11,9,10,100]], columns=quality.FIELDS, index=pd.to_datetime(['2001-02-05']))
        old.attrs=dict(source='proxy',venue='FRA',currency='EUR',symbol='DB1.F')
        self.store.save_series(self.info,old,'1D')
        plan=self.prepare();self.assertEqual(len(plan['frame']),210)
        self.assertEqual(plan['receipt']['excluded'][0]['count'],43)
        self.assertEqual(plan['receipt']['quality']['state'],'READY')
        self.assertEqual(ov.commit(self.store,plan)['inserted'],210)
        with self.store.connect() as db: before=db.execute('select * from input_revisions order by scope').fetchall()
        self.assertEqual(ov.commit(self.store,self.prepare())['state'],'UNCHANGED')
        with self.store.connect() as db: self.assertEqual(before,db.execute('select * from input_revisions order by scope').fetchall())
        reopened=Store(self.temp.name)
        self.addCleanup(reopened.read_cache.close)
        loaded=reopened.load_series(self.info['isin'],'1D','onvista','120479828','GETTEX','EUR')
        self.assertEqual(len(loaded),210)
        pd.testing.assert_frame_equal(loaded[list(quality.FIELDS)],plan['frame'],check_flags=False,check_names=False)
        self.assertEqual(len(reopened.load_series(self.info['isin'],'1D','proxy','DB1.F')),1)
        self.assertEqual(reopened.setting('onvista_archive:DE0005810055:120479828')['raw_rows'],253)

    def test_identity_fail_closed(self):
        for field,value in [('source','gettex_chart'),('venue','GER'),('currency','USD'),('isin','US0378331005'),('base_tf','5m'),('symbol','DB1'),('timezone','UTC')]:
            with self.subTest(field=field):
                meta={**self.meta,field:value}
                with self.assertRaisesRegex(ValueError,'IDENTITY'):self.prepare(meta=meta)

    def test_existing_cp_catalog_discovers_saved_source(self):
        from history_service import inspect_saved_archive
        ov.commit(self.store,self.prepare())
        result=inspect_saved_archive(self.info,self.store,now=pd.Timestamp(self.now))
        self.assertEqual(result['state'],'AUDITED')
        self.assertEqual(result['identity']['source'],'onvista')
        self.assertEqual(result['quality'],'READY')
        self.assertEqual(result['count'],210)
        self.assertEqual(inspect_saved_archive(self.info,self.store,now=pd.Timestamp(self.now))['state'],'DONE')

    def test_corrupt_ohlc_cannot_be_ready(self):
        path,meta=self.changed_file(lambda s:s.replace('02.01.2026;223,90 EUR;224,50 EUR','02.01.2026;223,90 EUR;1,00 EUR'))
        with self.assertRaises(quality.QualityError):self.prepare(path=path,meta=meta)
        with self.store.connect() as db:self.assertEqual(db.execute('select count(*) from series').fetchone()[0],0)

    def test_gap_and_stale_remain_visible(self):
        path,meta=self.changed_file(lambda s:'\n'.join(r for r in s.splitlines() if not r.startswith('05.01.2026;'))+'\n')
        plan=self.prepare(path=path,meta=meta)
        self.assertEqual(plan['receipt']['quality']['state'],'GAP')
        self.assertEqual(plan['receipt']['quality']['missing_count'],1)
        self.assertEqual(ov.commit(self.store,plan)['quality'],'GAP')
        self.assertEqual(self.prepare(now='2026-10-30T12:00:00Z')['receipt']['quality']['state'],'STALE')

    def test_forming_and_duplicate_rejected(self):
        with self.assertRaisesRegex(ValueError,'CLOSED_SESSION'):self.prepare(now='2026-10-27T19:00:00Z')
        path,meta=self.changed_file(lambda s:s+s.splitlines()[-1]+'\n')
        with self.assertRaisesRegex(ValueError,'DUPLICATE'):self.prepare(path=path,meta=meta)

    def test_revision_race_blocks_whole_transaction(self):
        plan=self.prepare()
        self.store.save_watchlist([{**self.info,'name':'changed'}])
        with self.assertRaises(InputRevisionChanged):ov.commit(self.store,plan)
        with self.store.connect() as db:self.assertEqual(db.execute('select count(*) from series').fetchone()[0],0)

    def test_conflict_never_overwrites_and_file_change_blocks(self):
        plan=self.prepare();ov.commit(self.store,plan)
        path,meta=self.changed_file(lambda s:s.replace('27.10.2026;313,80 EUR;315,50 EUR;310,80 EUR;312,90 EUR','27.10.2026;313,80 EUR;315,50 EUR;310,80 EUR;312,80 EUR'))
        with self.assertRaisesRegex(ValueError,'CONFLICT'):self.prepare(path=path,meta=meta)
        path,meta=self.changed_file(lambda s:s)
        plan=self.prepare(path=path,meta=meta);path.write_text('corrupt',encoding='utf-8')
        with self.assertRaisesRegex(ValueError,'EXPORT_CHANGED'):ov.commit(self.store,plan)

    def test_existing_aggregation_uses_only_imported_venue(self):
        plan=self.prepare();raw=plan['frame'];raw.attrs['data_quality']=plan['receipt']['quality']
        for tf in ('1W','1M'):
            with self.subTest(tf=tf):
                frame=resample(raw,'1D',tf,now=pd.Timestamp(self.now))
                self.assertGreater(len(frame),0)
                for key,value in raw.attrs.items():
                    if key in quality.IDENTITY:self.assertEqual(frame.attrs[key],value)
                self.assertLessEqual(frame.volume.sum(),raw.volume.sum())
                if 'bar_complete' not in frame:
                    # The first weekly bucket reaches unverified December 2025.
                    # Existing provenance must keep that calculation blocked.
                    self.assertEqual(frame.attrs['data_quality']['state'],'INVALID')
                else:
                    self.assertFalse(bool(frame.bar_complete.iloc[-1]))

if __name__=='__main__':unittest.main()
