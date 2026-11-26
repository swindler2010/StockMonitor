import sys,tempfile,unittest,gzip,json
from pathlib import Path
from unittest.mock import patch
import pandas as pd
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
from storage import Store
class ClosedStore(Store):
    instances=[]
    def __init__(self,*a):
        super().__init__(*a);self.instances.append(self)
from app import StockMonitor
import gettex_history as gh
import history_service as hs
import data_quality as dq

class Universal(unittest.TestCase):
    def tearDown(self):
        for s in ClosedStore.instances:s.read_cache.close()
        ClosedStore.instances.clear()
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.s=ClosedStore(self.tmp.name);self.info={'isin':'DE0005557508','name':'Test','venue':'GETTEX','currency':'EUR'}
        self.s.save_instrument(self.info);self.parse=lambda *a,**k:StockMonitor._parse_gettex_csv(None,*a,**k)
        self.now=pd.Timestamp('2026-10-23T05:00Z')
    def files(self,day='2026-10-21',omit=False,bad=False):
        cache=Path(self.tmp.name)/'gettex_files';cache.mkdir(exist_ok=True)
        stamps=pd.date_range(day+' 05:45',day+' 21:00',freq='15min',tz='UTC')
        paths=[]
        for n,t in enumerate(stamps):
            for venue in ('munc','mund'):
                if omit and n==0:continue
                p=cache/f"posttrade.{t.strftime('%Y%m%d.%H.%M')}.{venue}.csv.gz"
                line='DE0007236101,'+t.strftime('%H:%M:%S')+',EUR,10,1\n'
                if n==0:line+=self.info['isin']+',05:30:00,EUR,10,1\n'
                if bad and n==0:line=self.info['isin']+',05:40:00,EUR,-1,1\n'
                p.write_bytes(gzip.compress(line.encode()));paths.append(p)
        return paths
    def frame(self):
        f=pd.DataFrame([[10,11,9,10,2],[11,12,10,11,2]],columns=dq.FIELDS,index=pd.to_datetime(['2026-10-21 07:30','2026-10-21 07:40']))
        f.attrs=dict(source='gettex',venue='GETTEX',currency='EUR',symbol=self.info['isin'],timezone='Europe/Berlin')
        return f
    def test_automatic_arbitrary_isin_and_restart_skip(self):
        self.files();r=gh.classify_cached_session(self.info,self.s,self.parse,self.now)
        self.assertEqual((r['state'],r['no_trades']),('DONE',185))
        with patch.object(StockMonitor,'_parse_gettex_csv',side_effect=AssertionError('repeat')):
            self.assertEqual(gh.classify_cached_session(self.info,ClosedStore(self.tmp.name),self.parse,self.now)['state'],'IDLE')
        proof=self.s.setting('interval_evidence:'+self.info['isin']);self.assertEqual(proof['identity']['currency'],'EUR')
    def test_incomplete_then_new_files_resume(self):
        self.files(omit=True);r=gh.classify_cached_session(self.info,self.s,self.parse,self.now)
        self.assertEqual(r['state'],'UNCONFIRMED');self.assertIsNone(self.s.setting('interval_evidence:'+self.info['isin']))
        self.assertEqual(gh.classify_cached_session(self.info,self.s,self.parse,self.now)['state'],'IDLE')
        self.files();self.assertEqual(gh.classify_cached_session(self.info,self.s,self.parse,self.now)['state'],'DONE')
    def test_malformed_trade_never_proves_absence(self):
        self.files(bad=True);self.assertEqual(gh.classify_cached_session(self.info,self.s,self.parse,self.now)['state'],'UNCONFIRMED')
        self.assertIsNone(self.s.setting('interval_evidence:'+self.info['isin']))
    def test_one_session_per_job(self):
        self.files('2026-10-21');self.files('2026-10-22')
        self.assertEqual(gh.classify_cached_session(self.info,self.s,self.parse,self.now)['date'],'20261021')
        self.assertEqual(gh.classify_cached_session(self.info,self.s,self.parse,self.now)['date'],'20261022')
    def test_identity_race_no_commit(self):
        self.files();changed={**self.info,'currency':'USD'};self.s.save_instrument(changed)
        with self.assertRaisesRegex(ValueError,'instrument changed'):gh.classify_cached_session(self.info,self.s,self.parse,self.now)
        self.assertIsNone(self.s.setting('interval_evidence:'+self.info['isin']))
    def test_new_instrument_unknown_not_ready(self):
        f=self.frame();dq.attach_interval_evidence(self.s,f,self.info['isin'])
        self.assertEqual(dq.audit(f,'5m',now=self.now,check_stale=False)['state'],'UNCONFIRMED')
        self.s.save_frame(self.info['isin'],'gettex','5m',f)
        self.assertEqual(hs.inspect_saved_archive(self.info,self.s,self.now)['quality'],'UNCONFIRMED')
    def test_archive_restart_and_new_bar_revision(self):
        f=self.frame();self.s.save_frame(self.info['isin'],'gettex','5m',f)
        self.assertEqual(hs.inspect_saved_archive(self.info,self.s,self.now)['state'],'AUDITED')
        with patch.object(dq,'audit',side_effect=AssertionError('repeat')):
            self.assertEqual(hs.inspect_saved_archive(self.info,ClosedStore(self.tmp.name),self.now)['state'],'DONE')
        f.loc[pd.Timestamp('2026-10-21 07:45')]=[12,13,11,12,2];self.s.save_frame(self.info['isin'],'gettex','5m',f)
        self.assertEqual(hs.inspect_saved_archive(self.info,self.s,self.now)['count'],3)
    def test_real_invalid_kept(self):
        f=self.frame();f.iloc[0,1]=1;self.s.save_frame(self.info['isin'],'gettex','5m',f)
        self.assertEqual(hs.inspect_saved_archive(self.info,self.s,self.now)['quality'],'INVALID')
    def test_archive_revision_race(self):
        f=self.frame();self.s.save_frame(self.info['isin'],'gettex','5m',f);original=dq.audit
        def audit(*a,**k):
            r=original(*a,**k);f.iloc[0,0]=9;self.s.save_frame(self.info['isin'],'gettex','5m',f);return r
        with patch.object(dq,'audit',side_effect=audit):
            with self.assertRaisesRegex(ValueError,'input changed'):hs.inspect_saved_archive(self.info,self.s,self.now)
        self.assertIsNone(self.s.setting('history_archive_progress:'+self.info['isin']))
    def test_published_missing_bar_repaired_by_existing_recovery(self):
        paths=self.files()
        for p in paths:
            if '.05.45.' in p.name:
                p.write_bytes(gzip.compress(('\n'.join(self.info['isin']+','+t+',EUR,10,2' for t in ('05:30:00','05:35:00','05:40:00'))+'\n').encode()))
        gh.classify_cached_session(self.info,self.s,self.parse,self.now)
        f=self.frame();self.s.save_frame(self.info['isin'],'gettex','5m',f)
        cached=self.s.load_official(self.info['isin'],'5m');meta=cached.attrs
        self.assertEqual(dq.audit(cached,'5m',now=self.now,check_stale=False)['state'],'GAP')
        calls=[]
        def fetch(symbol,interval,start,end=None):
            calls.append((start,end));replacement=f.iloc[:1].copy();replacement.index=pd.to_datetime(['2026-10-21 07:35']);replacement.attrs=meta;return replacement
        report=dq.repair(self.info,self.s,self.info['isin'],'5m',meta,fetch,self.now,max_ranges=1)
        self.assertEqual(len(calls),1);self.assertNotIn(report['state'],('GAP','INVALID','UNCONFIRMED','SOURCE_UNAVAILABLE'))
        self.assertEqual(len(self.s.load_official(self.info['isin'],'5m')),3)
    def test_shared_summary_and_new_registration(self):
        self.files();other={**self.info,'isin':'DE0007236101'};self.s.save_instrument(other)
        gh.classify_cached_session(self.info,self.s,self.parse,self.now)
        with patch.object(StockMonitor,'_parse_gettex_csv',side_effect=AssertionError('shared decode repeated')):
            self.assertEqual(gh.classify_cached_session(other,self.s,self.parse,self.now)['state'],'DONE')
        new={**self.info,'isin':'US0378331005'};self.s.save_instrument(new)
        # No published or stored GETTEX trades: never claim this new listing exists.
        self.assertEqual(gh.classify_cached_session(new,self.s,self.parse,self.now)['state'],'UNCONFIRMED')
    def test_changed_bad_files_withdraw_old_proof(self):
        self.files();gh.classify_cached_session(self.info,self.s,self.parse,self.now)
        self.files(bad=True);self.assertEqual(gh.classify_cached_session(self.info,self.s,self.parse,self.now)['state'],'UNCONFIRMED')
        self.assertEqual(self.s.setting('interval_evidence:'+self.info['isin'])['no_trades'],[])
    def test_priority_uses_existing_feed(self):
        from desktop014 import Desktop014
        from types import SimpleNamespace
        calls=[];items=[{'isin':str(i)} for i in range(53)]+[{'isin':'DE0007164600'},{'isin':'US0378331005'}]
        ctx=SimpleNamespace(watchlist=items,start_feed=lambda info,**kw:calls.append(info['isin']))
        Desktop014.refresh_all_async(ctx)
        self.assertEqual(calls[:2],['DE0007164600','US0378331005']);self.assertEqual(len(set(calls)),55)
    def test_recovery_revision_race_rejects_write(self):
        f=self.frame().iloc[:1].copy();f.iloc[0,1]=1;self.s.save_frame(self.info['isin'],'gettex','5m',f)
        meta=f.attrs
        def fetch(*a,**k):
            self.s.save_instrument({**self.info,'currency':'USD'})
            valid=f.iloc[:1].copy();valid.iloc[0,1]=11;return valid
        report=dq.repair(self.info,self.s,self.info['isin'],'5m',meta,fetch,self.now,max_ranges=1)
        self.assertIn('input changed',report.get('recovery_error',''))
        self.assertEqual(self.s.load_official(self.info['isin'],'5m').iloc[0]['high'],1)
    def test_unknown_empty_recovery_not_invalid_and_cooldown(self):
        f=self.frame();self.s.save_frame(self.info['isin'],'gettex','5m',f);f=self.s.load_official(self.info['isin'],'5m')
        calls=[]
        def empty(*a,**k):calls.append(1);return pd.DataFrame()
        r=dq.repair(self.info,self.s,self.info['isin'],'5m',f.attrs,empty,self.now)
        self.assertEqual(r['state'],'UNCONFIRMED');self.assertEqual(r['recovery_quality'],'SOURCE_UNAVAILABLE')
        dq.repair(self.info,self.s,self.info['isin'],'5m',f.attrs,empty,self.now)
        self.assertEqual(len(calls),1)
        self.s.set_setting(dq.key(self.info,self.info['isin'],'5m'),{**r,'state':'INVALID'})
        self.assertEqual(dq.repair(self.info,self.s,self.info['isin'],'5m',f.attrs,empty,self.now)['state'],'UNCONFIRMED')
        self.assertEqual(len(calls),1)
    def test_new_add_before_watchlist_save(self):
        self.files()
        with self.s.connect() as db:db.execute('DELETE FROM instruments WHERE isin=?',(self.info['isin'],))
        self.assertEqual(gh.classify_cached_session(self.info,self.s,self.parse,self.now)['state'],'DONE')
        self.assertEqual(self.s.instruments(),[])
    def test_archive_routes_one_confirmed_gap_to_existing_repair(self):
        f=self.frame();self.s.save_frame(self.info['isin'],'gettex','5m',f)
        self.s.set_setting('interval_evidence:'+self.info['isin'],dict(isin=self.info['isin'],identity=f.attrs,basis='complete_published_trade_files',checked_at=self.now.isoformat(),reference={'fixture':'validated'},no_trades=[],trade_intervals=[['2026-10-21 07:35','2026-10-21 07:40']]))
        calls=[]
        def recover(symbol,start,end,base):
            calls.append((symbol,base));fixed=f.iloc[:1].copy();fixed.index=pd.to_datetime(['2026-10-21 07:35']);return fixed
        r=hs.inspect_saved_archive(self.info,self.s,self.now,recover_gettex=recover)
        self.assertEqual(calls,[(self.info['isin'],'5m')]);self.assertEqual(r['recovery'],'STALE')
        r=hs.inspect_saved_archive(self.info,self.s,self.now,recover_gettex=recover)
        self.assertEqual(r['quality'],'READY');self.assertEqual(len(calls),1)


    def test_archive_changed_series_do_not_starve(self):
        f=self.frame()
        for base in ('5m','15m'):self.s.save_frame(self.info['isin'],'gettex',base,f)
        for _ in range(2):hs.inspect_saved_archive(self.info,self.s,self.now)
        visited=[]
        for n in range(4):
            # Both live series change every feed; a stable lexical winner must
            # not permanently suppress the other previously audited series.
            f['volume']=n+3
            for base in ('5m','15m'):self.s.save_frame(self.info['isin'],'gettex',base,f)
            visited.append(hs.inspect_saved_archive(self.info,self.s,self.now)['base'])
        self.assertEqual(visited,['15m','5m','15m','5m'])
    def test_archive_fairness_persists_and_unchanged_skip(self):
        f=self.frame()
        for base in ('5m','15m'):self.s.save_frame(self.info['isin'],'gettex',base,f)
        for _ in range(2):hs.inspect_saved_archive(self.info,self.s,self.now)
        for base in ('5m','15m'):
            f['volume']=5;self.s.save_frame(self.info['isin'],'gettex',base,f)
        self.assertEqual(hs.inspect_saved_archive(self.info,self.s,self.now)['base'],'15m')
        reopened=ClosedStore(self.tmp.name)
        f['volume']=6;reopened.save_frame(self.info['isin'],'gettex','15m',f)
        self.assertEqual(hs.inspect_saved_archive(self.info,reopened,self.now)['base'],'5m')
        hs.inspect_saved_archive(self.info,reopened,self.now)
        with patch.object(dq,'audit',side_effect=AssertionError('unchanged repeated')):
            self.assertEqual(hs.inspect_saved_archive(self.info,reopened,self.now)['state'],'DONE')


    def test_expired_proxy_recovery_no_requests_or_false_ready(self):
        f=self.frame();f.index=pd.to_datetime(['2026-08-21 09:00','2026-08-21 09:10'])
        f.attrs.update(source='proxy',venue='GER',symbol='TEST.DE')
        self.s.save_series(self.info,f,'5m')
        with patch.object(hs,'download',side_effect=AssertionError('expired network request')) as fetch:
            r=dq.repair(self.info,self.s,'TEST.DE','5m',f.attrs,hs.download,self.now)
            fetch.assert_not_called()
            self.assertEqual(r['state'],'UNCONFIRMED')
            self.assertEqual(r['recovery_constraints']['status'],'SOURCE_LIMITED')
            self.assertEqual(len(self.s.load_series(self.info['isin'],'5m','proxy','TEST.DE','GER','EUR')),2)
    def test_retention_does_not_apply_to_gettex(self):
        f=self.frame();f.index=pd.to_datetime(['2026-08-21 07:30','2026-08-21 07:40'])
        self.s.save_frame(self.info['isin'],'gettex','5m',f);calls=[]
        def fetch(*a,**k):calls.append(1);return pd.DataFrame()
        r=dq.repair(self.info,self.s,self.info['isin'],'5m',f.attrs,fetch,self.now)
        self.assertEqual(len(calls),1);self.assertEqual(r['state'],'UNCONFIRMED')
    def test_expired_proxy_invalid_stays_invalid(self):
        f=self.frame();f.index=pd.to_datetime(['2026-08-21 09:00','2026-08-21 09:05'])
        f.attrs.update(source='proxy',venue='GER',symbol='TEST.DE');f.iloc[0,1]=1
        self.s.save_series(self.info,f,'5m')
        with patch.object(hs,'download',side_effect=AssertionError('expired network request')) as fetch:
            r=dq.repair(self.info,self.s,'TEST.DE','5m',f.attrs,hs.download,self.now)
        fetch.assert_not_called()
        self.assertEqual(r['state'],'INVALID');self.assertEqual(r['recovery_constraints']['status'],'SOURCE_LIMITED')


    def test_retention_crossing_range_repairs_only_available_bars(self):
        f=self.frame();f.index=pd.to_datetime(['2026-08-24 09:00','2026-08-24 09:20'])
        f.attrs.update(source='proxy',venue='GER',symbol='TEST.DE');self.s.save_series(self.info,f,'5m')
        calls=[];now=pd.Timestamp('2026-10-23T07:05Z')
        def fetch(symbol,interval,start,end=None):
            calls.append((start,end))
            fixed=f.copy();fixed.index=pd.to_datetime(['2026-08-24 09:10','2026-08-24 09:15']);return fixed
        r=dq.repair(self.info,self.s,'TEST.DE','5m',f.attrs,fetch,now)
        self.assertEqual(len(calls),1);self.assertEqual(calls[0][0],pd.Timestamp('2026-08-24 09:10',tz='Europe/Berlin'))
        self.assertEqual(r['state'],'UNCONFIRMED');self.assertEqual(r['recovery_constraints']['excluded_ranges'],[['2026-08-24 09:05:00','2026-08-24 09:05:00']])
        stored=self.s.load_series(self.info['isin'],'5m','proxy','TEST.DE','GER','EUR')
        self.assertEqual(len(stored),4);self.assertNotIn(pd.Timestamp('2026-08-24 09:05'),stored.index)
    def test_retention_hourly_and_daily_keep_existing_depth(self):
        import history_backfill as hb
        self.assertIsNone(hb.intraday_retention_start('1D',self.now))
        self.assertEqual(self.now-hb.intraday_retention_start('1H',self.now),pd.Timedelta(days=730)-pd.Timedelta(minutes=1))
        for base in ('5m','15m','1H'):
            captured=[]
            def fetch(symbol,interval,start,end=None):captured.append(start);raise RuntimeError('stop before network')
            with self.assertRaisesRegex(RuntimeError,'stop before network'):
                hb._sync(self.info,self.s,'TEST.DE',base,fetch,self.now)
            self.assertEqual(captured,[hb.intraday_retention_start(base,self.now)])


    def test_shared_retention_successful_backfill_preserves_coverage(self):
        import history_backfill as hb
        for base,stamps,days in [('5m',['09:00','09:05'],60),('15m',['09:00','09:15'],60),('1H',['09:00','10:00'],730)]:
            f=self.frame();f.index=pd.to_datetime(['2026-10-21 '+x for x in stamps]).tz_localize('Europe/Berlin')
            f.attrs.update(source='proxy',venue='GER',symbol='TEST.DE')
            def fetch(*a,**k):return f.copy()
            r=hb._sync(self.info,self.s,'TEST.DE',base,fetch,self.now)
            self.assertIn(str(days),r['limitation']);self.assertEqual(r['count'],2)
            self.assertTrue(r['retention_limited']);self.assertEqual(r['currency'],'EUR')

if __name__=='__main__':unittest.main()
