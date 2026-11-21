import sys,unittest,sqlite3,json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
from drawing_gallery import Favorites,clean,KEY,DEFAULT


class FavoriteTests(unittest.TestCase):
    def setUp(self):
        self.jobs=[];self.app=SimpleNamespace(store=SimpleNamespace(setting=Mock(return_value=['fib']),set_setting=Mock()))
        self.app.job=lambda work,done,failed,**meta:self.jobs.append((work,done,failed,meta)) or True
        self.f=Favorites(self.app)

    def finish(self):
        work,done,fail,meta=self.jobs.pop(0);done(work());return meta

    def test_load_and_write_are_deferred_and_only_touch_own_ui_key(self):
        self.f.load();self.app.store.setting.assert_not_called();self.assertFalse(self.f.loaded)
        self.finish();self.assertEqual(self.f.items,['fib'])
        self.f.toggle('channel');self.app.store.set_setting.assert_not_called()
        self.f.toggle('trend');self.assertEqual(self.f.items,['fib','channel']) # busy write serializes edits
        self.finish();self.app.store.set_setting.assert_called_once_with(KEY,['fib','channel'])
        self.assertFalse(self.f.busy)

    def test_failure_is_explicit_retry_keeps_exact_pending_items(self):
        self.f.load();self.finish();self.f.toggle('zone')
        work,done,failed,meta=self.jobs.pop();failed(RuntimeError('fixture'))
        self.assertTrue(self.f.pending);self.assertTrue(self.f.error)
        self.f.save();self.finish();self.assertFalse(self.f.pending)
        self.app.store.set_setting.assert_called_once_with(KEY,['fib','zone'])

    def test_queue_reject_and_shutdown_cannot_write(self):
        self.app.job=lambda *a,**k:False;self.f.load()
        self.assertTrue(self.f.error);self.assertFalse(self.f.loaded)
        self.app.closing=True;self.f.save();self.app.store.set_setting.assert_not_called()

    def test_invalid_saved_values_are_bounded_and_unknown_tools_rejected(self):
        self.assertEqual(clean(None),DEFAULT)
        self.assertEqual(clean(['fib','fib','unknown',None]),['fib'])
        self.f.loaded=True;self.f.toggle('not-a-tool');self.assertFalse(self.jobs)
        from drawing_tools import REGISTRY
        self.assertEqual(len(clean(list(REGISTRY))),12)

    def test_saved_favorites_reopen_without_mutating_unrelated_settings(self):
        # SQLite in memory deliberately isolates real state; Store methods open/close on each call.
        db=sqlite3.connect(':memory:');self.addCleanup(db.close)
        db.execute('create table settings(key primary key,value)')
        db.execute('insert into settings values (?,?)',('protected',json.dumps({'position':3})))
        def read(k,d):
            row=db.execute('select value from settings where key=?',(k,)).fetchone()
            return json.loads(row[0]) if row else d
        def write(k,v):db.execute('insert or replace into settings values (?,?)',(k,json.dumps(v)))
        self.app.store=SimpleNamespace(setting=read,set_setting=write)
        self.f.load();self.finish();self.f.toggle('fib');self.finish()
        reopened=Favorites(self.app);reopened.load();self.finish()
        self.assertNotIn('fib',reopened.items);self.assertEqual(read('protected',None),{'position':3})


class GalleryLayoutTests(unittest.TestCase):
    def test_all_groups_filter_collapse_and_shared_favorites(self):
        import tkinter as tk
        from test_drawing_dialog import DrawingDialogTests
        fixture=DrawingDialogTests('test_registered_tools_and_singleton_preserve_click_modes');fixture.setUp()
        self.addCleanup(fixture.doCleanups);d=fixture.d;root=fixture.root;g=d.gallery
        root.update_idletasks()
        self.assertEqual(d.tool_group.get(),'Все группы')
        self.assertEqual(len(g.sections),8)
        self.assertTrue(all(section.winfo_manager() for section,_,_ in g.sections.values()))
        g.collapse('Фибоначчи');self.assertFalse(g.sections['Фибоначчи'][2].winfo_manager())
        g.collapse('Фибоначчи');self.assertTrue(g.sections['Фибоначчи'][2].winfo_manager())
        d.tool_group.set('Каналы');root.update_idletasks()
        self.assertTrue(g.sections['Каналы'][0].winfo_manager())
        self.assertFalse(g.sections['Линии тренда'][0].winfo_manager())
        self.assertGreater(d.objects.winfo_height(),50)
        self.assertFalse(fixture.drafts)
        from unittest.mock import patch
        with patch.object(d,'reveal') as reveal:
            d.favorites()
            reveal.assert_called_once_with(d.tools_box)
        self.assertTrue(g.editing)
        d.win.destroy()
        self.assertEqual(g.favorites.views,[])

if __name__=='__main__':unittest.main()

