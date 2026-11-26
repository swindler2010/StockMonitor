"""V1 presentation: exact side filters and read-only, localized advice."""
import copy
from datetime import datetime, timedelta, timezone
import sys
from pathlib import Path
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent / 'stock_monitor'))
import i18n
import real_portfolio_view as view
import test_real_portfolio as fixture


class ReadableTests(unittest.TestCase):
    def test_recommendation_separates_decision_levels_and_quality_without_mutation(self):
        result = dict(signal='WAIT', core='READY',
            reason='V1_WEEKLY_TREND_WAIT; 1H: предупреждение CandlePilot UNCONFIRMED; расчёт по фактическим GETTEX-барам',
            identity={'currency':'EUR'}, long_term={'signal_close':12, 'support':10, 'entry_level':13})
        before = copy.deepcopy(result)
        with patch.object(i18n, '_language', 'ru'):
            heading, summary, reasons, warnings, levels = view.recommendation(result)
        self.assertEqual(heading, i18n.tr(view.DECISIONS['WAIT'][0]))
        self.assertIn('Недельный тренд', reasons[0])
        self.assertEqual(len(reasons), 1)
        self.assertEqual(len(warnings), 1)
        self.assertEqual([label for label,_ in levels], ['Закрытие дня', 'Поддержка', 'Ориентир для входа'])
        self.assertEqual(result, before)
        result['core'] = 'DATA BLOCKED'
        self.assertEqual(view.recommendation(result)[0], 'Расчёт недоступен')

    def test_nested_and_manual_execution_fields_keep_actual_values_and_currency(self):
        event = dict(id='synthetic-sell',status='SETTLED',lastEventAt='2026-11-25T10:00:00Z',
            security=dict(side='SELL',isin='TEST',quantity='2',amount='24',executionPrice='12',currency='USD'))
        before = copy.deepcopy(event)
        kind, row, detail = view.operation_view(event, {'TEST':'Synthetic'})
        self.assertEqual(kind,'SELL')
        self.assertIn('Synthetic',row)
        self.assertIn('USD',detail)
        self.assertIn('synthetic-sell',detail)
        self.assertEqual(event,before)
        manual = dict(id='synthetic-buy',kind='BUY',source='OWNER_RECORDED_EXECUTION',isin='TEST',
            at='2026-11-25T11:00:00Z',quantity='1',price='9',amount='9')
        self.assertEqual(view.operation_view(manual,{})[0],'BUY')
        self.assertIn('EUR',view.operation_view(manual,{})[2])

    def test_all_new_presentation_messages_have_both_translations(self):
        import ast
        tree = ast.parse(Path(view.__file__).read_text(encoding='utf-8'))
        catalog = i18n.catalog()
        keys = set(view.DECISIONS[k][0] for k in view.DECISIONS)
        keys.update(v for pair in view.DECISIONS.values() for v in pair)
        keys.update(view.REASONS.values())
        keys.update(['Когда покупать или докупать','Сколько покупать','Как долго держать','Прибыль на акцию','Свободный денежный поток'])
        keys.update(view.OPERATION_NAMES.values())
        keys.update(view.OPERATION_STATUSES.values())
        for node in ast.walk(tree):
            if isinstance(node,ast.Call) and isinstance(node.func,ast.Name) and node.func.id=='tr' and node.args:
                value=node.args[0]
                if isinstance(value,ast.Constant) and isinstance(value.value,str): keys.add(value.value)
        for key in keys:
            for lang in ('de','en'):
                with self.subTest(key=key,lang=lang):
                    self.assertIn(lang,catalog.get(key,{}))
                    self.assertEqual(sorted(i18n.fields(key)), sorted(i18n.fields(catalog[key][lang])))


class ReadableViewTests(unittest.TestCase):
    def setUp(self):
        self.fixture=fixture.RealViewTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def test_buy_sell_filters_reset_paging_and_show_exact_selected_event(self):
        from tkinter import ttk
        import tkinter as tk
        f=self.fixture
        before=copy.deepcopy(f.state)
        events=[]
        for i in range(60):
            events.append(dict(id=f'buy-{i}',status='SETTLED',lastEventAt=f'2026-11-25T10:{i:02}:00Z',
                security=dict(side='BUY',isin='TEST',quantity='1',amount='10',currency='EUR')))
        events.extend([dict(id='sell-only',status='FILLED',lastEventAt='2026-11-25T11:00:00Z',
            security=dict(side='SELL',isin='TEST',quantity='2',amount='24',currency='EUR')),
            dict(id='deposit-only',status='SETTLED',lastEventAt='2026-11-25T09:00:00Z',cash=dict(transactionType='DEPOSIT',amount='50',currency='EUR'))])
        f.state['broker_transactions']=events
        f.state['revision']+=1
        next(w for w in f.widgets() if isinstance(w,ttk.Button) and w.cget('text')==i18n.tr('Перечитать сохранённые данные')).invoke()
        f.finish(1)
        tree=next(w for w in f.widgets() if isinstance(w,ttk.Treeview) and 'status' in w['columns'])
        combo=next(w for w in f.widgets() if isinstance(w,ttk.Combobox) and i18n.tr('Покупки') in w['values'])
        next(w for w in f.widgets() if isinstance(w,ttk.Button) and w.cget('text')=='▶').invoke()
        self.assertEqual(len(tree.get_children()),12)
        combo.set(i18n.tr('Продажи'));combo.event_generate('<<ComboboxSelected>>');f.root.update()
        self.assertEqual(len(tree.get_children()),1)
        self.assertEqual(tree.set(tree.get_children()[0],'kind'),i18n.tr('Продажа'))
        tree.selection_set(tree.get_children()[0]);tree.event_generate('<<TreeviewSelect>>');f.root.update()
        detail=next(w for w in f.widgets() if isinstance(w,tk.Text) and 'sell-only' in w.get('1.0','end'))
        self.assertNotIn('deposit-only',detail.get('1.0','end'))
        combo.set(i18n.tr('Покупки'));combo.event_generate('<<ComboboxSelected>>');f.root.update()
        self.assertEqual(len(tree.get_children()),50)
        self.assertTrue(all(tree.set(k,'kind')==i18n.tr('Покупка') for k in tree.get_children()))
        self.assertNotIn('sell-only',detail.get('1.0','end'))
        self.assertEqual(f.state['cash'],before['cash'])
        self.assertEqual(f.state['positions'],before['positions'])
        self.assertEqual(len(f.jobs),2)
        f.app.compute_chart.calculate.assert_not_called()

    def test_advice_renders_sections_and_human_decision_without_writes(self):
        import tkinter as tk
        from tkinter import ttk
        f=self.fixture
        before=copy.deepcopy(f.state)
        next(w for w in f.widgets() if isinstance(w,ttk.Button) and w.cget('text')==i18n.tr('Проверить рекомендацию')).invoke()
        now=datetime.now(timezone.utc)
        result=dict(signal='WAIT',core='READY',reason='V1_DAILY_SETUP_WAIT',real_quantity='2',
            identity={'currency':'EUR'},long_term={'support':'10','signal_day':'2026-11-24'},
            generated_at=now.isoformat(),valid_until=(now+timedelta(minutes=5)).isoformat(),_input_revisions={})
        f.jobs[1][1](result);f.root.update()
        text=next(w.get('1.0','end') for w in f.widgets() if isinstance(w,tk.Text) and i18n.tr('Что показывает график') in w.get('1.0','end'))
        self.assertTrue(text.startswith(i18n.tr(view.DECISIONS['WAIT'][0])))
        self.assertIn('На дневном графике пока нет сигнала',text)
        self.assertNotIn('V1_DAILY_SETUP_WAIT',text)
        self.assertIn('Уровни на графике',text)
        self.assertEqual(f.state,before)
        self.assertEqual(len(f.jobs),2)


if __name__=='__main__': unittest.main()
