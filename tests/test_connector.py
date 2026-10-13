import json
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
(ROOT / '.test-work').mkdir(exist_ok=True)
sys.path.insert(0, str(ROOT))
from nbg import convert, decimal_amount, iso_date, parse_rate
from ledger import Ledger
from portal import TITLE, LABEL15, LABEL20, validate_form


def fixture(day='2025-09-02', code='EUR', rate='3.0424', quantity=1, effective=None):
    return [{'date': day+'T00:00:00.000Z', 'currencies': [{
        'code': code, 'rate': Decimal(rate), 'quantity': quantity,
        'validFromDate': (effective or day)+'T00:00:00.000Z',
        'date': '2025-09-01T17:00:00.000Z'}]}]


def fake_provider(day, code):
    rate = '3.0424' if day.endswith('02') else '3.1000'
    return parse_rate(fixture(day, code, rate), day, code)


class RateTests(unittest.TestCase):
    def test_exact_euro_conversion(self):
        quote = parse_rate(fixture(), '2025-09-02', 'EUR')
        self.assertEqual(convert('1000', quote)['gel_display'], '3042.40')
        self.assertEqual(quote['effective_date'], '2025-09-02')
        self.assertEqual(quote['published_at'][:10], '2025-09-01')

    def test_quoted_quantity(self):
        quote = parse_rate(fixture(code='JPY', rate='1.8750', quantity=100), '2025-09-02', 'JPY')
        self.assertEqual(convert('1000', quote)['gel_display'], '18.75')

    def test_rate_effective_later_rejected(self):
        with self.assertRaises(ValueError):
            parse_rate(fixture(effective='2025-09-03'), '2025-09-02', 'EUR')

    def test_weekend_uses_effective_prior_rate(self):
        quote = parse_rate(fixture(day='2025-09-07', effective='2025-09-06'), '2025-09-07', 'EUR')
        self.assertEqual(quote['effective_date'], '2025-09-06')

    def test_missing_currency_and_stale_rate_rejected(self):
        for payload in (fixture(code='USD'), fixture(effective='2025-08-01')):
            with self.assertRaises(ValueError):
                parse_rate(payload, '2025-09-02', 'EUR')

    def test_invalid_dates_amounts_rates(self):
        for day in ('09-02', '2025-02-30', '2025-9-2'):
            with self.assertRaises(ValueError):
                iso_date(day)
        for amount in ('NaN', '-1', '1.001', '1,000', '0'):
            with self.assertRaises(ValueError):
                decimal_amount(amount)
        for rate in ('NaN', '0', '-1'):
            with self.assertRaises(ValueError):
                parse_rate(fixture(rate=rate), '2025-09-02', 'EUR')


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=ROOT / '.test-work')
        assert Path(self.temp.name).resolve().is_relative_to((ROOT / '.test-work').resolve())
        self.addCleanup(self.temp.cleanup)
        self.ledger = Ledger(Path(self.temp.name) / 'test.sqlite3')

    def record(self, ref='first', day='2025-09-02', amount='1000', allow=False):
        return self.ledger.record(ref, day, amount, 'EUR', fake_provider, allow)

    def test_each_day_own_rate_and_month_total(self):
        self.record()
        self.record('second', '2025-09-20', '500')
        self.record('other-month', '2025-08-20', '10')
        summary = self.ledger.summary('2025-09')
        self.assertEqual(summary['receipt_count'], 2)
        self.assertEqual(summary['month_income_gel'], '4592.40')
        prepared = self.ledger.prepare('2025-09', '12000', True)
        self.assertEqual(prepared['year_to_date_income_gel'], '16592.40')
        self.assertFalse(prepared['submitted'])
        self.assertFalse(prepared['rs_form_connected'])

    def test_retry_is_idempotent_without_new_rate_lookup(self):
        self.record()
        result = self.ledger.record('first', '2025-09-02', '1000.00', 'EUR',
                                    lambda *args: self.fail('Idempotent retry fetched a new rate'))
        self.assertTrue(result['already_recorded'])
        self.assertEqual(self.ledger.summary('2025-09')['receipt_count'], 1)

    def test_conflicting_reference_rejected(self):
        self.record()
        with self.assertRaises(ValueError):
            self.record(amount='2000')

    def test_similar_receipt_requires_explicit_distinct_receipt(self):
        self.record()
        with self.assertRaises(ValueError):
            self.record('second')
        self.record('second', allow=True)
        self.assertEqual(self.ledger.summary('2025-09')['month_income_gel'], '6084.80')

    def test_preparation_requires_complete_month_and_prior_ytd(self):
        with self.assertRaises(ValueError):
            self.ledger.prepare('2025-09', '0', False)
        with self.assertRaises(ValueError):
            self.ledger.prepare('2025-01', '100', True)
        self.assertEqual(self.ledger.prepare('2025-01', '0', True)['month_income_gel'], '0.00')

    def test_live_or_future_month_not_prepared(self):
        from datetime import datetime
        from nbg import TBILISI
        with self.assertRaises(ValueError):
            self.ledger.prepare(datetime.now(TBILISI).strftime('%Y-%m'), '0', True)


class FormValidationTests(unittest.TestCase):
    def setUp(self):
        self.form = {'taxpayer_id':'00000000000', 'period':'2025-09', 'title':TITLE,
                     'fields': {str(i): {'value':'0', 'readonly':i in (16,17,22,23,24,25,26),
                                         'disabled':False,'label':''} for i in range(15,30)}}
        self.form['fields']['15']['label'] = LABEL15
        self.form['fields']['20']['label'] = LABEL20
        self.prepared = {'month_income_gel':'3042.40','year_to_date_income_gel':'15042.40'}

    def validate(self):
        validate_form(self.form,'2025-09','00000000000',self.prepared)

    def test_supported_blank_form(self):
        self.validate()

    def test_wrong_taxpayer_or_period_rejected(self):
        self.form['taxpayer_id'] = '11111111111'
        with self.assertRaises(ValueError): self.validate()
        self.form['taxpayer_id'] = '00000000000'
        self.form['period'] = '2025-08'
        with self.assertRaises(ValueError): self.validate()

    def test_other_income_and_accrual_cases_rejected(self):
        self.form['fields']['18']['value'] = '100'
        with self.assertRaises(ValueError): self.validate()
        self.form['fields']['18']['value'] = '0'
        self.form['fields']['22']['readonly'] = False
        with self.assertRaises(ValueError): self.validate()

    def test_changed_form_or_existing_different_draft_rejected(self):
        self.form['fields']['20']['label'] = 'Another field'
        with self.assertRaises(ValueError): self.validate()
        self.form['fields']['20']['label'] = LABEL20
        self.form['fields']['20']['value'] = '100'
        with self.assertRaises(ValueError): self.validate()

    def test_same_prepared_draft_is_safe_to_retry(self):
        self.form['fields']['15']['value'] = '15042.40'
        self.form['fields']['20']['value'] = '3042.40'
        self.validate()


if __name__ == '__main__':
    unittest.main()
