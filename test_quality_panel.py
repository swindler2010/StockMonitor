"""Separate archive warnings from current analysis without changing either."""
import copy,sys,unittest,tkinter as tk
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock,patch
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
from quality_panel import describe,Panel

class QualityModelTests(unittest.TestCase):
    def test_invalid_history_exposes_reason_and_tf_without_a_fake_percentage(self):
        report=dict(state='INVALID',issues=['Неполные агрегированные свечи: 17'],base_tf='4H',
                    interval_classification=dict(with_candles=331,expected_open_intervals=331),
                    ranges=[['2026-08-07 19:30','2026-08-07 19:30']],identity=dict(source='gettex_chart',venue='GETTEX',currency='EUR',symbol='AMZN.GTX'))
        before=copy.deepcopy(report);model=describe(report,'4H')
        self.assertEqual(model['state'],'INVALID');self.assertEqual(model['summary'],'Неполных агрегатов: 17')
        self.assertIn('331/331',model['details']);self.assertIn('2026-08-07',model['details']);self.assertNotIn('%',model['details'])
        self.assertEqual(model['style'],'DeskNegative.TLabel');self.assertEqual(report,before)

    def test_real_ohlcv_errors_and_unknown_evidence_are_not_hidden_or_marked_ready(self):
        model=describe(dict(state='INVALID',issues=['Невозможные OHLC/цены/объёмы','Не подтверждено venue']),'1D')
        self.assertIn('Некорректные OHLCV',model['summary']);self.assertIn('Не подтверждено venue',model['details'])
        self.assertEqual(describe(None,'4H')['state'],'UNKNOWN')
        self.assertIn('Полнота публикации не подтверждена',describe(dict(state='UNCONFIRMED',issues=['SOURCE_COVERAGE_CONFLICT: missing publication']),'15m')['summary'])

    def test_11_timeframes_and_languages_preserve_the_audit_code_and_bound_details(self):
        from timeframes import LABELS
        for lang in ('ru','de','en'):
            with patch('i18n._language',lang):
                for tf in LABELS:
                    model=describe(dict(state='INVALID',base_tf=tf,issues=['Неполные агрегированные свечи: 2']*200,ranges=[['2026-01-01','2026-01-02']]*200),'4H')
                    self.assertEqual(model['state'],'INVALID');self.assertIn(tf,model['details']);self.assertLess(len(model['details']),10000)
                    self.assertIn('2',model['summary'])

    def test_aggregation_only_marks_a_bucket_complete_for_validated_absent_children(self):
        import pandas as pd
        from timeframes import _session_buckets
        opening=pd.Timestamp('2026-11-02 07:30');closing=pd.Timestamp('2026-11-02 11:30')
        index=pd.date_range(opening,closing,freq='15min',inclusive='left');missing=index[2];actual=index.delete(2)
        values=tuple((100.,102.,99.,101.,10.) for _ in actual);identity=('gettex_chart','GETTEX','EUR','TEST','Europe/Berlin')
        raw=_session_buckets(identity,'15m','4H',opening,closing,tuple(actual),values,(),False,opening)[0][0]
        verified=_session_buckets(identity,'15m','4H',opening,closing,tuple(actual),values,(missing,),False,opening)[0][0]
        self.assertFalse(raw[-1]);self.assertTrue(verified[-1]);self.assertEqual(raw[:-1],verified[:-1])

class QualityPanelTests(unittest.TestCase):
    def setUp(self):
        from main_screen import theme
        from themes import THEMES
        self.root=tk.Tk();self.root.withdraw();self.addCleanup(self.root.destroy)
        self.app=SimpleNamespace(root=self.root,watchlist=[dict(name='Apple',isin='A'),dict(name='NVIDIA',isin='N')],
             symbol=tk.StringVar(self.root,'Apple'),timeframe=tk.StringVar(self.root,'4H'),period=tk.StringVar(self.root,'Max'),store=Mock())
        theme(self.app,THEMES['Тёмная']);self.panel=Panel(self.app,self.root)

    def frame(self,tf='4H'):
        import pandas as pd
        frame=pd.DataFrame({'close':[1]});frame.attrs=dict(timeframe=tf,data_quality=dict(state='INVALID',issues=['Неполные агрегированные свечи: 2']))
        return frame

    def test_core_refresh_does_not_erase_history_and_blocked_analysis_remains_blocked(self):
        panel=self.panel;frame=self.frame();before=copy.deepcopy(frame.attrs)
        panel.bind_chart({'frame':frame});panel.bind_engine(dict(status='READY',signal='WAIT'))
        self.assertIn('Анализ доступен',panel.analysis.get());self.assertEqual(panel.audit.get(),'DQ: INVALID')
        panel.bind_engine(None);self.assertEqual(panel.audit.get(),'DQ: INVALID');self.assertIn('2',panel.reason.get())
        self.assertEqual(panel.analysis.get(),'Анализ: ожидается расчёт')
        panel.bind_engine(dict(status='DATA BLOCKED',signal='BUY',blockers=['OHLCV']))
        self.assertIn('блокируют',panel.analysis.get());self.assertEqual(panel.audit.get(),'DQ: INVALID')
        self.assertEqual(frame.attrs,before);self.app.store.assert_not_called()

    def test_stock_tf_and_period_changes_clear_previous_scope_without_new_io(self):
        from timeframes import LABELS
        for n,tf in enumerate(LABELS):
            self.app.timeframe.set(tf);self.panel.bind_chart({'frame':self.frame(tf)})
            self.panel.bind_engine(dict(status='READY',signal='WAIT',timeframe_context={tf:{}}))
            self.app.symbol.set('NVIDIA' if n%2==0 else 'Apple');self.panel.ensure_context()
            self.assertEqual(self.panel.audit.get(),'Ожидание DQ');self.assertEqual(self.panel.reason.get(),'')
            self.assertEqual(str(self.panel.button['state']),'disabled')
        self.panel.bind_chart({'frame':self.frame(tf)});self.app.period.set('год');self.panel.ensure_context();self.assertFalse(self.panel.details)

    def test_no_progressbar_and_details_are_read_only_with_unchanged_source_reasons(self):
        self.panel.bind_chart({'frame':self.frame()})
        def classes(widget):
            return [widget.winfo_class()]+[c for child in widget.winfo_children() for c in classes(child)]
        self.assertNotIn('TProgressbar',classes(self.panel.frame))
        self.panel.show_details();self.root.update()
        windows=[x for x in self.root.winfo_children() if x.winfo_class()=='Toplevel'];self.assertEqual(len(windows),1)
        texts=[w for child in windows[0].winfo_children() for w in child.winfo_children() if w.winfo_class()=='Text']
        self.assertTrue(texts);self.assertEqual(str(texts[0]['state']),'disabled');self.assertIn('Неполные агрегированные свечи: 2',texts[0].get('1.0','end'))

    def test_missing_or_wrong_tf_frame_never_claims_current_analysis(self):
        self.panel.bind_chart({'frame':self.frame()});self.panel.bind_engine(dict(status='READY',signal='WAIT'))
        self.panel.bind_chart({'frame':None});self.assertNotIn('доступен',self.panel.analysis.get())
        self.app.timeframe.set('1H');self.panel.bind_chart({'frame':self.frame('4H')})
        self.assertEqual(self.panel.audit.get(),'Ожидание DQ')
        self.panel.bind_engine(dict(status='READY',signal='WAIT',timeframe_context={'4H':{}}));self.assertNotIn('доступен',self.panel.analysis.get())

if __name__=='__main__':unittest.main()
