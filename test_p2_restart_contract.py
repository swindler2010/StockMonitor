"""Isolated integration of the current Smart contract, CORE and paper ledger.

Only existing fixture data; never opens the user's TEST database.
"""
import copy
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

import test_smart_execution as fixtures
import test_trading_engine as core_fixtures
import paper_account as paper
from trading_engine import evaluate


class RestartContract(unittest.TestCase):
    save = fixtures.NewExecutionContract.save
    guard = fixtures.NewExecutionContract.guard
    trade = fixtures.NewExecutionContract.trade

    def setUp(self):
        fixtures.NewExecutionContract.setUp(self)
        self.temp = tempfile.TemporaryDirectory(prefix='p2-isolated-')
        self.path = Path(self.temp.name) / 'isolated.sqlite3'
        target = sqlite3.connect(self.path)
        self.db.backup(target)
        self.db.close()
        self.db = target

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def reopen(self):
        self.db.close()
        self.db = sqlite3.connect(self.path)

    def core(self, stop=False, gap=False, reliable=True):
        original = core_fixtures.EngineTests.contexts(self)
        contexts = {'5m':original['1H'], '4H':original['4H'], '1D':original['1D']}
        if stop:
            contexts['5m']['facts'].update(closed_below_stop=True, breakout=False,
                histogram=-1., histogram_delta=-1., rsi=40., candle_up=False,
                volume_reliable=reliable, relative_volume=1.5 if reliable else None)
        if gap:contexts['1D']['data_quality']['state']='GAP'
        return evaluate(contexts,'5m',{'quantity':2 if stop else 0},self.record['benchmark'])

    def bind_core(self, engine):
        candidate=self.record['candidates'][0]
        candidate['result']=engine
        candidate['analysis_ready']=engine['status']=='READY'
        candidate['signal_ready']=candidate['trade_ready']=candidate['analysis_ready'] and engine['signal'] in ('BUY','SELL')
        self.save()
        self.result['execution_context']['trading_engine']=engine
        self.result['execution_context']['smart_eligibility']=self.guard()

    def test_buy_restart_duplicate_then_core_stop_sell(self):
        self.bind_core(self.core())
        self.assertEqual(self.trade()['status'],'COMPLETED')
        self.assertEqual(paper.ledger(self.db)['cash'],'9798.90')
        payload={'info':self.info,'result':self.result,'args':self.args,'now':self.now}
        job=Path(self.temp.name)/'job.json';job.write_text(json.dumps(payload),encoding='utf-8')
        self.db.close()
        code="""import sys,json,sqlite3
sys.path.insert(0,sys.argv[1]);import paper_account as p
x=json.load(open(sys.argv[3],encoding='utf-8'));db=sqlite3.connect(sys.argv[2])
with db:
 db.execute('BEGIN IMMEDIATE');r=p.execute(db,x['info'],x['result'],x['args'],now=x['now'])
print(json.dumps({'idempotent':r['idempotent'],'fills':len(p.ledger(db)['fills'])}));db.close()
"""
        result=subprocess.run([sys.executable,'-c',code,str(Path(__file__).parent/'stock_monitor'),str(self.path),str(job)],capture_output=True,text=True,timeout=25,check=True)
        self.assertEqual(json.loads(result.stdout),{'idempotent':True,'fills':1})
        self.db=sqlite3.connect(self.path)
        sell=self.core(stop=True);self.assertEqual(sell['signal'],'SELL');self.bind_core(sell)
        self.args.update(request_id='sell-after-restart',side='SELL')
        self.trade();self.reopen();state=paper.ledger(self.db)
        self.assertEqual((state['cash'],state['realized'],state['fees']),('9997.80','-2.20','2.00'))
        self.assertEqual(state['positions'][0]['quantity'],'0');self.assertEqual(len(state['fills']),2)
        self.assertTrue(self.trade()['idempotent'])

    def test_commit_failure_retry_after_reopen_is_single_execution(self):
        self.bind_core(self.core())
        with self.assertRaisesRegex(RuntimeError,'controlled failure'):
            with self.db:
                self.db.execute('BEGIN IMMEDIATE')
                paper.execute(self.db,self.info,self.result,self.args,now=self.now)
                raise RuntimeError('controlled failure before commit')
        self.reopen();self.assertEqual(paper.ledger(self.db)['fills'],[])
        self.trade();self.reopen();self.assertTrue(self.trade()['idempotent'])
        self.assertEqual(len(paper.ledger(self.db)['fills']),1)
        self.args['quantity']=1
        with self.assertRaisesRegex(ValueError,'REQUEST_REUSED'):self.trade()
        self.assertEqual(len(paper.ledger(self.db)['fills']),1)

    def test_revision_change_after_restart_blocks_new_request_not_old_receipt(self):
        self.bind_core(self.core());self.trade();self.reopen()
        self.db.execute("UPDATE input_revisions SET revision=revision+1 WHERE scope=?",('market:'+self.isin+':5m',));self.db.commit()
        self.assertTrue(self.trade()['idempotent'])
        self.args['request_id']='new-request-after-revision'
        with self.assertRaisesRegex(ValueError,'STALE_INPUTS'):self.trade()
        self.assertEqual(len(paper.ledger(self.db)['fills']),1)

    def test_stop_is_not_permission_when_core_data_or_volume_unconfirmed(self):
        self.bind_core(self.core());self.trade();self.args.update(request_id='stop-blocked',side='SELL')
        for kwargs in ({'gap':True},{'reliable':False}):
            engine=self.core(stop=True,**kwargs);self.assertEqual(engine['signal'],'WAIT')
            self.record['candidates'][0].update(result=engine,analysis_ready=engine['status']=='READY',signal_ready=False,trade_ready=False)
            self.save();self.result['execution_context']['trading_engine']=engine
            with self.assertRaises(ValueError):self.trade()
        self.assertEqual(len(paper.ledger(self.db)['fills']),1)

    def test_restart_does_not_refresh_expiry_or_rewrite_ohlcv(self):
        self.db.execute('CREATE TABLE fixture_ohlcv(ts TEXT PRIMARY KEY,close REAL)')
        self.db.execute("INSERT INTO fixture_ohlcv VALUES ('2026-10-20T08:00:00Z',100)");self.db.commit()
        saved=self.db.execute('SELECT value FROM settings').fetchall()
        self.reopen();self.now+=121;self.args['_deadline']=self.now+60
        with self.assertRaisesRegex(ValueError,'NOT_CURRENT'):self.guard()
        with self.assertRaises(ValueError):self.trade()
        self.assertEqual(self.db.execute('SELECT value FROM settings').fetchall(),saved)
        self.assertEqual(self.db.execute('SELECT close FROM fixture_ohlcv').fetchall(),[(100.,)])
        self.assertEqual(paper.ledger(self.db)['fills'],[])


if __name__=='__main__':unittest.main()
