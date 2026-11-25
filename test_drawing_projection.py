import copy,sys,unittest,math
from pathlib import Path
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
import numpy as np
from matplotlib.backends.backend_agg import FigureCanvasAgg
from drawing_projection import KINDS,RULE,position_levels,ScreenSpiral,ScreenAngle
from drawing_tools import build,start,click,REGISTRY
from drawing_renderer import draw
from drawing_actions import modify,travel
import test_extended_drawing as fixture


class ManualProjectionTests(unittest.TestCase):
    setUp=fixture.ExtendedDrawingTests.setUp
    event=fixture.ExtendedDrawingTests.event

    def points(self,kind):
        points=copy.deepcopy(self.points_fixture)
        if kind=='long_position':points[1]['price']=90;points[2]['price']=120
        if kind=='short_position':points[1]['price']=110;points[2]['price']=80
        return points[:REGISTRY[kind]['points']]

    def setUp(self):
        fixture.ExtendedDrawingTests.setUp(self);self.points_fixture=self.points;del self.points

    def objects(self):
        return [{**build(k,self.points(k),text='$literal'),**dict(source_mode='gettex',currency='EUR',venue='GETTEX',symbol='ABC')} for k in KINDS]

    def test_all_new_kinds_render_without_touching_history_or_limits(self):
        from matplotlib.figure import Figure
        self.app.ax_price=Figure(figsize=(10,5)).subplots();self.app.ax_price.set(xlim=(0,40),ylim=(70,160))
        canvas=FigureCanvasAgg(self.app.ax_price.figure);before=self.frame.copy(deep=True)
        draw(self.app,self.objects(),self.frame,'#00c5ff');canvas.draw()
        self.assertEqual({i for _,i in self.app.drawing_artists},set(range(len(KINDS))))
        self.assertEqual(self.app.ax_price.get_xlim(),(0,40));self.assertEqual(self.app.ax_price.get_ylim(),(70,160))
        import pandas as pd
        pd.testing.assert_frame_equal(self.frame,before)

    def test_spiral_outer_endpoint_tracks_B_after_zoom_and_resize(self):
        from matplotlib.figure import Figure
        ax=Figure(figsize=(8,4)).subplots();item=ScreenSpiral(ax,(0,100),(10,120))
        for limits,size in [((0,40),(8,4)),((-5,15),(4,8))]:
            ax.set(xlim=limits,ylim=(70,160));ax.figure.set_size_inches(*size)
            end=item.get_transform().transform(item.get_path().vertices[-1])
            np.testing.assert_allclose(end,ax.transData.transform((10,120)),atol=1e-7)
        self.assertEqual(len(item.get_path().vertices),321)

    def test_angle_is_screen_based_and_updates_with_aspect(self):
        from matplotlib.figure import Figure
        ax=Figure(figsize=(8,4)).subplots();ax.set(xlim=(0,40),ylim=(70,160));canvas=FigureCanvasAgg(ax.figure)
        item=ScreenAngle(ax,(0,100),(10,120));ax.add_artist(item);canvas.draw();first=item.get_text()
        ax.figure.set_size_inches(4,8);canvas.draw();self.assertNotEqual(first,item.get_text())

    def test_invalid_positions_and_projections_are_rejected(self):
        self.assertEqual(position_levels('long_position',100,95,110),(5,10,2))
        self.assertEqual(position_levels('short_position',100,105,90),(5,10,2))
        for k in ('long_position','short_position'):
            for price in (100,float('nan'),-1):
                pts=self.points(k);pts[1]['price']=price
                with self.assertRaises(ValueError):build(k,pts)
            pts=self.points(k);pts[2]['at']=pts[0]['at']
            with self.assertRaises(ValueError):build(k,pts)
        pts=self.points('price_projection');pts[1]['price']=1;pts[2]['price']=1
        with self.assertRaises(ValueError):build('price_projection',pts)
        pts=self.points('time_projection');pts[1]['at']=pts[0]['at']
        with self.assertRaises(ValueError):build('time_projection',pts)

    def test_capture_publish_undo_and_context_rejection_for_new_kinds(self):
        for k in KINDS:
            self.app.drafts={};self.app.chart_meta=dict(self.frame.attrs);start(self.app,k)
            pts=self.points(k)
            if k=='callout':continue # modal is separately checked below
            for p in pts:click(self.app,self.event(self.frame.index.get_loc(p['at']),p['price']))
            obj=self.app.drafts['fixture-A']['drawings'][0]
            self.assertEqual(obj['annotation_rule'],RULE)
            moved=modify([obj],0,'price',obj['price']+1);self.assertEqual(moved[0]['price'],obj['price']+1)
            self.assertTrue(travel(self.app,'fixture-A','undo'));self.assertEqual(self.app.drafts['fixture-A']['drawings'],[])
        self.app.drafts={};start(self.app,'time_projection');click(self.app,self.event(0,100))
        self.app.chart_meta['symbol']='other';click(self.app,self.event(10,120));self.assertFalse(self.app.drafts)

    def test_callout_literal_text_and_modal_context_guard(self):
        from unittest.mock import patch
        start(self.app,'callout')
        with patch('tkinter.simpledialog.askstring',return_value='$literal <test>'):click(self.app,self.event(0,100))
        obj=self.app.drafts['fixture-A']['drawings'][0];self.assertEqual(obj['text'],'$literal <test>')
        self.assertEqual(modify([obj],0,'text','new')[0]['text'],'new')
        self.app.drafts={};start(self.app,'callout')
        def stale(*a,**k):self.app.chart_meta['symbol']='other';return 'bad'
        with patch('tkinter.simpledialog.askstring',side_effect=stale):click(self.app,self.event(0,100))
        self.assertFalse(self.app.drafts)

    def test_roundtrip_uses_existing_store_and_preserves_position(self):
        import tempfile,sqlite3,json
        from storage import Store
        from contextlib import closing
        archive=(Path(__file__).resolve().parents[2]/'СМ-Архив' if (Path(__file__).resolve().parents[2]/'СМ-Архив').is_dir() else Path(tempfile.gettempdir()))
        f=tempfile.NamedTemporaryFile(prefix='Projection isolated ',suffix='.sqlite3',dir=archive,delete=False);path=Path(f.name);f.close();self.addCleanup(lambda:path.unlink(missing_ok=True))
        with closing(sqlite3.connect(path)) as db:
            db.execute('create table instruments(isin primary key,payload)');db.execute('create table settings(key primary key,value)')
        store=Store.__new__(Store);store.path=path;store.pending_user_settings={}
        info={**self.info,'drawings':self.objects()};store.save_user_state([info])
        reopened=Store.__new__(Store);reopened.path=path;row=reopened.instruments()[0]
        self.assertEqual(row['drawings'],info['drawings']);self.assertEqual(row['position'],self.info['position'])

class CrossScreenStyleTests(unittest.TestCase):
    def test_rounded_combobox_keeps_canonical_values_in_all_languages(self):
        import tkinter as tk
        from tkinter import ttk
        import i18n
        from indicator_dialog import styles
        from localized_widgets import CanonicalCombobox
        from themes import THEMES
        root=tk.Tk();root.withdraw();self.addCleanup(root.destroy)
        prior=i18n._language;self.addCleanup(lambda:setattr(i18n,'_language',prior))
        for language in ('ru','de','en'):
            i18n._language=language
            for palette in THEMES.values():
                prefix=styles(root,palette);variable=tk.StringVar(root,'Фибоначчи')
                box=CanonicalCombobox(root,textvariable=variable,values=['Фибоначчи','Все группы'],state='readonly',style=prefix+'.TCombobox')
                box.pack();root.update_idletasks();self.assertEqual(box.get(),'Фибоначчи')
                box.set('Все группы');self.assertEqual(variable.get(),'Все группы')
                self.assertIn(prefix+'Field',repr(ttk.Style(root).layout(prefix+'.TCombobox')))
                box.destroy()

if __name__=='__main__':unittest.main()
