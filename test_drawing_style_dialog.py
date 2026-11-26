import copy,sys,tkinter as tk,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
from drawing_dialog import show
from main_screen import theme
from themes import THEMES

class StyleDialogTests(unittest.TestCase):
    def setUp(self):
        self.root=tk.Tk();self.root.withdraw();self.addCleanup(self.root.destroy)
        self.info=dict(isin='fixture',name='Fixture',drawings=[dict(type='hline',price=120,at='2026-11-12 08:00',currency='EUR',source_mode='GETTEX')])
        self.app=SimpleNamespace(root=self.root,store=SimpleNamespace(setting=lambda key,default=None:default),
            instrument_cfg=lambda:self.info,user_state=lambda _:self.app.drafts.get('fixture',self.info),drafts={},status=tk.StringVar(self.root),refresh_chart=Mock(),tooltip=Mock(),chart_meta={})
        theme(self.app,THEMES['Тёмная']);show(self.app);self.parent=self.app._drawing_dialog
        self.parent.objects.selection_set('0');self.root.update();self.dialog=self.parent.settings();self.root.update()

    def test_apply_updates_real_draft_and_loading_template_alone_does_not(self):
        d=self.dialog;d.values['width'].set('3');d.name.set('Неон');d.save()
        self.assertNotIn('style',self.app.drafts['fixture']['drawings'][0])
        d.values['width'].set('7');d.load();self.assertEqual(d.values['width'].get(),'3.0')
        d.apply();self.assertEqual(self.app.drafts['fixture']['drawings'][0]['style']['width'],3)
        self.assertEqual(self.app.drafts['fixture']['drawing_style_templates'][0]['name'],'Неон')
        self.assertEqual(self.info['drawings'][0]['price'],120)

    def test_real_context_change_rejects_style_and_template_publication(self):
        d=self.dialog;self.app.chart_meta={'symbol':'other'};d.apply();d.name.set('test');d.save()
        self.assertFalse(self.app.drafts);self.app.refresh_chart.assert_not_called()

    def test_all_scope_is_atomic_when_one_object_is_locked_and_footer_fits(self):
        d=self.dialog
        self.info['drawings'].append(dict(type='hline',price=130,locked=True))
        d.expected=copy.deepcopy(self.info['drawings']);self.parent.listed=copy.deepcopy(d.expected)
        d.scope.set('Всем');d.apply();self.assertFalse(self.app.drafts)
        self.assertIn('закреплён',d.note.get())
        for control in [d.apply_button,*d.entries.values(),d.preset_box]:
            bottom=control.winfo_rooty()-d.win.winfo_rooty()+control.winfo_height()
            self.assertGreater(control.winfo_height(),10);self.assertLessEqual(bottom,d.win.winfo_height())

if __name__=='__main__':unittest.main()
