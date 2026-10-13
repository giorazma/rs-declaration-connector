"""Local receipt ledger. Preparation never marks a declaration as submitted."""
from __future__ import annotations

import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from nbg import TBILISI, convert, currency_code, decimal_amount, iso_date, money


def validate_period(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}', value):
        raise ValueError('Use a monthly period including the year: YYYY-MM.')
    iso_date(value + '-01')
    return value


class Ledger:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS incomes (
                reference TEXT PRIMARY KEY, receipt_date TEXT NOT NULL,
                currency TEXT NOT NULL, amount TEXT NOT NULL,
                gel_exact TEXT NOT NULL, quote_json TEXT NOT NULL)''')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def serialize(row) -> dict:
        return {'reference': row['reference'], 'receipt_date': row['receipt_date'],
                'currency': row['currency'], 'amount': row['amount'],
                'gel_exact': row['gel_exact'], 'gel_display': money(Decimal(row['gel_exact'])),
                'rate': json.loads(row['quote_json'])}

    def record(self, reference: str, receipt_date: str, amount: str, currency: str,
               rate_provider, allow_identical_receipt: bool = False) -> dict:
        if not reference.strip() or len(reference) > 160:
            raise ValueError('Provide a stable receipt reference, at most 160 characters.')
        reference = reference.strip()
        iso_date(receipt_date)
        code = currency_code(currency)
        original = format(decimal_amount(amount), 'f')
        # Fetch only after checking idempotency; a retry keeps the original evidence.
        with self.connect() as db:
            existing = db.execute('SELECT * FROM incomes WHERE reference=?', (reference,)).fetchone()
        if existing:
            if (existing['receipt_date'] != receipt_date or existing['currency'] != code
                    or Decimal(existing['amount']) != Decimal(original)):
                raise ValueError('This reference already identifies a different receipt.')
            return {'saved': False, 'already_recorded': True, 'income': self.serialize(existing)}
        quote = rate_provider(receipt_date, code)
        if quote['requested_date'] != receipt_date or quote['currency'] != code:
            raise ValueError('The quote does not match this receipt.')
        result = convert(original, quote)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            # Another MCP client may have inserted the same receipt during lookup.
            existing = db.execute('SELECT * FROM incomes WHERE reference=?', (reference,)).fetchone()
            if existing:
                if (existing['receipt_date'] != receipt_date or existing['currency'] != code
                        or Decimal(existing['amount']) != Decimal(original)):
                    raise ValueError('This reference already identifies a different receipt.')
                return {'saved': False, 'already_recorded': True, 'income': self.serialize(existing)}
            similar = db.execute('SELECT * FROM incomes WHERE receipt_date=? AND currency=?',
                                 (receipt_date, code)).fetchall()
            duplicates = [r['reference'] for r in similar if Decimal(r['amount']) == Decimal(original)]
            if duplicates and not allow_identical_receipt:
                raise ValueError('An identical date/amount/currency is already recorded under '
                                 + ', '.join(duplicates) + '. Confirm it is a separate receipt.')
            db.execute('INSERT INTO incomes VALUES (?,?,?,?,?,?)',
                       (reference, receipt_date, code, original, result['gel_exact'],
                        json.dumps(quote, ensure_ascii=False)))
            row = db.execute('SELECT * FROM incomes WHERE reference=?', (reference,)).fetchone()
        return {'saved': True, 'already_recorded': False, 'income': self.serialize(row)}

    def summary(self, period: str) -> dict:
        validate_period(period)
        with self.connect() as db:
            rows = db.execute('SELECT * FROM incomes WHERE receipt_date LIKE ? ORDER BY receipt_date, reference',
                              (period + '-%',)).fetchall()
        exact = sum((Decimal(row['gel_exact']) for row in rows), Decimal('0'))
        return {'period': period, 'receipt_count': len(rows),
                'month_income_gel_exact': format(exact, 'f'),
                'month_income_gel': money(exact),
                'incomes': [self.serialize(row) for row in rows],
                'completeness': 'requires_user_confirmation'}

    def prepare(self, period: str, prior_year_to_date_gel: str, month_complete: bool) -> dict:
        validate_period(period)
        if period >= datetime.now(TBILISI).strftime('%Y-%m'):
            raise ValueError('Record receipts now; prepare the declaration after the month has ended.')
        if month_complete is not True:
            raise ValueError('Confirm all business income for this month has been included.')
        prior = decimal_amount(prior_year_to_date_gel, allow_zero=True)
        if period.endswith('-01') and prior != 0:
            raise ValueError('January must start with a zero prior-year-to-date amount.')
        summary = self.summary(period)
        cumulative = prior + Decimal(summary['month_income_gel_exact'])
        return {**summary, 'completeness': 'confirmed',
                'prior_year_to_date_gel': money(prior),
                'year_to_date_income_gel': money(cumulative),
                'status': 'prepared_locally', 'submitted': False,
                'rs_form_connected': False,
                'tax_gel': None,
                'next_step': 'Verify the current RS.GE form, tax treatment, previous YTD, and submit the reviewed monthly declaration.'}
