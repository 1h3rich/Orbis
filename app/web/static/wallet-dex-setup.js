/* Owner wallet setup. Backend prepares exact calls; only the wallet sends them. */
(function () {
  'use strict';
  var root=null,api=null,identity='',epoch=0,busy=false,walletPending=false,plan=null,deployments=[],controls=[],state=null;
  var hashes=new Map(),message='Conecta y verifica tu wallet en BNB Chain.';
  var tokens={USDT:'0x55d398326f99059ff775485246999027b3197955',WBNB:'0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c'};
  var confirmations={DEPLOY:'DESPLEGAR_EJECUTOR_DEX',CONFIGURE:'CONFIGURAR_EJECUTOR_DEX',STOP:'DETENER_EJECUTOR_DEX',APPROVE:'AUTORIZAR_TOKENS_DEX',REVOKE:'REVOCAR_TOKENS_DEX'};
  var titles={DEPLOY:'Desplegar ejecutor',CONFIGURE:'Configurar límites',STOP:'Detener ejecutor',APPROVE:'Autorizar',REVOKE:'Revocar'};
  function snap(){return api && api.snapshot();}
  function key(s){return s && s.accountId ? [s.accountId,s.generation,s.wallet,s.chain,s.verified].join(':') : '';}
  function valid(s){return !api.parentBusy() && s && s.accountId && s.provider && s.wallet && s.verified && s.chain==='0x38';}
  function current(host,issued,s){return root===host && root.isConnected && epoch===issued && key(snap())===key(s) && snap().provider===s.provider;}
  function assertCurrent(host,issued,s){if(!current(host,issued,s))throw Error('La wallet o la sesión cambió. Recupera la operación original.');}
  function path(s,kind){return '/accounts/'+encodeURIComponent(s.accountId)+(kind==='DEPLOY'?'/dex-deployments':'/dex-controls');}
  function post(url,body){return api.request(url,{method:'POST',body:JSON.stringify(body)});}
  function node(parent,tag,value,cls){var el=document.createElement(tag);el.textContent=value;if(cls)el.className=cls;parent.append(el);return el;}
  function field(name){return root.querySelector('[name="owner_'+name+'"]');}
  function selected(){return deployments.find(function(d){return d.id===field('executor').value;});}
  function template(){var p=api && api.review();return p ? JSON.stringify({kind:p.source_kind,source:p.source,amounts:p.amounts,dca:p.dca}) : '';}
  function decimal(value,places){
    value=String(value).trim().replace(',','.');
    if(!new RegExp('^(0|[1-9][0-9]*)(\\.[0-9]{1,'+places+'})?$').test(value))throw Error('Importe decimal no válido.');
    var p=value.split('.');return BigInt(p[0])*10n**BigInt(places)+BigInt((p[1]||'').padEnd(places,'0'));
  }
  function amount(value,places){var s=String(value).padStart(places+1,'0'),f=s.slice(-places).replace(/0+$/,'');return s.slice(0,-places)+(f?','+f:'');}
  function money(value,unit){return snap() && snap().hidden ? '•••• '+unit : amount(value,18)+' '+unit;}
  function quantity(value){if(typeof value!=='string'||!/^0x(0|[1-9a-f][0-9a-f]*)$/i.test(value))throw Error('Lectura EVM no válida.');return BigInt(value);}
  function integer(name,min,max){var s=field(name).value;if(!/^[0-9]+$/.test(s))throw Error('Revisa los parámetros de ejecución.');var n=Number(s);if(!Number.isSafeInteger(n)||n<min||n>max)throw Error('Parámetro fuera de rango.');return n;}
  async function wallet(s){
    var chain=await s.provider.request({method:'eth_chainId'}),accounts=await s.provider.request({method:'eth_accounts'});
    if(chain!=='0x38'||!Array.isArray(accounts)||!accounts.some(function(a){return typeof a==='string'&&a.toLowerCase()===s.wallet.toLowerCase();}))throw Error('Vuelve a conectar la wallet propietaria en BNB Chain.');
  }
  function notify(){if(api)api.onBusyChange();paint();}
  function resolutionBusy(){return !!(window.OrbisDeploymentResolution&&window.OrbisDeploymentResolution.isBusy());}
  function blocked(){return busy||walletPending||resolutionBusy()||!!(window.OrbisDexOperatorDraft&&window.OrbisDexOperatorDraft.isBusy());}
  function paint(){
    if(!root||!root.isConnected)return;
    var s=snap(),ready=valid(s),locked=blocked(),active=selected();
    root.querySelectorAll('input,select,button').forEach(function(el){if(!el.closest('[data-dex-operator-draft],[data-owner-resolution]'))el.disabled=locked||!ready;});
    root.querySelector('[data-owner-configure]').disabled=locked||!ready||!active||!active.executor_verified||!api.review();
    root.querySelector('[data-owner-stop]').disabled=locked||!ready||!active||!active.executor_verified;
    root.querySelectorAll('[data-owner-token]').forEach(function(button){
      var token=button.dataset.ownerToken,action=button.dataset.ownerAction;
      button.disabled=locked||!ready||!state||!active||state.deployment_id!==active.id||
        (action==='APPROVE'?state.allowance_caps[token]===null||BigInt(state.allowance_caps[token])<=0n||BigInt(state['allowance_'+token.toLowerCase()])!==0n:BigInt(state['allowance_'+token.toLowerCase()])===0n);
    });
    root.querySelector('[data-owner-status]').textContent=message;
    var summary=root.querySelector('[data-owner-current]');summary.replaceChildren();
    if(state&&active&&state.deployment_id===active.id){
      node(summary,'p',(state.operator_stopped?'Detenido':'Política '+state.policy_id)+' · Permiso USDT '+money(state.allowance_usdt,'USDT')+' · WBNB '+money(state.allowance_wbnb,'WBNB'),'muted');
    }
    var review=root.querySelector('[data-owner-review]');review.replaceChildren();review.hidden=!plan;
    if(plan){
      node(review,'strong',(plan.record.rollover?'Renovar bot':titles[plan.kind])+(plan.record.allowance?' '+plan.record.allowance.token:''));
      node(review,'p','Gas máximo · '+money(plan.record.max_gas_wei,'BNB'),'muted');
      if(plan.record.allowance)node(review,'p','Permiso · '+money(plan.record.allowance.units,plan.record.allowance.token),'muted');
      if(plan.kind==='CONFIGURE'){
        node(review,'p','Entrada '+money(plan.record.limits.max_buy_per_order,'USDT')+' · Presupuesto '+money(plan.record.limits.buy_budget,'USDT')+' · '+(!plan.record.limits.allow_buy?'Sólo cierre':plan.record.limits.allow_sell?'Compras y ventas':'Sólo compras'),'muted');
        node(review,'p','Caduca · '+new Date(plan.record.limits.expires_at*1000).toLocaleString(),'muted');
        if(plan.record.rollover)node(review,'p','Gas restante · '+money(plan.record.rollover.gas_remaining_wei,'BNB'),'muted');
      }
      var send=node(review,'button','Confirmar en wallet');send.type='button';send.dataset.ownerSend='';send.disabled=locked||!ready||Date.now()>=Date.parse(plan.record.expires_at);send.addEventListener('click',sendPlan);
      var cancel=node(review,'button','Descartar','ghost');cancel.type='button';cancel.disabled=locked;cancel.addEventListener('click',function(){plan=null;message='Revisión descartada · Sin envío.';paint();});
    }
    var history=root.querySelector('[data-owner-history]');history.replaceChildren();
    deployments.concat(controls).forEach(function(item){
      var kind=item.action||'DEPLOY',card=document.createElement('section');card.className='real-panel';card.dataset.ownerOperation=item.id;history.append(card);
      var resolved=item.resolution;
      var status=resolved?({CANCELLED:'Cancelado · Resuelto',DEPLOYED:'Recuperado · Resuelto',REVERTED:'Revertido · Resuelto'})[resolved.outcome]:({PREPARED:'Preparado',WALLET_REQUESTED:'Respuesta pendiente',HASH_REPORTED:'Recibo pendiente',WALLET_REJECTED:'Rechazado en wallet',FINALIZED:'Confirmado',REVERTED:'Revertido',CODE_OBSERVED:'Contrato comprobado · Falta recibo',NONCE_OBSERVED:'Nonce consumido · Falta recibo'}[item.status]||item.status);
      node(card,'strong',(item.rollover?'Renovación':titles[kind])+(item.allowance?' '+item.allowance.token:'')+' · '+status);
      node(card,'small',item.id.slice(0,8)+' · '+new Date(item.created_at).toLocaleString(),'muted');
      if(resolved)node(card,'p','Gas usado · '+money(resolved.receipt.gas_fee_wei,'BNB'),'muted');
      if(item.receipt)node(card,'p','Gas usado · '+money(item.receipt.gas_fee_wei,'BNB'),'muted');
      if(item.rollover&&item.status==='FINALIZED'){
        var apply=node(card,'button','Aplicar renovación','ghost');apply.type='button';apply.dataset.ownerRenewalApply=item.id;apply.disabled=locked||!ready;
        apply.addEventListener('click',function(){applyRenewal(item.id);});
      }
      if(!resolved&&['WALLET_REQUESTED','HASH_REPORTED','CODE_OBSERVED','NONCE_OBSERVED'].indexOf(item.status)>=0){
        var label=node(card,'label',kind==='DEPLOY'?'Hash de transacción':'Hash original','field'),input=document.createElement('input');input.type='text';input.autocomplete='off';input.spellcheck=false;input.maxLength=66;input.placeholder='0x… · opcional para recuperar estado';input.value=hashes.get(item.id)||'';input.disabled=locked||!ready;label.append(input);
        input.addEventListener('input',function(){hashes.set(item.id,input.value.trim());});
        var button=node(card,'button','Comprobar','ghost');button.type='button';button.disabled=locked||!ready;button.addEventListener('click',function(){observe(kind,item.id);});
        if(kind==='DEPLOY'){
          var resolve=node(card,'button','Revisar reemplazo','ghost');resolve.type='button';resolve.disabled=locked||!ready;
          resolve.setAttribute('data-help','Busca el recibo finalizado por nonce si dejas el hash vacío.');
          resolve.addEventListener('click',function(){window.OrbisDeploymentResolution.review(item.id,hashes.get(item.id)||'',false);});
        }

      }
    });
    if(window.OrbisDexOperatorDraft)window.OrbisDexOperatorDraft.update();
    if(window.OrbisDeploymentResolution)window.OrbisDeploymentResolution.update();
  }
  async function refreshData(s){
    var values=await Promise.all([api.request(path(s,'DEPLOY')),api.request(path(s,'CONFIGURE'))]);
    return {deployments:values[0],controls:values[1]};
  }
  function applyData(data){
    var previous=field('executor').value;deployments=data.deployments;controls=data.controls;
    if(window.OrbisDeploymentResolution)window.OrbisDeploymentResolution.reconcile(deployments);
    var select=field('executor');select.replaceChildren();
    deployments.filter(function(d){return d.executor_verified;}).forEach(function(d){var option=document.createElement('option');option.value=d.id;option.textContent='Ejecutor '+d.id.slice(0,8)+' · '+(d.fee/10000)+' %';select.append(option);});
    if(deployments.some(function(d){return d.id===previous&&d.executor_verified;}))select.value=previous;
  }
  async function readState(s){var d=selected();return d ? post(path(s,'CONFIGURE')+'/state',{deployment_id:d.id,address:s.wallet}) : null;}
  async function refresh(){
    var s=snap();if(blocked()||!valid(s))return;
    var host=root,issued=epoch;busy=true;notify();
    try{var data=await refreshData(s);assertCurrent(host,issued,s);applyData(data);state=null;var found=await readState(s);assertCurrent(host,issued,s);state=found?Object.assign(found,{deployment_id:selected().id}):null;message=state?'Estado del ejecutor actualizado.':'Prepara tu primer ejecutor.';}
    catch(e){if(current(host,issued,s))message=e.message||'No se pudo comprobar el ejecutor.';}
    finally{if(current(host,issued,s)){busy=false;notify();}}
  }
  async function prepare(kind,token){
    var s=snap();if(blocked()||!valid(s))return;
    var host=root,issued=epoch,source=api.review(),templateKey=template(),d=selected(),operator=null;
    busy=true;plan=null;message='Preparando llamada y límites…';notify();
    try{
      await wallet(s);assertCurrent(host,issued,s);
      var price=quantity(await s.provider.request({method:'eth_gasPrice'}));
      if(price<=0n||price>decimal(field('gas_price').value,9)||price>1000000000000n)throw Error('El precio de gas supera tu límite.');
      var body={request_id:api.uuid(),address:s.wallet,gas:kind==='DEPLOY'?'3500000':kind==='CONFIGURE'?'1000000':'100000',gas_price:price.toString(),max_gas_bnb:field('gas_budget').value.trim().replace(',','.')};
      if(kind==='DEPLOY'){
        body.chain_id='0x38';body.fee=Number(field('fee').value);
        body.nonce=quantity(await s.provider.request({method:'eth_getTransactionCount',params:[s.wallet,'pending']})).toString();
      }else{
        if(!d||!d.executor_verified)throw Error('Selecciona un ejecutor verificado.');
        body.deployment_id=d.id;body.action=kind;
        if(kind==='CONFIGURE'){
          if(!source)throw Error('Comprueba primero la estrategia y sus límites.');
          operator=field('operator').value.trim();if(!/^0x[0-9a-fA-F]{40}$/.test(operator))throw Error('Indica la dirección pública del operador.');
          body.operator=operator;
          body.limits={expires_at:Math.floor(Date.now()/1000)+60*integer('minutes',1,1440),cooldown_seconds:integer('cooldown',60,86400),max_entries:integer('entries',1,1000),
            twap_seconds:integer('twap',300,86400),max_deviation_bps:integer('deviation',1,500),max_buy_per_order:source.amounts.entry_units,buy_budget:source.amounts.budget_units,
            max_exposure:source.amounts.exposure_units,max_exit_notional:source.amounts.exposure_units,max_realized_loss:source.amounts.loss_units,
            max_gas_price:decimal(field('gas_price').value,9).toString(),max_wbnb_acquired:source.amounts.wbnb_quota_units,allow_buy:true,allow_sell:source.source_kind==='STRATEGY'};
        }else if(token){
          var latest=await readState(s);assertCurrent(host,issued,s);state=Object.assign(latest,{deployment_id:d.id});
          body.token=token;body.allowance_units=kind==='REVOKE'?'0':state.allowance_caps[token];
          if(body.allowance_units===null)throw Error(state.allowance_reasons[token]||'Permiso no disponible.');
        }
      }
      assertCurrent(host,issued,s);
      var record=await post(path(s,kind),body);assertCurrent(host,issued,s);
      var prepared={kind:kind,record:record,operator:operator,templateKey:templateKey,maxGas:decimal(body.max_gas_bnb,18),deployment:d?d.id:null,executor:state&&d&&state.deployment_id===d.id?state.executor_address:null};
      await inspect(s,prepared);assertCurrent(host,issued,s);
      if(kind==='CONFIGURE'&&template()!==templateKey)throw Error('Los límites cambiaron. Revisa la configuración.');
      plan=prepared;message='Revisión lista · Confirma la operación en tu wallet.';
    }catch(e){if(current(host,issued,s))message=e.message||'No se pudo preparar la llamada.';}
    finally{if(current(host,issued,s)){busy=false;notify();}}
  }
  async function inspect(s,p){
    var tx=p.record.transaction;
    if(!tx||tx.from.toLowerCase()!==s.wallet.toLowerCase()||tx.chainId!=='0x38'||tx.type!=='0x0'||tx.value!=='0x0'||!/^0x[0-9a-f]+$/i.test(tx.data)||quantity(tx.gas)*quantity(tx.gasPrice)>p.maxGas)throw Error('La llamada no coincide con la revisión.');
    if(p.kind==='DEPLOY'?tx.to!==undefined:(!tx.to||tx.to.toLowerCase()!==(p.record.allowance?tokens[p.record.allowance.token]:p.executor||'').toLowerCase()))throw Error('Destino del ejecutor no verificado.');
    if(Date.now()>=Date.parse(p.record.expires_at))throw Error('La revisión caducó.');
    await wallet(s);
    var nonce=quantity(await s.provider.request({method:'eth_getTransactionCount',params:[s.wallet,'pending']}));
    var balance=quantity(await s.provider.request({method:'eth_getBalance',params:[s.wallet,'pending']}));
    if(nonce!==quantity(tx.nonce)||balance<quantity(tx.gas)*quantity(tx.gasPrice))throw Error('El nonce o el saldo BNB cambió. Revisa de nuevo.');
    var estimate=quantity(await s.provider.request({method:'eth_estimateGas',params:[tx]}));
    if(estimate<=0n||estimate>quantity(tx.gas))throw Error('La estimación supera el gas revisado.');
    await wallet(s);
  }
  async function reviewRenewal(record,operator){
    var s=snap();if(blocked()||!valid(s))throw Error('Espera a que termine la operación actual.');
    var host=root,issued=epoch;busy=true;plan=null;notify();
    try{
      if(!record.rollover||record.action!=='CONFIGURE'||record.status!=='PREPARED')throw Error('La renovación ya está solicitada. Comprueba su operación original.');
      applyData(await refreshData(s));assertCurrent(host,issued,s);field('executor').value=record.deployment_id;
      if(!selected()||selected().id!==record.deployment_id)throw Error('Ejecutor no verificado.');
      var found=await readState(s);assertCurrent(host,issued,s);state=Object.assign(found,{deployment_id:record.deployment_id});
      var prepared={kind:'CONFIGURE',record:record,operator:operator,templateKey:null,
        maxGas:BigInt(record.max_gas_wei),deployment:record.deployment_id,executor:state.executor_address};
      await inspect(s,prepared);assertCurrent(host,issued,s);plan=prepared;
      root.querySelector(':scope > details').open=true;
      root.querySelector('[data-owner-review]').scrollIntoView({block:'center'});message='Límites restantes · Confirma en wallet.';
    }finally{if(current(host,issued,s)){busy=false;notify();}}
  }
  async function applyRenewal(identifier){
    var s=snap();if(blocked()||!valid(s))return;
    var host=root,issued=epoch;busy=true;message='Aplicando renovación…';notify();
    try{
      await post('/accounts/'+encodeURIComponent(s.accountId)+'/dex-bots/renewals/'+encodeURIComponent(identifier)+'/apply',
        {address:s.wallet,confirmation:'APLICAR_RENOVACION_BOT_DEX'});assertCurrent(host,issued,s);
      message='Renovación aplicada · Rearma el bot para operar.';
    }catch(e){if(current(host,issued,s))message=e.message||'Renovación sin confirmar.';}
    finally{if(current(host,issued,s)){busy=false;notify();if(window.OrbisDexRuns)window.OrbisDexRuns.refresh();}}
  }
  async function sendPlan(){
    var s=snap(),p=plan;if(blocked()||!valid(s)||!p)return;
    var host=root,issued=epoch;busy=true;walletPending=true;message='Comprobando la llamada antes de solicitar la firma…';notify();
    try{
      if(p.kind==='CONFIGURE'&&!p.record.rollover&&p.templateKey!==template())throw Error('Los límites cambiaron. Revisa de nuevo.');
      await inspect(s,p);assertCurrent(host,issued,s);
      var claimBody={address:s.wallet,plan_digest:p.record.plan_digest,confirmation:confirmations[p.kind]};
      if(p.kind==='CONFIGURE')claimBody.operator=p.operator;
      // A lost claim reply must never cause a second wallet request.
      plan=null;paint();
      var claim=await post(path(s,p.kind)+'/'+p.record.id+'/claim',claimBody);assertCurrent(host,issued,s);
      if(!claim.claim_granted)throw Error('La solicitud ya existe. Comprueba la operación original.');
      message='Confirma la operación en tu wallet.';paint();
      var hash;
      try{hash=await s.provider.request({method:'eth_sendTransaction',params:[p.record.transaction]});}
      catch(e){
        if(e&&e.code===4001&&current(host,issued,s)){
          await post(path(s,p.kind)+'/'+p.record.id+'/rejected',{confirmation:'WALLET_RECHAZO_4001'});
          throw Error('Operación rechazada en la wallet.');
        }
        throw Error('Respuesta de wallet sin confirmar. Comprueba la operación original; no se reenviará.');
      }
      assertCurrent(host,issued,s);
      if(typeof hash!=='string'||!/^0x[0-9a-fA-F]{64}$/.test(hash))throw Error('Hash de wallet no válido. Comprueba la operación original.');
      hashes.set(p.record.id,hash);
      await post(path(s,p.kind)+'/'+p.record.id+'/hash',{tx_hash:hash});assertCurrent(host,issued,s);
      var result=await post(path(s,p.kind)+'/'+p.record.id+'/observe',{address:s.wallet,tx_hash:hash});assertCurrent(host,issued,s);
      message=result.status==='FINALIZED'?(p.kind==='DEPLOY'?'Despliegue confirmado.':'Operación confirmada.') : result.status==='REVERTED'?'Operación revertida · Gas registrado.':'Recibo pendiente · Comprueba la operación original.';
    }catch(e){if(current(host,issued,s))message=e.message && !/fetch|network/i.test(e.message)?e.message:'Respuesta local sin confirmar · Comprueba la operación original.';}
    finally{
      walletPending=false;
      if(current(host,issued,s)){
        try{var data=await refreshData(s);assertCurrent(host,issued,s);applyData(data);state=null;var found=await readState(s);assertCurrent(host,issued,s);state=found?Object.assign(found,{deployment_id:selected().id}):null;}
        catch(e){if(current(host,issued,s))message+=' · Actualiza el estado para comprobarlo.';}
        if(current(host,issued,s)){busy=false;notify();}
      }else{if(api)api.onBusyChange();paint();}
    }
  }
  async function observe(kind,id){
    var s=snap();if(blocked()||!valid(s))return;
    var host=root,issued=epoch,body={address:s.wallet},hash=hashes.get(id);if(hash)body.tx_hash=hash;
    busy=true;plan=null;message='Comprobando la operación original…';notify();
    try{
      var result=await post(path(s,kind)+'/'+id+'/observe',body);assertCurrent(host,issued,s);
      var data=await refreshData(s);assertCurrent(host,issued,s);applyData(data);state=null;
      var found=await readState(s);assertCurrent(host,issued,s);state=found?Object.assign(found,{deployment_id:selected().id}):null;
      message=result.status==='FINALIZED'?'Operación confirmada.':result.status==='REVERTED'?'Operación revertida · Gas registrado.':result.status==='CODE_OBSERVED'?'Contrato comprobado · Añade el hash original para registrar el gas.':result.status==='NONCE_OBSERVED'?'Nonce consumido · Añade el hash original para comprobar el recibo.':'Aún pendiente · No se reenviará.';
    }catch(e){if(current(host,issued,s))message=e.message||'No se pudo comprobar la operación.';}
    finally{if(current(host,issued,s)){busy=false;notify();}}
  }
  function clear(){if(window.OrbisDeploymentResolution)window.OrbisDeploymentResolution.clear();if(window.OrbisDexOperatorDraft)window.OrbisDexOperatorDraft.clear();++epoch;identity='';busy=false;plan=null;deployments=[];controls=[];state=null;hashes=new Map();message='Conecta y verifica tu wallet en BNB Chain.';if(root&&root.isConnected){root.querySelector('[data-owner-form]').reset();field('executor').replaceChildren();}paint();}
  function update(){var next=key(snap());if(identity!==next){clear();identity=next;}if(plan&&plan.kind==='CONFIGURE'&&!plan.record.rollover&&plan.templateKey!==template()){plan=null;message='Los límites cambiaron. Revisa la configuración.';}paint();}
  function mount(element,client){
    root=element;api=client;root.innerHTML='<details><summary data-help="Despliega el ejecutor y configura permisos finitos desde tu wallet. Cada llamada requiere confirmación; la activación del bot tiene un permiso separado.">Ejecutor · BNB</summary>'+
      '<div data-dex-operator-draft></div><form data-owner-form><div class="row"><label class="field">Ejecutor<select name="owner_executor" aria-label="Ejecutor de wallet"></select></label><button type="button" class="ghost" data-owner-refresh>Actualizar ejecutor</button><label class="field">Pool<select name="owner_fee"><option value="500">0,05 %</option><option value="100">0,01 %</option><option value="3000">0,3 %</option><option value="10000">1 %</option></select></label><button type="button" class="ghost" data-owner-deploy>Preparar despliegue</button></div>'+
      '<div class="row"><label class="field" data-help="Dirección pública de una cuenta EOA separada de tu wallet. No introduzcas claves privadas.">Operador<input name="owner_operator" type="text" autocomplete="off" spellcheck="false" placeholder="0x…" maxlength="42"></label><label class="field">Vigencia · min<input name="owner_minutes" type="number" min="1" max="1440" value="60"></label><button type="button" class="ghost" data-owner-configure>Preparar límites</button><button type="button" class="ghost" data-owner-stop>Preparar STOP</button></div>'+
      '<details><summary>Gas y ejecución</summary><div class="dex-bot-fields"><label class="field">Gas máx. · BNB<input name="owner_gas_budget" type="text" inputmode="decimal" value="0.01"></label><label class="field">Precio máx. · gwei<input name="owner_gas_price" type="text" inputmode="decimal" value="3"></label><label class="field">Espera · s<input name="owner_cooldown" type="number" value="60" min="60" max="86400"></label><label class="field">Entradas máx.<input name="owner_entries" type="number" value="4" min="1" max="1000"></label><label class="field">TWAP · s<input name="owner_twap" type="number" value="1800" min="300" max="86400"></label><label class="field" data-help="Un punto básico equivale al 0,01 %. 50 pb = 0,5 %.">Desviación · pb<input name="owner_deviation" type="number" value="50" min="1" max="500"></label></div></details>'+
      '<div class="row"><button type="button" class="ghost" data-owner-token="USDT" data-owner-action="APPROVE">Autorizar USDT</button><button type="button" class="ghost" data-owner-token="WBNB" data-owner-action="APPROVE">Autorizar WBNB</button><button type="button" class="ghost" data-owner-token="USDT" data-owner-action="REVOKE">Revocar USDT</button><button type="button" class="ghost" data-owner-token="WBNB" data-owner-action="REVOKE">Revocar WBNB</button></div></form>'+
      '<p data-owner-status class="state muted" role="status"></p><div data-owner-current></div><div data-owner-review class="real-panel" aria-live="polite"></div><div data-owner-resolution class="real-panel" hidden></div><details><summary>Operaciones del ejecutor</summary><div data-owner-history></div></details></details>';
    root.querySelector('[data-owner-form]').addEventListener('submit',function(e){e.preventDefault();});
    root.querySelector('[data-owner-refresh]').addEventListener('click',refresh);
    root.querySelector('[data-owner-deploy]').addEventListener('click',function(){prepare('DEPLOY');});
    root.querySelector('[data-owner-configure]').addEventListener('click',function(){prepare('CONFIGURE');});
    root.querySelector('[data-owner-stop]').addEventListener('click',function(){prepare('STOP');});
    root.querySelectorAll('[data-owner-token]').forEach(function(b){b.addEventListener('click',function(){prepare(b.dataset.ownerAction,b.dataset.ownerToken);});});
    root.querySelector('[data-owner-form]').addEventListener('input',function(){plan=null;paint();});
    field('executor').addEventListener('change',function(){state=null;refresh();});
    root.querySelector(':scope > details').addEventListener('toggle',function(){if(this.open)refresh();});
    if(window.OrbisDexOperatorDraft)window.OrbisDexOperatorDraft.mount(root.querySelector('[data-dex-operator-draft]'),{
      request:api.request,uuid:api.uuid,snapshot:api.snapshot,blocked:function(){return busy||walletPending||api.parentBusy();},
      onBusyChange:notify,useOperator:function(address){plan=null;field('operator').value=address;paint();}
    });
    if(window.OrbisDeploymentResolution)window.OrbisDeploymentResolution.mount(root.querySelector('[data-owner-resolution]'),{request:api.request,snapshot:api.snapshot,onBusyChange:notify,
      blocked:function(){return busy||walletPending||api.parentBusy()||!!(window.OrbisDexOperatorDraft&&window.OrbisDexOperatorDraft.isBusy());},onResolved:refresh});
    clear();identity=key(snap());paint();
  }
  window.OrbisDexSetup={mount:mount,update:update,clear:clear,isBusy:blocked,reviewRenewal:reviewRenewal};
})();
