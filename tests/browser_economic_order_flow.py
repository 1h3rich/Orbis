"""Signed real receipts, explicit manual chronology, exact cost and cold UI reread."""
import json,os
from decimal import Context,Decimal as D,ROUND_HALF_EVEN,localcontext
from pathlib import Path
from playwright.sync_api import sync_playwright,expect
from tests.browser_dex_runs_common import configure,unlock

BASE=os.environ['ORBIS_TEST_BASE_URL'];out=Path(os.environ['ORBIS_TEST_DEX_DIRECTORY'])
with sync_playwright()as p:
    browser=p.chromium.launch(headless=True);page=browser.new_page(viewport={'width':1440,'height':1000})
    errors=[];confirmations=[];page.on('pageerror',lambda e:errors.append(str(e)));page.on('dialog',lambda d:d.accept())
    try:
        fixture=page.request.get(BASE+'/__test/state').json();assert fixture['fixture_id']==out.name and fixture['order_mode']in ('BATCH','EXISTING')
        existing=fixture['order_mode']=='EXISTING'
        configure(page,fixture);page.goto(BASE+'/ui/#/real');page.wait_for_load_state('networkidle');unlock(page)
        def select_context():
            if page.locator('#real-account').input_value()!=fixture['account_id']:
                page.locator('#real-account').select_option(fixture['account_id'])
                page.get_by_role('button',name='Verificar control',exact=True).click()
                expect(page.locator('#real-wallet-proof-state')).to_contain_text('Control EOA comprobado')
        select_context();before=page.request.get(BASE+'/__test/book').json()
        assert before['events']==(9 if existing else 6)and before['acceptances']==(1 if existing else 0)
        assert D(before['realized_pnl']['WBNB'])==D('0.02')
        page.get_by_role('button',name='Revisar cuenta',exact=True).click();dialog=page.get_by_role('dialog',name='Revisar cuenta',exact=True)
        expect(dialog.locator('[data-er-source]')).to_have_count(2)
        if not existing:
            wallet=dialog.locator('[data-er-source="WALLET_SWAP:'+fixture['wallet_source_id']+'"]')
            wallet.locator('[data-er-trade]').select_option(fixture['manual_trade'])
            wallet.locator('[data-er-gas="'+fixture['manual_gas']+'"]').check()
            operator=dialog.locator('[data-er-source^="OPERATOR:"]');operator.locator('[data-er-new-gas]').check()
            if operator.locator('[data-er-trade]').count():operator.locator('[data-er-trade]').select_option('new')
        with page.expect_response('**/economic-review/order-options')as captured:
            dialog.get_by_role('button',name='Revisar',exact=True).click()
        assert captured.value.status==200,captured.value.text()
        data=captured.value.json();assert len(data['groups'])==1
        group=data['groups'][0];assert len(group['nodes'])==4 and len(group['chain_order'])==2
        assert fixture['wallet'].lower()not in captured.value.text().lower()
        expect(dialog.locator('[data-er-fix-order]')).to_be_visible()
        expect(dialog.locator('[data-er-fix-order]')).not_to_be_checked()
        dialog.locator('[data-er-fix-order]').check()
        def node_ids():return dialog.locator('[data-er-order-node]').evaluate_all('(nodes)=>nodes.map(n=>n.dataset.erOrderNode)')
        manual=['event:'+fixture['manual_extra_buy'],'event:'+fixture['manual_extra_sell']]
        for key in manual:
            for _ in range(4):
                ids=node_ids()
                if ids.index(key)==len(ids)-1:break
                dialog.locator('[data-er-order-node="'+key+'"] [data-er-action="order-down"]').click()
            else:raise AssertionError('Manual node did not reach its chosen position')
        desired=[*group['chain_order'],*manual];assert node_ids()==desired
        expect(dialog.locator('[data-er-order-node]').nth(0).locator('[data-er-action="order-down"]')).to_be_disabled()
        expect(dialog.locator('[data-er-order-node]').nth(1).locator('[data-er-action="order-up"]')).to_be_disabled()
        dialog.screenshot(path=str(out/'order-desktop.png'))
        page.set_viewport_size({'width':390,'height':844})
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
        for selector in ('.economic-review-header','footer'):
            box=dialog.locator(selector).bounding_box();assert box and box['y']>=0 and box['y']+box['height']<=844
        dialog.screenshot(path=str(out/'order-mobile.png'));page.set_viewport_size({'width':1440,'height':1000})
        observed=page.request.get(BASE+'/__test/book').json();rpc_reads=observed['rpc_reads']
        assert observed['events']==before['events']and observed['acceptances']==before['acceptances']
        with page.expect_response('**/economic-review/preview')as reviewed:
            dialog.get_by_role('button',name='Revisar',exact=True).click()
        assert reviewed.value.status==200,reviewed.value.text();proposal=reviewed.value.json()
        assert proposal['mode']==('EXISTING'if existing else 'BATCH')
        assert proposal['review']['events_to_create']==(0 if existing else 3)
        request=reviewed.value.request.post_data_json
        assert request['order_options_digest']==data['revision']and request['tie_orders']==[{'account_id':group['account_id'],'at':group['at'],'nodes':desired}]
        with localcontext(Context(prec=300,rounding=ROUND_HALF_EVEN)):
            bought=sum((D(item['quantity'])for item in fixture['chain_inputs']),D(0))+D('0.02')
            paid=sum((D(item['gross_amount'])for item in fixture['chain_inputs']),D(0))+D('0.04')
            sold_basis=paid/bought*D('0.01');expected_pnl=D('0.04')-sold_basis;expected_cost=paid-sold_basis;expected_units=bought-D('0.01')
        account=next(item for item in proposal['review']['accounts']if item['account_id']==fixture['ledger_id'])
        position=next(item for item in account['network']['positions']if item['asset']=='WBNB')
        assert D(position['quantity'])==expected_units and D(position['cost_basis'])==expected_cost
        assert D(account['network']['realized_pnl']['USDT'])==expected_pnl and expected_pnl!=D('0.02')
        dialog.screenshot(path=str(out/'comparison-desktop.png'))
        def lost(route):
            result=route.fetch();assert result.status==200,result.text();confirmations.append(result.json());route.abort('failed')
        page.route('**/economic-review/confirm',lost)
        dialog.get_by_role('button',name='Aceptar criterio',exact=True).click()
        expect(dialog.get_by_role('button',name='Comprobar estado',exact=True)).to_be_visible();page.unroute('**/economic-review/confirm')
        dialog.get_by_role('button',name='Comprobar estado',exact=True).click()
        expect(dialog.locator('.economic-saved')).to_have_text('Criterio guardado ✓');assert len(confirmations)==1
        after=page.request.get(BASE+'/__test/book').json()
        assert after['events']==9 and after['acceptances']==before['acceptances']+1 and after['originals_preserved']
        assert all(after['accepted_payloads'][key]==value for key,value in before['accepted_payloads'].items())
        assert D(after['realized_pnl']['WBNB'])==expected_pnl and D(after['cost_basis']['WBNB'])==expected_cost
        assert D(after['positions']['WBNB'])==expected_units and after['contributions']==before['contributions']
        assert after['rpc_reads']==rpc_reads
        dialog.get_by_role('button',name='Cerrar revisión').click();page.locator('#real-lock').click();unlock(page);select_context()
        page.get_by_role('button',name='Revisar cuenta',exact=True).click()
        dialog.get_by_role('button',name='Revisar',exact=True).click()
        expect(dialog.locator('[data-er-fix-order]')).to_be_checked();assert node_ids()==desired
        expect(dialog.locator('.economic-order')).to_contain_text('Orden vigente')
        with page.expect_response('**/economic-review/preview')as reopened:
            dialog.get_by_role('button',name='Revisar',exact=True).click()
        assert reopened.value.status==200,reopened.value.text()
        assert all(item['current']==item['network']for item in reopened.value.json()['review']['accounts'])
        final=page.request.get(BASE+'/__test/book').json()
        assert all(final[key]==value for key,value in after.items()if key!='wallet_reads')
        assert page.evaluate('fixtureMethods.filter(m=>/send|signTransaction|signTypedData/.test(m))')==[]
        assert not errors,errors
        storage=page.evaluate('JSON.stringify([Object.entries(localStorage),Object.entries(sessionStorage)])')
        assert fixture['wallet'].lower()not in storage.lower() and data['revision']not in storage
        (out/'result.json').write_text(json.dumps({'passed':True,'mode':fixture['order_mode'],'exact_units':str(expected_units),
            'exact_cost':str(expected_cost),'exact_realized_pnl':str(expected_pnl),'originals_preserved':True,
            'prior_criteria_preserved':True,'lost_reply_single_confirm':True,'accepted_order_recovered_after_cipher_reopen':True,
            'wallet_sends':0,'errors':errors},indent=2))
    except BaseException:
        page.screenshot(path=str(out/'failure.png'),full_page=True);(out/'failure-dom.yml').write_text(page.locator('body').aria_snapshot());raise
    finally:browser.close()
print('Signed receipts, explicit manual chronology, independent cost/PnL and encrypted order recovery passed')
