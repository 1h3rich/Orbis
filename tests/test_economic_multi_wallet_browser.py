"""Chromium widget → actual ASGI HTTP API → signed receipts → owned SQLCipher.

Only the browser transport is bridged through TestClient; no response or
calculation is replaced. Socket-level full-application regressions run separately.
"""
from decimal import Decimal
from pathlib import Path
import json
import os
import shutil

from eth_account import Account
from eth_account.messages import encode_defunct
from playwright.sync_api import expect
import pytest

from app.portfolio.repository import account_snapshot, list_original_events
from app.portfolio.vault import RealVault
from tests.test_economic_review_wallet_widget import browser, page
from tests.test_economic_review_wallets import multi_case
from tests.test_economic_receipt_source import counts
from tests.test_wallet_swap import PASSWORD


@pytest.mark.parametrize('accepted',[False,True])
def test_actual_multi_wallet_widget_joint_review_lost_reply_and_cipher_reopen(page,tmp_path,accepted):
    with multi_case(tmp_path,accepted=accepted) as case:
        requests=[];saved_responses=[];lose_reply=[True]
        with case.vault.session()as db:
            originals={account:list_original_events(db,account)for account in (case.owner,case.peer)}
            primary=account_snapshot(db,case.owner)
        before=counts(case.vault)

        def http(url,options=None,passive=False):
            body=json.loads(options['body'])if options and options.get('body')else None
            headers={**case.headers,**({'X-Orbis-Passive':'1'}if passive else {})}
            response=case.client.request(options.get('method','GET')if options else 'GET','/real'+url,headers=headers,json=body)
            requests.append((url,body,response.status_code))
            if url.endswith('/economic-review/confirm')and response.status_code==200:
                saved_responses.append(response.json())
                if lose_reply[0]:lose_reply[0]=False;return {'lost':True}
            return {'status':response.status_code,'body':response.json()}

        def sign(message,address):
            key='11'if address.lower()==case.meta['owner'].lower()else '22'
            signer=Account.from_key('0x'+key*32)
            assert signer.address.lower()==address.lower()
            assert bytes.fromhex(message[2:]).decode('utf-8').startswith('ORBIS · Verificar control de wallet EOA')
            return '0x'+Account.sign_message(encode_defunct(hexstr=message),signer.key).signature.hex()

        page.expose_function('ownedHttp',http);page.expose_function('ownedControlSignature',sign)
        page.on('dialog',lambda dialog:dialog.accept())
        errors=[];page.on('pageerror',lambda error:errors.append(str(error)))
        page.evaluate("""(fixture)=>{
          window.methods=[];window.providers=[];
          function provider(address){const listeners=new Map();const item={
            on:(event,handler)=>listeners.set(event,handler),removeListener:(event)=>listeners.delete(event),
            request:async({method,params})=>{methods.push({method,address});
              if(method==='eth_accounts'||method==='eth_requestAccounts')return[address];
              if(method==='eth_chainId')return'0x38';
              if(method==='personal_sign')return ownedControlSignature(params[0],params[1]);
              throw Error('Forbidden wallet RPC '+method);}};providers.push(item);return item;}
          const primary=provider(fixture.wallet),secondary=provider(fixture.peer);
          window.actualState={wallet:fixture.wallet,accountId:fixture.context,chain:'0x38',generation:1,verified:true,provider:primary,hidden:false};
          window.actualClient={snapshot:()=>actualState,reviewProviders:()=>[{name:'Wallet secundaria',provider:secondary}],
            request:async(url,options,passive)=>{const response=await ownedHttp(url,options,!!passive);
              if(response.lost)throw Error('Owned lost response');
              if(response.status>=400){const error=Error(response.body.detail||'HTTP error');error.status=response.status;throw error;}
              return response.body;}};
          OrbisEconomicReview.mount(document.querySelector('#host'),actualClient);
        }""",{'wallet':case.meta['owner'],'peer':case.address,'context':case.context})
        page.get_by_role('button',name='Revisar cuenta',exact=True).click()
        dialog=page.get_by_role('dialog',name='Revisar cuenta',exact=True)
        expect(dialog.locator('[data-er-source]')).to_have_count(1)
        dialog.get_by_role('button',name='Conectar',exact=True).click()
        dialog.get_by_role('button',name='Elegir cuenta',exact=True).click()
        dialog.get_by_role('button',name='Verificar wallet',exact=True).click()
        expect(dialog.locator('[data-er-source]')).to_have_count(2)
        if accepted:
            dialog.get_by_role('button',name='Verificar contexto',exact=False).click()
        expect(dialog.get_by_role('button',name='Revisar',exact=True)).to_be_enabled()
        dialog.screenshot(path=str(tmp_path/'multi-wallet-desktop.png'))
        page.set_viewport_size({'width':390,'height':844})
        assert page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
        dialog.screenshot(path=str(tmp_path/'multi-wallet-mobile.png'))
        page.set_viewport_size({'width':1200,'height':900})
        dialog.get_by_role('button',name='Revisar',exact=True).click()
        expect(dialog.get_by_role('button',name='Aceptar criterio',exact=True)).to_be_visible()
        assert counts(case.vault)==before
        dialog.get_by_role('button',name='Aceptar criterio',exact=True).click()
        expect(dialog.get_by_role('button',name='Comprobar estado',exact=True)).to_be_visible()
        dialog.get_by_role('button',name='Comprobar estado',exact=True).click()
        expect(dialog.locator('.economic-saved')).to_have_text('Criterio guardado ✓')
        assert len(saved_responses)==1 and saved_responses[0]['created']
        assert all(status==200 for _,_,status in requests),requests
        assert len([url for url,_,_ in requests if url.endswith('/economic-review/confirm')])==1
        with case.vault.session()as db:
            snapshots={account:account_snapshot(db,account)for account in originals}
            assert snapshots[case.owner]==primary
            assert snapshots[case.peer].positions['WBNB','USDT'].quantity==Decimal('0.01')
            assert snapshots[case.peer].cash['USDT']==Decimal('100.99')
            for account,values in originals.items():
                now=list_original_events(db,account)
                assert all(item in now for item in values)
        backup=case.vault.backup(tmp_path/'multi-wallet-cold.db');cold=RealVault(backup);cold.unlock(PASSWORD)
        try:
            with cold.session()as db:
                assert {account:account_snapshot(db,account)for account in originals}==snapshots
        finally:cold.lock()
        assert not case.calls and not errors,errors
        assert {item['method']for item in page.evaluate('methods')} <= {'eth_requestAccounts','eth_accounts','eth_chainId','personal_sign'}
        dialog.get_by_role('button',name='Cerrar revisión',exact=True).click()
        page.get_by_role('button',name='Revisar cuenta',exact=True).click()
        expect(dialog.locator('[data-er-source]')).to_have_count(1)
        dialog.get_by_role('button',name='Revisar',exact=True).click()
        expect(dialog.get_by_role('button',name='Aceptar criterio',exact=True)).to_be_visible()
        assert counts(case.vault)==(before[0]+(2 if accepted else 0),before[1],before[2]+(1 if accepted else 0),before[3]+1)
        # Keep a compact receipt even when pytest removes its temporary fixture.
        if os.environ.get('ORBIS_TEST_MULTI_WALLET_EVIDENCE'):
            evidence=Path(os.environ['ORBIS_TEST_MULTI_WALLET_EVIDENCE']);evidence.mkdir(parents=True,exist_ok=True)
            profile='batch'if accepted else'existing'
            for screen in ('desktop','mobile'):
                shutil.copyfile(tmp_path/f'multi-wallet-{screen}.png',evidence/f'{profile}-{screen}.png')
            (evidence/f'{profile}.json').write_text(json.dumps({
                'passed':True,'transport':'Chromium bridge to real ASGI HTTP API','signed_anvil_receipts':True,
                'sqlcipher_cold_reopen':True,'lost_reply_recovered_without_resubmit':True,'wallet_sends':0,
                'owner_data_used':False,'errors':errors},indent=2))
