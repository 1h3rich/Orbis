"""Create/download/strategy/run/chart/export, then reopen in a new process."""
from decimal import Decimal as D
import json
import os
from pathlib import Path

from playwright.sync_api import expect,sync_playwright,TimeoutError as PlaywrightTimeout
from tests.perp_dataset_fixture import PASSWORD

BASE=os.environ['ORBIS_TEST_BASE_URL'];directory=Path(os.environ['ORBIS_BASIC_DIRECTORY']).parent
reopen=os.environ.get('ORBIS_BASIC_PHASE')=='reopen'
limited=os.environ.get('ORBIS_TEST_PERP_ENTRY')=='LIMIT'
partial=os.environ.get('ORBIS_TEST_PERP_PARTIAL')=='1'
compare_rules=os.environ.get('ORBIS_TEST_PERP_RULE_COMPARISON')=='1'
compare_runs=os.environ.get('ORBIS_TEST_PERP_RUN_COMPARISON')=='1'
errors=[];actions=[]
with sync_playwright() as p:
    browser=p.chromium.launch(headless=True)
    page=browser.new_page(viewport={'width':1440,'height':1000})
    page.on('pageerror',lambda e:errors.append(str(e)))
    page.on('request',lambda r:actions.append(r.url) if '/real/' in r.url and r.method not in ('GET','HEAD','OPTIONS') else None)
    state=page.request.get(BASE+'/__perp_fixture/state').json();assert state['locked'] and not state['real_exists']
    page.goto(BASE+'/ui/#/config');page.wait_for_load_state('networkidle')
    page.locator('#cfg-lab-create-form' if not reopen else '#cfg-lab-unlock-form').evaluate('(el)=>el.closest("details").open=true')
    form=page.locator('#cfg-lab-create-form' if not reopen else '#cfg-lab-unlock-form')
    form.locator('[name=passphrase]').fill(PASSWORD);form.locator('button[type=submit]').click()
    expect(page.locator('#cfg-lab-status')).to_contain_text('desbloqueado')
    page.goto(BASE+'/ui/#/laboratorio');page.wait_for_load_state('networkidle')
    page.evaluate('''() => {const original=window.LightweightCharts;window.__ownedPerpCharts=[];
        window.LightweightCharts={...original,createChart:(node,options)=>{const chart=original.createChart(node,options),series=[];
        if(node.id==='perp-run-compare-equity'){
          const add=chart.addSeries;chart.addSeries=function(...args){
            const line=add.apply(chart,args),set=line.setData;line.setData=function(points){series.push(points);return set.call(line,points);};
            return line;
          };
        }
        window.__ownedPerpCharts.push({id:node.id,chart,series});return chart;}};}''')
    (directory/('initial-reopen.html' if reopen else 'initial.html')).write_text(page.content())
    if not reopen:
        panel=page.locator('#lab-perp-data');panel.locator(':scope > summary').click()
        form=panel.locator('#perp-data-form');form.locator('[name=start]').fill('2025-01-01T00:00');form.locator('[name=end]').fill('2025-01-01T04:00')
        with page.expect_response(lambda r:r.url.endswith('/lab/perp/datasets/download')) as downloaded:form.locator('button[type=submit]').click()
        assert downloaded.value.status==200,downloaded.value.text();data=downloaded.value.json()
        strategy=page.locator('#lab-strategy-form');strategy.locator('[name=name]').fill('Perpetuo propio')
        rule=page.locator('#lab-rule-list .lab-rule').first;rule.locator('[name=kind]').select_option('BULLISH_STREAK');rule.locator('[name=period]').fill('2')
        for key,value in [('initial_capital','1000'),('allocation','0.5'),('fee_rate','0.001'),('slippage_rate','-0' if partial else '0'),('stop_loss','0.5'),('take_profit','0.5')]:strategy.locator('[name='+key+']').fill(value)
        with page.expect_response(lambda r:r.request.method=='POST' and r.url.endswith('/lab/strategies')) as created:page.locator('#lab-save-strategy').click()
        assert created.value.status==200,created.value.text();version=created.value.json()
    else:
        previous=json.loads((directory/'owned-run.json').read_text());data=previous['data'];version=previous['version']
    panel=page.locator('#lab-perp-runs');panel.locator(':scope > summary').click()
    expect(panel.locator('[name=version_id] option')).to_have_count(3 if reopen else 2)
    if not reopen:
        form=panel.locator('#perp-run-form');form.locator('[name=version_id]').select_option(version['version_id']);form.locator('[name=dataset_id]').select_option(data['id'])
        expect(panel.locator('#perp-run-config-summary')).to_contain_text('Asignación 50,00 %')
        if limited:
            form.locator('[name=entry_kind]').select_option('LIMIT')
            form.locator('[name=limit_offset]').fill('1.000001')
            form.locator('[name=limit_bars]').fill('3')
        if partial:
            form.locator('#perp-partial-config > summary').click()
            for index,(target,fraction) in enumerate((('0.5','25'),('1.5','35'))):
                form.get_by_role('button',name='Añadir objetivo',exact=True).click()
                form.locator('[data-partial-target]').nth(index).fill(target)
                form.locator('[data-partial-fraction]').nth(index).fill(fraction)
        with page.expect_response(lambda r:r.request.method=='POST' and r.url.endswith('/lab/perp/runs')) as tested:form.locator('button[type=submit]').click()
        assert tested.value.status==201,tested.value.text();saved=tested.value.json()
    else:
        saved=previous['saved'];panel.locator('#perp-run-selection').select_option(saved['id'])
    expect(panel.locator('#perp-run-verification')).to_have_text('Reproducible · experimental')
    expect(panel.locator('#perp-run-assumptions li').first).to_contain_text('Captura GRVT')
    expect(panel.locator('#perp-run-assumptions li').first).to_contain_text('Aplicadas al periodo como supuesto')
    expect(panel.locator('#perp-run-chart')).to_have_attribute('data-entry-count','1')
    expect(panel.locator('#perp-run-chart')).to_have_attribute('data-exit-count','1')
    expect(panel.locator('#perp-run-chart')).to_have_attribute('data-partial-count','2' if partial else '0')
    if compare_rules:
        capture_path=directory/'rule-comparison-capture.json'
        inside_path=directory/'rule-window-inside.json'
        capture_panel=panel.locator('#perp-rule-comparison')
        window_report=capture_panel.locator('[data-rules-window]')
        if not reopen:
            capture_panel.locator('summary').click()
            expect(window_report).to_contain_text('Sin capturas durante la prueba')
            expect(window_report).to_contain_text('posterior')
            capture_panel.locator('summary').click()
            inside=page.request.post(BASE+'/__perp_fixture/archive-rules/'+data['id'])
            assert inside.ok,inside.text()
            inside_path.write_text(json.dumps(inside.json()))
            assert page.request.post(BASE+'/__perp_fixture/rules-next?tight=true').ok
            captured=page.request.post(BASE+'/lab/perp/rules/capture',data={'symbol':'BTC/USDT:PERP'})
            assert captured.ok,captured.text()
            capture_path.write_text(json.dumps(captured.json()))
        capture=json.loads(capture_path.read_text())
        inside=json.loads(inside_path.read_text())
        capture_panel.locator('summary').click()
        expect(window_report).to_contain_text('Coincidencias 0 · Diferencias 1 · Ambiguas 0')
        expect(capture_panel.locator('select option')).to_have_count(4)
        expect(capture_panel.locator('option[value="'+inside['hash']+'"]')).to_contain_text('Durante')
        expect(capture_panel.locator('option[value="'+capture['hash']+'"]')).to_have_count(1)
        capture_panel.locator('select').select_option(inside['hash'])
        with page.expect_response('**/compare-rules') as compared:
            capture_panel.get_by_role('button',name='Comparar',exact=True).click()
        assert compared.value.ok,compared.value.text()
        report=compared.value.json()
        assert report['original_run']['id']==saved['id'] and report['alternative']['observation_hash']==inside['hash']
        assert report['original']['metrics']['trades']==1 and report['alternative']['metrics']['trades']==0
        assert D(report['final_equity_difference'])==D('1000')-D(report['original']['metrics']['final_equity'])
        expect(capture_panel.get_by_role('row',name='Operaciones')).to_contain_text('1')
        expect(capture_panel.get_by_role('status')).to_contain_text('Diferencia de capital')
        capture_panel.screenshot(path=str(directory/('rules-reopened.png' if reopen else 'rules-created.png')))
        export_path=directory/'rule-comparison-export.json'
        page.once('dialog',lambda dialog:dialog.accept())
        with page.expect_download() as download:
            capture_panel.get_by_role('button',name='Exportar comparación').click()
        downloaded_report=json.loads(Path(download.value.path()).read_text())
        assert downloaded_report==report
        if reopen:
            assert json.loads(export_path.read_text())==report
        else:
            export_path.write_text(json.dumps(report))
        capture_panel.locator('summary').click()
        expect(capture_panel.locator('tbody tr')).to_have_count(0)
    assert panel.locator('#perp-run-chart').bounding_box()['height']>=480
    expect(panel.locator('#perp-run-trades tbody tr')).to_have_count(1)
    panel.locator('#perp-run-trades [data-trade]').click()
    expect(panel.locator('#perp-run-trade-detail')).to_have_attribute('open','')
    if partial:
        expect(panel.locator('#perp-run-trade-partials li')).to_have_count(2)
        expect(panel.locator('#perp-run-trade-partials')).to_contain_text('P2')
    panel.locator('#perp-run-price-kind').select_option('MARK')
    expect(panel.locator('#perp-run-chart')).to_have_attribute('data-price-kind','MARK')
    panel.locator('#perp-run-price-kind').select_option('TRADE')
    try:
        page.wait_for_function('''(times) => {const last=window.__ownedPerpCharts.filter(x=>x.id==='perp-run-chart').at(-1);
            if(!last)return false;const scale=last.chart.timeScale(),a=scale.timeToCoordinate(times[0]),b=scale.timeToCoordinate(times[1]);
            return a!==null&&b!==null&&b-a>document.querySelector('#perp-run-chart').clientWidth/3;}''',arg=[data['start'],data['end']-3600],timeout=5000)
    except PlaywrightTimeout:
        debug=page.evaluate('''(times)=>{const c=window.__ownedPerpCharts.filter(x=>x.id==='perp-run-chart').at(-1).chart,s=c.timeScale();
            return {times,chart:c.options(),scale:s.options(),range:s.getVisibleLogicalRange(),visible:s.getVisibleRange(),
            a:s.timeToCoordinate(times[0]),b:s.timeToCoordinate(times[1]),width:document.querySelector('#perp-run-chart').clientWidth};}''',[data['start'],data['end']-3600])
        (directory/'chart-debug.json').write_text(json.dumps(debug,indent=2));page.screenshot(path=str(directory/'chart-debug.png'),full_page=True)
        raise
    with page.expect_download() as exported:panel.locator('#perp-run-export').click()
    output=directory/('run-reopened.json' if reopen else 'run-export.json');exported.value.save_as(output)
    bundle=json.loads(output.read_text());result=bundle['execution']['result']
    assert D(result['trades'][0]['entry_price'])==D('100.97' if limited else '102')
    quantity=D(result['trades'][0]['quantity']);assert quantity==D('22.830' if limited else '22.828')
    if partial:
        assert bundle['schema']=='orbis.lab.perp.bundle.v3' and result['engine']=='perp-ohlc-3.0'
        assert result['metrics']['partial_exits']==2
        fills=[event for event in result['journal'] if event['kind']=='FILL']
        assert len(fills)==4 and sum(abs(D(event['size_delta'])) for event in fills[1:])==quantity
        trade=result['trades'][0]
        assert D(trade['net_profit'])==D(trade['realized_pnl'])-D(trade['fees'])-D(trade['funding_paid'])
        assert [D(event['fraction']) for event in trade['partial_exits']]==[D('.25'),D('.35')]
        expect(panel.locator('[data-partial-target]').first).to_have_value('0.5')
    else:
        assert D(result['trades'][0]['funding_paid'])==quantity*D('110')*D('0.01')/100
        assert D(result['trades'][0]['net_profit'])==quantity*D('2.81403' if limited else '1.783')
    if not compare_rules and not compare_runs:
        cost = panel.locator('#perp-cost-comparison')
        cost.locator(':scope > summary').click()
        fee,slip = (cost.locator('[name='+key+']') for key in ('fee_rate','slippage_rate'))
        expect(fee).to_have_value('0.1');expect(slip).to_have_value('0')
        fee.fill('0,6');slip.fill('0.4')
        endpoint = '/lab/perp/runs/'+saved['id']+'/compare-costs'
        submit = cost.get_by_role('button',name='Comparar costes',exact=True)
        with page.expect_response(lambda r:r.url.endswith(endpoint)) as compared:
            submit.click()
        assert compared.value.ok,compared.value.text()
        report = compared.value.json()
        assert report['schema']=='orbis.lab.perp.cost-comparison.v1' and report['execution_authority']=='NONE'
        assert report['original_run']=={'id':saved['id'],'hash':saved['hash']}
        assert D(report['deltas']['fees_paid'])>0 and D(report['deltas']['final_equity'])<0
        if partial:
            assert report['base']['costs']['slippage_rate']=='-0'
        assert D(report['alternative']['costs']['fee_rate'])==D('0.006')
        assert D(report['alternative']['costs']['slippage_rate'])==D('0.004')
        expect(cost.get_by_role('status')).to_contain_text('escenario experimental')
        expect(cost.get_by_role('table')).to_be_visible()
        row = cost.get_by_role('row',name='Capital final · USDT',exact=False)
        expect(row.locator('td').last).to_have_attribute('data-exact',report['deltas']['final_equity'])
        if not limited and not reopen:
            cost.screenshot(path=str(directory/'cost-comparison.png'))
        page.once('dialog',lambda dialog:dialog.accept())
        with page.expect_download() as exported_cost:
            cost.get_by_role('button',name='Exportar JSON',exact=True).click()
        cost_path = directory/('cost-reopened.json' if reopen else 'cost-created.json')
        exported_cost.value.save_as(cost_path)
        assert json.loads(cost_path.read_text())==report
        if reopen:
            assert json.loads((directory/'cost-created.json').read_text())==report
        held_cost = []
        page.route('**'+endpoint,lambda route:held_cost.append(route))
        submit.click()
        expect(cost.get_by_role('status')).to_have_text('Reproduciendo escenarios…')
        page.wait_for_timeout(100);assert held_cost
        slip.fill('0.5')
        held_cost[0].fulfill(status=200,json=report)
        page.unroute('**'+endpoint)
        expect(cost.locator('tbody tr')).to_have_count(0)
        expect(cost.get_by_role('button',name='Exportar JSON',exact=True)).to_be_disabled()
        cost.locator(':scope > summary').click()
        assert page.request.get(BASE+'/lab/perp/runs/'+saved['id']).json()==bundle
        margin = panel.locator('#perp-margin-comparison')
        margin.locator(':scope > summary').click()
        margin.locator('[name=leverages]').fill('1,5 5 10000')
        margin_endpoint = '/lab/perp/runs/'+saved['id']+'/compare-margin'
        with page.expect_response(lambda r:r.url.endswith(margin_endpoint)) as risk:
            margin.get_by_role('button',name='Comparar margen').click()
        assert risk.value.ok,risk.value.text()
        margin_report = risk.value.json()
        assert margin_report['execution_authority']=='NONE'
        assert margin_report['original_run']=={'id':saved['id'],'hash':saved['hash']}
        assert len(margin_report['scenarios'])==6
        assert any(row['leverage']=='1.5' for row in margin_report['scenarios'])
        rejected = [row for row in margin_report['scenarios'] if row['leverage']=='10000']
        assert len(rejected)==2 and all(row['entry_restrictions'] for row in rejected)
        expect(margin.get_by_role('status')).to_contain_text('6 escenarios')
        expect(margin.get_by_role('table')).to_be_visible()
        expect(margin.get_by_role('row',name='Base · Aislado · 5×')).to_be_visible()
        expect(margin.get_by_role('row',name='Cruzado · 10000×')).to_contain_text('Sin operaciones')
        page.once('dialog',lambda dialog:dialog.accept())
        with page.expect_download() as exported_margin:
            margin.get_by_role('button',name='Exportar JSON',exact=True).click()
        margin_path = directory/('margin-reopened.json' if reopen else 'margin-created.json')
        exported_margin.value.save_as(margin_path)
        assert json.loads(margin_path.read_text())==margin_report
        if reopen:
            assert json.loads((directory/'margin-created.json').read_text())==margin_report
        margin.locator('[name=margin]').select_option('CROSS')
        expect(margin.get_by_role('table')).not_to_be_visible()
        expect(margin.get_by_role('button',name='Exportar JSON',exact=True)).to_be_disabled()
        margin.locator(':scope > summary').click()
        assert page.request.get(BASE+'/lab/perp/runs/'+saved['id']).json()==bundle
    if limited and not partial:
        assert bundle['schema']=='orbis.lab.perp.bundle.v2' and result['engine']=='perp-ohlc-2.0'
        assert bundle['run']['profile']['entry_order']['offset']=='0.01000001'
        expect(panel.locator('[name=entry_kind]')).to_have_value('LIMIT')
        expect(panel.locator('[name=limit_offset]')).to_have_value('1.000001')
        assert int(result['trades'][0]['entry_time_ns'])>data['start']*10**9+2*3600*10**9
        validation=page.locator('#lab-perp-validations')
        validation.locator(':scope > summary').click()
        option=validation.locator('option[value="'+saved['profile_id']+'"]')
        expect(option).to_have_count(1)
        assert option.get_attribute('disabled') is None
        expect(validation.locator('#perp-validation-form button[type=submit]')).to_be_disabled()
    assert bundle['run']['verification']=='REPLAY_VERIFIED' and not result['real_authority']
    if partial:
        validation=page.locator('#lab-perp-validations');validation.locator(':scope > summary').click()
        form=validation.locator('#perp-validation-form')
        if not reopen:
            form.locator('[name=profile_id]').select_option(saved['profile_id'])
            form.locator('[name=dataset_id]').select_option(data['id'])
            form.locator('[name=method]').select_option('PATH_SENSITIVITY')
            with page.expect_response(lambda r:r.url.endswith('/lab/perp/validations') and r.request.method=='POST') as validated:
                form.locator('button[type=submit]').click()
            assert validated.value.status==201,validated.value.text()
            validation_id=validated.value.json()['id']
            (directory/'partial-validation-id').write_text(validation_id)
        else:
            validation_id=(directory/'partial-validation-id').read_text()
            validation.locator('#perp-validation-selection').select_option(validation_id)
        expect(validation.locator('#perp-validation-windows tbody tr')).to_have_count(4)
        expect(validation.locator('#perp-validation-paper')).not_to_be_visible()
        expect(validation.locator('#perp-validation-reference')).not_to_be_visible()
        validation.locator('[data-window="0"]').click()
        expect(validation.locator('#perp-validation-status')).to_have_text('Tramo abierto en Pruebas de derivados.')
        expect(panel.locator('#perp-run-chart')).to_have_attribute('data-partial-count','2')
    if reopen:
        assert bundle==json.loads((directory/'run-export.json').read_text())
        assert panel.locator('[name=version_id]').input_value()==version['version_id']
        expect(panel.locator('#perp-run-config-summary')).to_contain_text('LONG')
    else:
        strategy=page.locator('#lab-strategy-form')
        if compare_runs:
            strategy.locator('[name=take_profit]').fill('0.01')
        else:
            strategy.locator('[name=direction]').select_option('SHORT')
        with page.expect_response(lambda r:r.request.method=='POST' and '/versions' in r.url) as edited:page.locator('#lab-save-strategy').click()
        assert edited.value.status==200,edited.value.text();assert edited.value.json()['version_id']!=version['version_id']
        saved_bundle={'data':data,'version':version,'saved':saved}
        if compare_runs:
            config={key:bundle['run']['profile'][key] for key in ('margin_mode','leverage','trade_path','mark_path')}
            config['version_id']=edited.value.json()['version_id']
            for name,mark_path in (('matched',config['mark_path']),('mismatched','O_L_H_C')):
                prepared=page.request.post(BASE+'/lab/perp/profiles',data={**config,'mark_path':mark_path})
                assert prepared.ok,prepared.text()
                compared=page.request.post(BASE+'/lab/perp/runs',data={'dataset_id':data['id'],'profile_id':prepared.json()['id']})
                assert compared.ok,compared.text();saved_bundle[name]=compared.json()
        copy=page.request.post(BASE+'/lab/backup/encrypted',data={'passphrase':'owned perp experiment backup key'})
        assert copy.ok,copy.text();assert copy.json()['verified']['counts']['perp_runs']==(3 if compare_runs else 1)
        (directory/'owned-run.json').write_text(json.dumps(saved_bundle,indent=2))
    if compare_runs:
        candidates=previous if reopen else saved_bundle
        compare=panel.locator('#perp-run-comparison')
        compare.locator(':scope > summary').click()
        selection=compare.locator(':scope > .row select')
        expect(selection.locator('option')).to_have_count(3)

        def compare_with(key):
            selection.select_option(candidates[key]['id'])
            with page.expect_response('**/perp/runs/compare') as checked:
                compare.get_by_role('button',name='Comparar pruebas',exact=True).click()
            response=checked.value
            assert response.ok and response.headers['cache-control']=='no-store',response.text()
            report=response.json()
            assert report['base']['id']==saved['id'] and report['other']['id']==candidates[key]['id']
            return report

        same=compare_with('matched')
        expect(compare.locator(':scope > p[role=status]')).to_have_text('Condiciones equivalentes · Cambios en objetivo')
        expect(compare.locator('[data-run-compare-chart]')).to_have_attribute('data-points','5')
        curve=page.evaluate("window.__ownedPerpCharts.filter(x=>x.id==='perp-run-compare-equity').at(-1).series")
        assert len(curve)==2 and all(len(points)==5 for points in curve)
        assert [row['time'] for row in curve[0]]==[row['time'] for row in curve[1]]
        assert D(same['deltas']['final_equity'])!=0 and same['difference_blockers']==[]
        assert same['base']['metrics']['final_equity']==bundle['execution']['result']['metrics']['final_equity']
        page.set_viewport_size({'width':390,'height':844})
        expect(compare.locator('[data-run-compare-chart]')).to_be_visible()
        assert page.evaluate('''() => new Promise(resolve => {
            const deadline = performance.now() + 3000;
            function check() {
                const fits = document.documentElement.scrollWidth <= window.innerWidth;
                if (fits || performance.now() >= deadline) resolve(fits);
                else requestAnimationFrame(check);
            }
            check();
        })''')
        page.set_viewport_size({'width':1440,'height':1000})
        page.once('dialog',lambda dialog:dialog.accept())
        with page.expect_download() as exported_compare:
            compare.get_by_role('button',name='Exportar comparación',exact=True).click()
        assert json.loads(exported_compare.value.path().read_text())==same
        comparison_path=directory/'perp-run-comparison.json'
        if reopen:
            assert json.loads(comparison_path.read_text())==same
        else:
            comparison_path.write_text(json.dumps(same))
        different=compare_with('mismatched')
        assert different['difference_blockers']==['SIMULATION_ASSUMPTIONS']
        assert different['equity'] is None and different['deltas'] is None
        expect(compare.locator('table')).to_be_visible()
        expect(compare.locator('[data-run-compare-chart]')).not_to_be_visible()
        expect(compare.locator(':scope > p[role=status]')).to_contain_text('supuestos de ejecución')
        held_compare=[]
        page.route('**/perp/runs/compare',lambda route:held_compare.append(route))
        selection.select_option(candidates['matched']['id'])
        compare.get_by_role('button',name='Comparar pruebas',exact=True).click()
        expect(compare.locator(':scope > p[role=status]')).to_have_text('Reproduciendo las dos pruebas…')
        page.wait_for_timeout(150);assert held_compare
        compare.locator(':scope > summary').click()
        held_compare[0].fulfill(status=200,json=same)
        page.unroute('**/perp/runs/compare')
        expect(compare.locator('tbody tr')).to_have_count(0)
        expect(compare.locator('[data-run-compare-chart]')).not_to_be_visible()
        (directory/('comparison-proof-reopen.json' if reopen else 'comparison-proof.json')).write_text(
            json.dumps({'matched_and_exported':True,'mismatched_hidden':True,'late_reply_discarded':True,
                        'chart_points':5,'reopened':reopen}))
    panel.screenshot(path=str(directory/('experiment-reopened.png' if reopen else 'experiment-created.png')))
    page.set_viewport_size({'width':390,'height':844});panel.scroll_into_view_if_needed()
    assert page.evaluate('document.documentElement.scrollWidth<=window.innerWidth')
    page.screenshot(path=str(directory/('experiment-mobile-reopened.png' if reopen else 'experiment-mobile.png')),full_page=True)
    if reopen:
        page.set_viewport_size({'width':1440,'height':1000})
        held=[];pattern='**/lab/perp/runs/'+saved['id']
        page.route(pattern,lambda route:held.append(route))
        panel.locator('#perp-run-selection').select_option('');panel.locator('#perp-run-selection').select_option(saved['id'])
        expect(panel.locator('#perp-run-status')).to_have_text('Reproduciendo originales…')
        panel.locator('#perp-run-selection').select_option('');assert held
        held[0].fulfill(status=200,json=bundle);page.unroute(pattern)
        expect(panel.locator('#perp-run-results')).not_to_be_visible()
        assert page.request.post(BASE+'/__perp_fixture/corrupt_run').ok
        panel.locator('#perp-run-selection').select_option(saved['id'])
        expect(panel.locator('#perp-run-status')).to_contain_text('no se reproduce')
        expect(panel.locator('#perp-run-results')).not_to_be_visible()
    final=page.request.get(BASE+'/__perp_fixture/state').json();assert final['calls']==(0 if reopen else 7 if compare_rules else 5)
    assert not final['real_exists'] and not actions
    assert page.request.post(BASE+'/lab/lock').ok
    page.reload();page.wait_for_load_state('networkidle');page.locator('#lab-perp-runs > summary').click()
    expect(page.locator('#perp-run-status')).to_contain_text('bloqueado')
    expect(page.locator('#perp-run-form button[type=submit]')).to_be_disabled()
    assert not errors,errors
    browser.close()
(directory/('result-reopened.json' if reopen else 'result.json')).write_text(json.dumps({'ok':True,'run_id':saved['id'],'entry_kind':'LIMIT'if limited else'MARKET','partial_exits':result['metrics'].get('partial_exits',0),'js_errors':errors,'private_actions':actions,'calls':final['calls'],'rule_comparison':compare_rules},indent=2))
print('Perp experiment: own encrypted creation, typed model, markers, export, backup and process reopen OK')
