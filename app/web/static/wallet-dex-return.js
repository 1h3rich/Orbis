/* Owner-only BNB recovery. No key, raw signature or destination input. */
(function(){
  'use strict';
  var root=null,api=null,identity='',epoch=0,busy=false,bindings=[],records=[],review=null,hashes=new Map(),resolver=null,canceller=null;
  var message='Detén el bot y revisa el BNB recuperable.';
  function snap(){return api&&api.snapshot();}
  function key(s){return s&&s.accountId?[s.accountId,s.generation,s.wallet,s.chain,s.verified].join(':'):'';}
  function ready(s){return s&&s.accountId&&s.wallet&&s.provider&&s.verified&&s.chain==='0x38';}
  function field(name){return root.querySelector('[name="dex_return_'+name+'"]');}
  function path(s){return '/accounts/'+encodeURIComponent(s.accountId)+'/dex-gas-returns';}
  function draft(){return window.OrbisDexOperatorDraft&&window.OrbisDexOperatorDraft.verifiedDraft();}
  function current(host,issued,s){return root===host&&root.isConnected&&epoch===issued&&key(snap())===key(s)&&snap().provider===s.provider;}
  function node(parent,tag,value,cls){var el=document.createElement(tag);el.textContent=value;if(cls)el.className=cls;parent.append(el);return el;}
  function amount(value){var s=String(value).padStart(19,'0'),fraction=s.slice(-18).replace(/0+$/,'');return s.slice(0,-18)+(fraction?','+fraction:'');}
  function money(value){return snap().hidden?'•••• BNB':amount(value)+' BNB';}
  function decimal(value,places){
    var match=/^(0|[1-9][0-9]*)(?:\.([0-9]+))?$/.exec(String(value).trim().replace(',','.'));
    if(!match||(match[2]||'').length>places)throw Error('Importe decimal no válido.');
    return BigInt(match[1])*10n**BigInt(places)+BigInt((match[2]||'').padEnd(places,'0'));
  }
  function post(s,suffix,body){return api.request(path(s)+suffix,{method:'POST',body:JSON.stringify(Object.assign({address:s.wallet},body))});}
  function save(row){records=[row].concat(records.filter(function(item){return item.id!==row.id;}));}
  function paint(){
    if(!root||!root.isConnected)return;
    var locked=busy||!!(resolver&&resolver.isBusy())||!!(canceller&&canceller.isBusy())||api.blocked()||!ready(snap()),profile=draft();
    root.querySelectorAll('button,input,select').forEach(function(el){if(!el.closest('[data-return-resolution],[data-return-cancellation]'))el.disabled=locked;});
    root.querySelector('[data-return-review]').disabled=locked||!profile||!field('binding').value;
    root.querySelector('[data-return-status]').textContent=message;
    var output=root.querySelector('[data-return-preview]');output.replaceChildren();
    if(review){
      node(output,'strong','Recuperable · '+money(review.amount_wei));
      node(output,'p','Coste · '+money(review.gas_wei)+' · A tu wallet conectada','muted');
      var send=node(output,'button','Confirmar devolución','ghost');send.type='button';send.dataset.returnSend='';
      send.disabled=locked||!profile||Date.now()>=Date.parse(review.expires_at);
      send.addEventListener('click',submit);
    }
    var history=root.querySelector('[data-return-history]');history.replaceChildren();
    records.forEach(function(item){
      var card=node(history,'section','','real-panel');card.dataset.gasReturnId=item.id;
      var labels={PREPARED:'Revisión preparada',RESERVED:'Reservada',SIGNED:'Firmada · Sin envío',BROADCAST_ATTEMPTED:'Recibo pendiente',
        FINALIZED:'BNB recuperado',REVERTED:'Devolución revertida',CANCELLED:'Revisión cancelada',ABANDONED_UNSENT:'Sin envío'};
      var resolved=item.resolution;
      node(card,'strong',resolved?({CANCELLED:'Cancelado · Resuelto',TRANSFERRED:'BNB recuperado · Resuelto',REVERTED:'Revertido · Resuelto'})[resolved.outcome]:labels[item.status]||'Revisar devolución');
      node(card,'p',(item.receipt?'Recuperado · '+money(item.receipt.transferred_wei):'Importe · '+money(item.amount_wei))+' · '+item.id.slice(0,8),'muted');
      if(item.receipt)node(card,'small','Coste · '+money(item.receipt.gas_fee_wei),'muted');
      if(resolved)node(card,'p','Recuperado · '+money(resolved.receipt.transferred_wei)+' · Coste · '+money(resolved.receipt.gas_fee_wei),'muted');
      if(item.status==='PREPARED'){
        var recover=node(card,'button','Recuperar revisión','ghost');recover.type='button';recover.disabled=locked||item.expired;
        recover.addEventListener('click',function(){review=item;field('binding').value=item.binding_id;message='Revisión recuperada · Confirma el importe.';paint();});
        var cancel=node(card,'button','Cancelar revisión','ghost');cancel.type='button';cancel.disabled=locked;
        cancel.addEventListener('click',function(){closeReview(item.id,false);});
      }
      if(['RESERVED','SIGNED'].includes(item.status)){
        var abandon=node(card,'button','Descartar sin envío','ghost');abandon.type='button';abandon.disabled=locked;
        abandon.setAttribute('data-help','Sólo descarta una reserva que aún no concedió un envío. Un envío incierto requiere su recibo original.');
        abandon.addEventListener('click',function(){closeReview(item.id,true);});
      }
      if(!resolved&&item.status==='BROADCAST_ATTEMPTED'){
        var hash=document.createElement('input');hash.type='text';hash.autocomplete='off';hash.spellcheck=false;hash.maxLength=66;hash.placeholder='Hash opcional';
        hash.value=hashes.get(item.id)||'';hash.disabled=locked;hash.setAttribute('aria-label','Hash de devolución '+item.id.slice(0,8));
        hash.addEventListener('input',function(){hashes.set(item.id,hash.value.trim());});card.append(hash);
        var observe=node(card,'button','Comprobar devolución','ghost');observe.type='button';observe.disabled=locked;
        observe.addEventListener('click',function(){observeReturn(item.id);});
        var replace=node(card,'button','Revisar reemplazo','ghost');replace.type='button';replace.disabled=locked;
        replace.addEventListener('click',function(){review=null;resolver.review(item.id,hashes.get(item.id)||'',false);});
        var cancelPending=node(card,'button','Cancelar devolución','ghost');cancelPending.type='button';cancelPending.disabled=locked||!profile;
        cancelPending.setAttribute('data-help','Revisa un intento de valor cero con el mismo nonce. La devolución original puede confirmarse antes. No se reenvía automáticamente.');
        cancelPending.addEventListener('click',function(){review=null;canceller.select(item);paint();});
      }
    });
    if(resolver)resolver.update();
    if(canceller)canceller.update();
  }
  async function run(task){
    var s=snap();if(busy||!!(resolver&&resolver.isBusy())||!!(canceller&&canceller.isBusy())||api.blocked()||!ready(s))return;
    var host=root,issued=epoch;busy=true;api.onBusyChange();paint();
    try{await task(s,function(){if(!current(host,issued,s))throw Error('La wallet o sesión cambió; comprueba el registro original.');});}
    catch(error){if(current(host,issued,s))message=error.message||'Respuesta sin confirmar · Comprueba el registro original.';}
    finally{if(current(host,issued,s)){busy=false;api.onBusyChange();paint();}}
  }
  function refresh(){return run(async function(s,check){
    var values=await Promise.all([api.request('/accounts/'+encodeURIComponent(s.accountId)+'/dex-bots/bindings'),api.request(path(s)),api.request('/accounts/'+encodeURIComponent(s.accountId)+'/dex-gas-cancellations')]);check();
    bindings=values[0];records=values[1];if(resolver)resolver.reconcile(records);if(canceller)canceller.reconcile(values[2],records);var previous=field('binding').value;field('binding').replaceChildren();
    if(review){var actual=records.find(function(item){return item.id===review.id;});if(actual)review=actual.status==='PREPARED'?actual:null;}
    bindings.forEach(function(item){var option=document.createElement('option');option.value=item.id;
      option.textContent=(item.source_kind==='DCA'?'DCA':'Long')+' · '+item.id.slice(0,8);field('binding').append(option);});
    if(bindings.some(function(item){return item.id===previous;}))field('binding').value=previous;
    message='Vínculos y devoluciones actualizados.';
  });}
  function prepare(event){event.preventDefault();return run(async function(s,check){
    var profile=draft();if(!profile)throw Error('Restaura y verifica la copia de la operadora.');
    var price=decimal(field('price').value,9),maximum=decimal(field('maximum').value,18);
    if(price<=0n||price>1000000000000n||maximum<=0n||maximum>100000000000000000n||21000n*price>maximum)throw Error('El límite de gas no cubre la devolución.');
    review=null;message='Comprobando el BNB recuperable…';paint();
    var result=await post(s,'',{request_id:api.uuid(),binding_id:field('binding').value,draft_id:profile.id,
      gas_price:price.toString(),max_gas_bnb:field('maximum').value.trim().replace(',','.')});check();
    review=result;save(result);message='Devolución revisada · Confirma el importe.';
  });}
  function submit(){return run(async function(s,check){
    var profile=draft(),selected=review;if(!profile||!selected)return;
    message='Confirmando devolución…';paint();
    var result;
    try{result=await post(s,'/'+encodeURIComponent(selected.id)+'/submit',{draft_id:profile.id,plan_digest:selected.plan_digest,confirmation:'DEVOLVER_GAS_OPERADORA_DEX'});}
    catch(error){check();review=null;throw Error(error.status>=400&&error.status<500?error.message:'Devolución sin confirmar · Actualiza el registro original.');}
    check();
    if(result.tx_hash)hashes.set(result.id,result.tx_hash);save(result);review=null;
    message=result.status==='FINALIZED'?'BNB recuperado':result.status==='REVERTED'?'Devolución revertida':'Devolución sin confirmar · Comprueba su recibo.';
  });}
  function observeReturn(identifier){return run(async function(s,check){
    var tx=hashes.get(identifier)||'';if(!/^0x[0-9a-fA-F]{64}$/.test(tx))throw Error('Indica el hash original de la devolución.');
    var result=await post(s,'/'+encodeURIComponent(identifier)+'/observe',{tx_hash:tx});check();save(result);
    message=result.status==='FINALIZED'?'BNB recuperado':'Recibo pendiente · Conserva el hash original.';
  });}
  function closeReview(identifier,abandon){return run(async function(s,check){
    var result=await post(s,'/'+encodeURIComponent(identifier)+(abandon?'/abandon':'/cancel'),{
      confirmation:abandon?'DESCARTAR_DEVOLUCION_NO_EMITIDA':'CANCELAR_REVISION_DEVOLUCION_GAS'});check();save(result);
    if(review&&review.id===identifier)review=null;message='Sin envío · Registro actualizado.';
  });}
  function clear(){
    ++epoch;identity='';busy=false;bindings=[];records=[];review=null;hashes.clear();message='Detén el bot y revisa el BNB recuperable.';
    if(resolver)resolver.clear();
    if(canceller)canceller.clear();
    if(root&&root.isConnected){root.querySelector('form').reset();field('binding').replaceChildren();}paint();
  }
  function update(){var next=key(snap());if(next!==identity){clear();identity=next;}paint();}
  function mount(element,client){
    root=element;api=client;
    root.innerHTML='<details><summary data-help="Devuelve el BNB residual de la operadora a tu wallet verificada. Exige bots detenidos y envíos conciliados. No vende posiciones.">Gas · Recuperar BNB</summary>'+
      '<button type="button" class="ghost" data-return-refresh>Actualizar devoluciones</button><form data-return-form>'+
      '<div class="row"><label class="field">Vínculo<select name="dex_return_binding" required></select></label>'+
      '<label class="field">Precio · gwei<input name="dex_return_price" type="text" inputmode="decimal" value="1" required></label>'+
      '<label class="field">Coste máx. · BNB<input name="dex_return_maximum" type="text" inputmode="decimal" value="0.001" required></label></div>'+
      '<button type="submit" class="ghost" data-return-review>Revisar devolución</button></form>'+
      '<p class="state muted" data-return-status role="status"></p><div data-return-preview></div><div data-return-resolution class="real-panel" hidden></div><div data-return-history></div><div data-return-cancellation class="real-panel" hidden></div></details>';
    resolver=window.OrbisReceiptResolution.create({nativeKind:'GAS_RETURN'});
    resolver.mount(root.querySelector('[data-return-resolution]'),{snapshot:api.snapshot,request:api.request,onBusyChange:function(){api.onBusyChange();paint();},
      blocked:function(){return busy||!!(canceller&&canceller.isBusy())||api.blocked();},onResolved:refresh});
    canceller=window.OrbisDexGasCancellation.create();
    canceller.mount(root.querySelector('[data-return-cancellation]'),{snapshot:api.snapshot,request:api.request,uuid:api.uuid,
      originalHash:function(id){return hashes.get(id)||'';},blocked:function(){return busy||!!(resolver&&resolver.isBusy())||api.blocked();},
      onBusyChange:function(){api.onBusyChange();paint();},onChanged:refresh});
    root.querySelector('[data-return-form]').addEventListener('submit',prepare);root.querySelector('[data-return-refresh]').addEventListener('click',refresh);
    root.querySelector('[data-return-form]').addEventListener('input',function(){review=null;paint();});
    root.querySelector(':scope > details').addEventListener('toggle',function(){if(this.open)refresh();});
    clear();identity=key(snap());paint();
  }
  window.OrbisDexGasReturn={mount:mount,update:update,clear:clear,isBusy:function(){return busy||!!(resolver&&resolver.isBusy())||!!(canceller&&canceller.isBusy());}};
})();
