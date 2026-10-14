"""Personal MCP prototype: NBG conversion and monthly declaration preparation."""
from __future__ import annotations

import os
import site
import inspect
from contextlib import asynccontextmanager
from functools import wraps
from pathlib import Path

# --target installs need .pth processing on Windows (not just PYTHONPATH).
if os.environ.get('RS_MCP_RUNTIME'):
    site.addsitedir(os.environ['RS_MCP_RUNTIME'])

from mcp.server import MCPServer
from mcp_types import ToolAnnotations
from mcp.server.mcpserver.exceptions import ToolError

from ledger import Ledger
from nbg import convert, get_rate
from portal import RsPortal

BASE = Path(__file__).resolve().parent
DATA = Path(os.environ.get('RS_LEDGER_PATH', str(BASE / 'private-data' / 'incomes.sqlite3')))
_ledger = None
portal = RsPortal()


def expected_errors(fn):
    """Return useful validation errors through MCP without logging a traceback."""
    if inspect.iscoroutinefunction(fn):
        @wraps(fn)
        async def wrapped(*args, **kwargs):
            try:
                return await fn(*args, **kwargs)
            except (ValueError, RuntimeError) as exc:
                raise ToolError(str(exc)) from exc
    else:
        @wraps(fn)
        def wrapped(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except (ValueError, RuntimeError) as exc:
                raise ToolError(str(exc)) from exc
    return wrapped


@asynccontextmanager
async def lifespan(_server):
    try:
        yield None
    finally:
        await portal.close()


def ledger() -> Ledger:
    global _ledger
    if _ledger is None:
        _ledger = Ledger(DATA)
    return _ledger


server = MCPServer('personal-rs-declaration', version='0.1.0', lifespan=lifespan, instructions=(
    'Dates require the year. Get the official NBG rate effective on each receipt date; '
    'never invent rates. Record each business receipt once using a stable reference. '
    'Sum converted GEL receipts for the entire month. Confirm all monthly income and '
    'the previous YTD. For RS.GE, use the connector browser, verify taxpayer and period, '
    'and explicitly confirm the supported cash-basis bank-income workflow before filling. '
    'Submission is unavailable. Never click Send or claim a declaration was submitted.'))

READ = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)
PUBLIC_READ = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=True)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False)
OPEN_BROWSER = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False, openWorldHint=True)


@server.tool(annotations=READ)
def get_connector_status() -> dict:
    """Report what the connector can actually do and whether RS.GE submission is connected."""
    return {'version': '0.1.0', 'official_nbg_rates': True, 'local_receipt_ledger': True,
            'monthly_preparation': True, 'rs_form_adapter_implemented': True,
            'rs_form_adapter_live_tested': False, 'rs_submission': False,
            'credentials_stored': False, 'host_configuration_detection': 'unavailable'}


@server.tool(annotations=PUBLIC_READ)
@expected_errors
def convert_receipt(receipt_date: str, amount: str, currency: str) -> dict:
    """Convert a receipt using NBG's effective official rate; do not save it. Date: YYYY-MM-DD, amount: decimal string."""
    return convert(amount, get_rate(receipt_date, currency))


@server.tool(annotations=WRITE)
@expected_errors
def record_business_income(reference: str, receipt_date: str, amount: str, currency: str,
                           allow_identical_receipt: bool = False) -> dict:
    """Save a BUSINESS income receipt locally with its NBG rate. Use the same reference on retries. Confirm a distinct identical receipt before overriding the duplicate check."""
    return ledger().record(reference, receipt_date, amount, currency, get_rate, allow_identical_receipt)


@server.tool(annotations=READ)
@expected_errors
def get_month_summary(period: str) -> dict:
    """List recorded business receipts and the total GEL income for a YYYY-MM month. Completeness must be verified."""
    return ledger().summary(period)


@server.tool(annotations=READ)
@expected_errors
def prepare_monthly_declaration(period: str, prior_year_to_date_gel: str, month_complete: bool) -> dict:
    """Prepare monthly totals, NOT an RS.GE filing. Prior YTD is through the previous month of the SAME year; obtain it from verified records, not an assumption. Confirm the entire month's income."""
    return ledger().prepare(period, prior_year_to_date_gel, month_complete)


@server.tool(annotations=OPEN_BROWSER)
@expected_errors
async def open_rs_browser() -> dict:
    """Open an ephemeral Edge session for direct RS.GE sign-in. The user must enter credentials themselves. No declaration is filled or submitted."""
    return await portal.open_login()


@server.tool(annotations=READ)
@expected_errors
async def inspect_rs_form() -> dict:
    """Inspect the one open RS.GE declaration in the connector's Edge window. Report the active taxpayer, month, fields, and calculated tax. Never submit."""
    return await portal.inspect()


@server.tool(annotations=WRITE)
@expected_errors
async def fill_rs_form(period: str, prior_year_to_date_gel: str, month_complete: bool,
                       expected_taxpayer_id: str, cash_basis_bank_income_only_confirmed: bool) -> dict:
    """Fill only fields 15 and 20 of the open monthly RS.GE draft. Requires confirmed complete monthly bank business income under cash-basis accounting and verified prior YTD. Other income, exemptions, payroll, regional allocations, or accrual cases require a different workflow. Does NOT save or submit."""
    if not cash_basis_bank_income_only_confirmed:
        raise ValueError('First confirm this declaration is for cash-basis bank business income only.')
    prepared = ledger().prepare(period, prior_year_to_date_gel, month_complete)
    return await portal.fill(period, expected_taxpayer_id, prepared)


if __name__ == '__main__':
    server.run()
