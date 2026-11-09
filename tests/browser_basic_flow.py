"""Empty encrypted LAB -> versioned long/short -> JSON bot -> genuine PAPER worker."""
from pathlib import Path
from datetime import datetime,timezone
from decimal import Decimal
import json,os,time
from playwright.sync_api import sync_playwright,expect
BASE=os.environ['ORBIS_TEST_BASE_URL'];OUT=Path(os.environ['ORBIS_BASIC_DIRECTORY']).parent;PASS='owned basic journey password'
assert os.environ.get('ORBIS_BASIC_FIXTURE') == '1'
assert os.environ['ORBIS_BASIC_PHASE'] == 'create'

def stamp(t):return datetime.fromtimestamp(t,timezone.utc).strftime('%Y-%m-%dT%H:%M')
with sync_playwright() as p:
    browser=p.chromium.launch(headless=True)
    page=browser.new_page(viewport={'width':1440,'height':1100},timezone_id='UTC')
    errors=[];writes=[]
    page.on('pageerror',lambda error:errors.append(str(error)))
    page.on('dialog',lambda dialog:dialog.accept())
    page.on('request',lambda r:writes.append((r.method,r.url)) if r.method in ('POST','PUT','DELETE') else None)
    try:
        fixture=page.request.get(BASE+'/__test/state').json();assert fixture['fixture_id']==OUT.name and not fixture['real_exists']
        page.goto(BASE+'/ui/#/config');page.wait_for_load_state('networkidle')
        def open_form(selector):
            form=page.locator(selector)
            form.evaluate("el=>{let p=el.parentElement;while(p){if(p.tagName==='DETAILS')p.open=true;p=p.parentElement;}}")
            expect(form).to_be_visible();return form
        for selector,url in [('#cfg-lab-create-form','/lab/create'),('#cfg-spot-create-form','/paper/spot/strategies/create'),
                             ('#cfg-perp-create-form','/paper/perp/create')]:
            form=open_form(selector);form.locator('[name="passphrase"]').fill(PASS)
            expect(form.locator('[name=session_seconds]')).to_have_value('900')
            duration='86400' if selector=='#cfg-perp-create-form' else '604800'
            form.locator('[name=session_seconds]').select_option(duration)
            if selector=='#cfg-lab-create-form':
                page.set_viewport_size({'width':390,'height':844})
                assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
                form.screenshot(path=str(OUT/'local-duration-mobile.png'))
                page.set_viewport_size({'width':1440,'height':1100})
            with page.expect_response(BASE+url) as created:form.locator('button[type="submit"]').click()
            assert created.value.status==200,created.value.text()
            assert created.value.json()['session_seconds']==int(duration)
            expect(form.locator('[name=passphrase]')).to_have_value('')
            if selector=='#cfg-perp-create-form':
                assert created.value.json()['session_seconds']==86400
                expect(page.locator('#cfg-perp-status')).to_contain_text('h restantes')
                expect(form.locator('[name=passphrase]')).to_have_value('')
                page.locator('#cfg-perp-lock').click()
                expect(page.locator('#cfg-perp-status')).to_contain_text('bloqueado')
        page.goto(BASE+'/ui/#/laboratorio');page.wait_for_load_state('networkidle')
        form=page.locator('#lab-download');form.locator('[name="provider"]').select_option('mexc')
        form.locator('[name="symbol"]').fill('BTC/USDT');form.locator('[name="timeframe"]').select_option('1h')
        form.locator('[name="start"]').fill(stamp(fixture['start']));form.locator('[name="end"]').fill(stamp(fixture['end']))
        with page.expect_response('**/lab/datasets/download') as downloaded:form.locator('button[type="submit"]').click()
        assert downloaded.value.status==200,downloaded.value.text();dataset=downloaded.value.json()
        expect(page.locator('#lab-dataset')).to_have_value(dataset['id'])
        strategy=page.locator('#lab-strategy-form');strategy.locator('[name="name"]').fill('Auditoría de flujo')
        rule=page.locator('#lab-rule-list .lab-rule').first
        rule.locator('[name="kind"]').select_option('CLOSE_ABOVE_EMA');rule.locator('[name="period"]').fill('2')
        for name,value in [('fee_rate','0'),('slippage_rate','0'),('stop_loss','0.1'),('take_profit','0.2')]:strategy.locator('[name="'+name+'"]').fill(value)
        with page.expect_response(lambda r:r.request.method=='POST' and r.url.endswith('/lab/strategies')) as saved:
            page.locator('#lab-save-strategy').click()
        assert saved.value.status==200,saved.value.text();original=saved.value.json()
        expect(page.locator('#lab-version')).to_have_value(original['version_id'])
        with page.expect_response(lambda r:r.request.method=='POST' and r.url.split('?',1)[0].endswith('/lab/runs')) as tested:page.locator('#lab-run').click()
        assert tested.value.status==200,tested.value.text();long=tested.value.json()
        expect(page.locator('#lab-run-verify')).to_have_attribute('data-state','verified')
        expect(page.locator('#lab-chart')).to_have_attribute('data-entry-count',str(long['metrics']['trades']))
        assert long['metrics']['trades']>0
        expect(page.locator('#lab-trade-rows tr').first).to_be_visible();page.locator('#lab-trade-rows tr').first.click()
        with page.expect_download() as archive:page.locator('#lab-export-strategy').click()
        bundle=json.loads(Path(archive.value.path()).read_text());(OUT/'bot.json').write_text(json.dumps(bundle))
        assert bundle['schema']=='orbis.lab.strategy.v1' and bundle['strategy']['definition']['direction']=='LONG'
        strategy.locator('[name="direction"]').select_option('SHORT')
        with page.expect_response(lambda r:r.request.method=='POST' and '/versions' in r.url) as edited:page.locator('#lab-save-strategy').click()
        assert edited.value.status==200,edited.value.text();short_version=edited.value.json()
        expect(page.locator('#lab-version')).to_have_value(short_version['version_id'])
        assert short_version['version_id']!=original['version_id']
        with page.expect_response(lambda r:r.request.method=='POST' and r.url.split('?',1)[0].endswith('/lab/runs')) as tested_short:page.locator('#lab-run').click()
        assert tested_short.value.status==200,tested_short.value.text();short=tested_short.value.json()
        assert short['id']!=long['id'] and short['version_id']==short_version['version_id']
        with page.expect_response('**/lab/strategies/import') as imported:
            page.locator('#lab-import-strategy-file').set_input_files({'name':'bot.json','mimeType':'application/json','buffer':json.dumps(bundle).encode()})
        assert imported.value.status==200,imported.value.text();imported_strategy=imported.value.json()
        assert imported_strategy['id']!=original['id']
        assert not page.request.get(BASE+'/paper/spot/strategies').json()
        page.locator('#lab-run-select').select_option(long['id'])
        expect(page.locator('#lab-run-verify')).to_have_attribute('data-state','verified')
        expect(page.locator('#lab-paper-action')).to_be_visible()
        page.locator('#lab-paper-action').click()
        expect(page.locator('#spot-paper-version')).to_have_value(original['version_id'])
        expect(page.locator('#spot-paper-dataset')).to_have_value(dataset['id'])
        expect(page.locator('#spot-paper-start')).to_be_enabled()
        assert not page.request.get(BASE+'/paper/spot/strategies').json()
        with page.expect_response(lambda r:r.request.method=='POST' and r.url.endswith('/paper/spot/strategies')) as activated:page.locator('#spot-paper-start').click()
        assert activated.value.status==201,activated.value.text();paper=activated.value.json();identifier=paper['id']
        assert paper['source_run_id']==long['id'] and Decimal(paper['state']['cash'])==1000 and Decimal(paper['state']['units'])==0
        def wait_state(predicate):
            deadline=time.monotonic()+15
            while True:
                response=page.request.get(BASE+'/paper/spot/strategies/'+identifier);assert response.status==200,response.text();value=response.json()
                if predicate(value):return value
                assert time.monotonic()<deadline,value
                page.wait_for_timeout(100)
        page.request.post(BASE+'/__test/advance',data={'fixture_id':OUT.name,'action':'ENTRY'})
        bought=wait_state(lambda r:Decimal(r['state']['units'])>0)
        for endpoint in ('/lab/status','/paper/spot/strategies/status'):
            lifetime=page.request.get(BASE+endpoint).json()
            assert lifetime['unlocked'] and lifetime['session_seconds']==604800
            assert lifetime['remaining_seconds']<604800-900
        page.locator('#spot-paper-poll').click()
        expect(page.locator('#spot-paper-chart')).to_have_attribute('data-buy-count','1')
        page.request.post(BASE+'/__test/advance',data={'fixture_id':OUT.name,'action':'TARGET'})
        closed=wait_state(lambda r:Decimal(r['state']['units'])==0 and int(r['state']['closed_trades'])==1)
        assert Decimal(closed['state']['cash'])>1000
        page.locator('#spot-paper-poll').click()
        expect(page.locator('#spot-paper-chart')).to_have_attribute('data-sell-count','1')
        page.locator('#spot-paper-stop').click()
        stopped=wait_state(lambda r:r['status']=='STOPPED')
        verified=page.request.get(BASE+'/paper/spot/strategies/'+identifier+'/verify');assert verified.status==200,verified.text()
        assert page.request.get(BASE+'/lab/runs/'+long['id']+'/verify').json()['verified']
        assert page.request.get(BASE+'/lab/runs/'+long['id']).json()['version_id']==original['version_id']
        assert not page.request.get(BASE+'/__test/state').json()['real_exists']
        assert not any('/real/' in url and method in ('POST','PUT','DELETE') for method,url in writes)
        assert not errors,errors
        page.screenshot(path=str(OUT/'paper-desktop.png'),full_page=True)
        (OUT/'result.json').write_text(json.dumps({'passed':True,'dataset':dataset,'long':long,
            'long_full':page.request.get(BASE+'/lab/runs/'+long['id']).json(),'short':short,'strategy':original,
            'imported_strategy':imported_strategy,'paper':stopped,'verified':verified.json(),'errors':errors,'server':fixture['server']},indent=2))
    except Exception:
        (OUT/'failure-state.json').write_text(json.dumps(page.request.get(BASE+'/__test/state').json(),indent=2))
        page.screenshot(path=str(OUT/'failure.png'),full_page=True)
        (OUT/'failure-dom.yml').write_text(page.locator('body').aria_snapshot())
        raise
    finally:browser.close()
print('Empty installation LAB -> version/import -> actual PAPER worker passed')
