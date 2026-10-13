"""Official NBG rates and decimal conversion; no estimated exchange rates."""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from urllib.error import URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

CENT = Decimal('0.01')
TBILISI = timezone(timedelta(hours=4))
API = 'https://nbg.gov.ge/gw/api/ct/monetarypolicy/currencies/en/json/'


def iso_date(value: str) -> date:
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
        raise ValueError('Use a full date including the year: YYYY-MM-DD.')
    result = date.fromisoformat(value)
    if result > datetime.now(TBILISI).date():
        raise ValueError('Future receipt dates are not supported.')
    return result


def decimal_amount(value: str, *, allow_zero: bool = False) -> Decimal:
    if not isinstance(value, str) or not re.fullmatch(r'\d{1,12}(\.\d{1,2})?', value):
        raise ValueError('Money must be a decimal string with at most two decimal places.')
    result = Decimal(value)
    if result < 0 or (result == 0 and not allow_zero):
        raise ValueError('Income must be positive.')
    return result


def money(value: Decimal) -> str:
    return format(value.quantize(CENT, rounding=ROUND_HALF_UP), '.2f')


def currency_code(value: str) -> str:
    result = value.upper().strip()
    if not re.fullmatch(r'[A-Z]{3}', result):
        raise ValueError('Use a three-letter currency code, such as EUR or USD.')
    return result


def parse_rate(payload: object, requested_date: str, currency: str) -> dict:
    """Use the rate effective on the requested date, not its publication date."""
    requested = iso_date(requested_date)
    code = currency_code(currency)
    candidates = []
    if not isinstance(payload, list):
        raise ValueError('NBG returned an unexpected response.')
    for batch in payload:
        if not isinstance(batch, dict) or not isinstance(batch.get('currencies'), list):
            raise ValueError('NBG returned an unexpected response.')
        batch_date = date.fromisoformat(str(batch.get('date', ''))[:10])
        for item in batch['currencies']:
            if item.get('code') != code:
                continue
            effective = date.fromisoformat(str(item.get('validFromDate', batch.get('date', '')))[:10])
            if batch_date > requested or effective > requested:
                continue
            # A dated official response may use the preceding business day's rate.
            # A very old response is rejected, rather than treated as today's rate.
            if (requested - effective).days > 7:
                continue
            try:
                rate = Decimal(str(item['rate']))
                quantity = Decimal(str(item['quantity']))
            except (KeyError, InvalidOperation) as exc:
                raise ValueError('NBG returned invalid rate data.') from exc
            if not rate.is_finite() or not quantity.is_finite() or rate <= 0 or quantity <= 0:
                raise ValueError('NBG returned invalid rate data.')
            if quantity != quantity.to_integral_value():
                raise ValueError('NBG returned an invalid currency quantity.')
            candidates.append((effective, {
                'currency': code, 'requested_date': requested_date,
                'effective_date': effective.isoformat(),
                'published_at': item.get('date'), 'quoted_rate_gel': str(rate),
                'quoted_quantity': str(quantity),
                'gel_per_unit': str(rate / quantity),
                'source_url': f'{API}?date={requested_date}',
            }))
    if not candidates:
        raise ValueError(f'No valid official {code} rate was returned for {requested_date}.')
    return max(candidates, key=lambda pair: pair[0])[1]


def get_rate(receipt_date: str, currency: str) -> dict:
    iso_date(receipt_date)
    code = currency_code(currency)
    if code == 'GEL':
        return {'currency': code, 'requested_date': receipt_date,
                'effective_date': receipt_date, 'published_at': None,
                'quoted_rate_gel': '1', 'quoted_quantity': '1',
                'gel_per_unit': '1', 'source_url': None}
    request = Request(f'{API}?date={receipt_date}', headers={
        'Accept': 'application/json', 'User-Agent': 'Personal-RS-Connector/0.1'})
    try:
        with urlopen(request, timeout=25) as response:
            if urlparse(response.url).hostname != 'nbg.gov.ge':
                raise ValueError('Unexpected redirect from the NBG API.')
            raw = response.read(2_000_001)
        if len(raw) > 2_000_000:
            raise ValueError('NBG response is too large.')
        payload = json.loads(raw, parse_float=Decimal)
    except (URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise RuntimeError('Could not obtain an official NBG rate; no income was saved.') from exc
    return parse_rate(payload, receipt_date, code)


def convert(amount: str, quote: dict) -> dict:
    original = decimal_amount(amount)
    converted = original * Decimal(quote['quoted_rate_gel']) / Decimal(quote['quoted_quantity'])
    return {'amount': format(original, 'f'), 'gel_exact': format(converted, 'f'),
            'gel_display': money(converted), 'rate': quote}
