"""Direct index display regression; isolated fixtures, no live data writes."""
import copy,sys,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch
import pandas as pd
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
import benchmarks as bm
from partial_chart import select_display
from benchmark_display import information_text

def frame(info):
    x=pd.DataFrame({'open':[100.,101.,102.],'high':[102.,103.,104.],
        'low':[99.,100.,101.],'close':[101.,102.,103.],'volume':[0.,0.,0.]},
        index=pd.to_datetime(['2026-09-28','2026-09-29','2026-09-30']))
    x.attrs={**bm.identity(info),'timeframe':'1D','data_quality':{'state':'READY'}}
    return x

class IndexDisplayTests(unittest.TestCase):
    def test_all_six_indices_keep_exact_daily_weekly_monthly_frames_without_stock_loader(self):
        from analytics import resample
        for info in bm.CATALOG.values():
            for tf in ('1D','1W','1M'):
                with self.subTest(isin=info['isin'],tf=tf):
                    normal=resample(frame(info),'1D',tf,now=pd.Timestamp('2026-10-01T22:00Z'))
                    before=copy.deepcopy(normal.attrs)
                    with patch('gettex_chart._load_native',side_effect=AssertionError('stock loader used')):
                        result=select_display(info,tf,normal,SimpleNamespace())
                    pd.testing.assert_frame_equal(result,normal)
                    self.assertEqual(result.attrs['data_quality'],before['data_quality']);self.assertEqual(normal.attrs,before)
                    for key,value in bm.identity(info).items():self.assertEqual(result.attrs[key],value)
                    if before['data_quality']['state']!='READY':self.assertTrue(result.attrs['partial_chart']['display_only'])
                    self.assertIsNot(result,normal)
                    self.assertNotIn('accepted_risk',result.attrs)

    def test_wrong_index_identity_and_unsupported_intraday_never_substitute_rows(self):
        info=bm.CATALOG['INDEX:SP500'];normal=frame(info)
        for key,value in [('symbol','^NDX'),('currency','EUR'),('venue','GETTEX'),('source','gettex_chart'),('timezone','Europe/Berlin')]:
            wrong=normal.copy();wrong.attrs[key]=value
            result=select_display(info,'1D',wrong,SimpleNamespace())
            self.assertTrue(result.empty);self.assertEqual(result.attrs['data_quality']['state'],'INVALID')
        unknown={**info,'isin':'INDEX:UNKNOWN'}
        self.assertEqual(select_display(unknown,'1D',normal,SimpleNamespace()).attrs['data_quality']['state'],'INVALID')
        invalid=normal.copy();invalid.attrs['data_quality']={'state':'INVALID'}
        self.assertTrue(select_display(info,'1D',invalid,SimpleNamespace()).empty)
        self.assertTrue(select_display(info,'4H',normal,SimpleNamespace()).empty)
        from benchmark_ui import BenchmarkMixin
        for tf in ('5m','15m','1H','4H'):
            with patch.object(bm,'load',return_value=normal):
                raw=BenchmarkMixin._frame_uncached(SimpleNamespace(store=None),info,tf)
            self.assertTrue(select_display(info,tf,raw,SimpleNamespace()).empty)
            self.assertEqual(raw.attrs['data_quality']['state'],'SOURCE_UNAVAILABLE')

    def test_stale_history_visible_but_analytical_frame_remains_blocked_and_invalid_not_rescued(self):
        from benchmark_ui import BenchmarkMixin
        info=bm.CATALOG['INDEX:VIX'];raw=frame(info);clock=pd.Timestamp('2026-10-02T22:00Z')
        ctx=SimpleNamespace(store=None,analysis_at=clock)
        with patch.object(bm,'load',return_value=raw):
            blocked=BenchmarkMixin._frame_uncached(ctx,info,'1D')
            self.assertTrue(blocked.empty);self.assertEqual(blocked.attrs['data_quality']['state'],'STALE')
            shown=select_display(info,'1D',blocked,None,clock)
            self.assertEqual(len(shown),3);self.assertTrue(shown.attrs['partial_chart']['display_only'])
            self.assertEqual(shown.attrs['data_quality']['state'],'STALE')
            self.assertTrue(blocked.empty);self.assertNotIn('accepted_risk',shown.attrs)
            broken=raw.copy();broken.loc[broken.index[1],'high']=1
            with patch.object(bm,'load',return_value=broken):
                self.assertTrue(select_display(info,'1D',blocked,None,clock).empty)

    def test_exact_page_capture_supplies_every_required_index_loader_field(self):
        from workspace_pages import IDENTITY,load
        for original in bm.CATALOG.values():
            info={k:original.get(k) for k in IDENTITY}
            store=SimpleNamespace(load_series=lambda *a:frame(original),setting=lambda k,d=None:d)
            packet=load(SimpleNamespace(store=store),info)
            self.assertEqual(packet['benchmark']['count'],3)
            self.assertEqual(packet['info']['volume_policy'],original['volume_policy'])
            self.assertNotIn('card',packet)

    def test_index_overview_reads_no_company_data_and_translates_facts_without_changing_identity(self):
        from workspace_pages import load
        import i18n
        info=bm.CATALOG['INDEX:DAX'];facts={'archive':{'status':'INCOMPLETE','archive_quality':'GAP'},'quality':'READY',
            'count':3,'first':'2026-09-28','last':'2026-09-30','close':103.}
        with patch('benchmark_display.information',return_value=facts):
            result=load(SimpleNamespace(store=SimpleNamespace()),info)
        self.assertEqual(result['benchmark'],facts);self.assertNotIn('card',result)
        for lang in ('ru','de','en'):
            with patch('i18n._language',lang):
                text=information_text(info,facts,'reports')
                for exact in ('^GDAXI','GER','EUR','Europe/Berlin','GAP','INCOMPLETE'):self.assertIn(exact,text)
                if lang=='en':self.assertIn('not a company',text)
                if lang=='de':self.assertIn('kein Unternehmen',text)

class IndexWorkspaceTests(unittest.TestCase):
    def test_real_tk_index_overview_analysis_sources_and_stock_restore(self):
        import test_main_screen as fixture
        from workspace_pages import technical
        fixture.MainScreenTests.setUp(self)
        app=self.app;screen=self.screen;info=bm.CATALOG['INDEX:DAX'];app.watchlist.append(info);app.timeframe.set('1D')
        app.job.side_effect=lambda work,done,*args,**kw:done(work()) or True
        app.symbol.set(info['name']);self.root.update()
        raw=frame(info)
        facts={'archive':{'status':'INCOMPLETE','archive_quality':'GAP'},'quality':'READY',
            'count':3,'first':'2026-09-28','last':'2026-09-30','close':103.}
        with patch('benchmark_display.information',return_value=facts):
            screen.pages.select('overview');self.root.update()
            self.assertEqual(screen.pages.model['info']['unit'],'index points')
            self.assertEqual(screen.pages.model['info']['history_source'],'proxy')
        screen.bind_chart({'frame':raw});self.root.update()
        self.assertEqual(screen.source_captions['GETTEX'].cget('text'),'Yahoo Finance')
        self.assertIn('не применяется',screen.header_volume.get())
        self.assertIn('ИНДЕКС',screen.signal_caption.get())
        for variable in screen.condition_vars.values():variable.set('old stock fact')
        app.decision.set('WAIT');screen.bind_engine(None)
        self.assertTrue(all(v.get()=='—' for v in screen.condition_vars.values()))
        self.assertEqual(str(screen.tf_buttons['5m'].cget('state')),'disabled')
        self.assertEqual(str(screen.tf_buttons['1D'].cget('state')),'normal')
        screen.choose_tf('1H')
        self.assertEqual(app.timeframe.get(),'1D')
        self.assertEqual(app.decision.get(),'ИНДЕКС')
        self.assertIn('Рыночный контекст',screen.quality_panel.analysis.get())
        numeric=raw.copy();numeric['ema10']=102;numeric['rsi']=50
        numeric.attrs=raw.attrs
        values={'watchlist':[info],'symbol':info['name'],'timeframe':app.timeframe.get(),'period':'Max'}
        prepared={'frame':raw,'analysis':(numeric,{'closed_bar_count':3}),'trading_engine':None,'_input_revisions':{}}
        app.store.read_cache=SimpleNamespace(current=lambda token:True)
        with patch('partial_chart.current',return_value=True):packet=technical(app,info,app.timeframe.get(),'Max',(values,prepared))
        screen.pages.clear();screen.pages.body=screen.pages.host
        screen.pages.analysis(packet);self.root.update()
        widgets=[]
        def collect(w):
            if w.winfo_class()=='Text':widgets.append(w)
            for child in w.winfo_children():collect(child)
        collect(screen.pages.host)
        self.assertTrue(any('Рыночный контекст' in w.get('1.0','end') and 'WAIT' not in w.get('1.0','end') for w in widgets))
        app.symbol.set('Apple');self.root.update();screen.bind_chart({'frame':raw})
        self.assertEqual(screen.source_captions['GETTEX'].cget('text'),'GETTEX')
        self.assertEqual(str(screen.tf_buttons['5m'].cget('state')),'normal')

if __name__=='__main__':unittest.main()
