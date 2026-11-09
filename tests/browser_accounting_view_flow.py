"""Actual API -> accepted effective history -> explicitly original CSV -> reopen."""
import csv,json,os
from datetime import datetime, timezone
from pathlib import Path
from playwright.sync_api import expect,sync_playwright

BASE=os.environ['ORBIS_TEST_BASE_URL'];output=Path(os.environ['ORBIS_TEST_EXECUTION_DIRECTORY'])
with sync_playwright() as p:
    browser=p.chromium.launch(headless=True);page=browser.new_page(viewport={'width':1440,'height':1050})
    page.add_init_script("""Object.defineProperty(window, "LightweightCharts", {
      configurable:true, set(library) {
        Object.defineProperty(window, "LightweightCharts", {configurable:true,value:{...library,
          createChart(host, options) {
            const chart = library.createChart(host, options);
            if (host.classList.contains("portfolio-history-chart")) window.historyChart = chart;
            return chart;
          }
        }});
      }
    });""")
    errors=[];dialogs=[];book_responses=[];standalone_events=[];home_reads=[];history_reads=[]
    page.on('pageerror',lambda error:errors.append(str(error)))
    def dialog(item):dialogs.append(item.message);item.accept()
    page.on('dialog',dialog)
    def response(item):
        if '/real/accounts/' in item.url and item.url.endswith('/snapshot?include_events=true') and item.ok:
            assert item.headers['cache-control']=='no-store'
            book_responses.append((item.url,item.json()))
    page.on('response',response)
    page.on('request',lambda request:standalone_events.append(request.url)
        if '/real/accounts/' in request.url and request.method=='GET' and request.url.endswith('/events') else None)
    page.on('request',lambda request: home_reads.append(request.url)
        if page.url.endswith('#/inicio') and '/real/accounts/' in request.url else None)
    page.on('request',lambda request: history_reads.append(request.url) if '/valuation-history?' in request.url else None)
    page.route('**/market/**',lambda route:route.fulfill(status=503,json={}))
    fixture=page.request.get(BASE+'/__test/state').json();assert fixture['fixture_id']==output.name
    page.goto(BASE+'/ui/#/real');page.wait_for_load_state('networkidle')
    (output/'initial-dom.yml').write_text(page.locator('body').aria_snapshot())
    def unlock():
        page.locator('#real-passphrase').fill(fixture['password'])
        page.get_by_role('button',name='Desbloquear',exact=True).click()
        expect(page.locator('#real-account')).to_be_visible()
    def select(account):
        page.locator('#real-account').select_option(account)
        expect(page.locator('#real-history [data-accounting-method]')).to_have_text('Criterio aceptado')
        expect(page.locator('#real-history th').first).to_have_text('Fecha efectiva UTC')
        expect(page.locator('#real-export-csv')).to_have_text('CSV · Original')
    unlock();select(fixture['owner_account'])
    sale=next(item for item in fixture['owner_events']if item['id']==fixture['sale_id'])
    row=page.locator('[data-event-id="'+sale['id']+'"]')
    expect(row.locator('td').nth(0)).to_have_text(sale['accounting']['occurred_at'][:16].replace('T',' '))
    expect(row.locator('td').nth(0)).to_have_attribute('data-help','Fecha original: '+sale['occurred_at']+' · Registrado: '+sale['recorded_at'])
    def download_original(expected):
        with page.expect_download() as downloaded:page.locator('#real-export-csv').click()
        file=downloaded.value;assert file.suggested_filename.startswith('orbis-real-original-')
        assert 'CSV original: no incluye el criterio contable aceptado' in dialogs[-1]
        with open(file.path(),encoding='utf-8-sig',newline='') as data:rows=list(csv.DictReader(data))
        originals={item['id']:item for item in expected};assert {row['event_id']for row in rows}==set(originals)
        for row in rows:
            original=originals[row['event_id']]
            assert row['occurred_at']==original['occurred_at'] and row['gross_amount']==original['gross_amount']
        assert 'accounting' not in rows[0] and 'acceptance_ids' not in rows[0]
        return rows
    download_original(fixture['owner_events'])
    select(fixture['target_account']);event=fixture['target_events'][0]
    row=page.locator('[data-event-id="'+event['id']+'"]')
    expect(row.locator('td').nth(4)).to_have_text('Coste 1.5 USDT')
    expect(row.locator('td').nth(4)).to_have_attribute('data-help','Coste original: '+event['gross_amount']+' USDT')
    download_original(fixture['target_events'])
    distribution=page.locator('#real-market-values [data-valuation-allocation="USDT"]')
    distribution.locator('summary').click()
    expect(distribution).to_contain_text('WBNB')
    expect(distribution).to_contain_text('100,00 %')
    expect(distribution.locator('meter')).to_have_attribute('value','10000')
    page.locator('#real-hide').click()
    distribution.locator('summary').click()
    expect(distribution).to_contain_text('Importes ocultos')
    expect(distribution.locator('meter')).to_have_count(0)
    row=page.locator('[data-event-id="'+event['id']+'"]')
    expect(row.locator('td').nth(4)).to_have_text('Coste •••• USDT')
    expect(row.locator('td').nth(4)).to_have_attribute('data-help','Coste original oculto')
    expect(page.locator('#real-history')).not_to_contain_text(event['gross_amount'])
    page.locator('#real-hide').click();expect(row.locator('td').nth(4)).to_have_text('Coste 1.5 USDT')
    page.locator('#real-history').screenshot(path=str(output/'accounting-history-desktop.png'))
    page.set_viewport_size({'width':390,'height':844})
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
    page.locator('#real-history').screenshot(path=str(output/'accounting-history-mobile.png'))
    page.set_viewport_size({'width':1440,'height':1050})
    page.locator('.navegacion-principal a[href="#/inicio"]').click()
    home=page.locator('#home-real')
    account=home.locator('[data-real-dashboard-row="'+fixture['target_account']+'"]')
    allocation=account.locator('[data-valuation-allocation="USDT"]')
    allocation.locator('summary').click()
    expect(allocation).to_contain_text('100,00 %')
    expect(account).to_contain_text('Aportado 0 · Retirado 0')
    assert any(url.endswith('/valuation') for url in home_reads)
    assert not any('/snapshot' in url for url in home_reads)
    allocation.screenshot(path=str(output/'portfolio-allocation-desktop.png'))
    page.set_viewport_size({'width':390,'height':844})
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
    allocation.screenshot(path=str(output/'portfolio-allocation-mobile.png'))
    page.set_viewport_size({'width':1440,'height':1050})
    home.get_by_role('link',name='Ver cartera real').click()
    page.locator('#real-lock').click();unlock();select(fixture['target_account'])
    expect(page.locator('[data-event-id="'+event['id']+'"] td').nth(4)).to_have_text('Coste 1.5 USDT')
    page.locator('#real-account').select_option(fixture['history_account'])
    panel=page.locator('#real-valuation-history')
    panel.locator(':scope > summary').click()
    assert not history_reads
    panel.get_by_label('Periodo').select_option('7')
    panel.get_by_label('Fuente').select_option('bitstamp')
    with page.expect_response(lambda item:'/valuation-history?' in item.url) as loaded:
        panel.get_by_role('button',name='Consultar').click()
    response=loaded.value
    assert response.ok and response.headers['cache-control']=='no-store'
    payload=response.json();rows=payload['rows']
    assert payload['provider']=='bitstamp' and payload['days']==7
    assert [row['equity'] for row in rows]==['110',None,'109','119']
    expect(panel.get_by_role('img',name='Patrimonio diario en EUR')).to_be_visible()
    expect(panel.locator('[data-history-status]')).to_have_text('EUR · BITSTAMP · Cierres UTC · 1 día incompleto')
    panel.get_by_text('Valores diarios',exact=True).click()
    table=panel.get_by_role('table')
    expect(table.locator('tbody tr')).to_have_count(4)
    expect(table.locator('tbody tr').nth(0).locator('td').nth(5)).to_have_text('29')
    expect(table.locator('tbody tr').nth(1).locator('td').nth(7)).to_have_text('10')
    expect(table.locator('tbody tr').nth(2).locator('td').nth(1)).to_have_text('—')
    expect(table.locator('tbody tr').nth(2)).to_contain_text('Sin cierre · BTC')
    expect(table.locator('tbody tr').nth(3).locator('td').nth(6)).to_have_text('100')
    panel.screenshot(path=str(output/'portfolio-history-desktop.png'))
    page.set_viewport_size({'width':390,'height':844})
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
    panel.screenshot(path=str(output/'portfolio-history-mobile.png'))
    page.set_viewport_size({'width':1440,'height':1050})
    page.locator('#real-account').select_option(fixture['target_account'])
    expect(panel.get_by_role('img')).to_have_count(0)
    expect(table).to_have_count(0)
    assert len(history_reads)==1
    page.locator('#real-account').select_option(fixture['long_history_account'])
    panel.get_by_label('Periodo').select_option('10y')
    with page.expect_response(lambda item:'/valuation-history?' in item.url) as long_loaded:
        panel.get_by_role('button',name='Consultar').click()
    assert long_loaded.value.ok, long_loaded.value.text()
    long_result=long_loaded.value.json()
    long_rows=long_result['rows']
    assert 3650<=long_result['days']<=3653 and len(long_rows)==long_result['days']
    assert long_rows[0]['date_t']==long_rows[-1]['date_t']-(len(long_rows)-1)*86400
    assert long_rows[0]['equity']=='1025.1234567890123456789'
    assert [row['missing_assets'] for row in long_rows if row['equity'] is None]==[['BTC']]
    range_control=panel.get_by_label('Rango visible del gráfico')
    expect(range_control).to_have_value('all')
    page.wait_for_function('start=>window.historyChart?.timeScale().getVisibleRange()?.from===start',
                           arg=long_rows[0]['date_t'])
    range_control.select_option('custom')
    def utc_date(stamp):return datetime.fromtimestamp(stamp,timezone.utc).date().isoformat()
    custom_from,custom_to=long_rows[25]['date_t'],long_rows[65]['date_t']
    panel.get_by_label('Desde fecha').fill(utc_date(custom_from))
    panel.get_by_label('Hasta fecha').fill(utc_date(custom_to))
    panel.get_by_role('button',name='Aplicar',exact=True).click()
    expect(range_control).to_have_value('custom')
    page.wait_for_function('expected=>{const range=window.historyChart.timeScale().getVisibleRange();return range?.from===expected[0]&&range?.to===expected[1]}',
                           arg=[custom_from,custom_to])
    panel.get_by_text('Valores diarios',exact=True).click()
    expect(table.locator('tbody tr')).to_have_count(100)
    scroll=panel.get_by_role('region',name='Desplazar valores diarios')
    assert scroll.evaluate('el=>el.scrollHeight>el.clientHeight&&el.clientHeight<=480')
    assert table.locator('thead th').first.evaluate('el=>getComputedStyle(el).position')=='sticky'
    expect(table.locator('tbody tr').first).to_have_attribute('data-day',utc_date(long_rows[-1]['date_t']))
    scroll.evaluate('el=>{el.scrollTop=300}')
    panel.get_by_role('button',name='Siguiente',exact=True).click()
    expect(table.locator('tbody tr').first).to_have_attribute('data-day',utc_date(long_rows[-101]['date_t']))
    assert scroll.evaluate('el=>el.scrollTop')==0
    panel.get_by_role('button',name='Anterior',exact=True).click()
    expect(table.locator('tbody tr').first).to_have_attribute('data-day',utc_date(long_rows[-1]['date_t']))
    page.set_viewport_size({'width':390,'height':844})
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
    panel.screenshot(path=str(output/'portfolio-history-long-mobile.png'))
    page.set_viewport_size({'width':1440,'height':1050})
    assert len(history_reads)==2  # Local range and table pagination use the same loaded data.
    page.locator('#real-account').select_option(fixture['history_account'])
    panel.get_by_label('Periodo').select_option('7')
    panel.get_by_role('button',name='Consultar').click()
    expect(panel.get_by_role('img',name='Patrimonio diario en EUR')).to_be_visible()
    page.locator('#real-hide').click()
    expect(panel.get_by_role('img')).to_have_count(0)
    expect(table).to_have_count(0)
    expect(page.locator('#real-hide')).to_have_text('Mostrar importes')
    panel.locator(':scope > summary').click()
    expect(panel.locator('[data-history-status]')).to_have_text('Importes ocultos')
    expect(panel.get_by_role('button',name='Consultar')).not_to_be_visible()
    expect(range_control).to_have_count(0)
    assert len(history_reads)==3
    for account,expected in [(fixture['owner_account'],fixture['owner_events']),(fixture['target_account'],fixture['target_events'])]:
        assert any('/accounts/'+account+'/snapshot?' in url and payload['account_id']==account
            and payload['events']==expected for url,payload in book_responses)
    assert sorted(standalone_events)==sorted(
        BASE+'/real/accounts/'+account+'/events' for account in (fixture['owner_account'],fixture['target_account'])
    )  # Sólo las dos exportaciones explícitas de originales.
    assert not errors,errors
    (output/'result.json').write_text(json.dumps({'passed':True,'effective_date':True,'derived_cost':'1.5',
        'original_csv_verified':True,'redacted_tooltip':True,'reopen':True,'actual_api_payloads':True,'combined_book_read':True,
        'portfolio_history':True,'history_gap':True,'history_flows':True,'history_masking':True,
        'ten_year_daily_rows':len(long_rows),'history_viewport_exact':True,'history_table_paged':True,'errors':errors},indent=2)+'\n')
    page.locator('#real-lock').click();browser.close()
print('Accepted economic dates/costs, original CSV, masking, actual API and encrypted reopen passed')
