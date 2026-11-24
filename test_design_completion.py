import copy
import json
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).parent/'stock_monitor'))
from drawing_management import batch, selection, export_document
from drawing_actions import History, finish_drag
from design_layout import dimensions
from news_sentiment import classify, summary
from premarket_view import project, present


def objects():
    return [dict(type='trend',at='2026-11-20 08:00',price=100,
                 end=dict(at='2026-11-20 09:00',price=105),currency='EUR',source_mode='GETTEX'),
            dict(type='hline',at='2026-11-20 08:00',price=95,currency='EUR',source_mode='GETTEX'),
            dict(type='marker',at='2026-11-20 09:00',price=107)]


class DrawingManagementTests(unittest.TestCase):
    def test_group_actions_expand_members_and_remain_one_undo_step(self):
        before=objects();original=copy.deepcopy(before)
        grouped=batch(before,[0,1],'group','Levels')
        self.assertEqual(selection(grouped,[0]),[0,1])
        hidden=batch(grouped,[0],'visible',False)
        self.assertFalse(hidden[0]['visible']);self.assertFalse(hidden[1]['visible'])
        self.assertNotIn('visible',hidden[2]);self.assertEqual(before,original)
        history=History();history.change(grouped,hidden)
        self.assertEqual(history.undo(hidden),grouped);self.assertEqual(history.redo(grouped),hidden)
        self.assertNotIn('group_id',batch(grouped,[0],'ungroup')[1])

    def test_locked_member_rejects_entire_delete_style_and_move(self):
        grouped=batch(objects(),[0,1],'group','G');grouped[1]['locked']=True
        original=copy.deepcopy(grouped)
        for operation,value in [('delete',None),('layer','X'),('style',{}),('front',None),('ungroup',None)]:
            with self.assertRaises(ValueError):batch(grouped,[0],operation,value)
            self.assertEqual(grouped,original)
        unlocked=batch(grouped,[0],'locked',False)
        self.assertFalse(unlocked[0]['locked']);self.assertFalse(unlocked[1]['locked'])

    def test_duplicate_group_has_new_membership_and_bulk_order_is_stable(self):
        grouped=batch(objects(),[0,1],'group','G')
        copied=batch(grouped,[0],'copy')
        self.assertEqual(len(copied),5)
        self.assertNotEqual(copied[3]['group_id'],grouped[0]['group_id'])
        self.assertEqual(selection(copied,[3]),[3,4])
        ordered=batch(grouped,[0],'front')
        self.assertEqual([o['type'] for o in ordered],['marker','trend','hline'])
        self.assertEqual([o['stack_order'] for o in ordered],[0,1,2])

    def test_filtered_selection_exports_group_without_private_instrument_fields(self):
        grouped=batch(objects(),[0,1],'group','G')
        exported=json.loads(export_document(dict(isin='A',position={'quantity':123},api_key='SECRET'),('gettex','GETTEX','EUR','A','4H','15m'),grouped,[1]))
        self.assertEqual(len(exported['drawings']),2)
        self.assertEqual(exported['chart']['timeframe'],'4H')
        self.assertNotIn('SECRET',json.dumps(exported));self.assertNotIn('position',exported['instrument'])
        with self.assertRaises(ValueError):export_document({},(),[{'price':float('nan')}])
        with self.assertRaises(ValueError):batch(grouped,[True],'delete')

    def test_group_drag_moves_all_points_and_does_not_write_when_identity_changes(self):
        import pandas as pd
        from drawing_actions import chart_identity
        before=batch(objects(),[0,1],'group','G');dates=pd.date_range('2026-11-20 08:00',periods=3,freq='h')
        state={'drawings':before};app=SimpleNamespace(instrument_cfg=lambda:{'isin':'A'},user_state=lambda _:state,
            current_df=SimpleNamespace(index=dates),chart_meta={},ax_price=object(),drafts={},status=Mock(),refresh_chart=Mock())
        def save():app._drawing_drag=dict(isin='A',expected=before,index=0,x=0,y=100,dates=dates,meta=chart_identity(app))
        save();event=SimpleNamespace(inaxes=app.ax_price,xdata=1,ydata=102)
        finish_drag(app,event)
        changed=app.drafts['A']['drawings'];self.assertEqual(changed[0]['price'],102);self.assertEqual(changed[1]['price'],97)
        self.assertEqual(changed[0]['end']['price'],107);self.assertEqual(changed[2],before[2])
        app.drafts.clear();save();app.chart_meta={'source':'other'};finish_drag(app,event);self.assertFalse(app.drafts)


class InformationalFieldsTests(unittest.TestCase):
    def test_sentiment_uses_original_title_and_does_not_turn_mixed_or_negated_into_positive(self):
        self.assertEqual(classify({'original_title':'Shares surge after results','title':'Акции падают'}),'positive')
        for title in ['Shares surge, forecasts tumble','Does not raise forecast','Quarterly results','Bad \ufffd text']:
            self.assertEqual(classify({'title':title}),'unknown')
        self.assertEqual(classify({'title':'Why ASML stock fell today'}),'negative')
        self.assertEqual(classify({'title':'NVIDIA shares rise after results'}),'positive')
        data=summary([{'title':'Shares surge'},{'title':'Shares plunge'},{'title':'News'}])
        self.assertEqual(data['counts'],dict(positive=1,negative=1,neutral=0,unknown=1))
        self.assertAlmostEqual(sum(data['percentages'].values()),100)
        self.assertEqual(summary([])['total'],0)

    def test_premarket_is_separate_listing_currency_and_archive_with_timestamp(self):
        at=datetime(2026,11,24,12,tzinfo=timezone.utc);info={'profile_symbol':'AAPL','currency':'EUR'}
        card={'symbol':'AAPL','source':'Yahoo Finance (через yfinance), не GETTEX',
              'overview':dict(symbol='AAPL',exchange='NMS',currency='USD',preMarketPrice=200,preMarketTime=at.timestamp()-1800,preMarketChangePercent=-.5)}
        result=project(info,card,at);self.assertEqual(result['currency'],'USD');self.assertTrue(result['archived'])
        text,detail,change=present(info,card,at);self.assertIn('USD',text);self.assertIn('NMS',detail);self.assertEqual(change,-.5)
        self.assertIsNone(project({'profile_symbol':'OTHER'},card,at))
        for key,value in [('preMarketPrice',float('nan')),('preMarketTime',at.timestamp()+1),('exchange',None),('symbol','OTHER')]:
            invalid=copy.deepcopy(card);invalid['overview'][key]=value;self.assertIsNone(project(info,invalid,at))
        self.assertIsNone(project(info,{},at))

    def test_reference_dialog_sizes_fit_laptop_and_large_screen(self):
        for sw,sh in [(1024,768),(1422,892),(1920,1080)]:
            for w,h,mw,mh in [(1540,840,1000,600),(1100,850,850,590),(660,870,500,500)]:
                got=dimensions(sw,sh,w,h,(mw,mh))
                self.assertLessEqual(got[0],sw-60);self.assertLessEqual(got[1],sh-100)
                self.assertLessEqual(got[2],got[0]);self.assertLessEqual(got[3],got[1])


class MultiSelectionDialogTests(unittest.TestCase):
    def setUp(self):
        import tkinter as tk
        from drawing_dialog import show
        from main_screen import theme
        from themes import THEMES
        self.root=tk.Tk();self.root.withdraw();self.addCleanup(self.root.destroy)
        self.info={'isin':'fixture','name':'Fixture','drawings':objects()}
        self.current=self.info;self.drafts={};self.jobs=[]
        self.app=SimpleNamespace(root=self.root,store=SimpleNamespace(setting=lambda k,d=None:d),
            instrument_cfg=lambda:self.current,user_state=lambda i:self.drafts.get(i['isin'],i),drafts=self.drafts,
            status=tk.StringVar(self.root),refresh_chart=Mock(),tooltip=Mock(),chart_meta={},job=lambda *a,**kw:self.jobs.append(a) or True)
        theme(self.app,THEMES['Тёмная']);show(self.app);self.d=self.app._drawing_dialog;self.root.update();self.jobs.clear()

    def test_group_multiselect_visibility_lock_and_undo_use_real_buttons(self):
        self.d.objects.selection_set(('0','1'));self.root.update()
        with patch('drawing_dialog.simpledialog.askstring',return_value='G'):self.d.actions['group'].invoke()
        self.d.objects.selection_set('0');self.root.update();self.d.flags['visible'].invoke()
        self.assertFalse(self.d.listed[1]['visible'])
        self.d.flags['locked'].invoke();self.root.update()
        self.assertEqual(str(self.d.actions['delete']['state']),'disabled')
        self.d.history('undo');self.assertFalse(self.d.listed[1].get('locked',False))

    def test_object_navigation_reveals_table_and_marks_active_tab(self):
        self.d.tabs['Объекты'].invoke();self.root.update()
        top=self.d.objects.winfo_rooty()-self.d.body_canvas.winfo_rooty()
        self.assertGreaterEqual(top,0)
        self.assertLessEqual(top+self.d.objects.winfo_height(),self.d.body_canvas.winfo_height())
        self.assertTrue(str(self.d.tabs['Объекты']['style']).endswith('.Primary.TButton'))
        self.d.tabs['Инструменты'].invoke();self.root.update()
        self.assertTrue(str(self.d.tabs['Инструменты']['style']).endswith('.Primary.TButton'))

    def test_export_modal_rejects_changed_chart_and_has_no_write_on_cancel(self):
        with patch('drawing_dialog.filedialog.asksaveasfilename',return_value=''):self.d.export()
        self.assertFalse(self.jobs)
        def changed(**kw):self.app.chart_meta={'source':'OTHER'};return 'unused.json'
        with patch('drawing_dialog.filedialog.asksaveasfilename',side_effect=changed):self.d.export()
        self.assertFalse(self.jobs)

    def test_filtered_multi_selection_keeps_indices_and_rejects_external_draft(self):
        self.d.query.set('107');self.root.update();self.assertEqual(self.d.objects.get_children(),('2',))
        self.d.objects.selection_set('2');self.root.update();self.d.actions['copy'].invoke()
        self.assertEqual(len(self.d.listed),4);self.assertEqual(self.d.listed[-1]['price'],107)
        expected=copy.deepcopy(self.drafts);self.current={'isin':'OTHER'}
        self.d.change('copy');self.assertEqual(self.drafts,expected)


if __name__=='__main__':unittest.main()
