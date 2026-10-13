"""Exercise the real stdio MCP protocol. No browser is launched or modified."""
import json
import os
import site
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
(ROOT / '.test-work').mkdir(exist_ok=True)
RUNTIME = os.environ.get('RS_MCP_RUNTIME')
if RUNTIME:
    site.addsitedir(RUNTIME)
import anyio
from mcp import Client, StdioServerParameters


def data(result):
    assert not result.is_error, result.content
    return result.structured_content or json.loads(result.content[0].text)


async def main():
    with tempfile.TemporaryDirectory(dir=ROOT/'.test-work') as tmp:
        assert Path(tmp).resolve().is_relative_to((ROOT/'.test-work').resolve())
        params = StdioServerParameters(
            command=sys.executable,
            args=[str(ROOT/'server.py')],
            env={**({'RS_MCP_RUNTIME':RUNTIME} if RUNTIME else {}), 'RS_LEDGER_PATH':str(Path(tmp)/'test.sqlite3'),
                 'PYTHONUTF8':'1', 'PYTHONIOENCODING':'utf-8'})
        async with Client(params) as client:
            result = await client.list_tools()
            names = [tool.name for tool in result.tools]
            assert len(names) == 8, names
            assert not any('submit' in name or 'send' in name for name in names)
            status = data(await client.call_tool('get_connector_status'))
            assert status['rs_submission'] is False
            quote = data(await client.call_tool('convert_receipt', {
                'receipt_date':'2025-09-02','amount':'1000','currency':'GEL'}))
            assert quote['gel_display'] == '1000.00', quote
            args = {'reference':'test-1','receipt_date':'2025-09-02','amount':'1000','currency':'GEL'}
            first = data(await client.call_tool('record_business_income',args))
            retry = data(await client.call_tool('record_business_income',args))
            assert first['saved'] and retry['already_recorded']
            prepared = data(await client.call_tool('prepare_monthly_declaration',{
                'period':'2025-09','prior_year_to_date_gel':'12000','month_complete':True}))
            assert prepared['year_to_date_income_gel'] == '13000.00'
            assert prepared['submitted'] is False
            invalid = await client.call_tool('convert_receipt',{
                'receipt_date':'2025-02-30','amount':'10','currency':'GEL'})
            assert invalid.is_error
            missing_login = await client.call_tool('inspect_rs_form')
            assert missing_login.is_error
            output = {'stdio_mcp':'passed','tool_count':len(names),'submission_tool':False,
                      'decimal_conversion':'passed','receipt_retry':'passed',
                      'monthly_preparation':'passed','invalid_input':'rejected',
                      'no_browser_session':'rejected'}
            if '--live' in sys.argv:
                live = data(await client.call_tool('convert_receipt',{
                    'receipt_date':'2026-09-02','amount':'1000','currency':'EUR'}))
                assert live['gel_display'] == '3042.40', live
                output['live_nbg_eur_2026_09_02'] = live
            print(json.dumps(output,ensure_ascii=False,indent=2))


if __name__ == '__main__':
    anyio.run(main)
