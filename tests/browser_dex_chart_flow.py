"""Actual DEX fills vs public reference candles; identified disposable EVM only."""
import hashlib,json,os
from pathlib import Path
from playwright.sync_api import sync_playwright,expect
from tests.browser_dex_runs_common import configure,unlock
from tests.test_dex_operator_drafts import BACKUP_PASSWORD

BASE=os.environ.get('ORBIS_TEST_BASE_URL','http://127.0.0.1:8801')
output=Path(os.environ['ORBIS_TEST_DEX_DIRECTORY'])
with sync_playwright() as p:
    browser=p.chromium.launch(headless=True);page=browser.new_page(viewport={'width':1440,'height':1050})
    errors=[];snapshots=[]
    page.on('pageerror',lambda error:errors.append(str(error)));page.on('dialog',lambda dialog:dialog.accept())
    def record(response):
        if '/executions-page?' in response.url and response.ok:snapshots.append(response.json())
    page.on('response',record)
    fixture=page.request.get(BASE+'/__test/state').json();assert fixture['fixture_id']==output.name
    configure(page,fixture);page.goto(BASE+'/ui/#/real');page.wait_for_load_state('networkidle');parent=unlock(page)
    # Observe the public chart API, without substituting chart implementation.
    page.evaluate('''() => {
      const lw=window.LightweightCharts,create=lw.createChart,markers=lw.createSeriesMarkers;
      window.fixtureDraws=[];
      const observedCreate=function(host,options){
        const chart=create(host,options);
        if(host.matches('[data-real-chart-canvas]')){
          window.fixtureDexChart=chart;const draw={lines:[],markers:[]};window.fixtureDraws.push(draw);
          const add=chart.addSeries;
          chart.addSeries=function(type,options,...rest){const series=add.call(chart,type,options,...rest);
            if(type===lw.LineSeries){const set=series.setData;series.setData=function(points){draw.lines.push(points);return set.call(series,points);};}
            return series;
          };
        }
        return chart;
      };
      const observedMarkers=function(series,values,...rest){window.fixtureDraws.at(-1).markers.push(values);return markers(series,values,...rest);};
      window.LightweightCharts=Object.assign({},lw,{createChart:observedCreate,createSeriesMarkers:observedMarkers});
    }''')
    owner=parent.locator('[data-dex-owner]');owner.locator(':scope > details > summary').click()
    operator=owner.locator('[data-dex-operator-draft]');operator.locator(':scope > details > summary').click()
    operator.locator('[name="operator_backup_file"]').set_input_files(output/'operator-backup.json')
    operator.locator('[name="operator_import_password"]').fill(BACKUP_PASSWORD)
    operator.get_by_role('button',name='Restaurar copia',exact=True).click()
    expect(operator.locator('[data-operator-status]')).to_contain_text('Copia restaurada',timeout=10000)
    runs=parent.locator('[data-dex-runs]');runs.locator(':scope > details > summary').click()
    expect(runs.locator('[name="dex_run_binding"]')).to_have_value(fixture['binding_id'])
    runs.locator('[name="dex_run_permission"]').check();runs.get_by_role('button',name='Activar',exact=True).click()
    expect(runs.locator('[data-run-history]')).to_contain_text('Activo',timeout=15000)
    runs.get_by_role('button',name='Gráfico',exact=True).click()
    graph=runs.locator('[data-dex-chart]');canvas=graph.locator('[data-real-chart-canvas]')
    export=graph.get_by_role('button',name='Exportar JSONL',exact=True)
    expect(canvas).to_have_attribute('data-candle-count','12')
    expect(export).to_be_disabled()
    expect(canvas).to_have_attribute('data-fill-count','0');expect(graph.locator('[data-real-chart-state]')).to_contain_text('Referencia MEXC')
    page.request.post(BASE+'/__test/advance',data={'fixture_id':fixture['fixture_id'],'method':'entry'})
    expect(runs.locator('[data-run-history]')).to_contain_text('Compra · Confirmada',timeout=20000)
    expect(canvas).to_have_attribute('data-buy-count','1',timeout=20000)
    assert snapshots and snapshots[-1]['total']==1
    buy=snapshots[-1]['executions'][0]
    assert buy['price']=='1' and buy['reference_price']=='1.01' and buy['fee_asset']=='BNB'
    assert buy['time_ms']//1000>buy['signal_candle_t']
    draw=page.evaluate('window.fixtureDraws.at(-1)')
    assert draw['lines']==[[{'time':buy['time_ms']//1000,'value':1}]]
    expect(graph.locator('[data-real-chart-inspect]')).to_contain_text('0.010000000000000001 WBNB a 1 USDT')
    expect(graph.locator('[data-real-chart-inspect]')).to_contain_text('Referencia MEXC · 1.01 USDT')
    expect(graph.locator('[data-real-chart-inspect]')).to_contain_text('Pendiente de contabilizar')
    expect(graph.locator('[data-real-chart-inspect]')).not_to_contain_text('Asiento')
    page.request.post(BASE+'/__test/advance',data={'fixture_id':fixture['fixture_id'],'method':'exit'})
    expect(runs.locator('[data-run-history]')).to_contain_text('Venta · Confirmada',timeout=20000)
    expect(canvas).to_have_attribute('data-fill-count','2',timeout=20000);expect(canvas).to_have_attribute('data-sell-count','1')
    assert len(snapshots[-1]['executions'])==2
    sale=snapshots[-1]['executions'][1]
    draw=page.evaluate('window.fixtureDraws.at(-1)')
    assert draw['lines']==[[{'time':buy['time_ms']//1000,'value':1}],[{'time':sale['time_ms']//1000,'value':float(sale['price'])}]]
    assert draw['markers'][0][0]['shape']=='arrowUp' and draw['markers'][1][0]['shape']=='arrowDown'
    expect(graph.locator('[data-real-chart-inspect]')).to_contain_text('Venta')
    graph.get_by_role('combobox',name='Inspeccionar fill REAL').select_option(buy['fill_id'])
    expect(graph.locator('[data-real-chart-inspect]')).to_contain_text('Compra')
    count=int(canvas.get_attribute('data-candle-count'));graph.get_by_role('button',name='Histórico anterior',exact=True).click()
    page.wait_for_function('count => Number(document.querySelector("[data-dex-chart] [data-real-chart-canvas]").dataset.candleCount)>count',arg=count)
    assert canvas.bounding_box()['height']>=420
    graph.screenshot(path=str(output/'dex-chart-desktop.png'))
    before=page.evaluate('window.fixtureDexChart.timeScale().getVisibleRange()')
    page.set_viewport_size({'width':390,'height':844})
    page.wait_for_function('range => { const now=window.fixtureDexChart.timeScale().getVisibleRange();return now&&now.from<=range.from&&now.to>=range.to;}',arg=before)
    page.wait_for_function('() => document.documentElement.scrollWidth<=innerWidth+1')
    assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
    graph.screenshot(path=str(output/'dex-chart-mobile.png'))
    page.set_viewport_size({'width':1440,'height':1050})
    runs.get_by_role('button',name='Detener',exact=True).click();expect(runs.locator('[data-run-history]')).to_contain_text('Detenido')
    expect(export).to_be_enabled()
    def corrupt(route):
        response=route.fetch();assert response.ok
        result=response.json();result['sha256']='0'*64;route.fulfill(status=200,json=result)
    page.route('**/research-export',corrupt);export.click()
    expect(graph.locator('[data-real-chart-export-state]')).to_contain_text('La huella del archivo no coincide')
    page.unroute('**/research-export')
    with page.expect_download() as downloaded:export.click()
    file=downloaded.value
    raw=file.path().read_bytes()
    assert file.suggested_filename=='orbis-uniswap-real-'+hashlib.sha256(raw).hexdigest()[:12]+'.jsonl'
    rows=[json.loads(line) for line in raw.splitlines()]
    assert [row['swap']['side'] for row in rows]==['BUY','SELL']
    assert [row['swap']['gas_fee_wei'] for row in rows]==[buy['gas_fee_wei'],sale['gas_fee_wei']]
    assert all(not row['swap']['financially_booked'] and row['provenance']['reserve']['artifact_id']==fixture['primary_reserve_id'] for row in rows)
    assert fixture['wallet'].lower() not in raw.decode().lower()
    file.save_as(output/'uniswap-verified.jsonl')
    expect(graph.locator('[data-real-chart-export-state]')).to_contain_text('2 swaps exportados')
    # Privacy must redact retained details, even without refetching a chart.
    page.get_by_role('button',name='Ocultar importes',exact=True).click()
    # The global privacy preference rebuilds the REAL view asynchronously.
    # Wait for disposal before opening the new, redacted chart instance.
    expect(graph).not_to_be_visible()
    parent.locator(':scope > details > summary').click();runs.locator(':scope > details > summary').click()
    runs.get_by_role('button',name='Gráfico',exact=True).click()
    expect(graph.locator('[data-real-chart-inspect]')).to_contain_text('••••')
    expect(export).to_be_disabled()
    expect(graph.locator('[data-real-chart-inspect]')).not_to_contain_text('0.010000000000000001')
    # Missing OHLC must retain actual receipt markers, with no fabricated bars.
    page.route('**/market/candles?*',lambda route:route.fulfill(status=503,json={'detail':'fixture offline'}))
    graph.get_by_role('button',name='Cerrar gráfico',exact=True).click();runs.get_by_role('button',name='Gráfico',exact=True).click()
    expect(canvas).to_have_attribute('data-fill-count','2');expect(canvas).to_have_attribute('data-candle-count','0')
    expect(graph.locator('[data-real-chart-state]')).to_contain_text('2 fills sin OHLC')
    page.unroute('**/market/candles?*')
    # A foreign snapshot is rejected instead of drawing another environment.
    def wrong(route):
        response=route.fetch();value=response.json();value['environment']='PAPER';route.fulfill(status=200,json=value)
    page.route('**/executions-page?*',wrong);graph.get_by_role('button',name='Actualizar gráfico',exact=True).click()
    expect(graph.locator('[data-real-chart-state]')).to_contain_text('Gráfico no verificable')
    assert canvas.get_attribute('data-fill-count') is None
    page.unroute('**/executions-page?*');graph.get_by_role('button',name='Actualizar gráfico',exact=True).click()
    expect(canvas).to_have_attribute('data-fill-count','2')
    page.locator('#real-lock').click();expect(graph).to_have_count(0)
    parent=unlock(page);runs=parent.locator('[data-dex-runs]');runs.locator(':scope > details > summary').click()
    expect(runs.locator('[data-run-history]')).to_contain_text('Detenido')
    runs.get_by_role('button',name='Gráfico',exact=True).click()
    expect(runs.locator('[data-dex-chart] [data-real-chart-canvas]')).to_have_attribute('data-fill-count','2')
    state=page.request.get(BASE+'/__test/state').json();assert state['sends']==2 and state['wallet_sends']==0
    assert page.evaluate("!(window.fixtureMethods||[]).includes('eth_sendTransaction')")
    assert not errors,errors
    (output/'chart-result.json').write_text(json.dumps({'passed':True,'swaps':2,'wallet_sends':0,'exact_receipt_price_time':True,
        'distinct_reference':True,'research_export':True,'digest_failure_rejected':True,'privacy':True,'ohlc_outage':True,'reopen_without_autorun':True,'errors':errors},indent=2))
    browser.close();print('DEX real fills, distinct reference OHLC, automatic update, inspection, privacy, mobile and stopped reopen')
