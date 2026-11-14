"""Isolated EUR demo/reset tests. All generated rows stay in RAM."""
import copy,json,sqlite3,sys,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
import pandas as pd
import paper_account as paper
import paper_hourly as hourly
from performance_cache import install_input_revisions,capture_closed_inputs


class HourlyTests(unittest.TestCase):
    def setUp(self):
        self.db=sqlite3.connect(':memory:')
        for schema in ['instruments(isin TEXT PRIMARY KEY,payload TEXT)', 'settings(key TEXT PRIMARY KEY,value TEXT)',
                'candles(isin,source,base_tf,ts,open,high,low,close,volume)',
                'series(isin,source,venue,currency,symbol,base_tf,ts,open,high,low,close,volume)']:
            self.db.execute('CREATE TABLE '+schema)
        install_input_revisions(self.db);paper.initialize(self.db)
        self.isin='US0378331005'
        self.identity=dict(source='gettex_chart',symbol='AAPL.GTX',venue='GETTEX',currency='EUR',timezone='Europe/Berlin')
        self.info=dict(isin=self.isin,name='Apple',kind='stock')
        self.db.execute('INSERT INTO instruments VALUES (?,?)',(self.isin,json.dumps(self.info)))
        self.cfg=dict(version=hourly.VERSION,approved=True,enabled=True,account=paper.ACCOUNT,
            identities={self.isin:self.identity},epoch='test-1',timeframe='1H',quantity=1,max_position=1)
        self.write(hourly.EPOCH,'test-1');self.write(hourly.CONFIG,self.cfg)
        self.now=pd.Timestamp('2026-11-04T09:35:00Z').timestamp()
        self.proof_setting='gettex_chart:'+self.isin+':15m'
        self.write(self.proof_setting,dict(state='SAVED',identity=self.identity,
            listing_proof=dict(self.identity,status='CONFIRMED',isin=self.isin)))
        for tf in ('1H','4H','1D'):
            for ts in pd.date_range('2026-09-01 07:30',periods=40,freq='D'):
                self.db.execute('INSERT INTO series VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                    (self.isin,'gettex_chart','GETTEX','EUR','AAPL.GTX',tf,str(ts),100,102,99,101,20))
        self.result=self.result_for('BUY')
        self.args=self.args_for('BUY')

    def write(self,k,v):self.db.execute('INSERT OR REPLACE INTO settings VALUES (?,?)',(k,json.dumps(v)))
    def result_for(self,side,signal_at='2026-11-04 09:30:00'):
        token=dict(self.db.execute('SELECT scope,revision FROM input_revisions'))
        specs=[dict(isin=self.isin,identity=self.identity,base_tf=t,timeframe=t,through='2026-11-04T09:30:00Z') for t in ('1H','4H','1D')]
        proof=capture_closed_inputs(self.db,specs,token)
        engine=dict(status='READY',signal=side,rules_version='1',timestamp=signal_at,
            timeframe_context={t:dict(data_quality=dict(state='READY')) for t in ('1H','4H','1D')},
            benchmark_context=dict(state='NEUTRAL',quality='READY'))
        return dict(isin=self.isin,timeframe='1H',identity=copy.deepcopy(self.identity),_input_revisions=token,
            hourly=dict(version=hourly.VERSION,config_digest=hourly.digest(self.cfg),epoch=self.cfg['epoch'],
                ledger_token=hourly.ledger_token(self.db),checked_at=pd.Timestamp(self.now,unit='s',tz='UTC').isoformat(),
                expires_at=pd.Timestamp(self.now+60,unit='s',tz='UTC').isoformat(),proof=proof,required_tf=['1H','4H','1D']),
            execution_context=dict(status='HOURLY_DEMO_READY',isin=self.isin,analysis_ready=True,trading_engine=engine,
                checked_at=pd.Timestamp(self.now,unit='s',tz='UTC').isoformat()),
            execution_quote=dict(isin=self.isin,timeframe='15m',identity=copy.deepcopy(self.identity),
                data_quality=dict(state='READY',identity=copy.deepcopy(self.identity),checked_at=pd.Timestamp(self.now,unit='s',tz='UTC').isoformat()),
                bars=[dict(open=100,high=102,low=99,close=100,volume=20,closed=True,
                    open_at='2026-11-04T10:15:00+01:00',close_at='2026-11-04T09:30:00Z')]))
    def args_for(self,side):
        return dict(request_id=hourly.request_id(self.cfg,self.isin,self.result['execution_context']['trading_engine']['timestamp']),
            isin=self.isin,side=side,quantity=1,timeframe='1H',_deadline=self.now+30)
    def trade(self):return paper.execute(self.db,self.info,self.result,self.args,self.now)
    def blocked(self,code):
        with self.assertRaisesRegex(ValueError,code):self.trade()
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM paper_fills').fetchone()[0],0)
    def tearDown(self):self.db.close()

    def test_hourly_buy_sell_fees_spread_and_duplicate(self):
        self.trade();self.assertEqual(paper.ledger(self.db)['cash'],'9898.95')
        self.assertTrue(self.trade()['idempotent']);self.assertEqual(len(paper.ledger(self.db)['fills']),1)
        self.now+=3600;self.result=self.result_for('SELL','2026-11-04 10:30:00')
        self.result['execution_quote']['bars'][0]['close_at']='2026-11-04T10:30:00Z'
        self.args=self.args_for('SELL');self.trade()
        self.assertEqual(paper.ledger(self.db)['cash'],'9997.90')
        self.assertEqual(paper.ledger(self.db)['realized'],'-2.10')
        self.assertEqual(paper.ledger(self.db)['positions'][0]['quantity'],'0')
    def test_reset_archives_receipt_and_never_replays(self):
        self.trade();hourly.reset(self.db,'test-2');state=paper.ledger(self.db)
        self.assertEqual(state['cash'],'10000.00');self.assertEqual(state['positions'],[])
        self.assertEqual(state['fills'],[]);self.assertTrue(self.trade()['archived'])
        hourly.reset(self.db,'test-2');self.assertEqual(self.db.execute('SELECT COUNT(*) FROM paper_fill_archive').fetchone()[0],1)
        self.assertFalse(hourly.read(self.db,hourly.CONFIG)['enabled'])
        clone=sqlite3.connect(':memory:');self.db.commit();self.db.backup(clone)
        self.assertEqual(paper.ledger(clone)['cash'],'10000.00');clone.close()
    def test_no_wait_orders(self):
        self.result['execution_context']['trading_engine']['signal']='WAIT';self.blocked('SIGNAL_MISMATCH')
    def test_disabled_authority(self):
        self.cfg['enabled']=False;self.write(hourly.CONFIG,self.cfg);self.blocked('STALE_INPUTS')
    def test_dq_blocked(self):
        self.result['execution_context']['trading_engine']['status']='DATA BLOCKED';self.blocked('ENGINE_DATA_BLOCKED')
    def test_unconfirmed_without_grant(self):
        self.result['execution_context']['trading_engine']['timeframe_context']['4H']['data_quality']['state']='UNCONFIRMED'
        self.blocked('AUTO_DQ_BLOCKED')
    def test_seed_instability(self):
        self.result['execution_context']['trading_engine']['demo_initialization']={'consistent':False};self.blocked('SEED_BLOCKED')
    def test_warmup_not_proven(self):
        self.result['hourly']['proof']['series'][0]['count']=34;self.blocked('DEPENDENCY_BLOCKED')
    def test_wrong_source_currency_and_other_stock(self):
        for key,value in [('venue','FRA'),('currency','USD'),('source','proxy')]:
            old=self.result['identity'][key];self.result['identity'][key]=value;self.blocked('IDENTITY_BLOCKED');self.result['identity'][key]=old
        self.cfg['identities']={};self.write(hourly.CONFIG,self.cfg);self.blocked('STALE_INPUTS')
    def test_revision_changed(self):
        self.db.execute('UPDATE series SET close=100 WHERE base_tf=?',('1H',));self.blocked('STALE_INPUTS')
    def test_stale_quote_and_zero_volume(self):
        bar=self.result['execution_quote']['bars'][0];bar['close_at']='2026-11-04T08:30:00Z';self.blocked('STALE_QUOTE')
        bar['close_at']='2026-11-04T09:30:00Z';bar['volume']=0;self.blocked('BAD_OHLCV')
    def test_expired_context(self):
        self.result['hourly']['expires_at']='2026-11-04T09:34:59Z';self.blocked('STALE_WINDOW')
    def test_position_and_one_bar_cap(self):
        self.trade();self.result=self.result_for('BUY','2026-11-04 10:30:00');self.args=self.args_for('BUY')
        with self.assertRaisesRegex(ValueError,'POSITION_LIMIT'):self.trade()
        self.result=self.result_for('SELL');self.args=self.args_for('SELL')
        with self.assertRaisesRegex(ValueError,'REQUEST_REUSED'):self.trade()
    def test_benchmark_and_quantity(self):
        self.result['execution_context']['trading_engine']['benchmark_context']={};self.blocked('BENCHMARK_BLOCKED')
        self.result=self.result_for('BUY');self.args['quantity']=2;self.blocked('SIZE_BLOCKED')
    def test_legacy_execution_requires_new_epoch(self):
        self.result['_input_revisions'].pop('setting:paper_epoch');self.blocked('ACCOUNT_RESET')

    def test_hourly_maintenance_scope_and_pause_without_permission(self):
        from contextlib import contextmanager
        from types import SimpleNamespace
        from candle_schedule import trade_maintenance
        @contextmanager
        def connect():yield self.db
        store=SimpleNamespace(connect=connect)
        for size in (53,200):
            infos=[self.info]+[dict(isin='XX'+str(n).zfill(10),kind='stock') for n in range(size-1)]
            result=trade_maintenance(store,infos,pd.Timestamp(self.now,unit='s',tz='UTC'))
            self.assertEqual(set(result),{self.isin})
            self.assertEqual(result[self.isin][0]['required_tf'],['15m','1H','4H','1D'])
            self.assertFalse(result[self.isin][0]['execution_permission'])
        self.cfg['enabled']=False;self.write(hourly.CONFIG,self.cfg)
        self.assertEqual(trade_maintenance(store,[self.info],pd.Timestamp(self.now,unit='s',tz='UTC')), {})

    def test_runtime_respects_cooldown_restart_and_close(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        calls=[];writes={}
        self.result['execution_context']['trading_engine']['signal']='WAIT'
        store=SimpleNamespace(path='unused',connect=lambda: self.db,setting=lambda k,d=None: self.cfg if k==hourly.CONFIG else writes.get(k,d),
            set_setting=lambda k,v:writes.update({k:v}),read_cache=SimpleNamespace(token_for=lambda scopes: {k: dict(self.db.execute('SELECT scope,revision FROM input_revisions')).get(k,0) for k in scopes}))
        root=SimpleNamespace(after=lambda ms,fn:calls.append(('timer',ms)))
        def job(work,done,failed=None,**kwargs):
            self.assertEqual(kwargs['lane'],'trading');calls.append(('job',kwargs['key']));done(work());return True
        app=SimpleNamespace(store=store,watchlist=[self.info],closing=False,user_dirty=False,ai_config={},root=root,job=job,
            compute_trading=SimpleNamespace(calculate=lambda *args:copy.deepcopy(self.result)))
        with patch.object(hourly.time,'time',return_value=self.now):
            runtime=hourly.Runtime(app);runtime.tick()
            for _ in range(50):runtime.tick()
            hourly.Runtime(app).tick()
        self.assertEqual(sum(x[0]=='job' for x in calls),1)
        self.assertEqual(writes[hourly.STATUS+self.isin]['state'],'WAIT')
        self.write('data_quality:'+self.isin+':AAPL.GTX:15m',{'state':'READY'})
        with patch.object(hourly.time,'time',return_value=self.now+15):
            runtime.tick()
        self.assertEqual(sum(x[0]=='job' for x in calls),2)
        app.closing=True;count=len(calls);runtime.tick();self.assertEqual(len(calls),count)

    def test_runtime_enters_on_4h_and_records_both_outcomes(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        from trading_context import plan
        for hourly_signal in ('WAIT', 'BUY'):
            with self.subTest(hourly_signal=hourly_signal):
                self.db.execute('DELETE FROM paper_fills')
                self.db.commit()
                reports={}
                def calculate(kind, values):
                    tf=values['timeframe']
                    result=self.result_for(hourly_signal if tf=='1H' else 'BUY')
                    result['timeframe']=tf
                    result['hourly']['timeframe_contract']=plan(tf)
                    return result
                store=SimpleNamespace(path='unused',connect=lambda:self.db,
                    set_setting=lambda key,value:reports.update({key:value}))
                app=SimpleNamespace(store=store,compute_trading=SimpleNamespace(calculate=calculate),
                    closing=False,user_dirty=False)
                with patch.object(hourly.sqlite3,'connect',return_value=self.db),patch.object(hourly.time,'time',return_value=self.now):
                    first=hourly.run_turn(app,self.info,{},self.cfg)
                    repeated=hourly.run_turn(app,self.info,{},self.cfg)
                fills=paper.ledger(self.db)['fills']
                self.assertEqual(len(fills),1)
                chosen='4H' if hourly_signal=='WAIT' else '1H'
                self.assertEqual(fills[0]['timeframe'],chosen)
                self.assertEqual(first['timeframes'][chosen]['state'],'COMPLETED')
                self.assertTrue(repeated['timeframes'][chosen]['idempotent'])
                if chosen=='1H':
                    self.assertIn('POSITION_LIMIT', first['timeframes']['4H']['execution_blocker'])

    def test_4h_rejects_missing_contract_and_changed_closed_input(self):
        from trading_context import plan
        self.result['timeframe']='4H'
        self.args.update(timeframe='4H',request_id=hourly.request_id(self.cfg,self.isin,
            self.result['execution_context']['trading_engine']['timestamp'],'4H'))
        self.blocked('DEPENDENCY_BLOCKED')
        self.result['hourly']['timeframe_contract']=plan('4H')
        self.db.execute("UPDATE series SET close=100 WHERE base_tf='4H'")
        self.blocked('STALE_INPUTS')

    def test_missing_owner_risk_receipt_blocks_hourly_manual(self):
        from cp_controls import manual_gate
        with self.assertRaisesRegex(ValueError,'MANUAL_NOT_GRANTED'):
            manual_gate(self.db,self.isin,self.identity,'1H',self.result['hourly']['proof'])

    def test_fresh_quote_does_not_replace_latest_invalid_or_stale(self):
        from types import SimpleNamespace
        from contextlib import contextmanager
        from unittest.mock import patch
        import data_quality as dq
        @contextmanager
        def connect():yield self.db
        store=SimpleNamespace(connect=connect,read_cache=SimpleNamespace(depend=lambda _:None))
        self.db.execute('INSERT INTO series VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
            (self.isin,'gettex_chart','GETTEX','EUR','AAPL.GTX','15m','2026-11-04 10:15:00',100,102,99,100,20))
        clock=pd.Timestamp(self.now,unit='s',tz='UTC')
        audit=dq.audit
        def timed_audit(*a,**kw):
            report=audit(*a,**kw);report['checked_at']=(clock+pd.Timedelta(milliseconds=10)).isoformat();return report
        with patch.object(hourly.time,'time',return_value=self.now+.02),patch.object(dq,'audit',side_effect=timed_audit):
            self.assertEqual(hourly.fresh_quote(store,self.info,self.identity,clock)['bars'][-1]['close'],100)
            with self.assertRaisesRegex(ValueError,'STALE_QUOTE'):hourly.fresh_quote(store,self.info,self.identity,clock+pd.Timedelta(hours=1))
            self.db.execute("UPDATE series SET high=0 WHERE base_tf='15m'")
            with self.assertRaisesRegex(ValueError,'DATA_BLOCKED'):hourly.fresh_quote(store,self.info,self.identity,clock)


if __name__=='__main__':unittest.main(verbosity=2)
