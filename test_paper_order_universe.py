"""Admitted stocks appear in owner controls and share the protected executor."""
import copy,json,unittest
from types import SimpleNamespace
from unittest.mock import patch
from test_paper_owner_orders import OwnerOrders
import paper_orders as orders
import paper_hourly as hourly
import paper_account as paper


class OrderUniverse(unittest.TestCase):
    def setUp(self):
        self.base=OwnerOrders();self.base.setUp();self.f=self.base.f
        f=self.f;old=f.isin;f.isin='US0231351067'
        f.info=dict(isin=f.isin,name='Amazon',kind='stock')
        f.identity=dict(f.identity,symbol='AMZN.GTX')
        f.db.execute('UPDATE instruments SET isin=?,payload=? WHERE isin=?',
                     (f.isin,json.dumps(f.info),old))
        f.db.execute('UPDATE series SET isin=?,symbol=? WHERE isin=?',(f.isin,'AMZN.GTX',old))
        f.cfg['identities']={f.isin:f.identity};f.cfg['names']={f.isin:'Amazon'}
        f.write(hourly.CONFIG,f.cfg)
        f.write('gettex_chart:'+f.isin+':15m',dict(state='SAVED',identity=f.identity,
            listing_proof=dict(f.identity,status='CONFIRMED',isin=f.isin)))

    def tearDown(self):self.base.tearDown()

    def test_current_and_future_admission_selector_and_bridge_route(self):
        f=self.f;state=dict(status='ACTIVE',automation=f.cfg,allowed_isins=[f.isin])
        self.assertEqual(orders.order_choices(state),{f.isin:'Amazon'})
        f.cfg['enabled']=False # Pausing V1 must not hide manual orders.
        self.assertEqual(orders.order_choices(state),{f.isin:'Amazon'})
        next_isin='US30303M1027'
        f.cfg['identities'][next_isin]=dict(f.identity,symbol='FB2A.GTX')
        f.cfg['names'][next_isin]='Meta';state['allowed_isins'].append(next_isin)
        self.assertEqual(list(orders.order_choices(state)),[f.isin,next_isin])
        f.cfg['identities'][next_isin]['currency']='USD'
        self.assertNotIn(next_isin,orders.order_choices(state))
        del f.cfg['identities'][next_isin]
        self.assertNotIn(next_isin,orders.order_choices(state))
        from features import Features
        app=SimpleNamespace(store=SimpleNamespace(setting=lambda k,d:f.cfg))
        with patch('paper_orders.submit',return_value='OWNER_ROUTE') as submit:
            self.assertEqual(Features.bridge_dispatch(app,'paper_trade',dict(isin=f.isin)),'OWNER_ROUTE')
            self.assertEqual(submit.call_args.args[2],'CHATGPT')
        f.cfg['approved']=False
        self.assertEqual(orders.order_choices(state),{})
        # Routing a retained but revoked membership still reaches authority(),
        # rather than silently falling back to a different execution path.
        self.assertTrue(orders.owner_route(f.cfg,f.isin))

    def test_amazon_wait_orders_and_revocation_quality_guards(self):
        f=self.f
        for initiator in ('MANUAL','CHATGPT'):
            self.base.prepare(initiator=initiator)
            result=f.trade();self.assertEqual(result['fill']['isin'],f.isin)
            self.assertEqual(result['fill']['initiator'],initiator)
            self.assertEqual(result['fill']['smart_eligibility']['signal'],'WAIT')
            self.base.prepare(side='SELL',initiator=initiator)
            f.trade()
        count=len(paper.ledger(f.db)['fills']);self.assertEqual(count,4)
        self.base.prepare(request='AMAZON-dq-denial')
        f.result['execution_context']['trading_engine']['timeframe_context']['4H']['data_quality']['state']='UNCONFIRMED'
        with self.assertRaisesRegex(ValueError,'AUTO_DQ_BLOCKED'):f.trade()
        self.base.prepare(request='AMAZON-stale-denial')
        f.result['execution_quote']['bars'][0]['close_at']='2026-11-04T08:30:00Z'
        with self.assertRaisesRegex(ValueError,'STALE_QUOTE'):f.trade()
        f.cfg['approved']=False;f.write(hourly.CONFIG,f.cfg)
        self.base.prepare(request='AMAZON-revoked-denial')
        with self.assertRaisesRegex(ValueError,'AUTO_NOT_AUTHORIZED'):f.trade()
        self.assertEqual(len(paper.ledger(f.db)['fills']),count)
        self.assertEqual(paper.ledger(f.db)['positions'][0]['quantity'],'0')


if __name__=='__main__':unittest.main()
