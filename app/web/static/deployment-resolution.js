/* Read and reconcile an already finalized replacement. Never wallet spending. */
(function(){
  'use strict';
  function create(options){
  options=options||{};
  var root=null,api=null,identity='',epoch=0,busy=false,review=null,message='',pendingId=null;
  function snap(){return api&&api.snapshot();}
  function key(s){return s&&s.accountId?[s.accountId,s.wallet,s.chain,s.generation,s.verified].join(':'):'';}
  function valid(s){return s&&s.wallet&&s.accountId&&s.verified&&s.chain==='0x38';}
  function current(host,e,k){return root===host&&host.isConnected&&epoch===e&&key(snap())===k;}
  function path(s,id){return '/accounts/'+encodeURIComponent(s.accountId)+(options.callKind?'/dex-call-resolutions/'+options.callKind:options.nativeKind?'/dex-native-resolutions/'+options.nativeKind:'/dex-deployments')+'/'+encodeURIComponent(id);}
  function reminder(){return options.nativeKind==='GAS_FUNDING'?'Actualiza el gas.':options.nativeKind==='GAS_RETURN'?'Actualiza las devoluciones.':'Actualiza el ejecutor.';}
  function body(s,id,hash,accept){var value={address:s.wallet,tx_hash:hash};value[options.nativeKind||options.callKind?'accept_changes':'accept_fee_overrun']=!!accept;return value;}
  function confirmed(value){return options.callKind?({EXECUTED:'Operación recuperada',CANCELLED:'Cancelación confirmada',REVERTED:'Reversión confirmada'})[value.outcome]:'Resolución confirmada · Gas disponible para contabilizar.';}
  function node(parent,tag,text,cls){var el=document.createElement(tag);el.textContent=text;if(cls)el.className=cls;parent.append(el);return el;}
  function money(value){if(snap().hidden)return '•••• BNB';var text=String(value).padStart(19,'0'),fraction=text.slice(-18).replace(/0+$/,'');return text.slice(0,-18)+(fraction?','+fraction:'')+' BNB';}
  function gasPrice(value){if(snap().hidden)return '•••• gwei';var text=String(value).padStart(10,'0'),fraction=text.slice(-9).replace(/0+$/,'');return text.slice(0,-9)+(fraction?','+fraction:'')+' gwei';}
  async function request(url,body,timeout){var control=new AbortController(),timer=setTimeout(function(){control.abort();},timeout||15000);
    try{return await api.request(url,{method:'POST',body:JSON.stringify(body),signal:control.signal});}finally{clearTimeout(timer);}}
  async function stored(s,id){var control=new AbortController(),timer=setTimeout(function(){control.abort();},15000);
    try{return await api.request(path(s,id)+'/resolution',{method:'GET',headers:{'X-Orbis-Wallet':s.wallet},signal:control.signal},true);}finally{clearTimeout(timer);}}
  function notify(){if(api)api.onBusyChange();paint();}
  function paint(){
    if(!root||!root.isConnected)return;root.replaceChildren();root.hidden=!review&&!message;
    if(message)node(root,'p',message,'state muted');if(!review){
      if(options.callKind&&pendingId){var recover=node(root,'button','Consultar resultado','ghost');recover.type='button';recover.disabled=busy||api.blocked()||!valid(snap());recover.addEventListener('click',recoverStatus);}
      return;}
    var s=snap(),locked=busy||api.blocked()||!valid(s),record=review.value;
    node(root,'strong','Resolver'+(review.info&&review.info.label?' · '+review.info.label:'')+' · '+review.id.slice(0,8));
    node(root,'p',({EXECUTED:'Operación confirmada',CANCELLED:'Cancelación confirmada',DEPLOYED:'Ejecutor recuperado',REVERTED:options.callKind?'Llamada revertida':options.nativeKind?'Transferencia revertida':'Despliegue revertido',TRANSFERRED:'Transferencia confirmada'})[record.outcome]);
    if(options.callKind&&record.outcome==='EXECUTED'&&record.effect&&review.info&&review.info.asset&&record.effect.amount_out_units){
      node(root,'p','Recibido · '+money(record.effect.amount_out_units).replace(/ BNB$/, ' '+review.info.asset));}
    node(root,'p','Gas real · '+money(record.receipt.gas_fee_wei),'muted');
    node(root,'p','Máximo original · '+money(record.reviewed_max_gas_wei),'muted');
    if(options.nativeKind){node(root,'p','Transferido · '+money(record.receipt.transferred_wei),'muted');node(root,'p','Importe original · '+money(record.reviewed_amount_wei),'muted');}
    if(record.fee_overrun||record.amount_changed||options.callKind&&(record.fee_changed||['OTHER_CALL','CREATE'].includes(record.call_kind))){node(root,'p','Precio firmado · '+gasPrice(record.receipt.fee_cap)+' · Máximo original '+gasPrice(record.reviewed_gas_price_wei),'muted');var label=node(root,'label','','dex-run-permission'),box=document.createElement('input');box.type='checkbox';box.checked=review.accept;box.disabled=locked;
      label.append(box,document.createTextNode(options.callKind?'Aceptar cambios de llamada y gas':options.nativeKind?'Aceptar cambios de importe y gas':'Aceptar gas superior al revisado'));
      box.addEventListener('change',function(){begin(review.id,review.hash,box.checked,review.info);});}
    var button=node(root,'button','Confirmar resolución');button.type='button';button.disabled=locked||!record.available;
    button.addEventListener('click',confirm);
    var cancel=node(root,'button','Cerrar revisión','ghost');cancel.type='button';cancel.disabled=busy;cancel.addEventListener('click',function(){review=null;message='';paint();});
  }
  async function begin(id,hash,accept,info){
    var s=snap();if(busy||api.blocked()||!valid(s))return;
    var discover=!hash;
    if(!discover&&!/^0x[0-9a-fA-F]{64}$/.test(hash||'')){message='Indica el hash de la transacción de reemplazo.';review=null;paint();return;}
    var host=root,e=epoch,k=key(s),done=false;pendingId=id;busy=true;review=null;message=discover?'Buscando en BNB…':'Verificando reemplazo…';notify();
    try{var value=discover?await request(path(s,id)+'/discover',body(s,id,undefined,accept),65000):
        await request(path(s,id)+'/resolution-preview',body(s,id,hash,accept));
      if(!current(host,e,k))return;
      if(value.resolved){message=options.callKind?confirmed(value):'Solicitud ya resuelta · '+reminder();done=!!options.callKind;}
      else if(discover&&options.callKind&&value.external_review_required===true){
        var external=window.OrbisExternalReview;
        if(external&&external.available()){message='';external.openSource(options.callKind,id,value.discovered_tx_hash||'');}
        else message='Recibo externo pendiente de revisión.';
      }
      else{var receiptHash=discover?value.discovered_tx_hash:hash;
        if(!/^0x[0-9a-fA-F]{64}$/.test(receiptHash||''))throw Error('Recibo no encontrado');
        review={id:id,hash:receiptHash,accept:!!accept,info:info,value:value};message='';}
    }catch(error){if(current(host,e,k))message=error.status&&error.message?error.message:discover?'Búsqueda sin confirmar · Puedes introducir el hash.':'Lectura sin confirmar · Revisa el hash.';}
    finally{if(current(host,e,k)){busy=false;notify();}}
    if(done&&current(host,e,k))await api.onResolved();
  }
  async function confirm(){
    var s=snap();if(busy||api.blocked()||!valid(s)||!review||!review.value.available)return;
    var host=root,e=epoch,k=key(s),submitted=review;busy=true;review=null;message='Confirmando resolución…';notify();var done=false;
    try{var value=await request(path(s,submitted.id)+'/resolve',Object.assign(body(s,submitted.id,submitted.hash,submitted.accept),{
        preview_digest:submitted.value.preview_digest,source_state_digest:submitted.value.source_state_digest,confirmation:options.callKind?'RESOLVER_LLAMADA_DEX':options.nativeKind?'RESOLVER_REEMPLAZO_BNB':'RESOLVER_REEMPLAZO_DEX'}));
      if(!current(host,e,k))return;done=!!value.resolved;message=confirmed(value);
    }catch(error){if(current(host,e,k)){
      message=error.status&&error.message?error.message:'Resolución sin confirmar · '+reminder();
      if(options.callKind){try{var value=await stored(s,submitted.id);if(current(host,e,k)&&value.resolved){done=true;message=confirmed(value);}}catch(_){} }
    }}
    finally{if(current(host,e,k)){busy=false;notify();}}
    if(done&&current(host,e,k))await api.onResolved();
  }
  async function recoverStatus(){
    var s=snap();if(busy||api.blocked()||!valid(s)||!pendingId)return;
    var host=root,e=epoch,k=key(s),id=pendingId,done=false;busy=true;notify();
    try{var value=await stored(s,id);if(!current(host,e,k))return;done=!!value.resolved;message=done?confirmed(value):'Sin resolución confirmada · Revisa el recibo.';}
    catch(error){if(current(host,e,k))message=error.status&&error.message?error.message:'Consulta sin confirmar.';}
    finally{if(current(host,e,k)){busy=false;notify();}}
    if(done&&current(host,e,k))await api.onResolved();
  }
  function clear(){++epoch;identity='';busy=false;review=null;message='';pendingId=null;paint();}
  function update(){var next=key(snap());if(next!==identity){clear();identity=next;}paint();}
  function mount(element,client){root=element;if(root)root.classList.add('receipt-resolution');api=client;clear();identity=key(snap());paint();}
  function reconcile(records){var item=pendingId&&records.find(function(item){return item.id===pendingId&&item.resolution;});if(item){review=null;pendingId=null;message=confirmed(item.resolution);paint();}}
  return {mount:mount,update:update,clear:clear,reconcile:reconcile,isBusy:function(){return busy;},review:begin};
  }
  window.OrbisReceiptResolution={create:create};
  window.OrbisDeploymentResolution=create();
})();
