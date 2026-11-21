import sys,unittest
from pathlib import Path
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
from position_calculator import calculate,FIELDS,show


class PositionCalculatorTests(unittest.TestCase):
    def values(self,**overrides):return {**{k:v for k,_,v in FIELDS},**overrides}

    def test_long_short_integer_and_fractional_size_respect_risk_and_cash(self):
        for direction,values in [('long',self.values()),('short',self.values(stop='105',target='90')),('long',self.values(step='0.001',unit_cost='0.1',fixed_cost='2'))]:
            r=calculate(values,direction);self.assertLessEqual(r['loss'],r['budget']);self.assertLessEqual(r['reserved'],Decimal(values['cash']))
        r=calculate(self.values());self.assertEqual(r['quantity'],20);self.assertEqual(r['loss'],100);self.assertEqual(r['rr'],2)
        r=calculate(self.values(cash='99'));self.assertEqual(r['quantity'],0);self.assertEqual(r['costs'],0);self.assertIsNone(r['rr'])

    def test_costs_reduce_reward_and_invalid_or_nonfinite_inputs_reject(self):
        r=calculate(self.values(unit_cost='0.1',fixed_cost='2'));self.assertEqual(r['quantity'],19);self.assertEqual(r['loss'],Decimal('98.9'));self.assertEqual(r['reward'],Decimal('186.1'))
        for override in [dict(stop='101'),dict(target='99'),dict(entry='NaN'),dict(risk_pct='101'),dict(step='0'),dict(capital='Infinity'),dict(cash='-1')]:
            with self.assertRaises(ValueError):calculate(self.values(**override))
        self.assertEqual(calculate(self.values(unit_cost='0,1'))['quantity'],19)

    def test_actual_widgets_calculate_in_three_languages_without_account_or_setting_writes(self):
        import tkinter as tk
        from main_screen import theme
        from themes import THEMES
        for code,word in [('ru','Количество'),('de','Menge'),('en','Quantity')]:
            with patch('i18n._language',code):
                root=tk.Tk();root.withdraw()
                def setting(key,default=None):
                    self.assertEqual(key,'theme');return default
                app=SimpleNamespace(root=root,store=SimpleNamespace(setting=setting))
                try:
                    theme(app,THEMES['Тёмная']);win=show(app);root.update();d=app._position_calculator_dialog
                    self.assertIn(word,d.output.get());d.values['stop'].set('98');self.assertNotIn(word+':',d.output.get());d.button.invoke();self.assertIn('50',d.output.get())
                    root.update();self.assertTrue(d.button.winfo_ismapped());self.assertIs(show(app),win)
                finally:root.destroy()


if __name__=='__main__':unittest.main()
