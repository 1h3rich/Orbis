/* Owner-wallet BNB funding. Exact reviewed recipient; durable one-shot claim. */
(function () {
  'use strict';
  var root=null,api=null,identity='',provider=null,epoch=0,busy=false,walletPending=false,resolver=null,canceller=null;
  var bindings=[],records=[],plan=null,hashes=new Map(),recipients=new Map(),cancellationReady=false,message='Selecciona un vínculo y comprueba su saldo de gas.';
  function snap(){return api&&api.snapshot();}
  function key(s){return s&&s.accountId?[s.accountId,s.generation,s.wallet,s.chain,s.verified].join(':'):'';}
  function ready(s){return s&&s.accountId&&s.provider&&s.wallet&&s.verified&&s.chain==='0x38';}
  function field(name){return root.querySelector('[name="dex_gas_'+name+'"]');}
  function path(s){return '/accounts/'+encodeURIComponent(s.accountId)+'/dex-gas-funding';}
  function post(s,suffix,body){return api.request(path(s)+suffix,{method:'POST',body:JSON.stringify(Object.assign({address:s.wallet},body))});}
  function current(host,issued,s){return root===host&&root.isConnected&&epoch===issued&&key(snap())===key(s)&&snap().provider===s.provider;}
  function assertCurrent(host,issued,s){if(!current(host,issued,s))throw Error('La wallet o la sesión cambió; comprueba el envío original.');}
  function verifiedDraft(){return window.OrbisDexOperatorDraft&&window.OrbisDexOperatorDraft.verifiedDraft();}
  function blocked(){return busy||!!(resolver&&resolver.isBusy())||!!(canceller&&canceller.isBusy())||api.blocked();}
  function notify(){api.onBusyChange();paint();}
  function node(parent,tag,value,cls){var el=document.createElement(tag);el.textContent=value;if(cls)el.className=cls;parent.append(el);return el;}
  function amount(value){var s=String(value).padStart(19,'0'),f=s.slice(-18).replace(/0+$/,'');return s.slice(0,-18)+(f?','+f:'');}
  function money(value){return snap().hidden?'•••• BNB':amount(value)+' BNB';}
  function decimal(value){
    value=String(value).trim().replace(',','.');if(!/^(0|[1-9][0-9]*)(\.[0-9]{1,18})?$/.test(value))throw Error('Importe BNB no válido.');
    var parts=value.split('.');return BigInt(parts[0])*10n**18n+BigInt((parts[1]||'').padEnd(18,'0'));
  }
  function quantity(value){if(typeof value!=='string'||!/^0x(?:0|[1-9a-f][0-9a-f]*)$/i.test(value))throw Error('Respuesta de wallet no válida.');return BigInt(value);}
  function read(s,method,params){
    var timer;
    return Promise.race([Promise.resolve().then(function(){return s.provider.request({method:method,params:params||[]});}),
      new Promise(function(_,reject){timer=setTimeout(function(){reject(Error('La lectura de la wallet agotó su tiempo.'));},8000);})]).finally(function(){clearTimeout(timer);});
  }
  async function wallet(s){
    var values=await Promise.all([read(s,'eth_accounts'),read(s,'eth_chainId')]);
    if(!Array.isArray(values[0])||!values[0][0]||values[0][0].toLowerCase()!==s.wallet.toLowerCase()||quantity(values[1])!==56n)throw Error('Selecciona la wallet propietaria en BNB Chain.');
  }
  function paint(){
    if(!root||!root.isConnected)return;
    var locked=blocked()||!ready(snap()),draft=verifiedDraft();
    root.querySelectorAll('input,select,button').forEach(function(el){if(!el.closest('[data-gas-resolution],[data-funding-cancellation]'))el.disabled=locked;});
    root.querySelector('[data-gas-prepare]').disabled=locked||walletPending||!draft||!field('binding').value;
    root.querySelector('[data-gas-status]').textContent=message;
    var review=root.querySelector('[data-gas-review]');review.replaceChildren();
    if(plan){
      node(review,'strong','Envío de gas · '+money(plan.record.amount_wei));
      node(review,'code',plan.recipient);
      node(review,'p','Saldo observado '+money(plan.balance)+' · Por financiar '+money(plan.fundable),'muted');
      node(review,'p','Comisión máx. '+money(plan.record.max_gas_wei),'muted');
      var send=node(review,'button','Confirmar en wallet','ghost');send.type='button';send.dataset.gasSend='';
      send.disabled=locked||walletPending||Date.now()>=Date.parse(plan.record.expires_at);send.addEventListener('click',sendPlan);
    }
    var history=root.querySelector('[data-gas-history]');history.replaceChildren();
    records.forEach(function(item){
      var card=node(history,'section','','real-panel');
      card.dataset.gasFundingId=item.id;
      var titles={PREPARED:'Preparado',WALLET_REQUESTED:'Respuesta pendiente',HASH_REPORTED:'Recibo pendiente',WALLET_REJECTED:'Rechazado',FINALIZED:'BNB confirmado',REVERTED:'Revertido',NONCE_OBSERVED:'Nonce consumido · Revisar recibo'};
      var resolved=item.resolution;
      node(card,'strong',resolved?({CANCELLED:'Cancelado · Resuelto',TRANSFERRED:'BNB confirmado · Resuelto',REVERTED:'Revertido · Resuelto'})[resolved.outcome]:titles[item.status]||item.status);
      node(card,'p',money(item.amount_wei)+' · '+item.id.slice(0,8),'muted');
      if(item.receipt)node(card,'p','Transferido '+money(item.receipt.transferred_wei)+' · Comisión '+money(item.receipt.gas_fee_wei),'muted');
      if(resolved)node(card,'p','Transferido '+money(resolved.receipt.transferred_wei)+' · Comisión '+money(resolved.receipt.gas_fee_wei),'muted');
      if(!resolved&&['WALLET_REQUESTED','HASH_REPORTED','NONCE_OBSERVED'].includes(item.status)){
        var input=document.createElement('input');input.type='text';input.autocomplete='off';input.spellcheck=false;input.maxLength=66;input.placeholder='Hash opcional';input.setAttribute('aria-label','Hash del envío '+item.id.slice(0,8));input.value=hashes.get(item.id)||'';input.disabled=locked;
        input.addEventListener('input',function(){hashes.set(item.id,input.value.trim());if(canceller)canceller.update();});card.append(input);
        var check=node(card,'button','Comprobar envío','ghost');check.type='button';check.disabled=locked;
        check.addEventListener('click',function(){observe(item.id);});
        var replace=node(card,'button','Revisar reemplazo','ghost');replace.type='button';replace.disabled=locked;
        replace.addEventListener('click',function(){plan=null;resolver.review(item.id,hashes.get(item.id)||'',false);});
        if(cancellationReady&&item.status!=='NONCE_OBSERVED'){
          var cancel=node(card,'button','Cancelar financiación','ghost');cancel.type='button';cancel.disabled=locked||walletPending;
          cancel.setAttribute('data-help','Revisa una sustitución de valor cero desde tu wallet al mismo nonce. El envío original puede confirmarse antes.');
          cancel.addEventListener('click',function(){plan=null;canceller.select(item);});
        }
      }
    });
    if(resolver)resolver.update();
    if(canceller)canceller.update();
  }
  async function run(task){
    var s=snap();if(blocked()||!ready(s))return;
    var host=root,issued=epoch;busy=true;notify();
    try{await task(s,function(){assertCurrent(host,issued,s);});}
    catch(e){if(current(host,issued,s))message=e.message||'Respuesta sin confirmar · Comprueba el envío original.';}
    finally{if(current(host,issued,s)){busy=false;notify();}}
  }
  function save(record){records=[record].concat(records.filter(function(item){return item.id!==record.id;}));}
  function refresh(){return run(async function(s,check){
    var cancellations=api.request('/accounts/'+encodeURIComponent(s.accountId)+'/dex-wallet-cancellations').then(function(rows){return {available:true,rows:rows};}).catch(function(error){if(error.status===404)return {available:false,rows:[]};throw error;});
    var values=await Promise.all([api.request('/accounts/'+encodeURIComponent(s.accountId)+'/dex-bots/bindings'),api.request(path(s)),cancellations]);check();
    bindings=values[0];records=values[1];cancellationReady=values[2].available;if(resolver)resolver.reconcile(records);if(canceller)canceller.reconcile(values[2].rows,records);var previous=field('binding').value;field('binding').replaceChildren();
    bindings.filter(function(item){return item.status==='READY'&&item.grant_status==='ACTIVE'&&!item.expired;}).forEach(function(item){
      var option=document.createElement('option');option.value=item.id;option.textContent=(item.source_kind==='DCA'?'DCA':'Long')+' · '+item.id.slice(0,8);field('binding').append(option);
    });
    if(bindings.some(function(item){return item.id===previous;}))field('binding').value=previous;
    message=records.some(function(item){return item.status==='WALLET_REQUESTED'&&!item.resolution;})?'Hay un envío sin respuesta · Compruébalo antes de repetir.':field('binding').value?'Vínculos y envíos actualizados.':'Guarda primero un vínculo con su política confirmada.';
    if(!cancellationReady)message+=' · Cancelación: reinicio pendiente.';
  });}
  function prepare(event){event.preventDefault();if(walletPending)return;
    return run(async function(s,check){
      plan=null;var draft=verifiedDraft();if(!draft)throw Error('Comprueba la copia de la operadora en «Ejecutor · BNB».');
      var binding=field('binding').value,amountText=field('amount').value.trim().replace(',','.'),maxText=field('maximum').value.trim().replace(',','.');
      var intended=decimal(amountText),maximum=decimal(maxText);if(intended<=0n)throw Error('Indica el importe BNB.');
      await wallet(s);check();
      var state=await post(s,'/state',{binding_id:binding,draft_id:draft.id});check();
      if(state.operator_address.toLowerCase()!==draft.operator_address.toLowerCase())throw Error('La política tiene otra operadora.');
      var price=quantity(await read(s,'eth_gasPrice'));check();
      if(price<=0n||price>3000000000n||price*21000n>maximum)throw Error('La comisión supera el límite revisado.');
      var record=await post(s,'',{binding_id:binding,draft_id:draft.id,request_id:api.uuid(),amount_bnb:amountText,
        gas:'21000',gas_price:price.toString(),max_gas_bnb:maxText});check();
      var prepared={record:record,recipient:draft.operator_address,amount:intended,maximum:maximum,
        balance:state.operator_balance_wei,fundable:state.maximum_funding_wei};
      await inspect(s,prepared);check();plan=prepared;recipients.set(record.id,prepared.recipient);save(record);message='Envío preparado · Revisa el destino y confirma en tu wallet.';
    });
  }
  async function inspect(s,p){
    var tx=p.record.transaction;
    if(!tx||tx.from.toLowerCase()!==s.wallet.toLowerCase()||tx.to.toLowerCase()!==p.recipient.toLowerCase()
      ||tx.chainId!=='0x38'||tx.type!=='0x0'||tx.data!=='0x'||quantity(tx.value)!==p.amount
      ||quantity(tx.gas)!==21000n||quantity(tx.gasPrice)*quantity(tx.gas)>p.maximum
      ||p.record.amount_wei!==p.amount.toString()||p.record.max_gas_wei!==p.maximum.toString())throw Error('El envío BNB no coincide con la revisión.');
    if(Date.now()>=Date.parse(p.record.expires_at))throw Error('La revisión del envío caducó.');
    await wallet(s);
    var values=await Promise.all([read(s,'eth_getCode',[tx.to,'latest']),read(s,'eth_getTransactionCount',[s.wallet,'pending']),
      read(s,'eth_getBalance',[s.wallet,'latest']),read(s,'eth_estimateGas',[tx])]);
    if(values[0]!=='0x'||quantity(values[1])!==quantity(tx.nonce)||quantity(values[2])<p.amount+p.maximum||quantity(values[3])>quantity(tx.gas))throw Error('El destino, nonce, saldo o gas cambió; prepara de nuevo.');
  }
  function sendPlan(){
    if(walletPending||!plan)return;
    return run(async function(s,check){
      var p=plan,id=p.record.id;await inspect(s,p);check();
      // The durable grant always precedes the only wallet send. A lost grant
      // response never calls the wallet or retries this request.
      plan=null;message='Solicitando confirmación…';paint();
      var grant=await post(s,'/'+encodeURIComponent(id)+'/claim',{plan_digest:p.record.plan_digest,confirmation:'FINANCIAR_GAS_OPERADORA_DEX'});check();save(grant);
      if(!grant.claim_granted)throw Error('Este envío ya se solicitó · Comprueba el original.');
      walletPending=true;notify();var timeout;
      var sent=Promise.resolve().then(function(){return s.provider.request({method:'eth_sendTransaction',params:[p.record.transaction]});});
      sent.finally(function(){walletPending=false;if(root&&root.isConnected)notify();}).catch(function(){});
      var hash;
      try{hash=await Promise.race([sent,new Promise(function(_,reject){timeout=setTimeout(function(){reject(Error('Wallet sin respuesta · Comprueba el envío original; no se repetirá.'));},30000);})]);}
      catch(e){
        check();
        if(e&&Number(e.code)===4001){save(await post(s,'/'+encodeURIComponent(id)+'/rejected',{confirmation:'WALLET_RECHAZADA_4001'}));message='Envío rechazado en wallet.';return;}
        throw e;
      }finally{clearTimeout(timeout);}
      check();if(typeof hash!=='string'||!/^0x[0-9a-f]{64}$/i.test(hash))throw Error('Hash sin confirmar · Comprueba el envío original.');
      hashes.set(id,hash);save(await post(s,'/'+encodeURIComponent(id)+'/hash',{tx_hash:hash}));check();
      save(await post(s,'/'+encodeURIComponent(id)+'/observe',{tx_hash:hash}));check();message='Envío comprobado · La activación del bot sigue pendiente.';
    });
  }
  function observe(id){return run(async function(s,check){
    var hash=hashes.get(id)||null;
    if(hash&&!/^0x[0-9a-f]{64}$/i.test(hash))throw Error('Indica el hash original de 66 caracteres.');
    var result=await post(s,'/'+encodeURIComponent(id)+'/observe',{tx_hash:hash});check();save(result);
    message=result.status==='FINALIZED'?'BNB confirmado · El bot todavía no está activado.':result.status==='NONCE_OBSERVED'?'Nonce consumido · Añade el hash para acreditar el importe y la comisión.':'Envío comprobado · '+(result.observation_status==='PENDING'?'Recibo pendiente':result.status);
  });}
  function clear(){++epoch;identity='';provider=null;busy=false;bindings=[];records=[];plan=null;hashes.clear();recipients.clear();cancellationReady=false;message='Selecciona un vínculo y comprueba su saldo de gas.';
    if(resolver)resolver.clear();
    if(canceller)canceller.clear();
    if(root&&root.isConnected){root.querySelector('form').reset();field('binding').replaceChildren();}paint();}
  function update(){if(!root||!root.isConnected)return;var s=snap();if(identity!==key(s)||provider!==s.provider){clear();identity=key(s);provider=s.provider;}paint();}
  function mount(element,client){root=element;api=client;
    root.innerHTML='<details><summary data-help="Envía BNB desde tu wallet a la operadora de un vínculo ya guardado. No aumenta su presupuesto ni activa el bot.">Gas · BNB</summary>'+
      '<button type="button" class="ghost" data-gas-refresh>Actualizar gas</button><form class="row"><label class="field">Vínculo<select name="dex_gas_binding" required></select></label>'+
      '<label class="field">Importe · BNB<input name="dex_gas_amount" type="text" inputmode="decimal" autocomplete="off" placeholder="0.001" required></label>'+
      '<label class="field">Comisión máx. · BNB<input name="dex_gas_maximum" type="text" inputmode="decimal" autocomplete="off" value="0.0001" required></label>'+
      '<button type="submit" class="ghost" data-gas-prepare>Preparar gas</button></form><p class="state muted" data-gas-status role="status"></p>'+
      '<div data-gas-review class="real-panel"></div><div data-gas-resolution class="real-panel" hidden></div><details><summary>Envíos de gas</summary><div data-gas-history></div></details><div data-funding-cancellation class="real-panel" hidden></div></details>';
    resolver=window.OrbisReceiptResolution.create({nativeKind:'GAS_FUNDING'});
    resolver.mount(root.querySelector('[data-gas-resolution]'),{snapshot:api.snapshot,request:api.request,onBusyChange:notify,
      blocked:function(){return busy||walletPending||!!(canceller&&canceller.isBusy())||api.blocked();},onResolved:refresh});
    canceller=window.OrbisDexFundingCancellation.create();
    canceller.mount(root.querySelector('[data-funding-cancellation]'),{request:api.request,uuid:api.uuid,snapshot:api.snapshot,
      originalHash:function(id){return hashes.get(id)||'';},originalRecipient:function(id){return recipients.get(id)||'';},
      blocked:function(){return busy||walletPending||!!(resolver&&resolver.isBusy())||api.blocked();},
      reviewReplacement:function(id,tx){plan=null;resolver.review(id,tx,false);},onBusyChange:notify,onChanged:refresh});
    root.querySelector('form').addEventListener('submit',prepare);root.querySelector('form').addEventListener('input',function(){plan=null;paint();});
    root.querySelector('[data-gas-refresh]').addEventListener('click',refresh);
    root.querySelector(':scope > details').addEventListener('toggle',function(){if(this.open)refresh();});
    clear();identity=key(snap());provider=snap().provider;paint();
  }
  window.OrbisDexGasFunding={mount:mount,update:update,clear:clear,isBusy:function(){return busy||walletPending||!!(resolver&&resolver.isBusy())||!!(canceller&&canceller.isBusy());}};
})();
