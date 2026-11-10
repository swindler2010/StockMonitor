import copy,json,unittest
from unittest.mock import patch
from types import SimpleNamespace
from datetime import datetime,timezone,timedelta
from test_paper_hourly import HourlyTests
import watchlist_order as order
import data_quality as dq
from calculation_window import SCOPE_POLICY


class OrderTests(unittest.TestCase):
    def setUp(self):
        self.f=HourlyTests();self.f.setUp();f=self.f
        f.cfg['approved']=False;f.write('paper_auto_1h',f.cfg);f.result=f.result_for('WAIT')
        self.now=datetime.fromtimestamp(f.now,timezone.utc)
        self.candidate=dict(context='full_closed_reference_v1',input_binding_version=2,timeframe='1H',
            analysis_ready=True,signal_ready=False,trade_ready=False,
            closed_inputs=f.result['hourly']['proof'],expires_at=(self.now+timedelta(minutes=5)).isoformat())
        self.record=dict(isin=f.isin,checked_at=self.now.isoformat(),expires_at=self.candidate['expires_at'],
            execution_version=1,calendar_revision=dq.XFRA_SESSION_REVISION,reference_binding_version=1,
            analysis_scope=dict(policy=SCOPE_POLICY,start='2021-11-04',timezone='Europe/Berlin'),
            _input_revisions=f.result['_input_revisions'],candidates=[self.candidate],
            timeframes={'1H':dict(chart_ready=True,identity=f.identity)})
        self.save()

    def save(self):self.f.write('cp_eligibility:'+self.f.isin,self.record);self.f.db.commit()
    def tearDown(self):self.f.tearDown()
    def collect(self,now=None):
        f=self.f;changes=f.db.total_changes
        with patch.object(order.sqlite3,'connect',return_value=f.db):
            result=order.collect('unused',[f.info],now or self.now)
        self.assertEqual(f.db.total_changes,changes)
        f.db.execute('PRAGMA query_only=OFF')
        return result['rows'][f.isin]

    def test_readonly_current_analysis_and_revision_revocation(self):
        self.assertEqual(self.collect()['tier'],2)
        self.f.db.execute("UPDATE series SET close=100 WHERE base_tf='1H'");self.f.db.commit()
        row=self.collect();self.assertEqual(row['analysis'],0);self.assertEqual(row['tier'],4)

    def test_expired_and_wrong_instrument_never_ready(self):
        row=self.collect(self.now+timedelta(minutes=6));self.assertEqual(row['tier'],4)
        self.record['isin']='US67066G1040';self.save()
        row=self.collect();self.assertEqual(row['analysis'],0);self.assertEqual(row['chart'],0)

    def test_false_trade_flag_does_not_become_permission(self):
        self.candidate['trade_ready']=True;self.candidate['execution_identity']=self.f.identity;self.save()
        self.assertEqual(self.collect()['tier'],2)  # Existing execution gate rejects unsupported 1H CP receipt.

    def test_demo_wait_and_group_order_for_existing_and_future_stocks(self):
        demo=order.rank(dict(kind='stock'),{},'UNKNOWN',[],dict(enabled=True,core='READY',signal='WAIT',quote_blocker='PAPER_STALE_QUOTE'))
        self.assertIn('WAIT',demo['label']);self.assertIn('цена не готова',demo['label'])
        self.assertNotIn('допуск',demo['label']);self.assertEqual(demo['analysis'],0)
        infos=[dict(isin=str(n),name='Stock '+str(n),kind='stock') for n in range(200)]
        infos[0]['name']='Apple';infos[1]['name']='NVIDIA'
        saved=copy.deepcopy(infos)
        rows={'0':demo,'1':{**demo,'analysis':9},'2':dict(tier=2,analysis=1,chart=1)}
        ordered=order.ordered(infos,rows)
        self.assertEqual([i['isin'] for i in ordered[:3]],['0','1','2'])
        self.assertEqual(infos,saved);self.assertEqual(len(ordered),200)

    def test_ui_preserves_selection_and_does_not_move_open_dropdown(self):
        class Box(dict):
            def __str__(self):return '.box'
        box=Box(values=('NVIDIA','Apple'));calls=[];shown=[True]
        box.tk=SimpleNamespace(call=lambda *args: '.popup' if args[0].startswith('ttk') else shown[0])
        app=SimpleNamespace(symbol_box=box,watchlist=[dict(isin='n',name='NVIDIA'),dict(isin='a',name='Apple')],
            symbol=SimpleNamespace(set=lambda *a:calls.append(a)))
        run=order.Runtime(app);run.apply();self.assertEqual(box['values'],('NVIDIA','Apple'))
        shown[0]=False;run.apply();self.assertEqual(box['values'],['Apple','NVIDIA'])
        self.assertEqual(calls,[])


if __name__=='__main__':unittest.main(verbosity=2)
