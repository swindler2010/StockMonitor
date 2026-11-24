import sys
import unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).parent/'stock_monitor'))
from financial_sentiment import evaluate,_evaluate_title


class FinancialSentimentTests(unittest.TestCase):
    def test_financial_subject_changes_direction(self):
        cases={'Profit increased':'positive','Revenue fell':'negative','Costs fell':'positive','Losses narrowed':'positive',
               'Costs increased':'negative','Debt increased':'negative','Sales grew but costs increased':'unknown',
               'Revenue fell while losses narrowed':'unknown','Shares surge':'positive','Shares plunge':'negative',
               'Annual general meeting scheduled for June':'neutral','Will profit rise?':'unknown',
               'Profit did not increase':'unknown','The company may raise its outlook':'unknown'}
        for text,label in cases.items():self.assertEqual(evaluate({'title':text})['label'],label,text)

    def test_original_not_translation_and_ru_de_phrases(self):
        cases={'Прибыль выросла':'positive','Выручка снизилась':'negative','Расходы снизились':'positive',
               'Убытки выросли':'negative','Акции падают':'negative','Акции растут':'positive',
               'Собрание акционеров':'neutral','Выручка не выросла':'unknown',
               'Gewinn gestiegen':'positive','Umsatz sank':'negative','Kosten gestiegen':'negative',
               'Kosten gesenkt':'positive','Aktien fallen':'negative','Hauptversammlung im Juni':'neutral',
               'Gewinn nicht gestiegen':'unknown'}
        for original,label in cases.items():
            self.assertEqual(evaluate({'original_title':original,'title':'Translation contradicts original'})['label'],label,original)

    def test_53_and_200_text_revisions_and_result_mutation_cannot_poison_memo(self):
        _evaluate_title.cache_clear()
        for count in (53,200):
            for i in range(count):
                a={'title':f'Company {i}: profit increased'};b={'title':f'Company {i}: profit fell'}
                value=evaluate(a);self.assertEqual(value['label'],'positive');value['label']='negative'
                self.assertEqual(evaluate(a)['label'],'positive');self.assertEqual(evaluate(b)['label'],'negative')
        self.assertLessEqual(_evaluate_title.cache_info().currsize,2048)


if __name__=='__main__':unittest.main()
