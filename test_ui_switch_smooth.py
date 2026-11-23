"""Regression: no per-stock SQL on a switch, no blank TkAgg resize frame."""
import sys,time,unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import Mock,patch
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))


class FocusWithoutPolicyReads(unittest.TestCase):
    def test_53_200_positions_match_full_owner_scope_without_policy_or_sql(self):
        from benchmark_ui import position_focus
        from eur_listings import owner_history_info
        for count in (53,200):
            with self.subTest(count=count):
                infos=[dict(isin=str(n),position={'quantity':1 if n%7==0 else 0}) for n in range(count)]
                drafts={'0':{**infos[0],'position':{'quantity':0}},'1':{**infos[1],'position':{'quantity':2}}}
                app=NS(watchlist=infos,drafts=drafts,store=Mock(),user_state=Mock(side_effect=AssertionError('No per-stock policy read')))
                for policy in ({},{'version':1,'approved':True,'scope':'registered_stocks','exclusive_venue':'GETTEX'}):
                    expected=[i['isin'] for i in infos if float(owner_history_info(drafts.get(i['isin'],i),policy)['position']['quantity'])!=0]
                    self.assertEqual(position_focus(app),expected)
                app.user_state.assert_not_called();self.assertEqual(app.store.mock_calls,[])
                self.assertEqual(infos[0]['position']['quantity'],1)

    def test_actual_refresh_keeps_guards_and_schedules_only_the_requested_read(self):
        from benchmark_ui import BenchmarkMixin
        infos=[dict(isin=str(n),name='Stock '+str(n),position={'quantity':n==7}) for n in range(200)]
        request=Mock(running=False)
        app=NS(watchlist=infos,drafts={},instrument_cfg=lambda:infos[0],
               timeframe=NS(get=lambda:'1H'),period=NS(get=lambda:'Max'),
               _benchmark_opened_isin='0',dispatcher=Mock(),bridge=None,
               user_state=Mock(side_effect=AssertionError('No policy fan-out')),
               store=Mock(),closing=False,_chart_jobs=request,current_quality_signature=lambda:'revisions',
               relative_status=Mock(),status=Mock(),compute_chart=Mock())
        values=dict(watchlist=[infos[0]],timeframe='1H',period='Max')
        with patch('chart_worker.capture',return_value=values),patch('trading_view.bind_engine') as bind:
            BenchmarkMixin.refresh_chart(app)
        app.dispatcher.focus.assert_called_once_with('0',['7'])
        app.user_state.assert_not_called();request.request.assert_called_once()
        app.compute_chart.calculate.assert_not_called();app.store.assert_not_called()
        self.assertIsNone(app._accepted_trading_chart);bind.assert_called_once_with(app,None)
        with patch('chart_worker.capture',return_value=values):
            app._retry_current_chart=Mock();app._render_current_display=Mock();app.store.read_cache.current.return_value=False
            BenchmarkMixin._apply_chart(app,(values,{'_input_revisions':{'market:0:15m':3}}))
        app._retry_current_chart.assert_called_once_with(values);app._render_current_display.assert_not_called()


class StableBitmapTests(unittest.TestCase):
    def fixture(self):
        import tkinter as tk
        from matplotlib.figure import Figure
        from smooth_canvas import FigureCanvasTkAgg
        root=tk.Tk();root.withdraw();self.addCleanup(root.destroy)
        fig=Figure(figsize=(2,1),dpi=50,facecolor='#ff0000')
        canvas=FigureCanvasTkAgg(fig,master=root);canvas.draw()
        self.addCleanup(lambda:canvas.get_tk_widget().destroy() if canvas.get_tk_widget().winfo_exists() else None)
        return root,fig,canvas

    def test_resize_retains_old_pixels_until_completed_swap_and_keeps_viewport(self):
        root,fig,canvas=self.fixture();ax=fig.add_subplot();ax.plot([1,2,3],[7,8,6]);ax.set_xlim(1.2,2.8);ax.set_ylim(5,9);canvas.draw()
        old=canvas._tkphoto;item=canvas._tkcanvas_image_region;limits=(ax.get_xlim(),ax.get_ylim())
        canvas.resize(NS(width=175,height=90))
        self.assertIs(canvas._tkphoto,old);self.assertEqual(canvas._tkcanvas.itemcget(item,'image'),str(old))
        self.assertEqual((old.width(),old.height()),(100,50));self.assertEqual(limits,(ax.get_xlim(),ax.get_ylim()))
        canvas.draw()
        self.assertIsNot(canvas._tkphoto,old);self.assertEqual(canvas._tkcanvas_image_region,item)
        self.assertEqual((canvas._tkphoto.width(),canvas._tkphoto.height()),(175,90));self.assertEqual(limits,(ax.get_xlim(),ax.get_ylim()))
        self.assertEqual(canvas._tkcanvas.coords(item),[87.5,45.]);self.assertEqual(canvas._tkcanvas.find_all(),(item,))
        self.assertEqual(canvas._tkphoto.get(1,1),(255,0,0))

    def test_53_200_layout_events_schedule_one_frame_without_clearing_visible_image(self):
        for count in (53,200):
            with self.subTest(count=count):
                root,fig,canvas=self.fixture();old=canvas._tkphoto;item=canvas._tkcanvas_image_region
                with patch.object(canvas,'draw',wraps=canvas.draw) as draw:
                    for n in range(count):canvas.resize(NS(width=140+n%9,height=90+n%7));canvas.draw_idle()
                    first=canvas._idle_draw_id;canvas.draw_idle();self.assertEqual(canvas._idle_draw_id,first)
                    self.assertEqual(draw.call_count,0);self.assertIs(canvas._tkphoto,old)
                    self.assertEqual(canvas._tkcanvas.itemcget(item,'image'),str(old))
                    time.sleep(.025);root.update()
                    self.assertEqual(draw.call_count,1);self.assertIsNone(canvas._idle_draw_id)
                canvas.get_tk_widget().destroy();root.destroy()
                self._cleanups.pop();self._cleanups.pop()

    def test_failed_render_preserves_original_visible_image_and_allows_retry(self):
        from smooth_canvas import BaseCanvas
        root,fig,canvas=self.fixture();old=canvas._tkphoto;item=canvas._tkcanvas_image_region
        canvas.resize(NS(width=170,height=100))
        with patch.object(BaseCanvas,'draw',side_effect=RuntimeError('isolated render failure')):
            with self.assertRaisesRegex(RuntimeError,'isolated'):canvas.draw()
        self.assertIs(canvas._tkphoto,old);self.assertEqual(canvas._tkcanvas.itemcget(item,'image'),str(old));self.assertEqual(old.get(1,1),(255,0,0))
        canvas.draw();self.assertEqual((canvas._tkphoto.width(),canvas._tkphoto.height()),(170,100))

    def test_destroy_cancels_pending_frame_without_late_tk_callback(self):
        root,fig,canvas=self.fixture();canvas.draw_idle();timer=canvas._idle_draw_id
        self.assertTrue(timer);canvas.get_tk_widget().destroy();self.assertIsNone(canvas._idle_draw_id)
        self.assertNotIn(timer,root.tk.call('after','info'));root.update()


if __name__=='__main__':unittest.main()
