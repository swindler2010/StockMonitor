"""New V2 / shared shadow execution tests. No writes to the real account."""
import copy,json,sqlite3,sys,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
import paper_strategy_v2 as v2
import paper_strategy_shadow as shadow

IDENT=dict(source='gettex_chart',symbol='AAPL.GTX',venue='GETTEX',currency='EUR',timezone='Europe/Berlin')
def engine():
    f=dict(price=100.,atr=1.,support=98.,resistance=110.,histogram=.2,histogram_delta=.1,rsi=55.,
        ema10_slope_atr=.2,regime='TRANSITION',volume_reliable=True,relative_volume=.8,
        breakout=True,reclaim=False,breakdown=False,bearish_retest=False,extended=False)
    proof=dict(stable=True,basis='Exact terminal indicators and CORE facts with first in-scope seed omitted; no older data')
    return dict(status='READY',signal='WAIT',timeframe_context={t:dict(facts=copy.deepcopy(f),scope_initialization=proof.copy()) for t in ('1H','4H','1D')})
def event(n=0,action='BUY'):
    from datetime import datetime,timedelta,timezone
    end=datetime(2026,11,10,10,tzinfo=timezone.utc)+timedelta(minutes=15*n)
    decision=dict(action=action,mode='STANDARD',stop=97.51,atr=1.,support=98.,risk_fraction=.01,reason='TEST')
    return dict(isin='US0378331005',identity=IDENT,quote=dict(open=100.,high=100.2,low=99.8,close=100.,volume=10.,closed=True,
        open_at=(end-timedelta(minutes=15)).isoformat(),close_at=end.isoformat()),
        available_at=end.isoformat(),signal_at='2026-11-10T08:30:00+00:00',signal_close='2026-11-10T09:30:00+00:00',
        decision_fingerprint='same-closed-inputs',hourly_price=100.,v1=copy.deepcopy(decision),v2=copy.deepcopy(decision))

class V2Tests(unittest.TestCase):
    def test_early_positive_without_volume_120(self):
        d=v2.proposal(engine());self.assertEqual((d['action'],d['mode'],d['risk_fraction']),('BUY','EARLY',.005))
    def test_entry_filters_and_reward_preserved(self):
        changes=[('1H','breakout',False),('1H','histogram',-.1),('1H','rsi',49),('4H','ema10_slope_atr',-.1),('1H','resistance',101)]
        for tf,key,value in changes:
            with self.subTest(key=key):
                e=engine();e['timeframe_context'][tf]['facts'][key]=value;self.assertEqual(v2.proposal(e)['action'],'WAIT')
        e=engine();e['timeframe_context']['1D']['facts'].update(regime='BEARISH',histogram=-1,histogram_delta=-1,rsi=40)
        self.assertIn('1D_STRONG',v2.proposal(e)['reason'])
    def test_unstable_seed_and_bad_core_blocked(self):
        e=engine();e['status']='DATA BLOCKED';self.assertEqual(v2.proposal(e)['action'],'BLOCKED')
        e=engine();e['timeframe_context']['1H']['scope_initialization'].update(mode='OWNER_ACCEPTED_DEMO_SENSITIVITY',max_absolute_deltas={k:.1 for k in ('atr','rsi','ema10','macd','macd_signal')})
        e['timeframe_context']['1H']['facts']['histogram']=.01
        self.assertEqual(v2.proposal(e)['action'],'BLOCKED')
    def test_v1_signal_preserved_standard_and_sell(self):
        for side in ('BUY','SELL'):
            e=engine();e['signal']=side;self.assertEqual(v2.proposal(e)['action'],side)
    def test_risk_half_cash_and_aggregate_caps(self):
        b=v2.book();d=v2.proposal(engine());n,p,r=v2.size(b,100,d,'V2');self.assertLessEqual(r['estimated_loss'],50)
        d.update(mode='STANDARD',risk_fraction=.01);n,p,r=v2.size(b,100,d,'V2');self.assertLessEqual(r['estimated_loss'],100)
        self.assertLessEqual(n*p+1,4500)
        b['positions']['OTHER']=dict(entry=100,stop=90,qty=20)
        with self.assertRaises(ValueError):v2.size(b,100,d,'V2')
        b=v2.book();b['cash']=50
        with self.assertRaises(ValueError):v2.size(b,100,d,'V2')
    def test_fill_rr_includes_costs(self):
        d=v2.proposal(engine());d['target']=100+2*(100-d['stop'])
        with self.assertRaisesRegex(ValueError,'RR_BELOW_2'):v2.size(v2.book(),100,d,'V2')
    def test_next_bar_only_and_no_duplicate(self):
        b=v2.book();self.assertFalse(any(x['kind']=='FILL' for x in v2.advance(b,'V2',event())))
        rows=v2.advance(b,'V2',event(1));self.assertEqual(sum(x['kind']=='FILL' for x in rows),1)
        previous=copy.deepcopy(b);self.assertEqual(v2.advance(b,'V2',event(1)),[]);self.assertEqual(previous,b)
    def test_revision_cancels_pending_fill(self):
        b=v2.book();v2.advance(b,'V2',event());e=event(1);e['decision_fingerprint']='revised'
        rows=v2.advance(b,'V2',e);self.assertEqual(b['positions'],{});self.assertEqual(rows[0]['reason'],'SIGNAL_OR_INPUTS_CHANGED')
    def test_proof_revision_revokes_even_if_terminal_facts_equal(self):
        b=v2.book();v2.advance(b,'V2',event());e=event(1);e['revoke_pending']=True
        rows=v2.advance(b,'V2',e);self.assertFalse(b['positions']);self.assertEqual(rows[0]['reason'],'PRIOR_CLOSED_INPUTS_CHANGED')
    def test_missing_execution_bar_is_not_silently_skipped(self):
        b=self.bought()
        with self.assertRaisesRegex(ValueError,'MISSED_EXECUTION_BARS'):v2.advance(b,'V2',event(3))
    def test_variant_decisions_are_independent(self):
        a=v2.book();b=v2.book();e=event();e['v1']['action']='WAIT'
        v2.advance(a,'V1',e);v2.advance(b,'V2',e)
        e=event(1);e['v1']['action']='WAIT';v2.advance(a,'V1',e);v2.advance(b,'V2',e)
        self.assertFalse(a['positions']);self.assertTrue(b['positions']);self.assertEqual(a['cash'],10000)
    def bought(self):
        b=v2.book();v2.advance(b,'V2',event());v2.advance(b,'V2',event(1));return b
    def test_stop_first_and_gap_execution(self):
        b=self.bought();e=event(2);e['quote'].update(open=96,low=95,high=110,close=100)
        rows=v2.advance(b,'V2',e);fill=next(x for x in rows if x['kind']=='FILL')
        self.assertEqual(fill['reason'],'STOP_OR_GAP_STOP');self.assertAlmostEqual(fill['price'],96*.9995);self.assertFalse(b['positions'])
    def test_breakeven_partial_and_trailing(self):
        b=self.bought();p=b['positions'][event()['isin']];initial=p['qty'];r=p['r'];entry=p['entry']
        e=event(2);e['quote'].update(high=entry+r+.01,low=100,close=entry+r)
        v2.advance(b,'V2',e);self.assertGreater(p['stop'],entry);self.assertEqual(p['qty'],initial)
        e=event(3);e['quote'].update(open=entry+r,low=entry+1,high=entry+2*r+.1,close=entry+2*r);e['hourly_price']=entry+2*r
        v2.advance(b,'V2',e);self.assertTrue(p['partial']);self.assertLess(p['qty'],initial);self.assertGreaterEqual(p['stop'],e['hourly_price']-2)
    def test_one_share_arms_trailing_without_inventing_fraction(self):
        b=self.bought();p=b['positions'][event()['isin']];p['qty']=1;p['cost']=p['entry']+1
        e=event(2);e['quote'].update(high=p['entry']+2*p['r']+.1,close=p['entry']+2*p['r']);e['hourly_price']=e['quote']['close']
        rows=v2.advance(b,'V2',e);self.assertEqual(p['qty'],1);self.assertTrue(p['partial']);self.assertFalse(any(r['kind']=='FILL' for r in rows))
    def test_v1_no_new_smart_stops(self):
        b=v2.book();v2.advance(b,'V1',event());v2.advance(b,'V1',event(1));e=event(2);e['quote'].update(open=90,low=89,close=90)
        self.assertFalse(any(x['kind']=='FILL' for x in v2.advance(b,'V1',e)));self.assertTrue(b['positions'])
    def test_bad_or_future_execution_bar_rejected(self):
        for key,value in [('closed',False),('volume',0),('high',90)]:
            e=event();e['quote'][key]=value
            with self.assertRaises(ValueError):v2.advance(v2.book(),'V2',e)
    def test_independent_books_same_cost_model_and_restart(self):
        db=shadow.connect(':memory:');shadow.apply(db,event());shadow.apply(db,event(1))
        books={n:json.loads(s) for n,s in db.execute('SELECT name,state FROM books')}
        self.assertEqual(books['V1']['cash'],books['V2']['cash'])
        saved=db.serialize();other=sqlite3.connect(':memory:');other.deserialize(saved)
        count=other.execute('SELECT count(*) FROM journal').fetchone()[0]
        self.assertEqual(shadow.apply(other,event(1))['state'],'UNCHANGED')
        self.assertEqual(other.execute('SELECT count(*) FROM journal').fetchone()[0],count)
        books['V1']['cash']=1;self.assertNotEqual(books['V1']['cash'],books['V2']['cash']);db.close();other.close()
    def test_atomic_guard_failure_rolls_back_both(self):
        db=shadow.connect(':memory:');calls=[]
        def guard():
            calls.append(1)
            if len(calls)==2:raise ValueError('REVISION_CHANGED')
        with self.assertRaises(ValueError):shadow.apply(db,event(),guard=guard)
        self.assertEqual(db.execute('SELECT count(*) FROM journal').fetchone()[0],0)
        self.assertEqual(db.execute('SELECT count(*) FROM observations').fetchone()[0],0);db.close()
    def test_metrics_profit_fees_drawdown(self):
        b=self.bought();e=event(2);e['quote'].update(open=96,low=95,high=101,close=96)
        v2.advance(b,'V2',e);m=v2.metrics(b)
        self.assertEqual(m['commissions'],2);self.assertEqual(m['closed_trades'],1);self.assertEqual(m['losses'],1)
        self.assertAlmostEqual(m['expectancy'],m['net_profit']);self.assertGreater(m['max_drawdown_pct'],0)
    def test_v1_sizing_parity_with_unchanged_implementation(self):
        # Reuse fixture only; do not run its old test suite.
        import test_paper_hourly as old
        h=old.HourlyTests();h.setUp()
        try:
            h.cfg.pop('quantity');h.cfg.pop('max_position');h.cfg.update(sizing=dict(version=old.hourly.SIZING,risk_fraction='0.01',allocation_fraction='0.45'),target_equity='20000.00')
            h.write(old.hourly.CONFIG,h.cfg)
            for atr,support in ((1,98),(2.3,95),(10,90)):
                h.result=h.result_for('BUY');h.result['execution_context']['trading_engine']['timeframe_context']['1H']['facts']=dict(atr=atr,support=support)
                a=old.hourly.order_size(h.db,h.info,h.result,'BUY',h.now)
                b=v2.size(v2.book(),100,dict(atr=atr,support=support),'V1')
                self.assertEqual(a['quantity'],b[0])
        finally:h.tearDown()

class GuardTests(unittest.TestCase):
    def setUp(self):
        import test_paper_hourly as old
        self.h=old.HourlyTests();self.h.setUp();h=self.h
        h.db.execute('INSERT INTO series VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',(h.isin,'gettex_chart','GETTEX','EUR','AAPL.GTX','15m','2026-11-04 10:15:00',100,102,99,100,20))
    def tearDown(self):self.h.tearDown()
    def test_valid_wait_is_analyzable_without_forcing_buy(self):
        h=self.h;h.result['execution_context']['trading_engine']['signal']='WAIT'
        self.assertEqual(shadow.validate(h.db,h.info,h.result,h.now)['close'],100)
        self.assertEqual(h.db.execute('SELECT count(*) FROM paper_fills').fetchone()[0],0)
    def test_quote_revision_rejected(self):
        h=self.h;h.db.execute('UPDATE series SET close=101 WHERE base_tf=?',('15m',))
        with self.assertRaisesRegex(ValueError,'EXECUTION_BAR_CHANGED'):shadow.validate(h.db,h.info,h.result,h.now)
    def test_closed_dependency_revision_rejected(self):
        h=self.h;h.db.execute('UPDATE series SET close=101.5 WHERE base_tf=?',('4H',))
        with self.assertRaises(Exception):shadow.validate(h.db,h.info,h.result,h.now)
    def test_dq_identity_benchmark_warmup_and_expiry(self):
        h=self.h;original=copy.deepcopy(h.result)
        for change in ('dq','identity','benchmark','warmup','expired'):
            h.result=copy.deepcopy(original)
            if change=='dq':h.result['execution_context']['trading_engine']['timeframe_context']['4H']['data_quality']['state']='UNCONFIRMED'
            if change=='identity':h.result['identity']['currency']='USD'
            if change=='benchmark':h.result['execution_context']['trading_engine']['benchmark_context']['quality']='INVALID'
            if change=='warmup':h.result['hourly']['proof']['series'][0]['count']=34
            if change=='expired':h.result['hourly']['expires_at']='2026-11-04T09:00:00Z'
            with self.subTest(change=change),self.assertRaises(ValueError):shadow.validate(h.db,h.info,h.result,h.now)
    def test_stale_price_blocked(self):
        h=self.h;h.result['execution_quote']['bars'][0]['close_at']='2026-11-04T08:30:00Z'
        with self.assertRaisesRegex(ValueError,'STALE_QUOTE'):shadow.validate(h.db,h.info,h.result,h.now)
    def test_open_bar_and_future_signal_blocked(self):
        h=self.h;h.result['execution_quote']['bars'][0]['closed']=False
        with self.assertRaisesRegex(ValueError,'NO_CLOSED_BAR'):shadow.validate(h.db,h.info,h.result,h.now)
        r=copy.deepcopy(h.result);r['execution_context']['trading_engine'].update(engine());r['execution_context']['trading_engine']['timestamp']='2026-11-10 12:30:00'
        with self.assertRaisesRegex(ValueError,'OPEN_SIGNAL_BAR'):shadow.event_from(r,event()['quote'],'2026-11-10T10:00:00Z')

if __name__=='__main__':unittest.main(verbosity=2)
