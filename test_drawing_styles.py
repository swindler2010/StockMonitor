import copy,json,sqlite3,sys,tempfile,unittest
from pathlib import Path
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import Mock
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
import pandas as pd
from matplotlib.figure import Figure
from drawing_styles import DEFAULT,validate,effective,template,save_template
from drawing_actions import modify,commit,travel,begin_drag
from drawing_renderer import draw

class DrawingStyleTests(unittest.TestCase):
    def setUp(self):
        self.frame=pd.DataFrame({'close':[100,101,102,103]},index=pd.date_range('2026-11-12 08:00',periods=4,freq='15min'))
        self.frame.attrs.update(source='gettex',currency='EUR')
        self.obj=dict(type='trend',price=100,at=str(self.frame.index[0]),end=dict(price=103,at=str(self.frame.index[-1])),source_mode='gettex',currency='EUR')
        self.info=dict(isin='fixture',name='Fixture',currency='EUR',drawings=[self.obj],position={'quantity':2},levels={'support':90})
        self.app=SimpleNamespace(instrument_cfg=lambda:self.info,user_state=lambda _:self.app.drafts.get('fixture',self.info),drafts={},refresh_chart=Mock(),status=SimpleNamespace(set=Mock()))

    def test_styles_change_artists_but_not_points_candles_or_position(self):
        original=copy.deepcopy(self.info);frame=self.frame.copy(deep=True)
        style={**DEFAULT,'color':'#ff0088','width':3,'linestyle':':','opacity':.6}
        changed=modify([self.obj],0,'style',style)
        fig=Figure();self.app.ax_price=fig.subplots();draw(self.app,changed,self.frame,'#60a5fa')
        line=self.app.ax_price.lines[0]
        self.assertEqual((line.get_color(),line.get_linewidth(),line.get_linestyle(),line.get_alpha()),('#ff0088',3,':',.6))
        self.assertTrue(commit(self.app,'fixture',[self.obj],changed))
        self.assertEqual(self.app.drafts['fixture']['position'],original['position']);self.assertEqual(self.app.drafts['fixture']['levels'],original['levels'])
        self.assertEqual({k:v for k,v in changed[0].items() if k!='style'},self.obj)
        self.assertEqual(self.info,original);pd.testing.assert_frame_equal(self.frame,frame)
        self.assertTrue(travel(self.app,'fixture','undo'));self.assertEqual(self.app.drafts['fixture']['drawings'],[self.obj])

    def test_legacy_rendering_and_marker_label_remain_compatible(self):
        objects=[{**self.obj,'type':kind} for kind in ('hline','vline','trend','ray','zone','marker')]
        self.app.ax_price=Figure().subplots();draw(self.app,objects,self.frame,'#123456')
        self.assertEqual(len(self.app.drawing_artists),6)
        self.assertEqual(self.app.ax_price.lines[0].get_color(),'#123456')
        self.assertEqual(self.app.ax_price.lines[1].get_linestyle(),':')
        self.assertAlmostEqual(self.app.ax_price.patches[0].get_facecolor()[3],.18)
        self.assertAlmostEqual(self.app.ax_price.patches[0].get_edgecolor()[3],.18)
        self.assertEqual(len(self.app.ax_price.texts),1)
        self.app.ax_price=Figure().subplots();draw(self.app,[{**objects[-1],'style':{**DEFAULT,'price_label':False}}],self.frame,'#123456')
        self.assertFalse(self.app.ax_price.texts)

    def test_hidden_locked_wrong_listing_and_drag_respect_original_indices(self):
        objects=[{**self.obj,'visible':False},{**self.obj,'locked':True},{**self.obj,'currency':'USD'},self.obj]
        self.app.ax_price=Figure().subplots();draw(self.app,objects,self.frame,'#123456')
        self.assertEqual(len(self.app.ax_price.lines),2);self.assertEqual([i for _,i in self.app.drawing_artists],[3])
        self.info['drawings']=[{**self.obj,'locked':True}]
        self.app.drawing_artists=[(SimpleNamespace(contains=lambda _: (True,{})),0)]
        self.assertFalse(begin_drag(self.app,SimpleNamespace(ydata=100)))
        with self.assertRaises(ValueError):modify(self.info['drawings'],0,'price',101)
        self.assertFalse(modify(self.info['drawings'],0,'locked',False)[0]['locked'])

    def test_layer_order_does_not_modify_geometry_and_lock_blocks_edits(self):
        objects=[self.obj,{**self.obj,'price':101}]
        changed=modify(objects,0,'front')
        self.assertEqual(changed[-1]['price'],100);self.assertEqual([o['stack_order'] for o in changed],[0,1])
        layered=modify(changed,1,'layer','Анализ')
        self.assertEqual(layered[1]['layer'],'Анализ');self.assertEqual(layered[1]['at'],objects[0]['at'])
        locked=modify(layered,1,'locked',True)
        for op,value in [('delete',None),('style',DEFAULT),('front',None),('layer','Другой')]:
            with self.subTest(op=op),self.assertRaises(ValueError):modify(locked,1,op,value)

    def test_invalid_styles_and_templates_cannot_carry_geometry(self):
        for bad in ({'width':float('nan')},{'color':'red;code'},{'opacity':2},{'fontsize':True},{'linestyle':'bad'},{'price':200}):
            with self.subTest(bad=bad),self.assertRaises(ValueError):validate(bad)
        self.assertEqual(effective({'type':'vline'},'#123456')['linestyle'],':')
        with self.assertRaises(ValueError):template('bad\nname',DEFAULT)
        with self.assertRaises(ValueError):save_template(self.app,'fixture',[self.obj],[],{'name':'x','style':DEFAULT,'price':200})
        self.assertFalse(save_template(self.app,'other',[self.obj],[],template('x',DEFAULT)))
        self.assertFalse(self.app.drafts)

    def test_template_and_style_survive_normal_atomic_save_and_reopen(self):
        from storage import Store
        archive=(Path(__file__).resolve().parents[2]/'СМ-Архив' if (Path(__file__).resolve().parents[2]/'СМ-Архив').is_dir() else Path(tempfile.gettempdir()))
        handle=tempfile.NamedTemporaryFile(prefix='Drawing style test ',suffix='.sqlite3',dir=archive,delete=False);path=Path(handle.name);handle.close()
        self.addCleanup(lambda:path.unlink(missing_ok=True))
        with closing(sqlite3.connect(path)) as db:
            db.execute('CREATE TABLE instruments(isin TEXT PRIMARY KEY,payload TEXT)');db.execute('CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT)')
        self.assertTrue(save_template(self.app,'fixture',[self.obj],[],template('Неон',DEFAULT)))
        changed=modify([self.obj],0,'style',{**DEFAULT,'width':4})
        self.assertTrue(commit(self.app,'fixture',[self.obj],changed))
        # Real Save implementation, isolated database with no account tables.
        store=Store.__new__(Store);store.path=path;store.pending_user_settings={}
        store.save_user_state([self.app.drafts['fixture']])
        reopened=Store.__new__(Store);reopened.path=path
        restored=reopened.instruments()[0]
        self.assertEqual(restored['drawings'][0]['style']['width'],4)
        self.assertEqual(restored['drawing_style_templates'],[template('Неон',DEFAULT)])
        self.assertEqual(restored['drawings'][0]['at'],self.obj['at'])
        self.assertEqual(restored['position'],self.info['position'])

if __name__=='__main__':unittest.main()
