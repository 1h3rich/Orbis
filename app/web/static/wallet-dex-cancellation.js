/* Explicit same-nonce zero-self cancellation. No keys, automatic resend or wallet send. */
(function(){
  'use strict';
  function create(){
    var root=null,api=null,identity='',epoch=0,busy=false,selected=null,review=null,records=[],sources=[],hashes=new Map(),message='',pendingId=null;
    function snap(){return api&&api.snapshot();}
    function key(s){return s&&s.accountId?[s.accountId,s.generation,s.wallet,s.chain,s.verified].join(':'):'';}
    function ready(s){return s&&s.accountId&&s.wallet&&s.provider&&s.verified&&s.chain==='0x38';}
    function current(host,e,s){return root===host&&host.isConnected&&epoch===e&&key(snap())===key(s)&&snap().provider===s.provider;}
    function node(parent,tag,text,cls){var el=document.createElement(tag);el.textContent=text;if(cls)el.className=cls;parent.append(el);return el;}
    function field(name){return root.querySelector('[name="dex_cancel_'+name+'"]');}
    function draft(){return window.OrbisDexOperatorDraft&&window.OrbisDexOperatorDraft.verifiedDraft();}
    function decimal(value,places){
      var match=/^(0|[1-9][0-9]*)(?:\.([0-9]+))?$/.exec(String(value).trim().replace(',','.'));
      if(!match||(match[2]||'').length>places)throw Error('Importe decimal no válido.');
      return BigInt(match[1])*10n**BigInt(places)+BigInt((match[2]||'').padEnd(places,'0'));
    }
    function units(value,places){var text=String(value).padStart(places+1,'0'),fraction=text.slice(-places).replace(/0+$/,'');return text.slice(0,-places)+(fraction?'.'+fraction:'');}
    function money(value){return snap().hidden?'•••• BNB':units(value,18).replace('.',',')+' BNB';}
    function path(s){return '/accounts/'+encodeURIComponent(s.accountId)+'/dex-gas-cancellations';}
    async function post(s,suffix,body){
      var control=new AbortController(),timer=setTimeout(function(){control.abort();},30000);
      try{return await api.request(path(s)+suffix,{method:'POST',body:JSON.stringify(Object.assign({address:s.wallet},body)),signal:control.signal});}
      finally{clearTimeout(timer);}
    }
    function save(record){records=[record].concat(records.filter(function(item){return item.id!==record.id;}));}
    function pending(item){return item&&item.status==='BROADCAST_ATTEMPTED'&&!item.resolution;}
    function label(item){return item.status==='SETTLED'?(item.winner==='CANCELLATION'?(item.winner_id===item.id?'Cancelación confirmada':'Resuelta · Otra cancelación'):item.winner==='ORIGINAL'?'Devolución ejecutada':'Resuelta externamente'):
      ({PREPARED:'Revisión de cancelación',RESERVED:'Cancelación reservada',SIGNED:'Firmada · Sin envío',BROADCAST_ATTEMPTED:'Cancelación pendiente',CANCELLED:'Revisión retirada',ABANDONED_UNSENT:'Sin envío'})[item.status];}
    function paint(){
      if(!root||!root.isConnected)return;
      root.hidden=!selected&&!records.length&&!message;
      var locked=busy||api.blocked()||!ready(snap()),profile=draft();
      root.querySelector('[data-cancel-status]').textContent=message;
      var form=root.querySelector('form');form.hidden=!selected;
      root.querySelector('[data-cancel-source]').textContent=selected?'Cancelar devolución · '+selected.id.slice(0,8):'Cancelaciones de gas';
      form.querySelectorAll('button,input').forEach(function(el){el.disabled=locked;});
      root.querySelector('[data-cancel-prepare]').disabled=locked||!profile||!pending(selected);
      var output=root.querySelector('[data-cancel-preview]');output.replaceChildren();
      if(review){
        node(output,'strong','Coste · '+money(review.gas_wei));
        var detail=node(output,'p','Límite · '+money(review.max_gas_wei)+' · Nonce '+review.nonce,'muted');
        detail.setAttribute('data-help','Intento de valor cero a la misma operadora. La devolución original puede confirmarse antes. Sólo se contabiliza el recibo ganador.');
        var send=node(output,'button','Confirmar cancelación','ghost');send.type='button';send.dataset.cancelSend='';
        send.disabled=locked||!profile||review.expired||Date.now()>=Date.parse(review.expires_at);send.addEventListener('click',submit);
      }
      var history=root.querySelector('[data-cancel-history]');history.replaceChildren();
      records.forEach(function(item){
        var card=node(history,'section','','real-panel');card.dataset.gasCancellationId=item.id;
        node(card,'strong',label(item));node(card,'p','Devolución · '+item.return_id.slice(0,8)+' · '+item.id.slice(0,8),'muted');
        node(card,'small',(item.winner_id&&item.winner_id!==item.id?'Ganador · '+item.winner_id.slice(0,8):item.receipt?'Gas ganador · '+money(item.receipt.gas_fee_wei):'Coste previsto · '+money(item.gas_wei)),'muted');
        function action(text,fn,disabled){var button=node(card,'button',text,'ghost');button.type='button';button.disabled=locked||disabled;button.addEventListener('click',fn);}
        if(item.status==='PREPARED'){
          action('Recuperar cancelación',function(){var parent=sources.find(function(row){return row.id===item.return_id;});if(!pending(parent))return;select(parent);
            field('price').value=units(item.gas_price,9);field('bump').value=String(item.bump_percent);field('maximum').value=units(item.max_gas_wei,18);
            review=item;paint();},item.expired||!sources.some(function(row){return row.id===item.return_id&&pending(row);}));
          action('Retirar revisión',function(){closeReview(item,false);},false);
        }
        if(['RESERVED','SIGNED'].includes(item.status))action('Descartar cancelación sin envío',function(){closeReview(item,true);},false);
        if(item.status==='BROADCAST_ATTEMPTED'){
          var input=document.createElement('input');input.type='text';input.autocomplete='off';input.spellcheck=false;input.maxLength=66;input.placeholder='0x…';input.value=hashes.get(item.id)||'';
          input.setAttribute('aria-label','Hash a comprobar '+item.id.slice(0,8));input.disabled=locked;
          input.addEventListener('input',function(){hashes.set(item.id,input.value.trim());});card.append(input);
          action('Comprobar cancelación',function(){observe(item);},false);
          var parent=sources.find(function(row){return row.id===item.return_id;});
          action('Revisar otra cancelación',function(){select(parent);},!profile||!pending(parent));
        }
      });
    }
    function notify(){api.onBusyChange();paint();}
    async function run(task){
      var s=snap();if(busy||api.blocked()||!ready(s))return;
      var host=root,e=epoch,changed=false;busy=true;notify();
      try{changed=!!await task(s,function(){if(!current(host,e,s))throw Error('La wallet o sesión cambió; actualiza las devoluciones.');});}
      catch(error){if(current(host,e,s))message=error.message&&(error.status||error.name==='Error')?error.message:'Sin confirmar · Actualiza devoluciones.';}
      finally{if(current(host,e,s)){busy=false;notify();}}
      if(changed&&current(host,e,s))await api.onChanged();
    }
    function select(item){
      if(busy||api.blocked()||!pending(item))return;
      selected=item;review=null;var base=BigInt(item.gas_wei)/21000n;
      records.filter(function(row){return row.return_id===item.id&&row.post_attempted;}).forEach(function(row){var price=BigInt(row.gas_price);if(price>base)base=price;});
      field('price').value=units((base*110n+99n)/100n,9);field('bump').value='10';field('maximum').value='0.0001';
      message='Revisa el coste de cancelar.';paint();
    }
    function prepare(event){event.preventDefault();return run(async function(s,check){
      var profile=draft(),parent=selected;if(!profile||!pending(parent))throw Error('Restaura la copia y actualiza las devoluciones.');
      review=null;var price=decimal(field('price').value,9),maximum=decimal(field('maximum').value,18),bump=Number(field('bump').value);
      if(price<=0n||price>1000000000000n||maximum<=0n||maximum>100000000000000000n||price*21000n>maximum||!Number.isSafeInteger(bump)||bump<1||bump>1000)throw Error('Revisa el precio, aumento y coste máximo.');
      message='Revisando cancelación…';paint();
      var result=await post(s,'',{request_id:api.uuid(),return_id:parent.id,draft_id:profile.id,gas_price:price.toString(),bump_percent:bump,
        max_gas_bnb:field('maximum').value.trim().replace(',','.')});check();save(result);review=result;pendingId=result.id;message='Cancelación revisada · Confirma el coste.';
    });}
    function submit(){return run(async function(s,check){
      var profile=draft(),item=review;if(!profile||!item)return;
      review=null;message='Enviando cancelación…';paint();var body={draft_id:profile.id,plan_digest:item.plan_digest,confirmation:'CANCELAR_DEVOLUCION_BNB'};
      var original=api.originalHash(item.return_id);if(/^0x[0-9a-fA-F]{64}$/.test(original||''))body.original_hash=original;
      var result=await post(s,'/'+encodeURIComponent(item.id)+'/submit',body);check();save(result);if(result.tx_hash)hashes.set(result.id,result.tx_hash);
      message=label(result)+' · Actualiza las devoluciones.';return true;
    });}
    function observe(item){return run(async function(s,check){
      var tx=hashes.get(item.id)||api.originalHash(item.return_id)||'';
      if(!/^0x[0-9a-fA-F]{64}$/.test(tx))throw Error('Indica el hash de cancelación o de la devolución original.');
      var result=await post(s,'/'+encodeURIComponent(item.id)+'/observe',{tx_hash:tx});check();save(result);message=label(result);return result.status==='SETTLED';
    });}
    function closeReview(item,abandon){return run(async function(s,check){
      var result=await post(s,'/'+encodeURIComponent(item.id)+(abandon?'/abandon':'/cancel'),{confirmation:abandon?'DESCARTAR_CANCELACION_BNB_NO_EMITIDA':'RETIRAR_REVISION_CANCELACION_BNB'});
      check();save(result);if(review&&review.id===item.id)review=null;message='Sin envío · Registro actualizado.';
    });}
    function reconcile(rows,parents){
      records=rows;sources=parents;
      if(selected){selected=sources.find(function(item){return item.id===selected.id;});if(!pending(selected)){selected=null;review=null;}}
      if(review){var actual=records.find(function(item){return item.id===review.id;});review=actual&&actual.status==='PREPARED'?actual:null;}
      var settled=records.find(function(item){return item.id===pendingId&&item.status==='SETTLED';});
      if(settled){message=label(settled);review=null;pendingId=null;}
      paint();
    }
    function clear(){++epoch;identity='';busy=false;selected=null;review=null;records=[];sources=[];hashes.clear();message='';pendingId=null;if(root&&root.isConnected)root.querySelector('form').reset();paint();}
    function update(){if(key(snap())!==identity){clear();identity=key(snap());}paint();}
    function mount(element,client){
      root=element;api=client;
      root.innerHTML='<strong data-cancel-source></strong><form class="row"><label class="field">Precio · gwei<input name="dex_cancel_price" type="text" inputmode="decimal" required></label>'+
        '<label class="field" data-help="Aumento mínimo sobre el original y todas las cancelaciones ya concedidas. La aceptación depende del nodo.">Aumento mín. · %<input name="dex_cancel_bump" type="number" min="1" max="1000" value="10" required></label>'+
        '<label class="field">Coste máx. · BNB<input name="dex_cancel_maximum" type="text" inputmode="decimal" value="0.0001" required></label>'+
        '<button type="submit" class="ghost" data-cancel-prepare>Revisar cancelación</button></form>'+
        '<p data-cancel-status class="state muted" role="status"></p><div data-cancel-preview></div><div data-cancel-history></div>';
      root.querySelector('form').addEventListener('submit',prepare);root.querySelector('form').addEventListener('input',function(){review=null;paint();});
      clear();identity=key(snap());paint();
    }
    return {mount:mount,update:update,clear:clear,reconcile:reconcile,select:select,isBusy:function(){return busy;}};
  }
  window.OrbisDexGasCancellation={create:create};
})();
