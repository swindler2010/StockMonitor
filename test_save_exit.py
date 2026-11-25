import copy,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
from storage import Store
from save_exit import SaveExitMixin,USER_SETTINGS

class Value:
    def __init__(self,value):self.value=value
    def get(self):return self.value
    def set(self,value):self.value=value

class Tests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.store=Store(self.temp.name)
        self.original=[{'isin':'US0378331005','name':'Apple','position':{'quantity':2},'drawings':[]}]
        self.store.save_watchlist(self.original)
        self.app=SaveExitMixin.__new__(SaveExitMixin)
        self.app.store=self.store;self.app.watchlist=copy.deepcopy(self.original);self.app.persisted=copy.deepcopy(self.original)
        self.app.drafts={};self.app.user_dirty=False;self.app.root=object();self.app.status=Value('')
        self.app.symbol=Value('Apple');self.app.timeframe=Value('4H');self.app.period=Value('Последние')
        self.app._saved_selection=self.app.selection_state()
        self.store.set_setting('selected',self.app.selection_state());self.store.enable_user_drafts()
    def tearDown(self):
        self.store.read_cache.close();self.temp.cleanup()
    def edit(self):
        draft=copy.deepcopy(self.original[0]);draft['position']['quantity']=9;draft['drawings']=[{'type':'hline','price':100}]
        self.app.drafts[draft['isin']]=draft
        self.store.set_setting('global_indicators',{'EMA':False})
        self.app.timeframe.set('1D')
    def test_save(self):
        self.edit()
        with patch('save_exit.ask_save_on_exit',return_value='save'):self.assertTrue(self.app.confirm_close())
        self.assertEqual(self.store.instruments()[0]['position']['quantity'],9)
        self.assertEqual(self.store.instruments()[0]['drawings'][0]['price'],100)
        self.assertEqual(self.store.setting('selected')['timeframe'],'1D')
        self.assertFalse(self.app.has_unsaved_changes())
    def test_discard(self):
        self.edit()
        with patch('save_exit.ask_save_on_exit',return_value='discard'):self.assertTrue(self.app.confirm_close())
        self.assertEqual(self.store.instruments(),self.original)
        with self.store.connect() as db:
            self.assertIsNone(db.execute("SELECT value FROM settings WHERE key='global_indicators'").fetchone())
    def test_cancel(self):
        self.edit()
        with patch('save_exit.ask_save_on_exit',return_value='cancel'):self.assertFalse(self.app.confirm_close())
        self.assertTrue(self.app.has_unsaved_changes());self.assertEqual(self.store.instruments(),self.original)
    def test_clean_exit(self):
        with patch('save_exit.ask_save_on_exit',side_effect=AssertionError('Unexpected dialog')):self.assertTrue(self.app.confirm_close())
    def test_normalized_loading_is_clean(self):
        prepared=self.app
        class Base:
            def __init__(self):
                self.__dict__.update(prepared.__dict__)
                self.watchlist=copy.deepcopy(self.watchlist)
                self.watchlist[0]['indicators']={'EMA':True}
        class Loaded(SaveExitMixin,Base):pass
        loaded=Loaded()
        self.assertFalse(loaded.has_unsaved_changes())
    def test_watchlist_remove_detected_and_saved(self):
        self.app.watchlist=[]
        self.assertTrue(self.app.has_unsaved_changes())
        with patch('save_exit.ask_save_on_exit',return_value='save'):self.assertTrue(self.app.confirm_close())
        self.assertEqual(self.store.instruments(),[])
    def test_setting_revert_and_all_keys(self):
        for key in USER_SETTINGS-{'selected'}:
            self.store.set_setting(key,{'test':1});self.assertTrue(self.app.has_unsaved_changes())
            self.store.set_setting(key,None);self.assertFalse(self.app.has_unsaved_changes())
    def test_selection_without_data(self):
        self.app.timeframe.set('1D');self.assertTrue(self.app.has_unsaved_changes())
    def test_save_failure_keeps_drafts_and_db(self):
        self.edit();self.app.watchlist.append({'isin':self.original[0]['isin'],'name':'duplicate'})
        with patch('save_exit.ask_save_on_exit',return_value='save'),patch('save_exit.messagebox.showerror'):
            self.assertFalse(self.app.confirm_close())
        self.assertEqual(self.store.instruments(),self.original);self.assertTrue(self.app.has_unsaved_changes())
    def test_system_writes_still_persist(self):
        self.store.set_setting('gettex_last_success:x','now')
        self.assertFalse(self.app.has_unsaved_changes())
        with self.store.connect() as db:self.assertIsNotNone(db.execute("SELECT value FROM settings WHERE key='gettex_last_success:x'").fetchone())

if __name__=='__main__':unittest.main()
