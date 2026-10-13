"""Browser adapter for the inspected 2026 RS.GE form. Submission is unavailable.

The browser has an ephemeral session. Passwords, cookies, and SMS codes are never
exported to this server's files or passed through MCP tool arguments.
"""
from __future__ import annotations

import asyncio
import re
from decimal import Decimal
from urllib.parse import urlparse

from nbg import money

TITLE = 'მცირე ბიზნესის სტატუსის მქონე ფიზიკური პირის საშემოსავლო გადასახადის ყოველთვიური დეკლარაცია'
LABEL15 = 'ეკონომიკური საქმიანობიდან მიღებული მცირე ბიზნესის ერთობლივი შემოსავალი'
LABEL20 = 'უნაღდო ანგარიშსწორებით მიღებული შემოსავალი'
FORM_GUIDE = ('RS.GE: მენიუ → დეკლარაციები → ყოველთვიური → '
              'მცირე ბიზნესის საშემოსავლო გადასახადი → შესაბამისი პერიოდი → ახალი დეკლარაცია.')


def validate_form(form: dict, period: str, expected_tin: str, prepared: dict) -> None:
    """Reject the wrong account, form, month, accounting basis, or competing data."""
    if not re.fullmatch(r'\d{11}', expected_tin):
        raise ValueError('Provide your expected 11-digit taxpayer ID for the account check.')
    if form.get('taxpayer_id') != expected_tin:
        raise ValueError('The active RS.GE taxpayer differs from the expected taxpayer.')
    if form.get('period') != period:
        raise ValueError('The open RS.GE form has a different reporting period.')
    if TITLE not in form.get('title', ''):
        raise ValueError('This is not the supported small-business monthly declaration.')
    fields = form.get('fields', {})
    for number, text in (('15', LABEL15), ('20', LABEL20)):
        item = fields.get(number)
        if not item or text not in item.get('label', '') or item.get('readonly') or item.get('disabled'):
            raise ValueError('The form layout changed or the income fields are not editable.')
    for number in ('18', '19', '21', '22', '23', '24', '25', '27', '28', '29'):
        item = fields.get(number)
        if not item or Decimal(item['value'] or '0') != 0:
            raise ValueError('This bank-income-only workflow cannot handle the other amounts in this form.')
    # Accrual-basis fields must be read-only in the supported cash-basis workflow.
    if any(not fields[n]['readonly'] for n in ('22', '23', '24')):
        raise ValueError('This workflow supports cash-basis bank income; the form appears to use accrual accounting.')
    for number, target in (('15', prepared['year_to_date_income_gel']),
                           ('20', prepared['month_income_gel'])):
        present = Decimal(fields[number]['value'] or '0')
        if present != 0 and present != Decimal(target):
            raise ValueError('The draft contains different income. It will not be overwritten.')


class RsPortal:
    def __init__(self):
        self.runtime = None
        self.browser = None
        self.context = None
        self.lock = asyncio.Lock()

    async def open_login(self) -> dict:
        async with self.lock:
            if self.context is None:
                from playwright.async_api import async_playwright
                self.runtime = await async_playwright().start()
                try:
                    self.browser = await self.runtime.chromium.launch(channel='msedge', headless=False)
                    self.context = await self.browser.new_context()
                except Exception:
                    await self.runtime.stop()
                    self.runtime = None
                    raise
            page = await self.context.new_page()
            await page.goto('https://eservices.rs.ge/Login.aspx')
            return {'status': 'login_required', 'guide': FORM_GUIDE,
                    'instruction': 'Sign in directly in the Edge window. Open the monthly form. Do not send passwords or SMS codes through chat.',
                    'submitted': False}

    async def _form_page(self):
        if self.context is None:
            raise ValueError('Open the connector browser and sign in first. ' + FORM_GUIDE)
        matches = []
        for page in self.context.pages:
            if urlparse(page.url).hostname == 'decl.rs.ge' and await page.locator('[id^="window_"]').count():
                matches.append(page)
        if len(matches) != 1:
            raise ValueError('Open exactly one declaration form in the connector browser. ' + FORM_GUIDE)
        return matches[0]

    async def _inspect(self, page) -> dict:
        # Read the visible DOM only. Do not call undocumented RS.GE endpoints.
        data = await page.evaluate('''() => {
            const visible = e => e.getBoundingClientRect().width > 0 && e.getBoundingClientRect().height > 0;
            const windows = Array.from(document.querySelectorAll('[id^="window_"]'))
                .filter(e => /^window_\\d+$/.test(e.id) && visible(e));
            if (windows.length !== 1) throw new Error('Open exactly one declaration window.');
            const modal = windows[0];
            const fields = {};
            for (const e of modal.querySelectorAll('input')) {
                const match = /^control_\\d+_(\\d+)$/.exec(e.id);
                if (match && visible(e)) fields[match[1]] = {
                    id:e.id, value:e.value, readonly:e.readOnly, disabled:e.disabled,
                    label:e.closest('tr')?.innerText ?? ''
                };
            }
            return {modal_id:modal.id, text:modal.innerText,
                    taxpayer_text:document.querySelector('#UserPanel')?.innerText ?? '', fields};
        }''')
        period = re.search(r'(20\d{2})/(\d{2})', data['text'])
        tin = re.findall(r'\b\d{11}\b', data['taxpayer_text'])
        draft = re.search(r'N:\s*(\d+)', data['text'])
        return {'title': data['text'].split('\n')[0],
                'period': '-'.join(period.groups()) if period else None,
                'taxpayer_id': tin[0] if len(tin) == 1 else None,
                'draft_number': draft.group(1) if draft else None,
                'modal_id': data['modal_id'], 'fields': data['fields'],
                'submitted': False, 'submission_available': False}

    async def inspect(self) -> dict:
        async with self.lock:
            return await self._inspect(await self._form_page())

    async def fill(self, period: str, expected_tin: str, prepared: dict) -> dict:
        async with self.lock:
            page = await self._form_page()
            before = await self._inspect(page)
            validate_form(before, period, expected_tin, prepared)
            for number, value in (('15', prepared['year_to_date_income_gel']),
                                  ('20', prepared['month_income_gel'])):
                field = page.locator('#' + before['fields'][number]['id'])
                await field.fill(value)
                await field.press('Tab')
            after = await self._inspect(page)
            validate_form(after, period, expected_tin, prepared)
            for number, expected in (('15', prepared['year_to_date_income_gel']),
                                     ('20', prepared['month_income_gel']),
                                     ('17', prepared['month_income_gel'])):
                if Decimal(after['fields'][number]['value'] or '0') != Decimal(expected):
                    raise ValueError('RS.GE did not calculate the expected income. Review the open form; nothing was submitted.')
            rate = Decimal(after['fields']['16']['value'])
            tax = Decimal(after['fields']['26']['value'])
            expected_tax = Decimal(prepared['month_income_gel']) * rate / 100
            if money(tax) != money(expected_tax):
                raise ValueError('The portal tax differs from the bank-income calculation. Review the open form; nothing was submitted.')
            return {'status': 'filled_unsubmitted', 'period': period,
                    'draft_number': after['draft_number'],
                    'month_income_gel': prepared['month_income_gel'],
                    'year_to_date_income_gel': prepared['year_to_date_income_gel'],
                    'portal_tax_rate_percent': str(rate), 'portal_tax_gel': money(tax),
                    'submitted': False, 'submission_available': False}

    async def close(self):
        async with self.lock:
            if self.browser is not None:
                await self.browser.close()
            if self.runtime is not None:
                await self.runtime.stop()
            self.context = self.browser = self.runtime = None
