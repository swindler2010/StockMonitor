"""AD: explicit GUI/Bridge decisions, same protected PAPER ledger; isolated only."""
import copy
import json
import sqlite3
import unittest
from decimal import Decimal
from test_paper_sizing import SizingTests
import paper_orders as orders
import paper_hourly as hourly
import paper_account as paper
from paper_protection import POLICY


class OwnerOrders(unittest.TestCase):
    def setUp(self):
        self.s=SizingTests();self.s.setUp();self.f=self.s.f
        self.f.cfg['protection']=POLICY.copy()
        self.f.write(hourly.CONFIG,self.f.cfg)

    def tearDown(self):self.s.tearDown()

    def prepare(self, side='BUY', initiator='MANUAL', qty=1, request=None):
        f=self.f;self.s.prepare('WAIT')
        intent=dict(version=orders.VERSION,initiator=initiator,isin=f.isin,side=side,
            quantity=qty,quote_tf='15m',request_id=request or initiator+'-'+side+'-test')
        f.result['discretionary']=intent
        f.args.update(side=side,quantity=qty,request_id=intent['request_id'],
            _initiator=initiator,_quote_tf='15m')

    def test_gui_and_chatgpt_wait_roundtrip_restart_and_duplicate(self):
        for initiator in ('MANUAL','CHATGPT'):
            self.prepare(initiator=initiator,qty=2)
            fill=self.f.trade()['fill']
            self.assertEqual(fill['initiator'],initiator)
            self.assertEqual(fill['smart_eligibility']['signal'],'WAIT')
            self.assertEqual(fill['smart_eligibility']['protection']['planned_quantity'],'2')
            self.assertTrue(self.f.trade()['idempotent'])
            self.prepare('SELL',initiator,1,initiator+'-partial')
            self.f.trade()
            self.assertEqual(paper.ledger(self.f.db)['positions'][0]['quantity'],'1')
            self.assertTrue(paper.ledger(self.f.db)['positions'][0]['protection'])
            self.prepare('SELL',initiator,1)
            self.f.trade()
        before=paper.ledger(self.f.db);self.assertEqual(len(before['fills']),6)
        clone=sqlite3.connect(':memory:');self.f.db.commit();self.f.db.backup(clone)
        self.assertEqual(paper.ledger(clone),before);clone.close()

    def test_paused_v1_still_manual_but_auto_cannot_execute_wait(self):
        self.f.cfg['enabled']=False;self.f.write(hourly.CONFIG,self.f.cfg)
        self.prepare();self.f.trade()
        self.prepare('SELL');self.f.result.pop('discretionary')
        with self.assertRaisesRegex(ValueError,'AUTO_NOT_AUTHORIZED'):self.f.trade()

    def test_dq_freshness_identity_warmup_and_revisions_still_block(self):
        cases=[('dq','AUTO_DQ_BLOCKED'),('core','ENGINE_DATA_BLOCKED'),
            ('age','STALE_QUOTE'),('identity','QUOTE_IDENTITY'),('warmup','DEPENDENCY'),
            ('seed','SEED_BLOCKED'),('revision','STALE_INPUTS'),('benchmark','BENCHMARK_BLOCKED')]
        for case,code in cases:
            self.prepare();r=self.f.result;e=r['execution_context']['trading_engine']
            if case=='dq':e['timeframe_context']['4H']['data_quality']['state']='UNCONFIRMED'
            if case=='core':e['status']='DATA BLOCKED'
            if case=='age':r['execution_quote']['bars'][0]['close_at']='2026-11-03T09:30:00Z'
            if case=='identity':r['execution_quote']['identity']['venue']='FRA'
            if case=='warmup':r['hourly']['proof']['series'][0]['count']=34
            if case=='seed':e['demo_initialization']={'consistent':False}
            if case=='revision':r['_input_revisions']['instrument:'+self.f.isin]-=1
            if case=='benchmark':e['benchmark_context']['quality']='UNCONFIRMED'
            with self.subTest(case=case),self.assertRaisesRegex(ValueError,code):self.f.trade()
        self.assertEqual(len(paper.ledger(self.f.db)['fills']),0)

    def test_risk_position_and_intent_binding(self):
        self.prepare(qty=24)
        with self.assertRaisesRegex(ValueError,'RISK_SIZE_LIMIT'):self.f.trade()
        self.prepare('SELL')
        with self.assertRaisesRegex(ValueError,'INSUFFICIENT_POSITION'):self.f.trade()
        self.prepare();self.f.args['_initiator']='CHATGPT'
        with self.assertRaisesRegex(ValueError,'BINDING_CHANGED'):self.f.trade()
        self.prepare();self.f.trade()
        self.prepare(request='different-buy')
        with self.assertRaisesRegex(ValueError,'POSITION_LIMIT'):self.f.trade()
        self.prepare('SELL',qty=2)
        with self.assertRaisesRegex(ValueError,'INSUFFICIENT_POSITION'):self.f.trade()
        self.prepare(initiator='CHATGPT',request='MANUAL-BUY-test')
        with self.assertRaisesRegex(ValueError,'REQUEST_REUSED'):self.f.trade()

    def test_v1_wait_still_rejected_and_no_shared_decision_mutation(self):
        self.prepare();r=copy.deepcopy(self.f.result)
        self.f.result.pop('discretionary')
        with self.assertRaisesRegex(ValueError,'SIGNAL_MISMATCH'):self.f.trade()
        self.f.result=r;self.f.trade()
        self.assertEqual(r['execution_context']['trading_engine']['signal'],'WAIT')


if __name__=='__main__':unittest.main()
