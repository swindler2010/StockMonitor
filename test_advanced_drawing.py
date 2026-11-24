import copy,sys,unittest,tempfile,sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from contextlib import closing
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
import numpy as np
import pandas as pd
from matplotlib.figure import Figure
from drawing_tools import start,build,click
from drawing_freeform import press,motion,release,abort,MAX_POINTS
from drawing_regression import fit
from drawing_renderer import draw
from drawing_actions import modify,travel


class AdvancedDrawingTests(unittest.TestCase):
    def setUp(self):
        self.frame=pd.DataFrame({'close':100+2*np.arange(30,dtype=float)},index=pd.date_range('2026-11-20 08:00',periods=30,freq='15min'))
        self.frame.attrs.update(source='gettex',currency='EUR',venue='GETTEX',symbol='ABC')
        self.info=dict(isin='fixture',name='Fixture',drawings=[],position={'quantity':3})
        self.app=SimpleNamespace(current_df=self.frame,chart_meta={**self.frame.attrs,'timeframe':'15m'},drafts={},refresh_chart=Mock(),
            status=SimpleNamespace(set=Mock()),canvas=SimpleNamespace(draw_idle=Mock()),closing=False)
        self.app.instrument_cfg=lambda:self.info;self.app.user_state=lambda info:self.app.drafts.get(info['isin'],info)
        self.app.ax_price=Figure().subplots();self.app.ax_price.set(xlim=(0,30),ylim=(90,170));self.jobs=[]
        self.app.job=lambda work,done,failed,**meta:self.jobs.append((work,done,failed,meta)) or True

    def event(self,x,y,dbl=False,button=1):return SimpleNamespace(inaxes=self.app.ax_price,xdata=x,ydata=y,button=button,dblclick=dbl)

    def test_brush_drag_commits_once_on_release_and_undo_is_shared(self):
        start(self.app,'brush');press(self.app,self.event(0,100))
        for x,y in [(2,105),(4,103),(6,110)]:motion(self.app,self.event(x,y))
        self.assertFalse(self.app.drafts);release(self.app,self.event(7,111))
        objects=self.app.drafts['fixture']['drawings'];self.assertEqual(len(objects),1);self.assertEqual(len(objects[0]['vertices']),5)
        self.assertEqual(self.app.refresh_chart.call_count,1);self.assertTrue(travel(self.app,'fixture','undo'))
        self.assertFalse(self.app.drafts['fixture']['drawings'])

    def test_desktop_mouse_hooks_capture_stroke_without_pan_or_duplicate_commit(self):
        from desktop014 import Desktop014
        start(self.app,'brush');Desktop014.on_press(self.app,self.event(0,100))
        Desktop014._process_motion(self.app,self.event(3,105));Desktop014.on_release(self.app,self.event(5,110))
        self.assertEqual(len(self.app.drafts['fixture']['drawings']),1)
        self.assertEqual(self.app.refresh_chart.call_count,1)

    def test_polygon_double_click_finishes_and_polyline_can_go_backwards(self):
        for kind in ('polygon','polyline'):
            self.app.drafts={};start(self.app,kind)
            for x,y in [(0,100),(8,110),(4,120)]:press(self.app,self.event(x,y))
            self.assertFalse(self.app.drafts);press(self.app,self.event(4,120,dbl=True))
            obj=self.app.drafts['fixture']['drawings'][0];self.assertEqual(len(obj['vertices']),3)
            shifted=modify([obj],0,'price',105)[0]
            self.assertEqual(shifted['vertices'][0]['price'],shifted['price'])

    def test_cancel_bad_release_and_changed_context_do_not_save_partial_stroke(self):
        start(self.app,'brush');press(self.app,self.event(0,100));motion(self.app,self.event(2,105));abort(self.app)
        self.assertFalse(self.app.drafts);self.assertIsNone(self.app._free_preview)
        start(self.app,'brush');press(self.app,self.event(0,100));self.app.chart_meta['currency']='USD'
        release(self.app,self.event(2,105));self.assertFalse(self.app.drafts)

    def test_freeform_limit_and_degenerate_polygon_are_explicit(self):
        points=[dict(at=str(self.frame.index[0]),price=100+n*.01) for n in range(MAX_POINTS)]
        start(self.app,'polyline');press(self.app,self.event(0,100));self.app._drawing_capture['points']=points
        press(self.app,self.event(1,110));self.assertEqual(len(self.app._drawing_capture['points']),MAX_POINTS)
        self.assertIn('256',self.app.status.set.call_args.args[0])
        with self.assertRaises(ValueError):build('brush',points+[points[0]])
        with self.assertRaises(ValueError):build('polygon',[dict(at=str(self.frame.index[i]),price=100+i) for i in range(3)])

    def test_regression_numeric_fit_residual_width_and_input_bounds(self):
        result=fit([100,102,104,106]);self.assertEqual(result['start'],100);self.assertEqual(result['end'],106);self.assertEqual(result['half_width'],0)
        noisy=fit([100,104,102,108]);self.assertGreater(noisy['sigma'],0)
        x=np.arange(4);y=np.array([100,104,102,108]);coeff=np.polyfit(x,y,1);res=y-np.polyval(coeff,x)
        self.assertAlmostEqual(noisy['half_width'],2*np.std(res))
        for bad in ([1,2],[1,np.nan,2],[1,-1,2],np.ones(5001)):
            with self.assertRaises(ValueError):fit(bad)

    def regression(self):
        start(self.app,'regression');click(self.app,self.event(0,100));click(self.app,self.event(10,120))
        self.assertEqual(len(self.jobs),1);self.assertFalse(self.app.drafts)
        return self.jobs.pop()

    def test_regression_uses_worker_and_rechecks_values_context_generation_and_close(self):
        work,done,failed,meta=self.regression();self.assertEqual(meta['lane'],'chart');result=work()
        self.app.current_df.loc[self.frame.index[5],'close']=111
        done(result);self.assertFalse(self.app.drafts)
        self.app.current_df=self.frame.copy();work,done,_,_=self.regression();result=work();start(self.app,'trend');done(result);self.assertFalse(self.app.drafts)
        work,done,_,_=self.regression();result=work();self.app.closing=True;done(result);self.assertFalse(self.app.drafts)

    def test_53_200_background_jobs_do_not_starve_the_interactive_chart_calculation(self):
        import threading
        from performance_dispatcher import Dispatcher
        for count in (53,200):
            with self.subTest(count=count):
                self.app.drafts={};gate=threading.Event();arrived=threading.Event();callbacks=[];started=[threading.Event() for _ in range(3)]
                def deliver(callback,value):callbacks.append((callback,value));arrived.set()
                dispatcher=Dispatcher(deliver,workers=4,sample=lambda _:({'cpu':0,'ram':0},None))
                try:
                    for n,lane in enumerate(('read','feed_update','snapshot')):
                        def hold(n=n):started[n].set();gate.wait(5)
                        dispatcher.submit(hold,lambda _:None,lambda _:None,lane=lane,key=('hold',n))
                    self.assertTrue(all(event.wait(2) for event in started))
                    for n in range(count):dispatcher.submit(lambda:None,lambda _:None,lambda _:None,key=('background',n),lane='misc')
                    self.app.job=lambda work,done,failed,**meta:dispatcher.submit(work,done,failed,**meta)
                    start(self.app,'regression');click(self.app,self.event(0,100));click(self.app,self.event(10,120))
                    self.assertTrue(arrived.wait(2),'Interactive chart job waited behind background jobs')
                    callback,result=callbacks[0];callback(result)
                    self.assertEqual(self.app.drafts['fixture']['drawings'][0]['type'],'regression')
                finally:
                    gate.set();dispatcher.close()
                    for worker in dispatcher.threads:worker.join(timeout=2)
                    dispatcher.monitor.join(timeout=2)

    def test_regression_and_shapes_draw_without_autoscale_and_persist_as_drafts(self):
        work,done,_,_=self.regression();done(work());obj=self.app.drafts['fixture']['drawings'][0]
        self.assertEqual(obj['regression']['samples'],11);self.assertEqual(obj['regression_tf'],'15m')
        self.assertTrue(modify([obj],0,'price',101)[0]['regression_transformed'])
        points=[dict(at=str(self.frame.index[x]),price=y) for x,y in [(0,100),(8,110),(4,120)]]
        objects=[obj,build('polygon',points),build('brush',points),build('polyline',points)]
        limits=(self.app.ax_price.get_xlim(),self.app.ax_price.get_ylim());before=self.frame.copy(deep=True)
        draw(self.app,objects,self.frame,'#00c5ff');self.app.ax_price.figure.draw_without_rendering()
        self.assertEqual((self.app.ax_price.get_xlim(),self.app.ax_price.get_ylim()),limits)
        self.assertEqual({i for _,i in self.app.drawing_artists},{0,1,2,3});pd.testing.assert_frame_equal(self.frame,before)
        from storage import Store
        archive=Path(__file__).resolve().parents[2]/'СМ-Архив';f=tempfile.NamedTemporaryFile(prefix='Advanced drawing test ',suffix='.sqlite3',dir=archive if archive.is_dir() else None,delete=False)
        path=Path(f.name);f.close();self.addCleanup(lambda:path.unlink(missing_ok=True))
        with closing(sqlite3.connect(path)) as db:
            db.execute('create table instruments(isin text primary key,payload text)');db.execute('create table settings(key text primary key,value text)')
        store=Store.__new__(Store);store.path=path;store.pending_user_settings={};store.save_user_state([{**self.info,'drawings':objects}])
        saved=store.instruments()[0];self.assertEqual(saved['drawings'],objects);self.assertEqual(saved['position'],self.info['position'])


class AdvancedDrawingUITests(unittest.TestCase):
    def test_channels_freeform_and_calculator_actions_reuse_the_existing_panel(self):
        from test_drawing_dialog import DrawingDialogTests
        DrawingDialogTests.setUp(self)
        from unittest.mock import patch
        self.d.tool_group.set('Каналы');self.root.update()
        self.assertTrue(self.d.tool_buttons['regression'].winfo_ismapped())
        self.d.tool_group.set('Свободная разметка');self.root.update()
        for kind in ('brush','polyline','polygon'):self.assertTrue(self.d.tool_buttons[kind].winfo_ismapped())
        self.assertIn('Esc',self.d.group_caption.get())
        with patch('position_calculator.show') as opened:
            self.d.tabs['Калькулятор позиции'].invoke();opened.assert_called_once_with(self.app)
        self.assertFalse(self.drafts)

if __name__=='__main__':unittest.main()
