/* Local receipt accounting. Wallet proof is read-only; no signing or sends. */
(function(){
  'use strict';
  var root=null,api=null,epoch=0,identity='',busy=false,items=[],page=null,chosen=null,proposal=null,trade=null,gas=[],message='';
  var proofs=Object.create(null);
  var orderProofSupported=false,capabilityEpoch=0;
  function snap(){return api&&api.snapshot();}
  function key(s){return s&&s.accountId?[s.accountId,s.wallet,s.chain,s.generation,s.verified].join(':'):'';}
  function ready(s){return s&&s.accountId&&s.wallet&&s.chain==='0x38'&&s.verified;}
  function current(host,e,k){return root===host&&host.isConnected&&epoch===e&&key(snap())===k;}
  function node(parent,tag,text,cls){var el=document.createElement(tag);el.textContent=text;if(cls)el.className=cls;parent.append(el);return el;}
  function exact(value){return snap().hidden?'••••':String(value);}
  function base(s){return '/accounts/'+encodeURIComponent(s.accountId)+'/dex-receipts';}
  function path(s,item){return base(s)+'/'+encodeURIComponent(item.source_kind)+'/'+encodeURIComponent(item.source_id);}
  function proofKey(item){return item.source_kind+'/'+item.source_id;}
  async function request(url,body,passive){
    var controller=new AbortController(),timer=setTimeout(function(){controller.abort();},15000);
    try{return await api.request(url,{method:'POST',body:JSON.stringify(body),signal:controller.signal},!!passive);}
    finally{clearTimeout(timer);}
  }
  function paint(){
    if(!root||!root.isConnected)return;var s=snap(),locked=busy||!ready(s);
    root.hidden=!s||!s.accountId;root.querySelector('[data-book-status]').textContent=message;
    root.querySelector('[data-book-refresh]').disabled=locked;
    root.querySelector('[data-book-prev]').disabled=locked||!page||page.offset===0;
    root.querySelector('[data-book-next]').disabled=locked||!page||page.next_offset===null;
    root.querySelector('[data-book-count]').textContent=page?(page.total?String(page.offset+1)+'–'+String(page.offset+page.items.length)+' / '+page.total:'Sin recibos'):'';
    var history=root.querySelector('[data-book-history]');history.replaceChildren();
    var labels={DEPLOYMENT:'Despliegue',CONTROL:'Permiso',GAS_FUNDING:'Gas · Financiar',GAS_RETURN:'Gas · Devolver',BUY:'Compra',SELL:'Venta',STOP:'STOP',APPROVE:'Aprobación',REVOKE:'Revocación',CONFIGURE:'Política',DEPLOYMENT_RESOLUTION:'Resolución',NATIVE_RESOLUTION:'Resolución BNB',CALL_RESOLUTION:'Resultado DEX',CANCEL:'Cancelación',CANCELLED:'Cancelación',REVERTED:'Reversión',REPLACED_CREATE:'Despliegue recuperado',REVERTED_CREATE:'Despliegue revertido'};
    items.forEach(function(item){
      var card=node(history,'section','','real-panel');card.dataset.bookSource=item.source_id;card.dataset.bookKind=item.source_kind;
      var gasOnly=['CANCELLED','REVERTED'].includes(item.economic_outcome);
      node(card,'strong',(labels[item.action]||labels[item.source_kind]||item.action)+' · '+(item.booked?'Contabilizado':gasOnly?'Gas pendiente':item.status==='REVERTED'?'Revertida · Gas pendiente':'Pendiente'));
      node(card,'p',new Date(item.occurred_at).toLocaleString()+' · '+item.source_id.slice(0,8),'muted');
      node(card,'p','Gas · '+exact(item.gas_amount)+' BNB','muted');
      if(['GAS_FUNDING','GAS_RETURN','NATIVE_RESOLUTION'].includes(item.source_kind))node(card,'p','Traslado interno · '+exact(item.internal_principal_bnb)+' BNB','muted');
      if(item.booked)node(card,'small',item.target_account_name+' · '+item.events_count+' asientos','muted');
      var controls=null;
      if(!item.booked){controls=node(card,'div','','row');var review=node(controls,'button','Revisar','ghost');review.type='button';review.disabled=locked;review.addEventListener('click',function(){chosen=item;trade=null;gas=[];reviewSelection();});}
      if(orderProofSupported&&['BUY','SELL'].includes(item.action)&&['OPERATOR','CALL_RESOLUTION','WALLET_SWAP'].includes(item.source_kind)){
        if(!controls)controls=node(card,'div','','row');
        var proof=proofs[proofKey(item)];
        if(proof){var badge=node(controls,'span','Bloque ✓','badge');badge.dataset.bookOrderSaved=item.source_id;
          badge.title=window.OrbisDexReceiptOrder.title(proof,s.hidden);
        }else{var check=node(controls,'button','Comprobar bloque','ghost');check.type='button';check.disabled=locked;
          check.dataset.bookOrder=item.source_id;check.title='Verifica el orden de ejecución en el bloque';
          check.addEventListener('click',function(){checkOrder(item);});}
      }
    });
    var panel=root.querySelector('[data-book-preview]');panel.replaceChildren();panel.hidden=!proposal;
    if(!proposal)return;
    node(panel,'strong',proposal.target_account_name);
    if(proposal.trade)node(panel,'p',(proposal.trade.kind==='BUY'?'Compra':'Venta')+' · '+exact(proposal.trade.quantity)+' WBNB · '+exact(proposal.trade.gross_amount)+' USDT');
    node(panel,'p','Gas · '+exact(proposal.gas_amount)+' BNB','muted');
    if(chosen&&['GAS_FUNDING','GAS_RETURN','NATIVE_RESOLUTION'].includes(chosen.source_kind))node(panel,'p','Principal · Traslado interno','muted');
    if(proposal.trade_candidates.length){
      var label=node(panel,'label','Operación existente','field'),select=document.createElement('select');select.setAttribute('aria-label','Operación existente');
      select.add(new Option('Seleccionar…',''));proposal.trade_candidates.forEach(function(item){select.add(new Option(item.kind+' · '+exact(item.quantity)+' WBNB · '+item.id.slice(0,8),item.id));});
      select.value=trade||'';select.disabled=locked;select.addEventListener('change',function(){trade=select.value||null;proposal=null;reviewSelection();});label.append(select);
    }
    if(proposal.gas_candidates.length){
      var group=node(panel,'fieldset',''),legend=node(group,'legend','Comisiones existentes');
      proposal.gas_candidates.forEach(function(item){var label=node(group,'label','','dex-run-permission'),box=document.createElement('input');box.type='checkbox';box.checked=gas.includes(item.id);box.disabled=locked;
        box.addEventListener('change',function(){gas=box.checked?gas.concat([item.id]):gas.filter(function(id){return id!==item.id;});proposal=null;reviewSelection();});label.append(box,document.createTextNode(exact(item.native_amount)+' BNB · '+item.id.slice(0,8)));});
    }
    if(!proposal.available)node(panel,'p',proposal.reason,'muted');
    var confirm=node(panel,'button','Contabilizar');confirm.type='button';confirm.disabled=locked||!proposal.available;
    confirm.addEventListener('click',confirmBooking);
    var cancel=node(panel,'button','Cerrar revisión','ghost');cancel.type='button';cancel.disabled=busy;cancel.addEventListener('click',function(){proposal=null;chosen=null;paint();});
  }
  async function refresh(offset){
    var s=snap();if(busy||!ready(s))return;
    var host=root,e=epoch,k=key(s),body={address:s.wallet,limit:100,offset:offset||0};if(body.offset&&page)body.revision=page.revision;
    busy=true;paint();try{var result=await request(base(s)+'/query',body,true);if(!current(host,e,k))return;
      page=result;items=result.items;message='';proposal=null;chosen=null;
    }catch(error){if(current(host,e,k))message=error.message||'Lista sin confirmar · Actualiza.';}
    finally{if(current(host,e,k)){busy=false;paint();}}
  }
  async function reviewSelection(){
    var s=snap();if(busy||!ready(s)||!chosen)return;
    var host=root,e=epoch,k=key(s),item=chosen;busy=true;paint();
    try{var result=await request(path(s,item)+'/booking-preview',{address:s.wallet,trade_event_id:trade,gas_event_ids:gas});if(!current(host,e,k))return;
      if(result.booked){proposal=null;message='Ya contabilizado · Actualiza la lista.';}else{proposal=result;message='Revisión local · '+result.target_account_name;}
    }catch(error){if(current(host,e,k)){proposal=null;message=error.message||'No se pudo revisar el recibo.';}}
    finally{if(current(host,e,k)){busy=false;paint();}}
  }
  async function confirmBooking(){
    var s=snap();if(busy||!ready(s)||!chosen||!proposal||!proposal.available)return;
    var host=root,e=epoch,k=key(s),item=chosen,review=proposal;
    busy=true;proposal=null;paint();
    try{var result=await request(path(s,item)+'/book',{address:s.wallet,trade_event_id:trade,gas_event_ids:gas,
      expected_revision:review.revision,preview_digest:review.preview_digest,confirmation:'CONTABILIZAR_RECIBO_DEX'});
      if(!current(host,e,k))return;
      items=items.map(function(value){return value.source_id===item.source_id&&value.source_kind===item.source_kind?Object.assign({},value,result):value;});
      message='Contabilizado · '+result.target_account_name;chosen=null;
      if(window.OrbisDexBotChart)window.OrbisDexBotChart.refresh();
      if(typeof api.onBooked==='function')api.onBooked();
    }catch(error){if(current(host,e,k))message=error.status&&error.message?error.message:'Registro sin confirmar · Actualiza la lista.';}
    finally{if(current(host,e,k)){busy=false;paint();}}
  }
  async function checkOrder(item){
    var s=snap();if(busy||!ready(s)||!orderProofSupported)return;
    var host=root,e=epoch,k=key(s);busy=true;proposal=null;chosen=null;message='Comprobando bloque…';paint();
    try{var proof=await window.OrbisDexReceiptOrder.check(api,s,item.source_kind,item.source_id);if(!current(host,e,k))return;
      proofs[proofKey(item)]=proof;message='Bloque verificado';
    }catch(error){if(current(host,e,k)){delete proofs[proofKey(item)];message=error.status&&error.message?error.message:'Bloque sin confirmar · Reintenta.';}}
    finally{if(current(host,e,k)){busy=false;paint();}}
  }
  function clear(){++epoch;identity='';busy=false;items=[];proofs=Object.create(null);page=chosen=proposal=null;trade=null;gas=[];message='Verifica tu wallet para consultar recibos.';paint();}
  function update(){var next=key(snap());if(next!==identity){clear();identity=next;}paint();}
  function mount(element,client){root=element;api=client;
    orderProofSupported=false;var mounted=++capabilityEpoch;
    root.innerHTML='<details><summary data-help="Registra recibos propios en la cartera vinculada. Reutiliza asientos existentes y descuenta gas BNB; no mueve fondos ni crea aportaciones. La cartera registrada incluye tu reserva de gas en la operadora.">Contabilidad DEX</summary>'+
      '<button type="button" class="ghost" data-book-refresh>Actualizar recibos</button><p class="state muted" data-book-status role="status"></p><div data-book-preview class="real-panel" hidden></div><div data-book-history></div>'+
      '<div class="row"><button type="button" class="ghost" data-book-prev>Anterior</button><span data-book-count class="muted"></span><button type="button" class="ghost" data-book-next>Siguiente</button></div></details>';
    root.querySelector('summary').addEventListener('click',function(){if(!root.querySelector('details').open)refresh(0);});
    root.querySelector('[data-book-refresh]').addEventListener('click',function(){refresh(0);});
    root.querySelector('[data-book-prev]').addEventListener('click',function(){if(page)refresh(Math.max(0,page.offset-100));});
    root.querySelector('[data-book-next]').addEventListener('click',function(){if(page&&page.next_offset!==null)refresh(page.next_offset);});clear();identity=key(snap());paint();
    if(window.OrbisDexReceiptOrder)window.OrbisDexReceiptOrder.supported(client).then(function(supported){
      if(mounted!==capabilityEpoch||root!==element||!element.isConnected)return;
      orderProofSupported=supported;paint();
    }).catch(function(){});
  }
  window.OrbisDexReceiptBooking={mount:mount,update:update,clear:clear};
})();
