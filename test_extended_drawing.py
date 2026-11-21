import copy,sys,unittest,tempfile,sqlite3
from pathlib import Path
from types import SimpleNamespace
from contextlib import closing
from unittest.mock import Mock,patch
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
import numpy as np
import pandas as pd
from matplotlib.figure import Figure
from drawing_tools import REGISTRY,build,fib_levels,start,click
from drawing_renderer import draw
from drawing_actions import modify,travel

class ExtendedDrawingTests(unittest.TestCase):
    def setUp(self):
        self.frame=pd.DataFrame({'close':np.linspace(100,140,40)},index=pd.date_range('2026-11-12 08:00',periods=40,freq='15min'))
        self.frame.attrs.update(source='gettex',currency='EUR',venue='GETTEX',symbol='ABC')
        self.points=[dict(at=str(self.frame.index[i]),price=p) for i,p in [(0,100),(10,120),(15,110)]]
        self.info=dict(isin='fixture-A',name='Fixture',drawings=[],position={'quantity':3},levels={'support':90})
        self.app=SimpleNamespace(current_info=self.info,current_df=self.frame,chart_meta={**self.frame.attrs,'timeframe':'15m'},drafts={},refresh_chart=Mock(),status=SimpleNamespace(set=Mock()),root=None)
        self.app.instrument_cfg=lambda:self.app.current_info
        self.app.user_state=lambda i:self.app.drafts.get(i['isin'],i)

    def event(self,i,p):return SimpleNamespace(xdata=i,ydata=p)

    def test_known_fibonacci_coordinates_both_directions(self):
        up=dict(fib_levels(100,120));down=dict(fib_levels(120,100));extension=dict(fib_levels(100,120,110))
        self.assertEqual((up[0],up[.5],up[1]),(120,110,100));self.assertAlmostEqual(up[.618],107.64)
        self.assertAlmostEqual(down[.618],112.36);self.assertAlmostEqual(extension[1.618],142.36)

    def test_every_registry_tool_really_renders_and_circle_stays_round(self):
        objects=[]
        for kind,definition in REGISTRY.items():
            all_points=self.points+[dict(at=str(self.frame.index[i]),price=price) for i,price in [(20,130),(25,115),(30,135)]]
            obj=build(kind,all_points[:definition['points']],text='$ literal <text>')
            objects.append({**obj,'source_mode':'gettex','currency':'EUR','venue':'GETTEX','symbol':'ABC'})
        original=self.frame.copy(deep=True)
        self.app.ax_price=Figure(figsize=(10,5)).subplots();self.app.ax_price.set(xlim=(0,40),ylim=(80,160))
        draw(self.app,objects,self.frame,'#00c5ff');self.app.ax_price.figure.draw_without_rendering()
        self.assertEqual({i for _,i in self.app.drawing_artists},set(range(len(objects))))
        circle=next(p for p in self.app.ax_price.patches if type(p).__name__=='DataCircle')
        for width,height in [(10,5),(5,10)]:
            self.app.ax_price.figure.set_size_inches(width,height);self.app.ax_price.set_xlim(-10,20)
            bounds=circle.get_extents();self.assertAlmostEqual(bounds.width,bounds.height,places=7)
        pd.testing.assert_frame_equal(self.frame,original)

    def test_three_clicks_publish_once_and_translate_all_anchors(self):
        before=copy.deepcopy(self.info);start(self.app,'channel')
        click(self.app,self.event(0,100));click(self.app,self.event(10,120));self.assertFalse(self.app.drafts)
        click(self.app,self.event(15,110));objects=self.app.drafts['fixture-A']['drawings']
        self.assertEqual(len(objects),1);self.assertEqual(objects[0]['third']['price'],110)
        self.app.refresh_chart.assert_called_once();self.assertEqual(self.info,before)
        shifted=modify(objects,0,'price',105)
        self.assertEqual((shifted[0]['price'],shifted[0]['end']['price'],shifted[0]['third']['price']),(105,125,115))
        shifted=modify(objects,0,'at',self.frame.index[1])
        self.assertEqual(pd.Timestamp(shifted[0]['third']['at']),self.frame.index[16])
        self.assertTrue(travel(self.app,'fixture-A','undo'));self.assertEqual(self.app.drafts['fixture-A']['drawings'],[])

    def test_capture_rejects_stock_source_calendar_and_external_draft_changes(self):
        for change in ('stock','source','dates','draft'):
            with self.subTest(change=change):
                self.app.current_info=self.info;self.app.chart_meta=dict(self.frame.attrs);self.app.current_df=self.frame;self.app.drafts={}
                start(self.app,'fib');click(self.app,self.event(0,100))
                if change=='stock':self.app.current_info={**self.info,'isin':'other'}
                elif change=='source':self.app.chart_meta={**self.frame.attrs,'symbol':'other'}
                elif change=='dates':self.app.current_df=self.frame.iloc[1:]
                else:self.app.drafts={'fixture-A':{**self.info,'drawings':[{'type':'hline','price':99}]}}
                before=copy.deepcopy(self.app.drafts);click(self.app,self.event(10,120));self.assertEqual(self.app.drafts,before);self.assertIsNone(self.app.drawing_mode)

    def test_text_modal_rechecks_context_and_text_is_literal(self):
        start(self.app,'text')
        def changed(*a,**k):self.app.chart_meta={**self.frame.attrs,'symbol':'other'};return 'text'
        with patch('tkinter.simpledialog.askstring',side_effect=changed):click(self.app,self.event(1,101))
        self.assertFalse(self.app.drafts)
        obj=build('text',self.points[:1],text='$broken');self.assertEqual(obj['text'],'$broken')
        self.app.ax_price=Figure().subplots();draw(self.app,[obj],self.frame,'#00c5ff');self.app.ax_price.figure.draw_without_rendering()

    def test_degenerate_shapes_and_pending_chart_are_not_saved(self):
        for kind in ('zone','ellipse','channel','triangle','fib','fib_extension'):
            with self.subTest(kind=kind),self.assertRaises(ValueError):build(kind,[self.points[0]]*REGISTRY[kind]['points'])
        self.app._accepted_trading_chart=None;start(self.app,'fib');click(self.app,self.event(0,100));self.assertFalse(self.app.drafts);self.assertIsNone(self.app.drawing_mode)

    def test_listing_filter_and_hidden_flags_apply_to_new_objects(self):
        obj=build('fib',self.points[:2]);self.app.ax_price=Figure().subplots()
        draw(self.app,[{**obj,'venue':'other'},{**obj,'visible':False},{**obj,'locked':True}],self.frame,'#00c5ff')
        self.assertEqual(len(self.app.ax_price.lines),8);self.assertFalse(self.app.drawing_artists)

    def test_same_name_different_isin_is_not_the_accepted_chart(self):
        self.app._accepted_trading_chart=({'symbol':'Fixture','watchlist':[{'name':'Fixture','isin':'other'}]}, {})
        start(self.app,'fib');click(self.app,self.event(0,100))
        self.assertFalse(self.app.drafts);self.assertIsNone(self.app.drawing_mode)

    def test_three_anchors_survive_real_save_reopen_without_account(self):
        from storage import Store
        original=copy.deepcopy(self.info);obj=build('fib_extension',self.points)
        item={**self.info,'drawings':[obj]}
        archive=Path(__file__).resolve().parents[2]/'СМ-Архив'
        file=tempfile.NamedTemporaryFile(prefix='Extended drawing test ',suffix='.sqlite3',dir=archive if archive.is_dir() else None,delete=False);path=Path(file.name);file.close();self.addCleanup(lambda:path.unlink(missing_ok=True))
        with closing(sqlite3.connect(path)) as db:
            db.execute('CREATE TABLE instruments(isin TEXT PRIMARY KEY,payload TEXT)');db.execute('CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT)')
        store=Store.__new__(Store);store.path=path;store.pending_user_settings={};store.save_user_state([item])
        reopened=Store.__new__(Store);reopened.path=path
        saved=reopened.instruments()[0];self.assertEqual(saved['drawings'][0],obj);self.assertEqual(saved['position'],original['position']);self.assertEqual(self.info,original)

if __name__=='__main__':unittest.main()
