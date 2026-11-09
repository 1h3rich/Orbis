"""Owned EVM provider and synchronized clocks for DEX browser scenarios."""
import json
from datetime import datetime,timedelta,timezone
from eth_account import Account
from eth_account.messages import encode_defunct
from playwright.sync_api import expect
from tests.test_dex_deployments import PASSWORD

PROVIDER=r"""
(() => {
 const provider={request:async({method,params})=>{
  (window.fixtureMethods ||= []).push(method);
  if(method==='eth_accounts'||method==='eth_requestAccounts')return [WALLET];
  if(method==='personal_sign')return window.fixtureSign(params[0]);
  if(method==='eth_sendTransaction'&&!ALLOW_SEND)throw Error('This wallet flow cannot send');
  const response=await fetch('/__test/wallet-rpc',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({fixture_id:FIXTURE,method,params:params||[]})});
  if(!response.ok)throw Error('Fixture read failed');return (await response.json()).result;
 },on:()=>{},removeListener:()=>{}};
 window.addEventListener('eip6963:requestProvider',()=>window.dispatchEvent(new CustomEvent('eip6963:announceProvider',{
  detail:{info:{uuid:'11111111-1111-4111-8111-111111111111',name:'Wallet EVM local'},provider}
 })));
})();
"""

def configure(page,fixture,*,wallet_sends=False):
    stamp=fixture['clock_ms'];at=datetime.fromtimestamp(stamp//1000,timezone.utc)+timedelta(milliseconds=stamp%1000)
    page.clock.install(time=at)
    assert abs(page.evaluate('Date.now()')-stamp)<1500
    page.expose_function('fixtureSign',lambda message:'0x'+Account.sign_message(encode_defunct(hexstr=message),bytes.fromhex('11'*32)).signature.hex())
    page.add_init_script(PROVIDER.replace('WALLET',json.dumps(fixture['wallet'])).replace('FIXTURE',json.dumps(fixture['fixture_id'])).replace('ALLOW_SEND',json.dumps(wallet_sends)))

def unlock(page):
    page.locator('#real-passphrase').fill(PASSWORD);page.get_by_role('button',name='Desbloquear',exact=True).click()
    page.get_by_role('button',name='Conectar wallet',exact=True).click()
    expect(page.locator('#real-wallet-state')).to_contain_text('Saldo actualizado')
    page.get_by_role('button',name='Verificar control',exact=True).click()
    expect(page.locator('#real-wallet-proof-state')).to_contain_text('Control EOA comprobado')
    parent=page.locator('#real-wallet-dex-bot');parent.locator(':scope > details > summary').click();return parent
