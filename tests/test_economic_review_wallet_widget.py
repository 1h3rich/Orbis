"""Secondary-provider accounting controls: no trade RPC or browser persistence."""
import pytest
from playwright.sync_api import expect
from tests.test_economic_review_widget import browser, page, opened


MULTI = """() => {
  window.peer='44444444-4444-4444-8444-444444444444';
  window.rootLink='55555555-5555-4555-8555-555555555555';
  window.peerLink='66666666-6666-4666-8666-666666666666';
  window.peerSource='77777777-7777-4777-8777-777777777777';
  window.peerAddress='0x2222222222222222222222222222222222222222';
  window.peerEvents=new Map();window.secondaryCalls=[];window.sourceWait=[];
  window.storageWrites=[];
  ['localStorage','sessionStorage'].forEach(name=>Object.defineProperty(window,name,{configurable:true,
    value:new Proxy({setItem:(...args)=>storageWrites.push({name,args})},{set:(target,key,value)=>{storageWrites.push({name,key,value});return true;}})}));
  window.secondary={on:(event,handler)=>peerEvents.set(event,handler),
    removeListener:(event,handler)=>{if(peerEvents.get(event)===handler)peerEvents.delete(event);},
    request:async({method,params})=>{secondaryCalls.push({method,params});
      if(method==='eth_requestAccounts'||method==='eth_accounts')return window.peerAccounts||[peerAddress];
      if(method==='eth_chainId')return window.peerChain||'0x38';
      if(method==='personal_sign')return window.signWait?await new Promise(resolve=>pending.push({resolve})):('0x'+'22'.repeat(65));
      throw new Error('Forbidden secondary method '+method);}};
  const originalProvider=state.provider;
  state.provider={...originalProvider,request:async(request)=>{
    if(request.method==='eth_accounts')return[state.wallet];
    if(request.method==='eth_chainId')return'0x38';
    return originalProvider.request(request);}};
  row.wallet_link_id=rootLink;row.wallet_account_id=account;
  window.peerRow={...row,source_id:peerSource,wallet_link_id:peerLink,wallet_account_id:peer,context_verified:false};
  window.scope={schema:'orbis.portfolio.economic.wallet.scope.v1',moves_funds:false,trading_authority:false,
    wallets:[{id:rootLink,account_id:account,account_name:'Principal',alias:'Uno',provided:true},
      {id:peerLink,account_id:peer,account_name:'Secundaria',alias:'Dos',provided:false}],
    missing_receipts:[{source_kind:'OPERATOR',source_id:peerSource}]};
  catalog.wallet_scope=scope;
  window.baseRequest=client.request;
  client.reviewProviders=()=>[{name:'Proveedor secundario',provider:secondary}];
  client.request=(url,options,passive)=>{
    let body=options&&JSON.parse(options.body);
    if(url.endsWith('/sources')&&body.wallets&&body.wallets.length){
      calls.push({url,body,passive});
      const value={...catalog,sources:[row,peerRow],wallet_scope:{...scope,
        wallets:scope.wallets.map(w=>({...w,provided:true})),missing_receipts:[]}};
      if(window.wrongLink)value.wallet_scope.wallets[1].provided=false;
      if(window.delaySources)return new Promise(resolve=>sourceWait.push({resolve,value}));
      return Promise.resolve(value);
    }
    return baseRequest(url,options,passive);
  };
  capability.capabilities.economic_review_wallets=true;
  OrbisEconomicReview.mount(document.querySelector('#host'),client);
}"""


def multi(page):
    page.evaluate(MULTI)
    page.on('dialog', lambda dialog: dialog.accept())
    opened(page)
    expect(page.get_by_role('button', name='Revisar', exact=True)).to_be_disabled()
    page.get_by_role('button', name='Conectar', exact=True).click()
    page.get_by_role('button', name='Elegir cuenta', exact=True).click()
    expect(page.get_by_role('button', name='Verificar wallet', exact=True)).to_be_visible()


def verified(page):
    multi(page)
    page.get_by_role('button', name='Verificar wallet', exact=True).click()
    expect(page.locator('[data-er-source]')).to_have_count(2)


def test_wallet_and_source_context_are_separate_and_correct_addresses_reach_preview(page):
    verified(page)
    expect(page.get_by_role('button', name='Revisar', exact=True)).to_be_disabled()
    page.get_by_role('button', name='Verificar contexto · Dos · Principal', exact=True).click()
    expect(page.get_by_role('button', name='Revisar', exact=True)).to_be_enabled()
    page.get_by_role('button', name='Revisar', exact=True).click()
    expect(page.get_by_role('button', name='Aceptar criterio', exact=True)).to_be_visible()
    calls=page.evaluate('calls')
    proofs=[item for item in calls if item['url'].endswith('/wallet-proof/start')]
    assert [item['url'].split('/')[2] for item in proofs]==[page.evaluate('peer'),page.evaluate('account')]
    assert all(item['body']['address']==page.evaluate('peerAddress') for item in proofs)
    body=next(item['body'] for item in calls if item['url'].endswith('/preview'))
    assert body['wallets']==[{'context_account_id':page.evaluate('peer'),'address':page.evaluate('peerAddress')}]
    assert {item['source_id']:item['wallet_address'] for item in body['receipts']}=={
        page.evaluate('source'):page.evaluate('state.wallet'),page.evaluate('peerSource'):page.evaluate('peerAddress')}
    assert {item['method'] for item in page.evaluate('secondaryCalls')}=={'eth_requestAccounts','eth_accounts','eth_chainId','personal_sign'}
    assert page.evaluate('secondaryCalls.filter(c=>c.method==="personal_sign").length')==2
    assert page.evaluate('storageWrites')==[]


@pytest.mark.parametrize('event',['accountsChanged','chainChanged','disconnect'])
def test_secondary_provider_change_discards_draft_and_ignores_late_catalogue(page,event):
    multi(page)
    page.evaluate('delaySources=true')
    page.get_by_role('button', name='Verificar wallet', exact=True).click()
    expect(page.locator('[data-er-message]')).to_have_text('Verificando wallet…')
    page.wait_for_function('sourceWait.length===1')
    page.evaluate('(event)=>peerEvents.get(event)([])',event)
    expect(page.locator('dialog')).not_to_be_visible()
    page.evaluate('sourceWait.shift().resolve({ ...catalog, sources:[row,peerRow], wallet_scope:scope})')
    assert page.locator('[data-er-source]').count()==0
    assert page.evaluate('peerEvents.size')==0
    assert not page.evaluate('calls.some(c=>c.url.endsWith("/preview"))')


def test_provider_change_during_signature_never_submits_confirmation(page):
    multi(page)
    page.evaluate('signWait=true')
    page.get_by_role('button', name='Verificar wallet', exact=True).click()
    page.wait_for_function('pending.length===1')
    page.evaluate('peerEvents.get("accountsChanged")([]);pending.shift().resolve("0x"+"22".repeat(65))')
    expect(page.locator('dialog')).not_to_be_visible()
    assert not page.evaluate('calls.some(c=>c.url.endsWith("/wallet-proof/confirm"))')


def test_wrong_wallet_link_is_not_admitted_and_rejected_signature_does_not_attach_wallet(page):
    multi(page)
    page.evaluate('wrongLink=true')
    page.get_by_role('button', name='Verificar wallet', exact=True).click()
    expect(page.locator('[data-er-message]')).to_have_text('Catálogo de wallets inválido.')
    expect(page.locator('[data-er-source]')).to_have_count(1)
    expect(page.get_by_role('button', name='Revisar', exact=True)).to_be_disabled()
    page.evaluate('() => {wrongLink=false;secondary.request=async()=>{throw new Error("Firma rechazada")};}')
    page.get_by_role('button', name='Verificar wallet', exact=True).click()
    expect(page.locator('[data-er-message]')).to_have_text('Firma rechazada')
    assert not page.evaluate('calls.some(c=>c.url.endsWith("/preview"))')


def test_close_releases_secondary_provider_and_refresh_keeps_explicit_wallet_contexts(page):
    verified(page)
    page.get_by_role('button', name='Actualizar', exact=True).click()
    page.wait_for_function('calls.filter(c=>c.url.endsWith("/sources")).length===3')
    assert page.evaluate('calls.filter(c=>c.url.endsWith("/sources")).at(-1).body.wallets[0].address')==page.evaluate('peerAddress')
    page.get_by_role('button', name='Cerrar revisión', exact=True).click()
    assert page.evaluate('peerEvents.size')==0
    page.get_by_role('button', name='Revisar cuenta', exact=True).click()
    expect(page.locator('[data-er-source]')).to_have_count(1)
    assert page.evaluate('calls.filter(c=>c.url.endsWith("/sources")).at(-1).body.wallets')==[]


@pytest.mark.parametrize('flag',[False,'true',1])
def test_wallet_controls_require_strict_boolean_capability_and_preserve_old_requests(page,flag):
    page.evaluate(MULTI)
    page.evaluate('(flag)=>{capability.capabilities.economic_review_wallets=flag;OrbisEconomicReview.mount(document.querySelector("#host"),client)}',flag)
    opened(page)
    assert not page.get_by_role('button', name='Conectar', exact=True).count()
    body=page.evaluate('calls.find(c=>c.url.endsWith("/sources")).body')
    assert body=={'address':page.evaluate('state.wallet')}
