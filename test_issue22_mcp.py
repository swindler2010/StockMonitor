"""Issue 22: real readonly HTTP, pinned totals and independent chart delivery."""
import hashlib,json,queue,sqlite3,sys,tempfile,time,unittest
from contextlib import closing
from pathlib import Path
from urllib.request import Request,urlopen
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
from bridge import Bridge
from mcp_projection import instruments,read_summary
from mcp_pages import ReadPages
from mcp_routes import route

ARCHIVE=(Path(__file__).resolve().parents[2]/'СМ-Архив' if (Path(__file__).resolve().parents[2]/'СМ-Архив').is_dir() else Path(tempfile.gettempdir()))

class MappedDirectory:
    """Use small files in the owner's existing archive, not a new directory."""
    def __init__(self,db,descriptor):self.db=db;self.descriptor=descriptor
    def __truediv__(self,name):
        if name=='stock_monitor.sqlite3':return self.db
        if name=='bridge.json':return self.descriptor
        if name=='cp-diagnostic-baseline.sqlite3':return self.descriptor.with_suffix('.diagnostic-baseline.sqlite3')
        raise AssertionError('Unexpected test write: '+name)
    def mkdir(self,**kwargs):pass

class Tests(unittest.TestCase):
    def setUp(self):
        self.files=[]
        for suffix in ('.sqlite3','.json'):
            f=tempfile.NamedTemporaryFile(prefix='issue22-fixture-',suffix=suffix,dir=ARCHIVE,delete=False)
            self.files.append(Path(f.name));f.close()
        self.db,self.descriptor=self.files
        with closing(sqlite3.connect(self.db)) as d:
            d.executescript('''CREATE TABLE instruments(isin TEXT PRIMARY KEY,payload TEXT);
                CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT);
                CREATE TABLE input_revisions(scope TEXT PRIMARY KEY,revision INTEGER);
                CREATE TABLE series(isin TEXT,source TEXT,venue TEXT,currency TEXT,symbol TEXT,base_tf TEXT,ts TEXT,open REAL,high REAL,low REAL,close REAL,volume REAL,PRIMARY KEY(isin,source,venue,currency,symbol,base_tf,ts));
                CREATE TABLE candles(isin TEXT,source TEXT,base_tf TEXT,ts TEXT,open REAL,high REAL,low REAL,close REAL,volume REAL,PRIMARY KEY(isin,source,base_tf,ts));''')
            for isin,kind in [('US0378331005','stock'),('INDEX:DAX','benchmark')]:
                d.execute('INSERT INTO instruments VALUES (?,?)',(isin,json.dumps(dict(isin=isin,name=isin,kind=kind))))
            d.execute('INSERT INTO series VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',('US0378331005','proxy','GER','EUR','APC.DE','1H','2026-11-20 09:00:00',10,11,9,10,3))
            d.execute('INSERT INTO candles VALUES (?,?,?,?,?,?,?,?,?)',('US0378331005','gettex','15m','2026-11-20 09:00:00',10,11,9,10,3))
            d.execute('INSERT INTO input_revisions VALUES (?,?)',('instrument:US0378331005',1))
            # A literal instrument namespace must not scan/decode unrelated data.
            d.execute('INSERT INTO settings VALUES (?,?)',('data_quality:US0378331005:APC.DE:1H',json.dumps(dict(state='UNCONFIRMED',ranges=[['x','y']]))))
            d.execute('INSERT INTO settings VALUES (?,?)',('token','private-token-must-not-leak'))
            d.commit()
        self.bridge=Bridge(MappedDirectory(self.db,self.descriptor),queue.Queue(),lambda a,b:None)
        self.read_audit=[];self.bridge.audit=self.read_audit.append
        self.selected=dict(isin='US0378331005',timeframe='1H',period='6 месяцев')
        self.bridge.selection_changed(self.selected)

    def tearDown(self):
        self.bridge.commands.close();self.bridge.server.shutdown();self.bridge.server.server_close()
        for path in self.files:
            if path.exists():path.unlink()

    def request(self,action,args):
        request=Request(f'http://127.0.0.1:{self.bridge.server.server_port}/bridge',
            data=json.dumps(dict(action=action,arguments=args)).encode(),
            headers={'Authorization':'Bearer '+self.bridge.token,'Content-Type':'application/json'})
        with urlopen(request,timeout=5) as response:return json.load(response)

    def test_catalog_is_independent_of_pending_and_pinned_across_changes(self):
        result=self.request('list_instruments',dict(limit=1))
        self.assertEqual(result['instrument_total'],2)
        self.assertEqual(result['stock_total'],1)
        with closing(sqlite3.connect(self.db)) as d:
            d.execute('INSERT INTO instruments VALUES (?,?)',('NEW',json.dumps(dict(name='New'))));d.commit()
        continued=self.request('list_instruments',dict(limit=1,offset=1,expected_revision=result['revision']))
        self.assertEqual(continued['instrument_total'],2)
        fresh=self.request('list_instruments',dict(limit=1))
        self.assertEqual(fresh['instrument_total'],3)

    def test_indexed_namespace_preserves_exact_saved_quality(self):
        rows=instruments(self.db,{},True)
        apple=next(x for x in rows if x['isin']=='US0378331005')
        self.assertEqual(len(apple['saved_quality']),1)
        self.assertEqual(apple['stored_series'][0]['count'],1)
        with closing(sqlite3.connect(self.db)) as d:
            plan=d.execute('EXPLAIN QUERY PLAN SELECT key,value FROM settings WHERE key>=? AND key<?',('data_quality:US0378331005:','data_quality:US0378331005:\uffff')).fetchall()
        self.assertTrue(any('SEARCH' in r[3] and 'INDEX' in r[3] for r in plan))

    def test_summary_is_readonly_no_false_errors_or_trade_permission(self):
        before=hashlib.sha256(self.db.read_bytes()).hexdigest()
        result=read_summary(self.db)
        self.assertEqual(result['totals']['stored_rows_by_table'],dict(series=1,candles=1))
        self.assertEqual(result['totals']['raw_dq_series'],1)
        self.assertIsNone(result['totals']['unresolved_errors'])
        self.assertEqual(result['totals']['owner_accepted_risk'],0)
        self.assertFalse(result['execution_permission'])
        self.assertNotIn('private-token',json.dumps(result))
        self.assertEqual(hashlib.sha256(self.db.read_bytes()).hexdigest(),before)

    def test_summary_first_call_is_bounded_then_all_stocks_share_revision(self):
        first=self.request('snapshot',dict(path='/@summary'))
        self.assertEqual(first['status'],'INDETERMINATE')
        end=time.monotonic()+3
        while time.monotonic()<end:
            result=self.request('snapshot',dict(path='/@summary',limit=20))
            if result['status']=='AVAILABLE':break
            time.sleep(.02)
        self.assertEqual(result['status'],'AVAILABLE')
        page=self.request('snapshot',dict(path='/@summary/by_stock',limit=50,expected_revision=result['revision']))
        self.assertEqual(page['total_count'],2)
        self.assertEqual(page['revision'],result['revision'])
        self.assertTrue(page['complete'])

    def test_selected_analysis_publishes_without_finishing_global_job(self):
        self.bridge.diagnostic_event('RUNNING')
        self.bridge.publish_selected_analysis(self.selected,dict(status='READY',signal='WAIT'),{'instrument:US0378331005':1})
        value,_=self.bridge.snapshot_state()
        self.assertTrue(value['ready']);self.assertEqual(value['analytics_status'],'COMPLETED')
        self.assertEqual(self.bridge.diagnostic_pipeline['status'],'RUNNING')
        self.assertFalse(value['execution_permission'])

    def test_selected_analysis_revoked_on_relevant_revision(self):
        self.bridge.publish_selected_analysis(self.selected,dict(status='READY'),{'instrument:US0378331005':1})
        with closing(sqlite3.connect(self.db)) as d:
            d.execute('UPDATE input_revisions SET revision=2');d.commit()
        value,_=self.bridge.snapshot_state()
        self.assertFalse(value['ready']);self.assertEqual(value['selected_analysis_status'],'STALE')

    def test_ten_selected_recalculations_keep_current_publication(self):
        self.bridge.diagnostic_event('RUNNING')
        for turn in range(10):
            with closing(sqlite3.connect(self.db)) as d:
                d.execute('UPDATE input_revisions SET revision=?',(turn+1,));d.commit()
            self.bridge.publish_selected_analysis(self.selected,dict(status='READY',signal='WAIT'),{'instrument:US0378331005':turn+1})
            result=self.request('snapshot',dict(limit=50))
            self.assertTrue(result['ready']);self.assertTrue(result['is_latest_publication'])
            values={r['key']:r.get('value') for r in result['items'] if r['expanded']}
            self.assertFalse(values['execution_permission'])
        self.assertEqual(self.bridge.diagnostic_pipeline['attempts'],1)

    def test_selection_does_not_relabel_previous_analysis(self):
        self.bridge.publish_selected_analysis(self.selected,dict(status='READY'),{'instrument:US0378331005':1})
        self.bridge.selection_changed({**self.selected,'timeframe':'4H'})
        value,_=self.bridge.snapshot_state()
        self.assertFalse(value['ready']);self.assertNotIn('selected_analysis',value)

    def test_expired_lease_never_silently_switches_revision(self):
        clock=[0.];pages=ReadPages(ttl=1,clock=lambda:clock[0])
        _,revision,_,_=pages.read('test',None,lambda:({'value':1},{}))
        clock[0]=2.
        with self.assertRaisesRegex(ValueError,'PAGE_SESSION_EXPIRED'):
            pages.read('test',revision,lambda:({'value':2},{}))

    def test_raw_archive_auxiliary_minutes_are_independent_of_readiness(self):
        with closing(sqlite3.connect(self.db)) as d:
            d.executemany('INSERT INTO series VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',[
                ('US0378331005','proxy','GER','EUR','APC.DE','1m',f'2026-11-20 09:{n:02}:00',-1,None,9,10,0) for n in range(3)])
            d.commit()
        path='/@archive/series/US0378331005/proxy/GER/EUR/APC.DE/1m'
        first=self.request('snapshot',dict(path=path,limit=2))
        self.assertEqual(first['total_count'],3);self.assertEqual(first['next_offset'],2)
        self.assertFalse(first['readiness_required']);self.assertFalse(first['market_open_required'])
        self.assertFalse(first['execution_permission']);self.assertEqual(first['bars'][0]['open'],-1)
        self.assertIsNone(first['bars'][0]['high'])
        last=self.request('snapshot',dict(path=path,limit=2,offset=2,expected_revision=first['revision']))
        self.assertEqual(len(last['bars']),1);self.assertIsNone(last['next_offset'])
        self.assertEqual(last['revision'],first['revision'])
        with closing(sqlite3.connect(self.db)) as d:
            d.execute('UPDATE input_revisions SET revision=revision+1');d.commit()
        with self.assertRaises(Exception):self.request('snapshot',dict(path=path,offset=2,expected_revision=first['revision']))

    def test_raw_archive_legacy_source_and_pointer_escaping(self):
        with closing(sqlite3.connect(self.db)) as d:
            d.execute('INSERT INTO candles VALUES (?,?,?,?,?,?,?,?,?)',('US0378331005','old/feed','1m','2020-01-01 09:00:00',1,2,0,1,0));d.commit()
        result=self.request('snapshot',dict(path='/@archive/candles/US0378331005/old~1feed/1m'))
        self.assertEqual(result['total_count'],1)
        self.assertEqual(result['identity'],dict(storage_table='candles',isin='US0378331005',source='old/feed',timeframe='1m'))
        self.assertNotIn('currency',result['identity'])

    def test_archive_catalog_includes_removed_instruments_and_auxiliary_tf(self):
        with closing(sqlite3.connect(self.db)) as d:
            d.execute('INSERT INTO series VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',('REMOVED','old','GER','EUR','REMOVED','1m','2010-01-01',1,2,0,1,3));d.commit()
        result=self.request('snapshot',dict(path='/@archives',limit=50))
        self.assertEqual(result['stored_rows_by_table'],dict(series=2,candles=1))
        self.assertFalse(result['registered_instrument_required'])
        for item in result['items']:
            detail=self.request('snapshot',dict(path=item['path'],expected_revision=result['revision'],limit=20))
            value={r['key']:r.get('value') for r in detail['items']}
            if value['isin']=='REMOVED':
                self.assertFalse(value['registered_instrument'])
                raw=self.request('snapshot',dict(path=value['read_path']))
                self.assertEqual(raw['total_count'],1);self.assertEqual(raw['bars'][0]['at'],'2010-01-01')

    def test_raw_archive_has_exact_key_allowlist(self):
        for path in ('/@archive/settings/secret','/@archive/series/US0378331005/proxy/GER/EUR/APC.DE/1m/extra','/@archive/candles/US0378331005//1m'):
            with self.assertRaises(ValueError):route('snapshot',dict(path=path))

    def test_summary_keeps_independent_analysis_and_reports_only_comparable_delta(self):
        from mcp_observability import baseline
        state={'selected':self.selected,'selected_analysis':{'status':'READY','signal':'WAIT'},'selected_analysis_status':'CURRENT','selected_analysis_completed_at':1}
        before=read_summary(self.db,state)
        self.assertEqual(before['published_analysis'][0]['engine_status'],'READY')
        self.assertEqual(before['by_stock'][1]['independent_analysis']['status'],'READY')
        with closing(sqlite3.connect(self.db)) as d:
            d.execute('INSERT INTO candles VALUES (?,?,?,?,?,?,?,?,?)',('US0378331005','gettex','15m','2026-11-20 09:15:00',10,11,9,10,3));d.commit()
        after=read_summary(self.db,state,baseline(before))
        self.assertEqual(after['short_report']['metrics']['stored_physical_rows']['delta'],1)
        self.assertTrue(after['short_report']['baseline_comparable'])
        self.assertEqual(len(after['short_report']['changed_stocks']),1)
        self.assertLessEqual(len(after['report'].split()),350)
        self.assertNotIn('TV',after['report'])

    def test_exact_visual_evidence_matches_identity_interval_and_current_fingerprint(self):
        from mcp_observability import exact_reconciliation
        import cp_reconciliation as rv
        import pandas as pd
        meta=dict(source='gettex_chart',venue='GETTEX',symbol='AAPL.GTX',currency='EUR',timezone='Europe/Berlin')
        q=dict(state='GAP',identity=meta,base_tf='15m',ranges=[['2026-11-20 09:00:00','2026-11-20 09:00:00']])
        job=dict(isin='US0378331005',identity=meta,tf='15m',first='2026-11-20 09:00:00',end='2026-11-20 09:15:00',observation=dict(result='ABSENT',identity_confirmed=True,input_fingerprint='current'))
        with closing(sqlite3.connect(self.db)) as d:
            d.execute('INSERT INTO settings VALUES (?,?)',(rv.PREFIX+'US0378331005:test',json.dumps(job)));d.commit()
            with patch.object(rv,'_input',return_value={'fingerprint':'current'}):
                good=exact_reconciliation(d,dict(isin='US0378331005'),{'data_quality:US0378331005:AAPL.GTX:15m':q},pd.Timestamp.now(tz='UTC'),time.monotonic()+5)
            self.assertEqual(good['internal_excluded_scope_count'],1);self.assertEqual(good['unverified_saved_scopes'],0)
            with patch.object(rv,'_input',return_value={'fingerprint':'changed'}):
                stale=exact_reconciliation(d,dict(isin='US0378331005'),{'x:15m':q},pd.Timestamp.now(tz='UTC'),time.monotonic()+5)
            self.assertEqual(stale['internal_excluded_scope_count'],0);self.assertEqual(stale['unverified_saved_scopes'],1)
            q['identity']={**meta,'currency':'USD'}
            with patch.object(rv,'_input',return_value={'fingerprint':'current'}):
                foreign=exact_reconciliation(d,dict(isin='US0378331005'),{'x:15m':q},pd.Timestamp.now(tz='UTC'),time.monotonic()+5)
            self.assertEqual(foreign['internal_excluded_scope_count'],0)

    def test_direct_reads_are_audited_and_bounded(self):
        from urllib.error import HTTPError
        from mcp_read_access import ReadAccess
        self.request('snapshot',dict(path='/@archives',limit=1))
        event=self.read_audit[-1]
        self.assertEqual(event['action'],'archive_catalog')
        self.assertNotIn('arguments',event);self.assertNotIn(self.bridge.token,json.dumps(event))
        self.bridge.read_access=ReadAccess(concurrency=1)
        self.assertTrue(self.bridge.read_access.acquire())
        with self.assertRaises(HTTPError) as caught:self.request('health',{})
        self.assertEqual(caught.exception.code,429)
        self.bridge.read_access.release()
        self.assertEqual(self.request('health',{})['bridge'],'running')

    def test_stuck_diagnostic_reports_timeout_without_starting_another(self):
        clock=[0.];pages=ReadPages(clock=lambda:clock[0])
        self.assertEqual(pages.begin_deferred('health')[0],'START')
        clock[0]=61.
        self.assertEqual(pages.begin_deferred('health')[0],'TIMED_OUT')
        self.assertEqual(pages.begin_deferred('health')[0],'TIMED_OUT')
        pages.finish_deferred('health',lambda:({'value':1},{}))
        self.assertEqual(pages.begin_deferred('health')[0],'READY')

    def test_interrupted_diagnostic_is_unavailable_not_zero(self):
        pages=ReadPages();pages.begin_deferred('health')
        def interrupted():raise sqlite3.OperationalError('interrupted')
        pages.finish_deferred('health',interrupted)
        _,revision=pages.begin_deferred('health')
        result,_,_,_=pages.read('health',revision,lambda:({},{}))
        self.assertEqual(result['status'],'UNAVAILABLE')
        self.assertEqual(result['reason_code'],'READ_DIAGNOSTIC_TIMEOUT')
        self.assertNotIn('count',result)

    def test_frozen_evidence_memo_matches_native_for_53_and_200_jobs(self):
        import cp_reconciliation as rv
        import pandas as pd
        with closing(sqlite3.connect(self.db)) as d:
            slots=[[str(pd.Timestamp('2026-11-20')+pd.Timedelta(minutes=5*n)),str(pd.Timestamp('2026-11-20')+pd.Timedelta(minutes=5*(n+1)))] for n in range(300)]
            d.execute('INSERT INTO settings VALUES (?,?)',('interval_evidence:US0378331005',json.dumps({'no_trades':slots})));d.commit();d.execute('BEGIN')
            memo={}
            with patch('eur_listings.stored_identity',return_value={'confirmed':True}):
                for n in range(200):
                    a,b=slots[n];job=dict(isin='US0378331005',identity=dict(source='proxy',venue='GER',currency='EUR',symbol='APC.DE'),tf='1H',first=a,interval_start=a,last=a,end=b,id=str(n))
                    native=rv._input(d,job);cached=rv._input(d,job,_memo=memo)
                    self.assertEqual(cached,native)
                    if n==52:self.assertIn(('parsed_intervals','US0378331005','no_trades'),memo)
            d.rollback()
            with self.assertRaisesRegex(ValueError,'READ_MEMO_REQUIRES_TRANSACTION'):rv._input(d,job,_memo=memo)

    def test_readonly_compute_timeout_stops_only_owned_reader(self):
        import subprocess
        from compute_process import Client
        child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        client=Client(self.db);client.timeouts['eligibility']=.05
        try:
            with patch('compute_process.subprocess.Popen',return_value=child):
                with self.assertRaisesRegex(RuntimeError,'READ_CALCULATION_TIMEOUT:eligibility'):client.calculate('eligibility',{})
            self.assertIsNone(client.process);self.assertIsNotNone(child.poll())
        finally:client.close()

    def test_framed_compute_reply_handles_partial_packet(self):
        import subprocess
        from compute_process import receive_readonly
        code="import sys,pickle,time;p=pickle.dumps((True,{'value':42}));sys.stdout.buffer.write(len(p).to_bytes(8,'big'));sys.stdout.buffer.flush();time.sleep(.05);sys.stdout.buffer.write(p);sys.stdout.buffer.flush()"
        child=subprocess.Popen([sys.executable,'-c',code],stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        try:self.assertEqual(receive_readonly(child,time.monotonic()+5),(True,{'value':42}))
        finally:child.wait(timeout=5);child.stdout.close()

    def test_writer_keeps_original_protocol_without_readonly_timeout(self):
        import subprocess
        from compute_process import Client
        code="import sys,pickle,time;pickle.load(sys.stdin.buffer);time.sleep(.05);pickle.dump((True,{'writer':True}),sys.stdout.buffer);sys.stdout.buffer.flush()"
        child=subprocess.Popen([sys.executable,'-c',code],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        client=Client(self.db,feed_updates=True);client.timeouts['feed_update']=.001
        try:
            with patch('compute_process.subprocess.Popen',return_value=child):self.assertEqual(client.calculate('feed_update',{}),{'writer':True})
        finally:
            client.close();child.wait(timeout=5)
            if not child.stdin.closed:child.stdin.close()
            if not child.stdout.closed:child.stdout.close()

    def test_frozen_view_is_coherent_and_cannot_write_or_drop_transaction(self):
        from storage import Store
        from performance_cache import ReadCache,frozen_store
        from contextlib import contextmanager
        class Reader(Store):
            def __init__(reader,path):reader.path=path;reader.read_cache=ReadCache(path)
        with closing(sqlite3.connect(self.db)) as d:d.execute('PRAGMA journal_mode=WAL')
        live=Reader(self.db)
        try:
            with frozen_store(live) as frozen:
                self.assertEqual(frozen.path,self.db)
                with frozen.connect() as d:
                    d.execute('BEGIN');old=d.execute('SELECT revision FROM input_revisions').fetchone()[0]
                with closing(sqlite3.connect(self.db)) as writer:
                    writer.execute('UPDATE input_revisions SET revision=revision+1');writer.commit()
                self.assertEqual(frozen.read_cache.token_for(['instrument:US0378331005'])['instrument:US0378331005'],old)
                for sql in ('UPDATE input_revisions SET revision=99','COMMIT','ROLLBACK','PRAGMA query_only=OFF'):
                    with self.assertRaises(sqlite3.DatabaseError):d.execute(sql)
                self.assertTrue(d.in_transaction)
            self.assertEqual(live.read_cache.token_for(['instrument:US0378331005'])['instrument:US0378331005'],old+1)
        finally:live.read_cache.close()

    def test_official_interval_index_matches_original_order_for_53_and_200_scopes(self):
        import data_quality as dq
        import pandas as pd
        for size in (53,200):
            base=pd.Timestamp('2026-11-20');scopes=[dict(first=str(base+pd.Timedelta(minutes=n)),end=str(base+pd.Timedelta(minutes=n+3)),authority={}) for n in range(size)]
            meta={'official_retiming_empty':scopes};index=dq._evidence_lookup(meta)
            for n in range(size+3):
                a=base+pd.Timedelta(minutes=n);b=a+pd.Timedelta(minutes=1)
                expected=[s for s in scopes if pd.Timestamp(s['first'])<=a<b<=pd.Timestamp(s['end'])]
                self.assertEqual(index.official_candidates(a,b),expected)
            scopes[0]['end']=str(base+pd.Timedelta(days=1))
            fresh=dq._evidence_lookup(meta)
            self.assertIn(scopes[0],fresh.official_candidates(base+pd.Timedelta(hours=3),base+pd.Timedelta(hours=3,minutes=1)))

    def test_official_window_memo_keeps_full_proof_and_rejects_changed_view(self):
        import revision_watcher as rw
        import pandas as pd
        isin='US0378331005';first='2026-11-20 09:00:00';end='2026-11-20 09:15:00'
        meta=dict(source='gettex',venue='GETTEX',currency='EUR',symbol=isin,timezone='Europe/Berlin')
        proof=dict(isin=isin,identity=meta,basis='complete_published_trade_files',checked_at='2026-11-21T10:00Z',no_trades=[['2026-11-20 09:05:00',end]],trade_intervals=[[first,'2026-11-20 09:05:00']],zero_quantity_intervals=[],reference={'GETTEX:20261120':{f'posttrade.20261120.08.15.{board}.csv.gz':'a'*64 for board in ('munc','mund')}})
        with closing(sqlite3.connect(self.db)) as d:
            d.execute('PRAGMA journal_mode=WAL');d.execute('CREATE TABLE trades(isin TEXT,ts TEXT,price REAL,volume REAL)')
            d.execute('DELETE FROM candles');d.execute('INSERT INTO trades VALUES (?,?,?,?)',(isin,'2026-11-20 09:01:00',10,3))
            for base in ('5m','15m'):d.execute('INSERT INTO candles VALUES (?,?,?,?,?,?,?,?,?)',(isin,'gettex',base,first,10,10,10,10,3))
            d.execute('INSERT INTO settings VALUES (?,?)',('interval_evidence:'+isin,json.dumps(proof)));d.commit();d.execute('PRAGMA query_only=ON');d.execute('BEGIN')
            now=pd.Timestamp('2026-11-23T07:00Z');native=rw._official_window(d,isin,first,end,now);memo={}
            for count in (53,200):
                for _ in range(count):self.assertEqual(rw._official_window(d,isin,first,end,now,_memo=memo),native)
            d.rollback()
            with closing(sqlite3.connect(self.db)) as writer:
                writer.execute("UPDATE candles SET close=99 WHERE base_tf='5m'");writer.commit()
            d.execute('BEGIN')
            with self.assertRaisesRegex(ValueError,'READ_MEMO_VIEW_CHANGED'):rw._official_window(d,isin,first,end,now,_memo=memo)
            with self.assertRaisesRegex(ValueError,'CANDLES_TRADE_MISMATCH'):rw._official_window(d,isin,first,end,now,_memo={})

    def test_factual_depth_bounds_match_full_ohlcv_aggregation(self):
        import pandas as pd
        from gettex_first import bind
        from timeframes import factual_closed_bounds
        from analytics import resample,bar_ends
        meta=dict(source='gettex',symbol='US0378331005',venue='GETTEX',currency='EUR',timezone='Europe/Berlin')
        # Include off-session observations, leading partial periods, holes,
        # and the European DST boundary. Oracle is the unchanged full kernel.
        index=pd.date_range('2026-10-23 06:45','2026-10-26 23:15',freq='15min').delete([7,9,18,21])
        frame=pd.DataFrame(dict(open=10.,high=11.,low=9.,close=10.,volume=1.,source='gettex'),index=index)
        frame.attrs=meta;frame=bind(frame,{'isin':meta['symbol']})
        for tf in ('15m','30m','1H','2H','3H','4H'):
            for now in ('2026-10-26T07:20Z','2026-10-26T10:30Z','2026-10-27T08:00Z'):
                with self.subTest(tf=tf,now=now):
                    data=resample(frame,'15m',tf,now=pd.Timestamp(now));ends=bar_ends(data);closed=data.loc[ends<=pd.Timestamp(now)]
                    expected=None if closed.empty else (closed.index.min(),ends[ends<=pd.Timestamp(now)].max())
                    self.assertEqual(factual_closed_bounds(frame,'15m',tf,pd.Timestamp(now)),expected)

    def test_compute_progress_53_and_200_packets_retains_one_final_reply(self):
        import subprocess
        from compute_process import Client
        for count in (53,200):
            code="import sys,pickle;q=pickle.load(sys.stdin.buffer);assert q[2].get('_read_progress_protocol')==1\nfor n in range("+str(count)+"):\n p=pickle.dumps(('PROGRESS',{'completed':n+1}));sys.stdout.buffer.write(len(p).to_bytes(8,'big')+p);sys.stdout.buffer.flush()\np=pickle.dumps((True,{'complete':True}));sys.stdout.buffer.write(len(p).to_bytes(8,'big')+p);sys.stdout.buffer.flush()"
            child=subprocess.Popen([sys.executable,'-c',code],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0));client=Client(self.db);seen=[]
            try:
                with patch('compute_process.subprocess.Popen',return_value=child):result=client.calculate('snapshot',{},progress=seen.append)
                self.assertEqual(len(seen),count);self.assertEqual(result,{'complete':True})
            finally:
                client.close();child.wait(timeout=5)
                if not child.stdin.closed:child.stdin.close()
                if not child.stdout.closed:child.stdout.close()

    def test_shared_53_200_snapshot_values_keep_every_original_pointer(self):
        from mcp_projection import page
        from mcp_shared_values import ReferenceTree
        import copy
        for count in (53,200):
            shared=dict(confirmation='verified',identity=dict(source='GETTEX',currency='EUR'),values=list(range(200)),text='a'*5000)
            source=dict(ready=True,stocks=[dict(isin='S'+str(n),metadata=shared) for n in range(count)],literal=dict(_read_reference='/not-a-real-reference'))
            original=copy.deepcopy(source);pages=ReadPages(max_bytes=256000)
            tree,revision,_,_=pages.read('snapshot',None,lambda:(source,{}))
            self.assertIsInstance(tree,ReferenceTree)
            self.assertEqual(tree.get('ready'),True)
            self.assertEqual(tree.resolve('/literal'),original['literal'])
            for n in range(count):
                result=page(tree,dict(path='/stocks/'+str(n)+'/metadata/values',expected_revision=revision,limit=50),revision)
                self.assertEqual(result['total_count'],200)
                self.assertEqual([x['value'] for x in result['items']],list(range(50)))
                self.assertEqual(tree.resolve('/stocks/'+str(n)+'/metadata/identity/currency'),'EUR')
            shared['identity']['currency']='USD'
            self.assertEqual(tree.resolve('/stocks/0/metadata/identity/currency'),'EUR')
            self.assertTrue(all(tree.resolve(alias) is tree.resolve(target) for alias,target in tree.references.items()))
            with self.assertRaisesRegex(ValueError,'STALE_PAGE'):page(tree,dict(path='/stocks',expected_revision='other'),revision)
        cyclic={};cyclic['self']=cyclic
        with self.assertRaisesRegex(ValueError,'PAGE_CIRCULAR_VALUE'):ReadPages().read('snapshot',None,lambda:(cyclic,{}))
        with self.assertRaisesRegex(ValueError,'PAGE_SESSION_TOO_LARGE'):ReadPages(max_bytes=100).read('snapshot',None,lambda:(dict(unique='x'*200),{}))

    def test_complete_53_200_stock_tf_rows_fit_bounded_small_pages(self):
        from mcp_projection import page
        for count in (53,200):
            rows=[dict(isin='S'+str(n),name='x'*1400,saved_all_tf=dict(timeframes={tf:dict(validity='UNKNOWN',execution_permission=False) for tf in ('5m','10m','15m','30m','1H','2H','3H','4H','1D','1W','1M')})) for n in range(count)]
            value=dict(by_stock=rows);seen=[];offset=0
            while True:
                result=page(value,dict(path='/by_stock',offset=offset,limit=8,expected_revision='fixed'),'fixed')
                self.assertLess(len(json.dumps(result,ensure_ascii=False).encode()),65000)
                self.assertTrue(all(item['expanded'] for item in result['items']))
                seen.extend(item['value'] for item in result['items'])
                if result['next_offset'] is None:break
                offset=result['next_offset']
            self.assertEqual(seen,rows)
            self.assertEqual(sum(len(row['saved_all_tf']['timeframes']) for row in seen),count*11)
        big=dict(by_stock=[dict(isin='large',detail='x'*70000)])
        self.assertFalse(page(big,dict(path='/by_stock',limit=1),'fixed')['items'][0]['expanded'])

    def test_summary_reconciliation_53_200_preserves_exact_scope_decisions(self):
        import pandas as pd
        import cp_controls as controls,cp_reconciliation as rv
        from mcp_observability import exact_reconciliation
        isin='US0378331005';meta=dict(source='gettex_chart',symbol='AAPL.GTX',venue='GETTEX',currency='EUR',timezone='Europe/Berlin');report_key='data_quality:'+isin+':AAPL.GTX:15m'
        for count in (53,200):
            spans=[[str(pd.Timestamp('2026-11-20 09:00')+pd.Timedelta(minutes=n*30)),str(pd.Timestamp('2026-11-20 09:00')+pd.Timedelta(minutes=n*30))] for n in range(count)]
            q=dict(state='UNCONFIRMED',identity=meta,base_tf='15m',ranges=spans,issues=['unknown'])
            accepted={}
            for span,actor in zip(spans,(controls.TV_ACTOR,'owner_local_ui')):
                item=dict(isin=isin,kind='archive',identity=meta,tf='15m',state='UNCONFIRMED',reasons=['unknown'],range=span,report_key=report_key)
                accepted[controls.digest({k:v for k,v in item.items() if k!='reasons'})]=dict(actor=actor)
            job=dict(identity=meta,tf='15m',first=spans[2][0],end=str(pd.Timestamp(spans[2][1])+pd.Timedelta(minutes=15)),observation=dict(identity_confirmed=True,result='ABSENT'))
            with closing(sqlite3.connect(self.db)) as d:
                d.execute('INSERT OR REPLACE INTO settings VALUES (?,?)',(rv.PREFIX+isin+':oracle',json.dumps(job)))
                for grants in ({},accepted):
                    with patch('cp_controls.effective_accepted',return_value=grants),patch.object(rv,'_input',return_value={}),patch.object(rv,'_observation_current',return_value=True):
                        args=(d,dict(isin=isin),{report_key:q},pd.Timestamp.now(tz='UTC'),time.monotonic()+30)
                        actual=exact_reconciliation(*args)
                    self.assertEqual(actual['owner_accepted_risk'],1 if grants else 0)
                    self.assertEqual(actual['internal_excluded_scope_count'],2 if grants else 1)
                    self.assertEqual(actual['unverified_saved_scopes'],count-(3 if grants else 1))
                    self.assertIsNone(actual['unresolved_errors'])

    def test_server_progress_requires_explicit_client_opt_in(self):
        import io,pickle
        from types import SimpleNamespace
        from compute_process import serve
        def calculate(store,values,last_client,progress=None):
            self.assertNotIn('_read_progress_protocol',values)
            if progress:progress(dict(completed=1,total=1))
            return dict(ready=True)
        for enabled in (False,True):
            values={'_read_progress_protocol':1} if enabled else {}
            incoming=io.BytesIO(pickle.dumps((str(self.db),'snapshot',values,None)));outgoing=io.BytesIO()
            with patch('sys.stdin',SimpleNamespace(buffer=incoming)),patch('sys.stdout',SimpleNamespace(buffer=outgoing)),patch('snapshot_worker.calculate',side_effect=calculate),patch('snapshot_worker.validate_snapshot',side_effect=lambda store,state:state):
                serve()
            outgoing.seek(0);packets=[]
            while header:=outgoing.read(8):packets.append(pickle.loads(outgoing.read(int.from_bytes(header,'big'))))
            self.assertEqual(len(packets),2 if enabled else 1)
            self.assertIs(packets[-1][0],True)
            if enabled:self.assertEqual(packets[0][0],'PROGRESS')

    def test_progress_is_metadata_only_and_cannot_revive_failed_cycle(self):
        self.bridge.diagnostic_event('RUNNING')
        start=time.perf_counter()
        for count in (53,200):
            for n in range(count):self.bridge.snapshot_progress(dict(completed=n+1,total=count,phase='CALCULATED',unsafe='must-not-copy'))
        self.assertLess(time.perf_counter()-start,.25)
        self.assertEqual(self.bridge.diagnostic_pipeline['attempts'],1)
        self.assertNotIn('unsafe',self.bridge.diagnostic_pipeline['progress'])
        self.bridge.diagnostic_event('FAILED',cause='test');before=dict(self.bridge.diagnostic_pipeline)
        self.bridge.snapshot_progress(dict(completed=999));self.assertEqual(self.bridge.diagnostic_pipeline,before)

    def test_closed_evidence_ignores_later_files_but_rejects_used_facts(self):
        import copy
        from performance_cache import capture_closed_inputs,validate_closed_inputs,InputRevisionChanged
        isin='US0378331005';scope='setting:interval_evidence:'+isin
        meta=dict(source='gettex',venue='GETTEX',currency='EUR',symbol=isin,timezone='Europe/Berlin')
        proof=dict(isin=isin,identity=meta,basis='complete_published_trade_files',checked_at='2026-11-21T10:00Z',no_trades=[['2026-11-20 09:05:00','2026-11-20 09:15:00']],trade_intervals=[],reference={'GETTEX:20261120':{'posttrade.20261120.08.15.munc.csv.gz':'a'*64}})
        from closed_evidence import canonical
        scope_later=dict(version=1,isin=isin,start='2026-11-20',through='2026-11-23T08:00Z')
        renewed={**proof,'checked_at':'2026-11-22T10:00Z'}
        self.assertEqual(canonical(proof,scope_later),canonical(renewed,scope_later))
        self.assertNotEqual(canonical(proof,scope_later),canonical({**proof,'checked_at':'2026-11-20T08:10Z'},scope_later))
        with closing(sqlite3.connect(self.db)) as d:
            d.execute('INSERT INTO settings VALUES (?,?)',('interval_evidence:'+isin,json.dumps(proof)))
            d.execute('INSERT INTO input_revisions VALUES (?,?)',(scope,1))
            identity={**meta,'source':'gettex_chart','symbol':'AAPL.GTX'}
            d.execute('INSERT INTO series VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',(isin,'gettex_chart','GETTEX','EUR','AAPL.GTX','15m','2026-11-20 09:00:00',10,11,9,10,3))
            spec=dict(isin=isin,identity=identity,base_tf='15m',timeframe='4H',through='2026-11-20T08:15:00Z',**{'from':'2026-11-20'},calculation_policy='gettex_first_v1')
            guard=capture_closed_inputs(d,[spec],{scope:1,'instrument:'+isin:1})
            later=copy.deepcopy(proof);later['checked_at']='2026-11-22T10:00Z';later['no_trades'].append(['2026-11-21 09:00:00','2026-11-21 09:15:00']);later['reference']['GETTEX:20261121']={'posttrade.20261121.08.15.munc.csv.gz':'c'*64}
            def publish(value):
                d.execute('UPDATE settings SET value=? WHERE key=?',(json.dumps(value),'interval_evidence:'+isin));d.execute('UPDATE input_revisions SET revision=revision+1 WHERE scope=?',(scope,))
            publish(later);validate_closed_inputs(d,guard)
            for kind in ('hash','identity','future','unknown','old_range','bad_hash','off_grid'):
                changed=copy.deepcopy(later)
                if kind=='hash':changed['reference']['GETTEX:20261120']['posttrade.20261120.08.15.munc.csv.gz']='b'*64
                elif kind=='identity':changed['identity']['currency']='USD'
                elif kind=='future':changed['checked_at']='2099-01-01T00:00Z'
                elif kind=='unknown':changed['unknown_fact']='changed'
                elif kind=='bad_hash':changed['reference']['GETTEX:20261121']['posttrade.20261121.08.15.munc.csv.gz']='z'*64
                elif kind=='off_grid':changed['reference']['GETTEX:20261121']['posttrade.20261121.08.10.munc.csv.gz']='c'*64
                else:changed['no_trades'][0][1]='2026-11-20 09:10:00'
                publish(changed)
                with self.assertRaises(InputRevisionChanged):validate_closed_inputs(d,guard)

    def test_alias_cannot_turn_summary_into_mutation(self):
        action,args,_=route('snapshot',dict(path='/@summary/by_stock',limit=2))
        self.assertEqual(action,'read_summary');self.assertEqual(args['path'],'/by_stock')
        with self.assertRaisesRegex(ValueError,'INVALID_READ_ROUTE'):
            route('snapshot',dict(path='/@summary',section='persisted'))

if __name__=='__main__':unittest.main()
