"""Only the new saved-source binding, no provider/network or full regression."""
import copy, json, pathlib, sys, tempfile, unittest
from types import SimpleNamespace
from unittest.mock import patch
import pandas as pd
ROOT=pathlib.Path(__file__).parent
E=ROOT/'history64-evidence/onvista28'
sys.path.insert(0,str(ROOT/'stock_monitor'))
sys.path.insert(0,str(E/'candidate'))
from storage import Store
import onvista_archive as ov
import data_quality as dq
from eur_listings import cached_daily_history
from desktop014 import Desktop014

class BindingTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=Store(self.tmp.name);self.addCleanup(self.store.read_cache.close)
        self.listing=dict(status='CONFIRMED',isin='DE0005810055',source='proxy',symbol='DB1.F',venue='FRA',currency='EUR',timezone='Europe/Berlin')
        self.info=dict(isin='DE0005810055',name='DEUTSCHE BOERSE AG',source_mode='proxy',eur_listings=[self.listing])
        self.store.save_watchlist([self.info])
        self.now=pd.Timestamp('2026-10-28T20:41:54Z')
        manifest=json.loads((E/'manifest.json').read_text(encoding='utf-8'))
        ov.commit(self.store,ov.prepare(self.store,self.info,E/'DE0005810055-gettex-1D-table.csv',manifest,self.now))
        self.identity={k:manifest[k] for k in dq.IDENTITY}
        self.context=SimpleNamespace(store=self.store,analysis_at=self.now)
    def frame(self,tf='1D',pin=None):
        return Desktop014._frame_uncached(self.context,self.info,tf,pin)
    def test_real_daily_fallback_and_pinned_identity_no_writes(self):
        with self.store.connect() as db:before=db.execute('select * from input_revisions order by scope').fetchall()
        with patch('requests.get',side_effect=AssertionError('network forbidden')):
            raw=self.frame(); pinned=self.frame(pin=self.identity)
        self.assertEqual(len(raw),210);self.assertEqual(len(pinned),210)
        for key,value in self.identity.items():self.assertEqual(raw.attrs[key],value)
        self.assertEqual(raw.attrs['data_quality']['state'],'READY')
        with self.store.connect() as db:self.assertEqual(before,db.execute('select * from input_revisions order by scope').fetchall())
    def test_usable_deeper_existing_archive_is_not_replaced(self):
        idx=dq.expected_index({k:self.listing[k] for k in dq.IDENTITY},'1D',pd.Timestamp('2025-01-02'),pd.Timestamp('2026-10-27'),now=self.now)
        old=pd.DataFrame([[10,11,9,10,100]]*len(idx),index=idx,columns=dq.FIELDS)
        old.attrs={k:self.listing[k] for k in dq.IDENTITY};self.store.save_series(self.info,old,'1D')
        raw=cached_daily_history(self.store,self.info,'1D',{},self.now)
        self.assertGreater(len(raw),210);self.assertEqual(raw.attrs['symbol'],'DB1.F');self.assertEqual(raw.attrs['source'],'proxy')
    def test_import_proof_does_not_require_legacy_yahoo_listing(self):
        self.info.pop('eur_listings')
        raw=self.frame()
        self.assertEqual(len(raw),210);self.assertEqual(raw.attrs['source'],'onvista')
        key='onvista_archive:'+self.info['isin']+':'+self.identity['symbol']
        receipt=self.store.setting(key,{});receipt['listing_proof']['status']='UNCONFIRMED'
        self.store.set_setting(key,receipt)
        self.assertTrue(self.frame().empty)
    def test_wrong_pin_and_unconfirmed_receipt_fail_closed(self):
        for field,value in [('currency','USD'),('venue','GER'),('symbol','wrong')]:
            with self.subTest(field=field),self.assertRaisesRegex(ValueError,'Unconfirmed'):
                self.frame(pin={**self.identity,field:value})
        key='onvista_archive:'+self.info['isin']+':'+self.identity['symbol']
        receipt=self.store.setting(key,{})
        for field,value in [('status','UNCONFIRMED'),('isin','US0378331005')]:
            broken=copy.deepcopy(receipt);broken['listing_proof'][field]=value
            self.store.set_setting(key,broken)
            self.assertEqual(ov.available_daily(self.store,self.info,self.now),[])
        self.store.set_setting(key,receipt)
        broken=copy.deepcopy(receipt);broken['source_url']='https://example.com/aktien/historische-kurse/X-'+self.info['isin']
        self.store.set_setting(key,broken);self.assertEqual(ov.available_daily(self.store,self.info,self.now),[])
    def test_new_invalid_revision_and_stale_are_not_ready(self):
        self.assertEqual(len(self.frame()),210)
        with self.store.connect() as db:db.execute("UPDATE series SET high=1 WHERE source='onvista' AND ts=(SELECT min(ts) FROM series WHERE source='onvista')")
        self.assertEqual(ov.available_daily(self.store,self.info,self.now)[0].attrs['data_quality']['state'],'INVALID')
        self.assertIsNone(cached_daily_history(self.store,self.info,'1D',{},self.now))
        self.assertEqual(self.frame(pin=self.identity).attrs['data_quality']['state'],'INVALID')
    def test_stale_and_persisted_hard_block_survive(self):
        self.context.analysis_at=pd.Timestamp('2026-11-02T18:00:00Z')
        self.assertEqual(self.frame(pin=self.identity).attrs['data_quality']['state'],'STALE')
        self.store.set_setting(dq.key(self.info,self.identity['symbol'],'1D'),dict(state='SOURCE_UNAVAILABLE',identity=self.identity,issues=['saved provider failure']))
        self.assertIsNone(cached_daily_history(self.store,self.info,'1D',{},self.now))
        self.assertEqual(self.frame(pin=self.identity).attrs['data_quality']['state'],'SOURCE_UNAVAILABLE')
    def test_derived_calendar_provenance_stays_strict(self):
        week=self.frame('1W');month=self.frame('1M')
        self.assertFalse(week.empty);self.assertFalse(month.empty)
        self.assertEqual(week.attrs['source'],'onvista');self.assertEqual(month.attrs['currency'],'EUR')
        self.assertEqual(week.attrs['data_quality']['state'],'INVALID')
        self.assertFalse(bool(week.iloc[0].get('bar_complete',False)))
        self.assertFalse(bool(month.iloc[-1].get('bar_complete',False)))

if __name__=='__main__':unittest.main(verbosity=2)
