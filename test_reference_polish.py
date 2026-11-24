"""Targeted presentation regression; no working database, source, or order execution."""
import copy
from pathlib import Path
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import Mock,patch

sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
import i18n
from reference_design import signal_reason,concise_reason,state_label,polish_chart
from portfolio_design import metrics,Surface


class PresentationTests(unittest.TestCase):
    def setUp(self):
        old=i18n.language();self.addCleanup(setattr,i18n,'_language',old)

    def test_reason_is_complete_localized_and_unknown_identity_is_preserved(self):
        reason='DAX: OUTPERFORMING; Timing TF сохраняет bearish структуру'
        expected={'ru':'нисходящая структура','de':'bärische Struktur','en':'bearish structure'}
        for lang,part in expected.items():
            i18n._language=lang
            result=signal_reason(reason)
            self.assertIn(part,result);self.assertTrue(result.startswith('DAX: '))
            self.assertNotIn('OUTPERFORMING',result);self.assertNotIn('…',result)
            self.assertEqual(signal_reason('GETTEX / EUR / US1234567890 / provider x'),
                             'GETTEX / EUR / US1234567890 / provider x')
        i18n._language='ru'
        self.assertEqual(state_label('BEARISH'),'Снижение')

    def test_presentation_leaves_engine_inputs_and_decisions_unchanged(self):
        from trading_view import format_engine
        engine=dict(status='READY',signal='WAIT',confidence=50,reason='DAX: OUTPERFORMING; Timing TF сохраняет bearish структуру',
                    timeframe_context={'4H':dict(facts={'regime':'BEARISH'},data_quality={'state':'INVALID'})},
                    benchmark_context={'state':'OUTPERFORMING','benchmark_name':'DAX','quality':'READY'})
        before=copy.deepcopy(engine)
        for lang in ('ru','de','en'):
            i18n._language=lang;title,details=format_engine(engine)
            self.assertEqual(title,'WAIT');self.assertIn('INVALID',details)
            self.assertIn('DAX',details);self.assertEqual(engine,before)

    def test_metrics_preserve_zero_negative_and_missing_account_values(self):
        state=dict(status='ACTIVE',equity='9950',cash='0',fees='4',policy={'starting_cash':'10000'})
        before=copy.deepcopy(state);result=metrics(state)
        self.assertEqual(float(result['return']),-.005);self.assertEqual(result['cash'],'0')
        self.assertEqual(state,before)
        self.assertIsNone(metrics({**state,'equity':None})['return'])
        self.assertIsNone(metrics({**state,'policy':{'starting_cash':'0'}})['return'])
        self.assertTrue(all(v is None for v in metrics({'status':'ERROR'}).values()))

    def test_card_selects_a_whole_deciding_reason_and_keeps_full_analysis_available(self):
        from trading_view import format_engine
        engine=dict(status='READY',signal='WAIT',reason='DAX: OUTPERFORMING; Timing TF сохраняет bearish структуру; extra evidence',
                    entry_blockers=['Timing TF сохраняет bearish структуру'])
        before=copy.deepcopy(engine)
        self.assertEqual(concise_reason(engine),signal_reason(engine['entry_blockers'][0]))
        self.assertIn('extra evidence',format_engine(engine)[1]);self.assertEqual(engine,before)
        self.assertEqual(concise_reason(dict(status='DATA BLOCKED',blockers=[dict(text='GETTEX / EUR / unknown proof')])),
                         'GETTEX / EUR / unknown proof')

    def test_chart_notes_have_their_own_measured_strip_and_keep_limits(self):
        from matplotlib.figure import Figure
        from matplotlib.backends.backend_agg import FigureCanvasAgg
        fig=Figure(figsize=(8,5),dpi=100);canvas=FigureCanvasAgg(fig)
        gs=fig.add_gridspec(2,1,top=.93,left=.04,right=.8)
        ax=fig.add_subplot(gs[0]);ax.plot([0,1,2],[10,12,11],label='EMA 10')
        ax.legend(loc='upper left');ax.set_title('GETTEX · EUR · 4H',loc='left',fontsize=8)
        notes=['S/R: сплошные — ближайшие; пунктир — исторические',
               'Живая свеча — предварительные данные','Получены данные до 24.11 22:00']
        for gid,note in zip(('sm-level-key','sm-chart-live','sm-chart-through'),notes):ax.text(.9,.9,note,gid=gid)
        app=SimpleNamespace(main_screen=True,ax_price=ax,fig=fig,canvas=canvas)
        limits=ax.get_xlim(),ax.get_ylim();data=copy.deepcopy(ax.lines[0].get_ydata())
        for width in (8,5,10):
            fig.set_size_inches(width,5);polish_chart(app);canvas.draw()
            strip=next(t for t in fig.texts if t.get_gid()=='sm-chart-notes')
            self.assertEqual(' '.join(strip.get_text().split()),' '.join(' '.join(notes).split()))
            box=strip.get_window_extent(canvas.get_renderer())
            self.assertGreater(box.y0,ax.get_position().y1*fig.bbox.height+8)
            self.assertLessEqual(box.x1,fig.bbox.width*.8+2)
            self.assertEqual((ax.get_xlim(),ax.get_ylim()),limits)
            self.assertTrue((ax.lines[0].get_ydata()==data).all())
        self.assertEqual(len(fig.texts),1)


class TkPresentationTests(unittest.TestCase):
    def setUp(self):
        import tkinter as tk
        self.root=tk.Tk();self.root.geometry('1100x850+20000+20000')
        self.addCleanup(self.root.destroy)

    def test_full_reason_fits_at_narrow_and_wide_signal_widths(self):
        import tkinter as tk
        from tkinter import ttk
        from main_screen import decision_row
        ttk.Style(self.root).configure('DeskSignal.TLabel',font=('Segoe UI',16,'bold'))
        title=tk.StringVar(self.root,'WAIT');reason=tk.StringVar(self.root,signal_reason('DAX: OUTPERFORMING; Timing TF сохраняет bearish структуру'))
        row=decision_row(self.root,title,reason)
        for width in (290,380,290):
            self.root.geometry(f'{width}x450+20000+20000');self.root.update()
            for child in row.winfo_children():
                self.assertLessEqual(child.winfo_height(),row.winfo_height())
                self.assertLessEqual(child.winfo_x()+child.winfo_width(),row.winfo_width())
                self.assertGreaterEqual(child.winfo_height(),child.winfo_reqheight())
        self.assertNotIn('…',reason.get())

    def test_surface_changes_styles_without_replacing_buttons_variables_or_bindings(self):
        import tkinter as tk
        from tkinter import ttk
        from main_screen import theme
        from themes import THEMES
        app=SimpleNamespace(root=self.root,store=SimpleNamespace(setting=lambda k,d=None:d))
        theme(app,THEMES['Тёмная']);host=ttk.Frame(self.root);host.pack(fill='both',expand=True)
        surface=Surface(app,host);card=surface.card(host,'Поручение владельца • только PAPER');card.pack(fill='x')
        command=Mock();button=ttk.Button(card,text='BUY',command=command,state='disabled');button.pack()
        quantity=tk.StringVar(self.root,'7');entry=ttk.Entry(card,textvariable=quantity);entry.pack()
        binding=entry.bind('<Return>',lambda _:None);old_command=button.cget('command')
        surface.finish();surface.bind(dict(status='ACTIVE',account='V1',equity='10000',cash='9000',fees='2',policy={'starting_cash':'10000'}))
        self.root.update()
        self.assertEqual(button.cget('command'),old_command);self.assertIn('disabled',button.state())
        self.assertEqual(quantity.get(),'7');self.assertIn(binding,entry.bind('<Return>'))
        command.assert_not_called();card.configure(text='Поручение владельца • V1')
        self.assertIn('V1',card.heading.cget('text'))
        self.assertIn('10 000',surface.values['equity'].get())

    def test_actual_paper_ui_retains_four_order_callbacks_and_creates_no_jobs_or_settings(self):
        import tkinter as tk
        from tkinter import ttk
        import paper_view
        app=SimpleNamespace(root=self.root,store=SimpleNamespace(setting=lambda k,d=None:d),
                            watchlist=[],symbol=tk.StringVar(self.root,''),job=Mock(return_value=False),closing=False)
        host=ttk.Frame(self.root);host.pack(fill='both',expand=True)
        with patch.object(paper_view,'_auto_refresh'),patch('paper_orders.submit') as submit:
            paper_view.attach(app,host);self.root.update()
            def walk(widget):
                return [widget]+[c for child in widget.winfo_children() for c in walk(child)]
            buttons=[w for w in walk(host) if isinstance(w,ttk.Button)]
            names=[w.cget('text') for w in buttons]
            for name in ('Цена BUY','Цена SELL','BUY','SELL'):
                button=next(w for w in buttons if w.cget('text')==name)
                self.assertTrue(button.cget('command'))
            submit.assert_not_called();app.job.assert_not_called()

    def test_portfolio_language_changes_only_displayed_labels_and_not_canonical_choices(self):
        import tkinter as tk
        from tkinter import ttk
        from localized_widgets import CanonicalCombobox
        import paper_view
        old=i18n.language();self.addCleanup(setattr,i18n,'_language',old)
        for lang,buy in (('en','BUY price'),('de','BUY-Preis'),('ru','Цена BUY')):
            i18n._language=lang
            app=SimpleNamespace(root=self.root,store=SimpleNamespace(setting=lambda k,d=None:d),
                                watchlist=[],symbol=tk.StringVar(self.root,''),job=Mock(return_value=False),closing=False)
            host=ttk.Frame(self.root);host.pack(fill='both',expand=True)
            with patch.object(paper_view,'_auto_refresh'),patch('paper_orders.submit') as submit:
                paper_view.attach(app,host);self.root.update()
                def walk(widget):return [widget]+[c for child in widget.winfo_children() for c in walk(child)]
                widgets=walk(host)
                self.assertIn(buy,[w.cget('text') for w in widgets if isinstance(w,ttk.Button)])
                scope=next(w for w in widgets if isinstance(w,CanonicalCombobox) and 'Архив' in w.cget('values'))
                scope.set('Архив');self.assertEqual(scope.get(),'Архив')
                self.assertEqual(scope._display.get(),i18n.tr('Архив'))
                submit.assert_not_called();app.job.assert_not_called()
            host.destroy()

    def test_legacy_dialog_caption_uses_current_theme_without_duplicate_bindings(self):
        import tkinter as tk
        from reference_design import window_captions
        from themes import THEMES
        app=SimpleNamespace(root=self.root)
        with patch('main_screen.native_titlebar') as apply:
            window_captions(app,THEMES['Тёмная'])
            binding=self.root._reference_caption_binding
            win=tk.Toplevel(self.root);win.geometry('300x180+20000+20000')
            self.root.update()
            self.assertTrue(any(c.args[0] is win for c in apply.call_args_list))
            window_captions(app,THEMES['Светлая']);self.root.update()
            self.assertEqual(self.root._reference_caption_binding,binding)
            self.assertEqual(app.root._reference_caption_palette,THEMES['Светлая'])
            self.assertEqual(apply.call_args.args[1],THEMES['Светлая'])
            win.destroy()


class HeaderLayoutTests(unittest.TestCase):
    def test_status_date_and_menu_do_not_overlap_at_supported_widths(self):
        from test_main_screen import MainScreenTests
        from tkinter import ttk
        fixture=MainScreenTests('test_menu_clones_preserve_callbacks_and_signal_percent_is_not_invented')
        fixture.setUp();self.addCleanup(fixture.doCleanups)
        root=fixture.root;screen=fixture.screen
        fixture.app.cp_label.configure(text='● CandlePilot (CP) — ACTIVE')
        screen.sync_text.set('Синхронизация: обновление')
        for width in (1000,1422,1800):
            root.geometry(f'{width}x850+20000+20000');root.update();root.update_idletasks()
            menu=screen.menu_buttons[-1].master
            status=screen.sync_label.master
            self.assertLessEqual(menu.winfo_x()+menu.winfo_width(),status.winfo_x())
            for child in status.winfo_children():
                self.assertGreaterEqual(child.winfo_width(),child.winfo_reqwidth())
            self.assertEqual(len(screen.menu_buttons),8)
        fixture.test_menu_clones_preserve_callbacks_and_signal_percent_is_not_invented()


if __name__=='__main__':unittest.main()
