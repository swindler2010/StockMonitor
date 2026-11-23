"""News/search presentation regressions; isolated settings, no market writes."""
import io,json,sqlite3,sys,tempfile,unittest
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
from PIL import Image
from news_engine import parse_feed,normalize
from news_cards import clean_saved,identity,Cards
from news_media import load,image_url
from ui_collections import toggle_saved,Collection,decode


class CollectionsTests(unittest.TestCase):
    def test_independent_flags_merge_latest_state_and_preserve_other_settings(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'settings.sqlite'
            @contextmanager
            def connect():
                db=sqlite3.connect(path)
                try:
                    with db:yield db
                finally:db.close()
            store=SimpleNamespace(connect=connect)
            with store.connect() as db:
                db.execute('CREATE TABLE settings(key TEXT PRIMARY KEY,value TEXT)')
                db.execute('INSERT INTO settings VALUES (?,?)',('paper_account','{"sentinel":10000}'))
            row={'target':'A','article':dict(title='Translated',original_title='Original',summary='Übersetzt',original_summary='Source',url='https://example.com/a')}
            flags=('favorite','saved');key='ui_news_collection_v1';iid=identity('A',row['article'])
            for flag in flags:toggle_saved(store,key,iid,row,flag,flags,clean_saved)
            value=toggle_saved(store,key,'second',{**row,'target':'B'},'saved',flags,clean_saved)
            self.assertTrue(value[iid]['favorite'] and value[iid]['saved'])
            self.assertEqual(value[iid]['item']['article']['title'],'Original')
            self.assertEqual(value[iid]['item']['article']['summary'],'Source')
            value=toggle_saved(store,key,iid,row,'saved',flags,clean_saved)
            self.assertTrue(value[iid]['favorite']);self.assertFalse(value[iid]['saved'])
            with store.connect() as db:
                persisted=json.loads(db.execute('SELECT value FROM settings WHERE key=?',(key,)).fetchone()[0])
                self.assertEqual(decode(persisted,clean_saved,flags,200),value)
                self.assertEqual(db.execute('SELECT value FROM settings WHERE key="paper_account"').fetchone()[0],'{"sentinel":10000}')
            with self.assertRaisesRegex(ValueError,'UI_COLLECTION_LIMIT'):
                toggle_saved(store,key,'third',row,'saved',flags,clean_saved,limit=2)

    def test_failed_save_is_not_optimistically_shown_as_saved(self):
        pending=[];app=SimpleNamespace(job=lambda work,done,failed,**kw:pending.append((work,done,failed)) or True)
        manager=Collection(app,'ui',lambda x:x);manager.loaded=True
        self.assertTrue(manager.toggle('a',{'symbol':'A'}));self.assertTrue(manager.busy)
        self.assertFalse(manager.has('a'));pending[0][2]('SQLITE_BUSY')
        self.assertFalse(manager.busy);self.assertFalse(manager.has('a'));self.assertEqual(manager.error,'SQLITE_BUSY')
        self.assertEqual(decode({'version':1,'items':None},lambda x:x,('favorite',),200),{})

    def test_saved_views_are_scoped_by_instrument_and_language(self):
        import i18n
        cards=Cards.__new__(Cards)
        a=dict(title='A',url='https://example.com/a');b=dict(title='B',url='https://example.com/b')
        cards.d=SimpleNamespace(target=lambda:('Apple','A',None),view_mode=SimpleNamespace(get=lambda:'Закладки'))
        cards.saved=SimpleNamespace(has=lambda iid,flag:iid in (identity('A',a),identity('B',b)))
        cards.projected=[a,b];cards.project_key=('B','ru')
        with patch.object(i18n,'_language','ru'):
            self.assertEqual(cards.rows([a]),[a])
            cards.project_key=('A','de');self.assertEqual(cards.rows([]),[])

    def test_marking_in_all_news_refreshes_buttons_without_rebuilding_cards(self):
        import i18n
        cards=Cards.__new__(Cards);cards.saved=SimpleNamespace(error=None);cards.had_error=False
        cards.d=SimpleNamespace(closed=False,view_mode=SimpleNamespace(get=lambda:'Все новости'),render=Mock())
        cards.project=Mock();cards.refresh_buttons=Mock()
        with patch.object(i18n,'_language','ru'):cards.changed()
        cards.refresh_buttons.assert_called_once();cards.d.render.assert_not_called()


class PreviewTests(unittest.TestCase):
    def response(self,data,status=200,headers=None):
        response=Mock(status_code=status,headers=headers or {},is_redirect=status in (301,302))
        response.__enter__=Mock(return_value=response);response.__exit__=Mock(return_value=False)
        response.iter_content.return_value=[data]
        return response

    def png(self,size=(200,120)):
        data=io.BytesIO();Image.new('RGB',size,'navy').save(data,format='PNG');return data.getvalue()

    def test_feed_images_are_source_fields_and_original_text_is_preserved(self):
        rss=b'<rss xmlns:media="http://search.yahoo.com/mrss/"><channel><item><title>Original results</title><link>https://example.com/a</link><media:thumbnail url="https://example.com/a.png"/></item></channel></rss>'
        row=normalize(parse_feed(rss,'Source'))[0]
        self.assertEqual((row['title'],row['image_url']),('Original results','https://example.com/a.png'))
        atom=b'<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Original</title><link href="https://example.com/b"/><link rel="enclosure" type="image/png" href="https://example.com/b.png"/></entry></feed>'
        self.assertEqual(parse_feed(atom,'Source')[0]['image_url'],'https://example.com/b.png')
        self.assertFalse(image_url('javascript:alert(1)'));self.assertFalse(image_url('http://example.com/a.png'))

    def test_bounded_preview_cache_hit_avoids_network_and_unrelated_files_survive(self):
        with tempfile.TemporaryDirectory() as directory,patch('news_media.public_url',side_effect=lambda url:url):
            session=SimpleNamespace(get=Mock(return_value=self.response(self.png())))
            sentinel=Path(directory)/'source-evidence.json';sentinel.write_text('unchanged')
            image=load('https://example.com/a.png',directory,session,clock=lambda:1000)
            self.assertEqual(image.size,(160,96))
            image=load('https://example.com/a.png',directory,session,clock=lambda:1001)
            self.assertEqual(session.get.call_count,1);self.assertEqual(sentinel.read_text(),'unchanged')

    def test_source_cooldown_and_unsafe_redirect_never_fetch_again(self):
        with tempfile.TemporaryDirectory() as directory,patch('news_media.public_url',side_effect=lambda url:url):
            session=SimpleNamespace(get=Mock(return_value=self.response(b'',429,{'Retry-After':'7200'})))
            for now in (1000,1100):
                with self.assertRaisesRegex(RuntimeError,'COOLDOWN'):load('https://example.com/a.png',directory,session,clock=lambda:now)
            self.assertEqual(session.get.call_count,1)
        with tempfile.TemporaryDirectory() as directory,patch('news_media.public_url',side_effect=[None,ValueError('private')]):
            session=SimpleNamespace(get=Mock(return_value=self.response(b'',302,{'Location':'https://127.0.0.1/a'})))
            with self.assertRaisesRegex(ValueError,'private'):load('https://example.com/a.png',directory,session)
            self.assertEqual(session.get.call_count,1)

    def test_oversize_and_tiny_images_do_not_create_usable_previews(self):
        with tempfile.TemporaryDirectory() as directory,patch('news_media.public_url',side_effect=lambda url:url):
            for content,message in ((b'x'*1_500_001,'PREVIEW_LIMIT'),(self.png((10,10)),'PREVIEW_DIMENSIONS')):
                session=SimpleNamespace(get=Mock(return_value=self.response(content)))
                with self.assertRaisesRegex(ValueError,message):load('https://example.com/a.png',directory,session)
            self.assertFalse(list(Path(directory).glob('*.png')))


class SearchDesignTests(unittest.TestCase):
    def test_sort_keeps_candidate_ids_and_late_logo_cannot_replace_new_search(self):
        import tkinter as tk
        from tkinter import ttk
        from instrument_dialog import Dialog,favorite_id
        from main_screen import theme
        from themes import THEMES
        from logos016 import LogoMixin
        root=tk.Tk();root.withdraw();self.addCleanup(root.destroy);jobs=[]
        def job(work,done,failed,**options):
            if (options.get('key') or (None,))[0]=='ui-collection-load':done({});return True
            jobs.append((work,done));return True
        app=SimpleNamespace(root=root,store=SimpleNamespace(setting=lambda k,d=None:d),job=job,tooltip=Mock())
        theme(app,THEMES['Тёмная']);d=Dialog(app);root.update()
        self.assertEqual(ttk.Style(root).lookup('Instrument.Treeview.Heading','background',('active',)),'#12304a')
        d.insert(0,{'symbol':'Z','longname':'Zeta'});d.insert(1,{'symbol':'A','longname':'Alpha'})
        d.sort('#0');self.assertEqual(d.results.get_children(),('1','0'))
        d.results.selection_set('0');d.confirmed('0',dict(symbol='Z.DE',isin='identity',venue='GETTEX',currency='EUR'))
        self.assertEqual(d.results.selection(),('0',));self.assertEqual(d.results.item('0','values')[:4],('Z.DE','identity','GETTEX','EUR'))
        d.favorites.items[favorite_id(d.items['0'])]={'favorite':True};d.favorite_order()
        self.assertEqual(d.results.get_children()[0],'0')
        LogoMixin.attach_logo(app,d.results,'0',{'symbol':'OLD','name':'Old'})
        old_done=jobs.pop()[1]
        d.results.delete(*d.results.get_children());d.insert(0,{'symbol':'NEW','longname':'New'})
        LogoMixin.attach_logo(app,d.results,'0',{'symbol':'NEW','name':'New'})
        before=d.results.item('0','image');old_done(Image.new('RGBA',(24,24),'red'))
        self.assertEqual(d.results.item('0','image'),before)
        self.assertEqual(d.results._logo_bindings['0'],'NEW')
        self.assertEqual(d.original_photos['0'].width(),24)


if __name__=='__main__':unittest.main()
