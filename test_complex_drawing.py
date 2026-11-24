import copy,sys,tempfile,sqlite3,unittest
from pathlib import Path
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import Mock
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
import pandas as pd
from matplotlib.figure import Figure
from drawing_complex import gann_rays,MAX_CYCLES,PATTERNS
from drawing_tools import build,start,click
from drawing_renderer import draw
from drawing_actions import modify,travel

class ComplexDrawingTests(unittest.TestCase):
    def setUp(self):
        self.frame=pd.DataFrame({'close':range(100,140)},index=pd.date_range('2026-11-12 08:00',periods=40,freq='15min'))
        self.frame.attrs.update(source='gettex',currency='EUR',venue='GETTEX',symbol='ABC')
        self.points=[dict(at=str(self.frame.index[i]),price=p) for i,p in [(0,100),(5,120),(10,110),(15,130),(20,115),(25,135)]]
        self.info=dict(isin='fixture-A',name='Fixture',drawings=[],position={'quantity':3})
        self.app=SimpleNamespace(current_df=self.frame,chart_meta={**self.frame.attrs,'timeframe':'15m'},drafts={},refresh_chart=Mock(),status=SimpleNamespace(set=Mock()))
        self.app.instrument_cfg=lambda:self.info;self.app.user_state=lambda i:self.app.drafts.get(i['isin'],i)
        self.app.ax_price=Figure().subplots();self.app.ax_price.set(xlim=(0,40),ylim=(90,150))

    def test_calibrated_gann_slopes_and_box_clipping_are_numerical(self):
        rays={name:(a,b) for name,a,b in gann_rays((0,100),(10,120),30)}
        self.assertEqual(rays['1×1'][1],(30,160));self.assertEqual(rays['2×1'][1],(30,220));self.assertEqual(rays['1×8'][1],(30,107.5))
        for _,a,b in gann_rays((0,120),(10,100),30,boxed=True):
            self.assertTrue(0<=b[0]<=10);self.assertTrue(100<=b[1]<=120)
        with self.assertRaises(ValueError):gann_rays((0,100),(0,120),30)

    def test_gann_tools_and_wave_labels_draw_without_changing_viewport(self):
        objects=[build(kind,self.points[:2]) for kind in ('gann_fan','gann_box','gann_square')]
        objects += [build(kind,self.points[:len(labels)]) for kind,labels in PATTERNS.items()]
        limits=(self.app.ax_price.get_xlim(),self.app.ax_price.get_ylim());before=self.frame.copy(deep=True)
        draw(self.app,objects,self.frame,'#00c5ff');self.app.ax_price.figure.draw_without_rendering()
        self.assertEqual({i for _,i in self.app.drawing_artists},set(range(len(objects))))
        self.assertEqual((self.app.ax_price.get_xlim(),self.app.ax_price.get_ylim()),limits)
        texts=[t.get_text() for t in self.app.ax_price.texts]
        self.assertIn('5',texts);self.assertIn('E',texts);self.assertTrue(any('EUR/бар' in t for t in texts))
        pd.testing.assert_frame_equal(self.frame,before)

    def test_logarithmic_scale_is_explicitly_rejected_for_gann(self):
        self.app.ax_price.set_yscale('log');draw(self.app,[build('gann_fan',self.points[:2])],self.frame,'#00c5ff')
        self.assertFalse(self.app.ax_price.lines);self.assertTrue(any('линейная' in t.get_text() for t in self.app.ax_price.texts))

    def test_cycle_output_is_bounded(self):
        frame=self.frame.reindex(pd.date_range(self.frame.index[0],periods=5000,freq='15min'));frame.attrs=self.frame.attrs.copy()
        obj=build('cycles',[self.points[0],{'at':str(frame.index[1]),'price':100}])
        draw(self.app,[obj],frame,'#00c5ff')
        self.assertEqual(len(self.app.ax_price.lines),MAX_CYCLES)
        self.assertTrue(any('64' in t.get_text() for t in self.app.ax_price.texts))

    def test_six_clicks_publish_once_and_all_vertices_move_together(self):
        before=copy.deepcopy(self.info);start(self.app,'elliott_impulse')
        for n,p in enumerate(self.points):
            click(self.app,SimpleNamespace(xdata=n*5,ydata=p['price']))
            if n<5:self.assertFalse(self.app.drafts)
        objects=self.app.drafts['fixture-A']['drawings'];self.assertEqual(len(objects),1);self.assertEqual(len(objects[0]['vertices']),6)
        self.app.refresh_chart.assert_called_once();self.assertEqual(self.info,before)
        shifted=modify(objects,0,'price',105)[0]
        self.assertEqual(shifted['price'],105);self.assertEqual([p['price'] for p in shifted['vertices']],[p['price']+5 for p in self.points])
        dated=modify(objects,0,'at',self.frame.index[1])[0]
        self.assertEqual(pd.Timestamp(dated['vertices'][-1]['at']),self.frame.index[26])
        self.assertEqual(dated['at'],dated['vertices'][0]['at'])
        self.assertTrue(travel(self.app,'fixture-A','undo'));self.assertEqual(self.app.drafts['fixture-A']['drawings'],[])

    def test_changing_context_mid_wave_does_not_publish_partial_pattern(self):
        start(self.app,'elliott_impulse')
        for n in range(5):click(self.app,SimpleNamespace(xdata=n*5,ydata=self.points[n]['price']))
        self.app.chart_meta={**self.app.chart_meta,'symbol':'other'}
        click(self.app,SimpleNamespace(xdata=25,ydata=135));self.assertFalse(self.app.drafts);self.assertIsNone(self.app.drawing_mode)

    def test_reverse_time_and_invalid_gann_calibration_cannot_be_built(self):
        with self.assertRaises(ValueError):build('elliott_impulse',list(reversed(self.points)))
        with self.assertRaises(ValueError):build('gann_box',[self.points[0],{**self.points[1],'price':100}])
        obj=build('xabcd',self.points[:5]);self.assertEqual(obj['pattern_mode'],'manual_labels_only')
        self.assertNotIn('signal',obj)

    def test_six_points_survive_real_atomic_save_and_reopen(self):
        from storage import Store
        archive=Path(__file__).resolve().parents[2]/'СМ-Архив'
        file=tempfile.NamedTemporaryFile(prefix='Complex drawing test ',suffix='.sqlite3',dir=archive if archive.is_dir() else None,delete=False);path=Path(file.name);file.close();self.addCleanup(lambda:path.unlink(missing_ok=True))
        with closing(sqlite3.connect(path)) as db:
            db.execute('CREATE TABLE instruments(isin TEXT PRIMARY KEY,payload TEXT)');db.execute('CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT)')
        obj=build('elliott_impulse',self.points);item={**self.info,'drawings':[obj]}
        store=Store.__new__(Store);store.path=path;store.pending_user_settings={};store.save_user_state([item])
        reopened=Store.__new__(Store);reopened.path=path;saved=reopened.instruments()[0]
        self.assertEqual(saved['drawings'],[obj]);self.assertEqual(saved['position'],self.info['position'])

if __name__=='__main__':unittest.main()
