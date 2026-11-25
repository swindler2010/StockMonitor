"""Risk explanation reflects actual calculation; never changes a decision."""
import copy, unittest
import test_trading_engine as fixture
from trading_engine import evaluate
from cp_controls import TV_PRICE_MODEL, RISK_CONTEXT


class RiskExplanationTests(unittest.TestCase):
    def run_case(self, risk):
        contexts = fixture.EngineTests().contexts(trigger=False)
        before = evaluate(contexts, '1H', {'quantity': 0})
        contexts['1H']['accepted_risk'] = risk
        after = evaluate(contexts, '1H', {'quantity': 0})
        for key in ('signal', 'status', 'score', 'confidence', 'blockers',
                    'entry_blockers', 'bullish_reasons', 'bearish_reasons'):
            self.assertEqual(before[key], after[key])
        return contexts, after

    def test_actual_only_does_not_claim_estimated_prices(self):
        _, result = self.run_case(dict(price_model=TV_PRICE_MODEL, price_estimates_used=0))
        self.assertIn('только по реальным закрытым свечам', '; '.join(result['warnings']))
        self.assertNotIn('средние соседей', '; '.join(result['warnings']))

    def test_estimates_are_disclosed_only_when_used(self):
        _, result = self.run_case(dict(price_model='legacy', price_estimates_used=3))
        self.assertIn('средние соседей', '; '.join(result['warnings']))
        _, result = self.run_case(dict(price_model='legacy', price_estimates_used=0))
        self.assertNotIn('средние соседей', '; '.join(result['warnings']))

    def test_explanation_never_accepts_invalid_or_stale_data(self):
        contexts, _ = self.run_case(dict(price_model=TV_PRICE_MODEL, context=RISK_CONTEXT, approved=True))
        for quality in ({'state': 'INVALID'}, {'state': 'UNCONFIRMED', 'stale_count': 1}):
            changed = copy.deepcopy(contexts); changed['1H']['data_quality'] = quality
            result = evaluate(changed, '1H', {'quantity': 0})
            self.assertEqual(result['status'], 'DATA BLOCKED')
            self.assertEqual(result['signal'], 'WAIT')


if __name__ == '__main__': unittest.main()
