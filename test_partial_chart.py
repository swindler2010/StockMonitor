import copy,json,sys,unittest,sqlite3
from pathlib import Path
from unittest.mock import Mock,patch
import pandas as pd
import numpy as np
import matplotlib
matplotlib.use('Agg')
from matplotlib import pyplot as plt
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
import partial_chart as pc
import data_quality as q
from chart_modes import draw_prices

class Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.entries=json.loads(pc.REGISTRY.read_text())['archives']
        cls.frames={};cls.partial={}
        for e in cls.entries:
            raw=pd.read_pickle(pc.REGISTRY.parent/(e['identity']['symbol']+'-18oct-probe.pkl'))
            cls.frames[e['isin']]=raw
            cls.partial[e['isin']]=pc.split_archive(raw,e['identity'])

    def fixture(self,e=None):
        e=e or self.entries[0];meta=e['identity']
        info=dict(isin=e['isin'],eur_history_auto=True,eur_listings=[dict(meta,isin=e['isin'],status='CONFIRMED')])
        store=Mock();store.setting.return_value=dict(state='INVALID',identity=meta)
        return info,store

    def test_real_two_archives_preserve_exact_rows_identity_and_invalid(self):
        for e in self.entries:
            raw=self.frames[e['isin']];f=self.partial[e['isin']]
            self.assertIsNotNone(f)
            np.testing.assert_equal(f[list(q.FIELDS)].values,raw.loc[f.index,list(q.FIELDS)].values)
            self.assertEqual(f.attrs['data_quality']['state'],'INVALID')
            self.assertEqual(f.attrs['partial_chart']['raw_count'],len(raw))
            for k in q.IDENTITY:self.assertEqual(f.attrs[k],e['identity'][k])
            self.assertLess(len(f),len(raw))

    def test_confirmed_archive_load_has_no_store_writes(self):
        info,store=self.fixture();f=pc.load(info,'1D',pd.DataFrame(),store)
        self.assertIsNotNone(f);self.assertTrue(pc.current(f.attrs['partial_chart']['files']))
        self.assertTrue(all(call[0]=='setting' for call in store.mock_calls))

    def test_identity_currency_hash_and_pinned_source_fail_closed(self):
        info,store=self.fixture()
        for key,value in [('isin','OTHER'),('currency','USD'),('venue','GER'),('source','gettex')]:
            bad=copy.deepcopy(info);bad['eur_listings'][0][key]=value
            self.assertIsNone(pc.load(bad,'1D',pd.DataFrame(),store))
        info['eur_history_auto']=False
        self.assertIsNone(pc.load(info,'1D',pd.DataFrame(),store))
        info['eur_history_auto']=True
        with patch.object(pc.hashlib,'sha256') as sha:
            sha.return_value.hexdigest.return_value='changed'
            self.assertIsNone(pc.load(info,'1D',pd.DataFrame(),store))

    def test_normal_history_and_other_tf_untouched(self):
        info,store=self.fixture();normal=self.frames[info['isin']].copy()
        normal.attrs['data_quality']={'state':'SOURCE_GAP'}
        self.assertIsNone(pc.load(info,'1D',normal,store))
        self.assertIsNone(pc.load(info,'4H',pd.DataFrame(),store))
        store.setting.assert_not_called()

    def test_global_identity_error_not_convertible_to_preview(self):
        e=self.entries[0];raw=self.frames[e['isin']].copy()
        self.assertIsNone(pc.split_archive(raw,{**e['identity'],'currency':'USD'}))

    def test_line_breaks_and_candle_gap_bands_without_fake_rows(self):
        f=self.partial[self.entries[0]['isin']];before=f.copy(deep=True)
        for mode in ['Line','Candles','Candles + Close line','Bars']:
            fig,ax=plt.subplots();draw_prices(ax,f,mode)
            self.assertEqual(sum(x.get_alpha()==.45 for x in ax.patches),len(f.attrs['partial_chart']['break_before']))
            if mode in ('Line','Candles + Close line'):
                line=next(x for x in ax.lines if x.get_label()=='Close')
                self.assertEqual(np.isnan(line.get_ydata()).sum(),len(f.attrs['partial_chart']['break_before']))
            plt.close(fig)
        pd.testing.assert_frame_equal(f,before)

    def test_preview_cannot_reuse_buy_or_enable_indicators(self):
        f=self.partial[self.entries[0]['isin']]
        prepared={'trading_engine':{'signal':'BUY','status':'READY','confidence':99}}
        result=pc.bind(prepared,f)
        self.assertEqual(result['trading_engine']['signal'],'WAIT')
        self.assertEqual(result['trading_engine']['status'],'DATA BLOCKED')
        self.assertFalse(result['analysis'][1]['ohlcv_valid'])
        self.assertTrue(result['analysis'][0].ema30.isna().all())
        self.assertEqual(prepared['trading_engine']['signal'],'BUY')

    def test_changed_archive_is_rejected_before_ui_publication(self):
        saved=pc.token(pc.REGISTRY)
        self.assertFalse(pc.current([(saved[0],saved[1]+1,saved[2])]))

    def test_existing_chart_worker_binds_preview_without_trading_analysis(self):
        import chart_worker
        info,store=self.fixture();ctx=Mock()
        ctx.instrument_cfg.return_value=info;ctx.user_state.return_value={}
        ctx.frame_for.return_value=pd.DataFrame()
        ctx.trading_analysis.return_value={'signal':'WAIT','status':'DATA BLOCKED','benchmark_context':{}}
        # Isolate the chart binding contract from the optional Store read cache;
        # a generic Mock cache otherwise returns a Mock instead of a DataFrame.
        with patch.object(chart_worker,'SnapshotContext',return_value=ctx),patch.object(pc,'select_display',return_value=self.partial[info['isin']]):
            result=chart_worker.prepare(store,{'timeframe':'1D'})
        self.assertTrue(result['frame'].attrs['partial_chart']['display_only'])
        ctx.trading_analysis.assert_not_called()
        ctx.analyse_cached.assert_not_called()
        self.assertEqual(result['trading_engine']['status'],'DATA BLOCKED')

if __name__=='__main__':unittest.main()
