/* Explicit bounded LONG/DCA activation. Claims/keys never enter the browser. */
(function () {
  'use strict';
  var root=null,api=null,identity='',epoch=0,busy=false,reading=false,timer=null,pending=null;
  var bindings=[],allBindings=[],runs=[],hashes=new Map(),stopping=new Set(),stopped=new Set(),rendered='',pendingRenewal=null;
  var message='Selecciona un vínculo preparado.',resolver=null;
  var researchFor='',researchEpoch=0;
  var fundingReview=null,fundingPending=false,fundingEpoch=0;
  function resolutionBusy(){return !!(resolver&&resolver.isBusy());}
  function snap(){return api&&api.snapshot();}
  function key(s){return s&&s.accountId?[s.accountId,s.generation,s.wallet,s.chain,s.verified].join(':'):'';}
  function ready(s){return s&&s.accountId&&s.verified&&s.wallet&&s.chain==='0x38';}
  function current(host,issued,scope){return root===host&&root.isConnected&&epoch===issued&&key(snap())===scope;}
  function field(name){return root.querySelector('[name="dex_run_'+name+'"]');}
  function path(s){return '/accounts/'+encodeURIComponent(s.accountId)+'/dex-bots/runs';}
  function draft(){return window.OrbisDexOperatorDraft&&window.OrbisDexOperatorDraft.verifiedDraft();}
  function previousForBinding(id){
    var own=runs.find(function(item){return item.binding_id===id;});if(own)return own;
    var binding=bindings.find(function(item){return item.id===id;});
    return binding&&runs.find(function(item){return item.id===binding.resume_from_run_id;});
  }
  function node(parent,tag,value,className){var el=document.createElement(tag);el.textContent=value;if(className)el.className=className;parent.append(el);return el;}
  function human(error,fallback){return error&&error.status&&error.message?error.message:fallback;}
  function decimal(text,places){
    text=String(text).trim().replace(',','.');var match=/^(0|[1-9][0-9]*)(?:\.([0-9]+))?$/.exec(text);
    if(!match||(match[2]||'').length>places)throw Error('Importe decimal no válido.');
    return BigInt(match[1])*10n**BigInt(places)+BigInt((match[2]||'').padEnd(places,'0')||'0');
  }
  function money(value,unit){
    if(snap().hidden)return '•••• '+unit;
    var text=String(value),whole=text.length>18?text.slice(0,-18):'0';
    var fraction=text.padStart(19,'0').slice(-18).replace(/0+$/,'');return whole+(fraction?','+fraction:'')+' '+unit;
  }
  function executionInputs(){
    var gas=Number(field('gas').value),price=decimal(field('price').value,9);
    var maximum=decimal(field('maximum').value,18),slippage=decimal(field('slippage').value,2);
    if(!Number.isSafeInteger(gas)||gas<21000||gas>2000000)throw Error('Gas por orden: entre 21.000 y 2.000.000.');
    if(price<=0n||price>1000000000000n)throw Error('Precio de gas: mayor que cero y hasta 1.000 gwei.');
    if(maximum<=0n||maximum>100000000000000000n||BigInt(gas)*price>maximum)throw Error('El gas máximo debe cubrir la orden y no superar 0,1 BNB.');
    if(slippage<1n||slippage>500n)throw Error('Deslizamiento: entre 0,01 % y 5 %.');
    return {gas:gas,gas_price:price.toString(),max_gas_bnb:field('maximum').value.trim().replace(',','.'),slippage_bps:Number(slippage)};
  }
  function clearFundingReview(){
    if(fundingPending||fundingReview)message='Revisa el saldo con los valores actuales.';
    ++fundingEpoch;fundingReview=null;fundingPending=false;
  }
  function paintFundingReview(){
    var panel=root.querySelector('[data-run-funding-review]');panel.replaceChildren();
    panel.hidden=!fundingReview;
    if(!fundingReview)return;
    var gas=fundingReview.gas,wallet=fundingReview.wallet;
    node(panel,'strong',gas.shortfall_wei==='0'?'Gas cubierto':'Gas insuficiente');
    node(panel,'p','Disponible '+money(gas.available_wei,'BNB')+' · Orden '+money(gas.order_wei,'BNB')+
      ' · Reserva STOP '+money(gas.stop_reserve_wei,'BNB'),'muted');
    if(gas.shortfall_wei!=='0')node(panel,'small','Falta '+money(gas.shortfall_wei,'BNB'),'muted');
    node(panel,'p','USDT utilizable '+money(wallet.available_usdt_units,'USDT')+' · Entrada de referencia '+money(wallet.reference_entry_units,'USDT'),'muted');
    node(panel,'p','WBNB utilizable '+money(wallet.available_wbnb_units,'WBNB')+
      ' · Posición '+money(wallet.bot_position_wbnb_units,'WBNB'),'muted');
    node(panel,'small','Bloque finalizado '+fundingReview.block_number,'muted');
  }
  async function reviewFunding(){
    var s=snap(),profile=draft(),binding=field('binding').value;
    if(busy||fundingPending||api.blocked()||!ready(s)||!profile||!binding)return;
    var host=root,issued=epoch,scope=key(s),serial=++fundingEpoch;
    fundingReview=null;fundingPending=true;message='Comprobando saldo y permisos…';paint();
    try{
      var body=Object.assign({binding_id:binding,draft_id:profile.id,address:s.wallet},executionInputs());
      var result=await request(path(s)+'/activation-review',{method:'POST',body:JSON.stringify(body)},22000,true);
      if(!current(host,issued,scope)||serial!==fundingEpoch)return;
      if(field('binding').value!==binding||draft()?.id!==profile.id){clearFundingReview();paint();return;}
      if(result.binding_id!==binding||result.draft_id!==profile.id||result.automatic_execution!==false||
        !result.gas||!result.wallet||![result.block_number,...Object.values(result.gas),...Object.values(result.wallet)]
          .every(function(value){return typeof value==='string'&&/^\d+$/.test(value);}))throw Error('Saldo no verificable.');
      fundingReview=result;message='Saldo y permisos consultados.';
    }catch(error){if(current(host,issued,scope)&&serial===fundingEpoch)message=error.message||'No se pudo revisar el saldo.';}
    finally{if(current(host,issued,scope)&&serial===fundingEpoch){fundingPending=false;paint();}}
  }
  async function request(url,options,timeout,passive){
    var controller=new AbortController(),cancel=setTimeout(function(){controller.abort();},timeout||12000);
    try{return await api.request(url,Object.assign({},options||{},{signal:controller.signal}),passive||!options||!options.method||options.method==='GET');}
    finally{clearTimeout(cancel);}
  }
  async function loadResearch(){
    var selected=field('binding').value,s=snap();
    if(!selected){researchFor='';++researchEpoch;field('research').replaceChildren();return;}
    if(!s||!s.accountId||researchFor===selected)return;
    researchFor=selected;var serial=++researchEpoch,host=root,issued=epoch,scope=key(s),select=field('research');
    var previous=select.value;select.replaceChildren();
    var empty=document.createElement('option');empty.value='';empty.textContent='Elegir reserva';select.append(empty);paint();
    try{
      var result=await request('/accounts/'+encodeURIComponent(s.accountId)+'/dex-bots/bindings/'+encodeURIComponent(selected)+'/research-options');
      if(!current(host,issued,scope)||serial!==researchEpoch||field('binding').value!==selected)return;
      var binding=bindings.find(function(item){return item.id===selected;});
      var last=previousForBinding(selected);
      var proof=last&&last.research_qualification||binding&&binding.research_qualification;
      var original=proof&&proof.evidence.research.artifact_id;
      (result.options||[]).forEach(function(item){var option=document.createElement('option');option.value=item.id;
        option.textContent=(item.method==='ROLLING_FIXED_VERSION'?'Ventanas':'Reserva')+' · '+item.test_bars+' velas · '+item.id.slice(0,8);select.append(option);});
      if(previous&&result.options.some(function(item){return item.id===previous;}))select.value=previous;
      else if(original&&result.options.some(function(item){return item.id===original;}))select.value=original;
      else if(select.options.length===2)select.selectedIndex=1;
      if(select.options.length===1)message='Sin reserva compatible · Valida esta versión en LAB o DCA.';
    }catch(error){if(current(host,issued,scope)&&serial===researchEpoch)message=human(error,'No se pudo consultar la investigación.');}
    finally{if(current(host,issued,scope)&&serial===researchEpoch)paint();}
  }
  function paint(){
    if(!root||!root.isConnected)return;
    var s=snap(),valid=!!(s&&s.accountId),locked=busy||resolutionBusy()||api.blocked()||!ready(s);
    root.hidden=!valid;
    root.querySelectorAll('form[data-run-activate] input,form[data-run-activate] select').forEach(function(el){el.disabled=locked;});
    var active=runs.some(function(item){return item.execution_enabled||item.worker&&(item.worker.running||item.worker.pending_receipt);});
    var option=field('binding').selectedOptions[0];
    if(fundingReview&&(fundingReview.binding_id!==field('binding').value||fundingReview.draft_id!==draft()?.id))clearFundingReview();
    var gasShortfall=fundingReview&&fundingReview.gas.shortfall_wei!=='0';
    var previous=previousForBinding(field('binding').value),resuming=previous&&previous.rearm_supported;
    var start=root.querySelector('[data-run-start]');start.textContent=resuming?'Rearmar':'Activar';
    start.disabled=locked||fundingPending||!!gasShortfall||!!pending||active||!draft()||!option||option.disabled||!field('research').value||!field('permission').checked||!!resuming&&previous.stored_status!=='STOPPED';
    root.querySelector('[data-run-funding]').disabled=locked||fundingPending||!!pending||!draft()||!option||option.disabled;
    root.querySelector('[data-run-refresh]').disabled=reading||!valid;
    root.querySelector('[data-run-cancel]').hidden=!pending;
    root.querySelector('[data-run-cancel]').disabled=!valid||!!pending&&stopping.has(pending.id);
    root.querySelector('[data-run-status]').textContent=message;
    paintFundingReview();
    var selected=bindings.find(function(item){return item.id===field('binding').value;});
    var dca=selected&&selected.source_kind==='DCA';
    root.querySelector('[data-run-dca-window]').hidden=!dca;
    field('window').required=!!dca;field('window').disabled=locked||!dca||!!resuming;
    if(dca&&resuming&&previous.dca)field('window').value=previous.dca.limits.activation_window_seconds;
    root.querySelector('[data-run-limits]').textContent=selected?'Entrada máx. '+money(selected.effective_limits.max_buy_per_order,'USDT')+' · Exposición '+money(selected.effective_limits.max_exposure,'USDT'):'';
    var signature=JSON.stringify([runs,allBindings,locked,!!draft(),pendingRenewal])+':'+s.hidden+':'+Array.from(stopping).join(',');
    if(signature!==rendered){
      rendered=signature;var history=root.querySelector('[data-run-history]');history.replaceChildren();
      runs.forEach(function(item){
        var card=document.createElement('section');card.className='real-panel';card.dataset.dexRunId=item.id;history.append(card);
        var labels={RUNNING:'Activo',SETTLING:'Confirmando',REARM_REQUIRED:'Rearme necesario',STOPPED:'Detenido',PAUSED:'En pausa',REVIEW_REQUIRED:'Parada local · Revisar'};
        node(card,'strong',labels[item.runtime_status]||'Revisar');
        node(card,'p',(item.source_kind==='DCA'?'DCA':'Long')+' · '+item.source.symbol+' · '+item.source.timeframe+' · '+item.id.slice(0,8),'muted');
        node(card,'small',item.research_qualification?'Reserva '+item.research_qualification.evidence.research.artifact_id.slice(0,8):'Legado · Sin reserva','muted');
        if(item.previous_run_id)node(card,'small','Retomado · '+item.previous_run_id.slice(0,8),'muted');
        if(item.execution_enabled)node(card,'p',(item.permission_mode==='OPERATIONAL'?'Segundo plano':'Sesión')+' · hasta '+new Date(item.expires_at).toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'}),'muted');
        var latest=runs.find(function(run){return (run.root_binding_id||run.binding_id)===(item.root_binding_id||item.binding_id);});
        var positionLabel=latest&&latest.id!==item.id?'Histórico · ':'Posición · ';
        node(card,'p',item.review_required?'Posición · Sin verificar':positionLabel+money(item.position.owned_wbnb,'WBNB')+' · Coste '+money(item.position.cost_usdt,'USDT'));
        if(item.dca)node(card,'p','Compras · '+money(item.dca.spent_units,'USDT')+' · '+item.dca.consumed_keys.length+(item.dca.consumed_keys.length===1?' activación':' activaciones'),'muted');
        var lastAverage=item.dca&&item.dca.last_average;
        if(lastAverage){
          var signal=lastAverage.signal,kind=(signal.ma_kind||'sma').toUpperCase();
          var decision={DCA_MA_ZERO_AMOUNT:'Importe ×0',DCA_AMOUNT_BELOW_UNIT:'Importe inferior a una unidad mínima',
            DCA_SMA_CONDITION_NOT_MET:'Filtro sin señal',DCA_MA_WARMUP:'Historial insuficiente',DCA_SMA_WARMUP:'Historial insuficiente'}[lastAverage.reason];
          node(card,'small','Última señal · '+kind+' '+(signal.ma_period||signal.sma_period)+
            (signal.amount_factor!=null&&!decision?' · ×'+signal.amount_factor:'')+(decision?' · '+decision:''),'muted');
        }
        var lastVolume=item.dca&&item.dca.last_volume;
        if(lastVolume){
          var volume=lastVolume.signal,ratio=window.OrbisUI.fmtDecimal(volume.volume_ratio,0,4);
          var volumeWait={DCA_VOLUME_BELOW_MINIMUM:'Volumen por debajo del mínimo',DCA_VOLUME_WARMUP:'Historial insuficiente',
            DCA_VOLUME_BASELINE_ZERO:'Volumen medio cero'}[lastVolume.reason];
          var volumeNote=node(card,'small','Volumen MEXC'+(ratio!==null?' · '+ratio+'×':'')+
            (volumeWait?' · '+volumeWait:''),'muted');
          volumeNote.title='Mínimo '+volume.volume_min_ratio+'× · '+volume.volume_period+' velas'+
            (volume.volume_ratio!==null?' · Valor exacto '+volume.volume_ratio+'×':'');
        }
        if(item.stored_status==='ACTIVE'){
          var stop=node(card,'button','Detener','ghost');stop.type='button';stop.dataset.runStop=item.id;stop.disabled=stopping.has(item.id);
          stop.addEventListener('click',function(){stopRun(item.id);});
        }
        var graph=node(card,'button','Gráfico','ghost');graph.type='button';graph.dataset.runChart=item.id;
        graph.disabled=!!item.review_required;
        graph.addEventListener('click',function(){window.OrbisDexBotChart.show(item);});
        var binding=allBindings.find(function(value){return value.id===item.binding_id;});
        if(latest&&latest.id===item.id&&binding&&binding.status==='READY'&&item.stored_status==='STOPPED'){
          var renew=node(card,'button',item.policy_renewal_supported===false?'Renovación pendiente':pendingRenewal&&pendingRenewal.previous_run_id===item.id?'Recuperar renovación':'Renovar','ghost');
          renew.type='button';renew.dataset.runRenew=item.id;
          renew.disabled=item.policy_renewal_supported===false||locked||active||!draft()||!!pendingRenewal&&pendingRenewal.previous_run_id!==item.id;
          renew.setAttribute('data-help',item.policy_renewal_supported===false?'La renovación de políticas con resultados recuperados está pendiente de implementación.':'Revisa la vigencia con los presupuestos restantes. Requiere confirmar la configuración en tu wallet; no arranca el bot.');
          renew.addEventListener('click',function(){renewRun(item.id);});
        }
        if(item.worker&&item.worker.review_required)node(card,'p','Parada local · Revisar registro','muted');
        (item.intents||[]).forEach(function(intent){
          var line=document.createElement('div');line.dataset.dexIntentId=intent.id;card.append(line);
          var action=intent.action==='BUY'?'Compra':intent.action==='SELL'?'Venta':'STOP';
          var effective=intent.effective_outcome||intent.status,receipt=intent.resolution?intent.resolution.receipt:intent.receipt;
          var manuallyReviewed=effective==='EXTERNAL_REVIEWED';
          node(line,'p',action+' · '+(manuallyReviewed?'Revisada · externa':['FINALIZED','EXECUTED'].includes(effective)?'Confirmada':effective==='CANCELLED'?'Cancelada':effective==='REVERTED'?'Revertida':effective==='ABANDONED_UNSENT'?'Sin envío':'Recibo pendiente'),'muted');
          if(receipt)node(line,'small','Gas '+money(receipt.gas_fee_wei,'BNB'),'muted');
          if(manuallyReviewed&&window.OrbisExternalReview&&window.OrbisExternalReview.available()){
            var savedExternal=node(line,'button','Ver registro','ghost');savedExternal.type='button';savedExternal.addEventListener('click',function(){window.OrbisExternalReview.showRecord('OPERATOR',intent.id);});
          }
          if(['BROADCAST_ATTEMPTED','NONCE_OBSERVED'].includes(intent.status)&&!intent.resolution&&!manuallyReviewed){
            var hash=document.createElement('input');hash.type='text';hash.autocomplete='off';hash.spellcheck=false;hash.maxLength=66;hash.placeholder='Hash opcional';
            hash.setAttribute('aria-label','Hash de transacción '+intent.id.slice(0,8));hash.value=hashes.get(intent.id)||'';
            hash.addEventListener('input',function(){hashes.set(intent.id,hash.value.trim());});line.append(hash);
            var observe=node(line,'button','Comprobar recibo','ghost');observe.type='button';observe.disabled=!ready(s)||reading;
            observe.addEventListener('click',function(){observeRun(item.id,intent.id);});
            var resolve=node(line,'button','Revisar resultado','ghost');resolve.type='button';resolve.dataset.callResolve=intent.id;
            resolve.disabled=locked||item.stored_status!=='STOPPED'||!!(item.worker&&item.worker.running);
            resolve.setAttribute('data-help','Comprueba el recibo de una aceleración o sustitución al mismo nonce. Requiere detener el bot; no envía órdenes.');
            resolve.addEventListener('click',function(){resolver.review(intent.id,hashes.get(intent.id)||'',false,{label:action,asset:intent.action==='BUY'?'WBNB':intent.action==='SELL'?'USDT':null});});
            if(window.OrbisExternalReview&&window.OrbisExternalReview.available()){
              var external=node(line,'button','Recibo externo','ghost');external.type='button';external.dataset.runExternal=intent.id;external.disabled=resolve.disabled;
              external.addEventListener('click',function(){window.OrbisExternalReview.openSource('OPERATOR',intent.id,hashes.get(intent.id)||'');});
            }
          }
        });
      });
    }
    if(resolver)resolver.update();
  }
  function schedule(){
    if(timer)clearTimeout(timer);timer=null;
    if(root&&root.isConnected&&root.querySelector('details').open)timer=setTimeout(function(){timer=null;refresh();},4000);
  }
  async function refresh(){
    if(reading||!snap()||!snap().accountId){schedule();return;}
    var s=snap(),host=root,issued=epoch,scope=key(s);reading=true;paint();
    try{
      var result=await Promise.all([request(path(s)),request('/accounts/'+encodeURIComponent(s.accountId)+'/dex-bots/bindings')]);
      if(!current(host,issued,scope))return;
      runs=result[0];runs.forEach(function(item){window.OrbisDexBotChart.sync(item);});
      if(resolver)resolver.reconcile(runs.flatMap(function(item){return item.intents||[];}));
      allBindings=result[1];bindings=allBindings.filter(function(item){return ['STRATEGY','DCA'].includes(item.source_kind)&&item.status==='READY'&&item.grant_status==='ACTIVE'&&!item.expired;});
      var previous=field('binding').value;field('binding').replaceChildren();
      bindings.forEach(function(item){var option=document.createElement('option');option.value=item.id;
        var used=runs.some(function(run){return run.binding_id===item.id&&run.has_execution_history;});
        var previous=previousForBinding(item.id),canResume=previous&&previous.rearm_supported&&previous.stored_status==='STOPPED';
        option.textContent=(item.source_kind==='DCA'?'DCA':'Long')+' · '+item.source.symbol+' · '+item.source.timeframe+' · '+item.id.slice(0,8)+(canResume?' · Retomar':used?' · Rearme pendiente':'');option.disabled=used&&!canResume;field('binding').append(option);});
      if(bindings.some(function(item){return item.id===previous;}))field('binding').value=previous;
      loadResearch();
      if(pending&&runs.some(function(item){return item.id===pending.id;})){
        pending=null;field('permission').checked=false;field('background').checked=false;
      }
    }catch(error){if(current(host,issued,scope)&&!busy)message=human(error,'No se pudo actualizar el estado.');}
    finally{if(current(host,issued,scope)){reading=false;api.onBusyChange();paint();schedule();}}
  }
  async function activate(event){
    event.preventDefault();var s=snap(),profile=draft();
    if(busy||pending||fundingPending||root.querySelector('[data-run-start]').disabled||api.blocked()||!ready(s)||!profile||!field('permission').checked)return;
    var host=root,issued=epoch,scope=key(s),identifier=api.uuid(),submitted=false;
    try{
      var minutes=Number(field('minutes').value),options=executionInputs();
      if(!Number.isSafeInteger(minutes)||minutes<1||minutes>1440)throw Error('Vigencia: entre 1 minuto y 24 horas.');
      var previous=previousForBinding(field('binding').value),resuming=previous&&previous.rearm_supported;
      if(resuming&&previous.stored_status!=='STOPPED')throw Error('Detén el permiso anterior antes de rearmar.');
      var body={request_id:identifier,binding_id:field('binding').value,draft_id:profile.id,address:s.wallet,
        research_validation_id:field('research').value,
        ttl_seconds:minutes*60,permission_mode:field('background').checked?'OPERATIONAL':'SESSION',
        ...options,confirmation:resuming?'REARMAR_BOT_DEX':'ACTIVAR_BOT_DEX'};
      var selected=bindings.find(function(item){return item.id===body.binding_id;});
      if(selected&&selected.source_kind==='DCA'){
        var windowSeconds=Number(field('window').value);
        if(!Number.isSafeInteger(windowSeconds)||windowSeconds<60||windowSeconds>86400)throw Error('Ventana DCA: entre 60 y 86.400 segundos.');
        body.dca_activation_window_seconds=windowSeconds;
      }
      pending={id:identifier,path:path(s)};submitted=true;busy=true;message='Activando…';api.onBusyChange();paint();
      var target=path(s)+(resuming?'/'+encodeURIComponent(previous.id)+'/rearm':'');
      var result=await request(target,{method:'POST',body:JSON.stringify(body)},20000);
      if(!current(host,issued,scope))return;
      if(!stopped.has(identifier)){runs=[result].concat(runs.filter(function(item){return item.id!==identifier;}));message='Activación confirmada';}
      pending=null;field('permission').checked=false;field('background').checked=false;
    }catch(error){if(current(host,issued,scope)){
      if(error.status>=400&&error.status<500)pending=null;
      message=submitted?human(error,'Activación sin confirmar · Consulta o detén la solicitud.'):error.message;
    }}finally{if(current(host,issued,scope)){busy=false;api.onBusyChange();paint();refresh();}}
  }
  async function stopRun(identifier){
    var s=snap();if(!s||!s.accountId||stopping.has(identifier))return;
    var host=root,issued=epoch,scope=key(s);stopping.add(identifier);stopped.add(identifier);paint();
    try{
      var result=await request(path(s)+'/'+encodeURIComponent(identifier)+'/stop',{method:'POST',body:JSON.stringify({confirmation:'DETENER_BOT_DEX'})});
      if(!current(host,issued,scope))return;
      if(pending&&pending.id===identifier)pending=null;
      if(result.source)runs=[result].concat(runs.filter(function(item){return item.id!==identifier;}));
      else if(result.worker)runs=runs.map(function(item){return item.id===identifier?Object.assign({},item,result):item;});
      message='Parada local confirmada';
    }catch(error){if(current(host,issued,scope))message=human(error,'Parada sin confirmar · Consulta el estado.');}
    finally{if(current(host,issued,scope)){stopping.delete(identifier);paint();refresh();}}
  }
  async function renewRun(identifier){
    var s=snap(),profile=draft();if(busy||api.blocked()||!ready(s)||!profile||!window.OrbisDexSetup)return;
    var host=root,issued=epoch,scope=key(s),record=null;busy=true;message='Revisando límites restantes…';api.onBusyChange();paint();
    try{
      if(!pendingRenewal){
        var minutes=Number(field('minutes').value),price=decimal(field('price').value,9);
        if(!Number.isSafeInteger(minutes)||minutes<1||minutes>1440)throw Error('Vigencia: entre 1 minuto y 24 horas.');
        if(price<=0n||price>1000000000000n)throw Error('Revisa el precio máximo de gas.');
        pendingRenewal={request_id:api.uuid(),previous_run_id:identifier,draft_id:profile.id,address:s.wallet,
          expires_at:Math.floor(Date.now()/1000)+minutes*60,gas:'1000000',gas_price:price.toString(),max_gas_bnb:field('maximum').value.trim().replace(',','.')};
      }
      record=await request('/accounts/'+encodeURIComponent(s.accountId)+'/dex-bots/renewals',
        {method:'POST',body:JSON.stringify(pendingRenewal)},20000);
      if(!current(host,issued,scope))return;
      pendingRenewal=null;message='Renovación preparada · Confirma en wallet.';
    }catch(error){if(current(host,issued,scope)){
      if(error.status>=400&&error.status<500)pendingRenewal=null;
      message=human(error,error.message||'Revisión sin confirmar · Recupera la renovación.');
    }}finally{if(current(host,issued,scope)){busy=false;api.onBusyChange();paint();}}
    if(record&&current(host,issued,scope)){
      try{await window.OrbisDexSetup.reviewRenewal(record,profile.operator_address);}
      catch(error){if(current(host,issued,scope)){message=error.message||'Comprueba la renovación original.';paint();}}
    }
  }
  async function observeRun(runId,intentId){
    var s=snap(),tx=hashes.get(intentId)||'';if(!ready(s)||!/^0x[0-9a-fA-F]{64}$/.test(tx)){message='Indica el hash original del envío.';paint();return;}
    var host=root,issued=epoch,scope=key(s);
    try{var result=await request(path(s)+'/'+runId+'/intents/'+intentId+'/observe',{method:'POST',body:JSON.stringify({address:s.wallet,tx_hash:tx})});
      if(current(host,issued,scope)){runs=runs.map(function(item){return item.id===runId?result:item;});message='Recibo comprobado';paint();}}
    catch(error){if(current(host,issued,scope)){message=human(error,'Recibo sin confirmar.');paint();}}
  }
  function clear(){
    clearFundingReview();
    if(resolver)resolver.clear();
    var cancel=pending;pending=null;
    if(cancel&&api){api.request(cancel.path+'/'+encodeURIComponent(cancel.id)+'/stop',{
      method:'POST',keepalive:true,body:JSON.stringify({confirmation:'DETENER_BOT_DEX'})}).catch(function(){});}
    if(window.OrbisDexBotChart)window.OrbisDexBotChart.reset();
    ++epoch;++researchEpoch;researchFor='';if(timer)clearTimeout(timer);timer=null;busy=false;reading=false;pending=null;pendingRenewal=null;bindings=[];allBindings=[];runs=[];hashes.clear();stopping.clear();stopped.clear();rendered='';identity='';
    if(root&&root.isConnected){root.querySelector('form').reset();field('binding').replaceChildren();field('research').replaceChildren();}message='Selecciona un vínculo preparado.';paint();}
  function update(){var next=key(snap());if(next!==identity){clear();identity=next;if(root.querySelector('details').open)refresh();}if(window.OrbisDexBotChart)window.OrbisDexBotChart.redact();paint();}
  function mount(element,client){
    if(timer)clearTimeout(timer);root=element;api=client;
    root.innerHTML='<details><summary data-help="Activa un Long o DCA vinculado y financiado. La política, la operadora y el permiso limitan su vigencia. Bloquear la bóveda detiene la firma; no vende posiciones.">Bot · Activar</summary>'+
      '<button type="button" class="ghost" data-run-refresh>Actualizar bots</button><form data-run-activate>'+
      '<div class="row"><label class="field">Vínculo<select name="dex_run_binding" required></select></label><label class="field" data-help="Máximo solicitado. Termina antes si caduca la operadora o su política.">Vigencia · min<input name="dex_run_minutes" type="number" min="1" max="1440" step="1" value="15" required></label></div>'+
      '<label class="field" data-help="Se reproduce la reserva original antes de conceder este permiso. Sus señales son de MEXC; los costes y precios DEX se registran al ejecutar.">Reserva<select name="dex_run_research" required></select></label>'+
      '<p data-run-limits class="muted"></p><button type="button" class="ghost" data-run-funding>Revisar saldo</button>'+
      '<div data-run-funding-review hidden></div><details><summary>Gas y ejecución</summary><div class="row">'+
      '<label class="field">Gas por orden<input name="dex_run_gas" type="number" min="21000" max="2000000" step="1" value="400000" required></label>'+
      '<label class="field">Precio · gwei<input name="dex_run_price" type="text" inputmode="decimal" value="1" required></label>'+
      '<label class="field">Gas máximo · BNB<input name="dex_run_maximum" type="text" inputmode="decimal" value="0.001" required></label>'+
      '<label class="field">Deslizamiento · %<input name="dex_run_slippage" type="text" inputmode="decimal" value="0.25" required></label></div></details>'+
      '<label class="field" data-run-dca-window hidden data-help="Tiempo desde el cierre diario para ejecutar BASE y caídas. No compra días pasados. Se conserva al rearmar o renovar.">Ventana DCA · s<input name="dex_run_window" type="number" min="60" max="86400" step="1" value="900"></label>'+
      '<div class="row"><label class="dex-run-permission"><input name="dex_run_permission" type="checkbox" required> Órdenes automáticas</label><label class="dex-run-permission" data-help="Continúa con la vista protegida hasta su caducidad original. Bloquear la bóveda o detener la operativa siempre lo para."><input name="dex_run_background" type="checkbox"> Segundo plano</label><button type="submit" data-run-start>Activar</button></div></form>'+
      '<button type="button" class="ghost" data-run-cancel hidden>Detener activación</button><p data-run-status class="state muted" role="status"></p><div data-run-resolution class="real-panel" hidden></div><div data-run-history></div><section class="real-bot-chart-panel" data-dex-chart hidden></section></details>';
    resolver=window.OrbisReceiptResolution.create({callKind:'OPERATOR'});
    resolver.mount(root.querySelector('[data-run-resolution]'),{request:api.request,snapshot:api.snapshot,
      onBusyChange:function(){api.onBusyChange();paint();},blocked:function(){return busy||api.blocked();},
      onResolved:async function(){await refresh();window.OrbisDexBotChart.refresh();}});
    root.querySelector('form').addEventListener('submit',activate);root.querySelector('form').addEventListener('input',paint);root.querySelector('form').addEventListener('change',paint);
    root.querySelector('[data-run-refresh]').addEventListener('click',function(){researchFor='';refresh();});root.querySelector('[data-run-cancel]').addEventListener('click',function(){if(pending)stopRun(pending.id);});
    root.querySelector('[data-run-funding]').addEventListener('click',reviewFunding);
    ['gas','price','maximum','slippage'].forEach(function(name){field(name).addEventListener('input',clearFundingReview);});
    field('binding').addEventListener('change',function(){clearFundingReview();researchFor='';loadResearch();});
    root.querySelector(':scope > details').addEventListener('toggle',function(){if(this.open)refresh();else if(timer){clearTimeout(timer);timer=null;}});
    window.OrbisDexBotChart.mount(root.querySelector('[data-dex-chart]'),{
      getAccountId:function(){var s=snap();return s&&s.accountId;},
      request:function(url,options,passive){return request(url,options,undefined,passive);},
      amountsHidden:function(){var s=snap();return !s||s.hidden;},
      exact:function(value){return snap().hidden?'••••':String(value);}
    });
    clear();identity=key(snap());paint();
  }
  window.OrbisDexRuns={mount:mount,update:update,clear:clear,refresh:refresh,isBusy:function(){return busy||resolutionBusy();},forBinding:function(id){return runs.find(function(item){return item.binding_id===id;})||null;}};
})();
