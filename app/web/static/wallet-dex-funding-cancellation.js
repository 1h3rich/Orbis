/* Owner wallet replacement: durable grant before one zero-self request. */
(function(){
  'use strict';
  // Survives component clear/remount. A pending provider promise is not a
  // rejected transaction and must never release another automatic prompt.
  var walletPending=false,pendingChanged=function(){};
  function create(){
    var root=null,api=null,identity='',provider=null,epoch=0,busy=false,selected=null,review=null,records=[],sources=[],hashes=new Map(),pendingId=null,message='';
    function snap(){return api&&api.snapshot();}
    function key(s){return s&&s.accountId?[s.accountId,s.generation,s.wallet,s.chain,s.verified].join(':'):'';}
    function ready(s){return s&&s.accountId&&s.wallet&&s.provider&&s.verified&&s.chain==='0x38';}
    function current(host,e,s){return root===host&&host.isConnected&&epoch===e&&key(snap())===key(s)&&snap().provider===s.provider;}
    function field(name){return root.querySelector('[name="dex_wallet_cancel_'+name+'"]');}
    function path(s){return '/accounts/'+encodeURIComponent(s.accountId)+'/dex-wallet-cancellations';}
    function hash(value){return /^0x[0-9a-fA-F]{64}$/.test(value||'');}
    function address(value){if(!/^0x[0-9a-fA-F]{40}$/.test(value||''))throw Error('Indica la operadora original.');return value.toLowerCase();}
    function quantity(value){if(typeof value!=='string'||!/^0x(?:0|[1-9a-f][0-9a-f]*)$/i.test(value))throw Error('Respuesta de wallet no válida.');return BigInt(value);}
    function decimal(value,places){var match=/^(0|[1-9][0-9]*)(?:\.([0-9]+))?$/.exec(String(value).trim().replace(',','.'));
      if(!match||(match[2]||'').length>places)throw Error('Importe decimal no válido.');
      return BigInt(match[1])*10n**BigInt(places)+BigInt((match[2]||'').padEnd(places,'0'));}
    function units(value,places){var text=String(value).padStart(places+1,'0'),fraction=text.slice(-places).replace(/0+$/,'');return text.slice(0,-places)+(fraction?'.'+fraction:'');}
    function money(value){return snap().hidden?'•••• BNB':units(value,18).replace('.',',')+' BNB';}
    function node(parent,tag,text,cls){var el=document.createElement(tag);el.textContent=text;if(cls)el.className=cls;parent.append(el);return el;}
    function eligible(item){return item&&!item.resolution&&['WALLET_REQUESTED','HASH_REPORTED'].includes(item.status);}
    function title(item){return item.status==='SETTLED'?(item.winner==='CANCELLATION'?(item.winner_id===item.id?'Cancelación confirmada':'Resuelta · Otra cancelación'):item.winner==='ORIGINAL'?'Financiación confirmada':'Resuelta externamente'):
      ({PREPARED:'Revisión de cancelación',WALLET_REQUESTED:'Wallet · Resultado pendiente',HASH_REPORTED:'Cancelación · Recibo pendiente',WALLET_REJECTED:'Rechazada en wallet',CANCELLED:'Revisión retirada'})[item.status];}
    function save(row){records=[row].concat(records.filter(function(item){return item.id!==row.id;}));}
    function notify(){api.onBusyChange();paint();}
    async function post(s,suffix,body){var control=new AbortController(),timer=setTimeout(function(){control.abort();},30000);
      try{return await api.request(path(s)+suffix,{method:'POST',body:JSON.stringify(Object.assign({address:s.wallet},body)),signal:control.signal});}finally{clearTimeout(timer);}}
    async function read(s,method,params){var timer;
      try{return await Promise.race([Promise.resolve().then(function(){return s.provider.request({method:method,params:params||[]});}),
        new Promise(function(_,reject){timer=setTimeout(function(){reject(Error('La lectura de wallet agotó su tiempo.'));},8000);})]);}finally{clearTimeout(timer);}}
    async function wallet(s){var values=await Promise.all([read(s,'eth_accounts'),read(s,'eth_chainId')]);
      if(!Array.isArray(values[0])||!values[0][0]||address(values[0][0])!==address(s.wallet)||quantity(values[1])!==56n)throw Error('Selecciona tu wallet propietaria en BNB Chain.');}
    function paint(){
      if(!root||!root.isConnected)return;root.hidden=!selected&&!records.length&&!message&&!walletPending;
      var locked=busy||walletPending||api.blocked()||!ready(snap());
      root.querySelector('[data-wallet-cancel-source]').textContent=selected?'Cancelar financiación · '+selected.id.slice(0,8):'Cancelaciones · Wallet';
      root.querySelector('[data-wallet-cancel-status]').textContent=message||(walletPending?'Wallet pendiente · Revisa el diálogo.':'');
      var form=root.querySelector('form');form.hidden=!selected;form.querySelectorAll('input,button').forEach(function(el){el.disabled=locked;});
      root.querySelector('[data-wallet-cancel-review]').disabled=locked||!eligible(selected);
      var output=root.querySelector('[data-wallet-cancel-preview]');output.replaceChildren();
      if(review){
        node(output,'strong','Gas solicitado · '+money(review.gas_wei));
        var detail=node(output,'p','Máximo solicitado · '+money(review.max_gas_wei)+' · Nonce '+review.nonce,'muted');
        detail.setAttribute('data-help','La solicitud es de valor cero a tu propia wallet. Revisa nonce y tarifa al firmar: la wallet puede modificarlos. Se comprobará el recibo real; el envío original puede ganar.');
        if(review.original_fee_unknown||review.previous_fees_unknown)node(output,'small','Tarifa original o anterior no verificada','muted');
        var send=node(output,'button','Confirmar cancelación en wallet','ghost');send.type='button';send.dataset.walletCancelSend='';
        send.disabled=locked||review.expired||Date.now()>=Date.parse(review.expires_at)||(review.original_hash_required&&!hash(api.originalHash(review.funding_id)));
        send.addEventListener('click',sendReview);
      }
      var history=root.querySelector('[data-wallet-cancel-history]');history.replaceChildren();
      records.forEach(function(item){
        var card=node(history,'section','','real-panel');card.dataset.walletCancellationId=item.id;
        node(card,'strong',title(item));node(card,'p','Envío · '+item.funding_id.slice(0,8)+' · '+item.id.slice(0,8),'muted');
        node(card,'small',item.winner_id&&item.winner_id!==item.id?'Ganador · '+item.winner_id.slice(0,8):item.receipt?'Gas ganador · '+money(item.receipt.gas_fee_wei):'Gas solicitado · '+money(item.gas_wei),'muted');
        function action(text,fn,disabled){var button=node(card,'button',text,'ghost');button.type='button';button.disabled=locked||disabled;button.addEventListener('click',fn);}
        var parent=sources.find(function(source){return source.id===item.funding_id;});
        if(['PREPARED','WALLET_REQUESTED','HASH_REPORTED'].includes(item.status)&&parent&&(parent.resolution||parent.receipt)){
          action('Vincular resultado',function(){reconcileKnown(item);},false);
        }
        if(item.status==='PREPARED'){
          action('Recuperar cancelación',function(){select(parent,item);},item.expired||!eligible(parent));
          action('Retirar revisión',function(){closeReview(item);},false);
        }
        if(['WALLET_REQUESTED','HASH_REPORTED'].includes(item.status)){
          var input=document.createElement('input');input.type='text';input.autocomplete='off';input.spellcheck=false;input.maxLength=66;input.placeholder='0x…';input.value=hashes.get(item.id)||'';
          input.setAttribute('aria-label','Hash de cancelación '+item.id.slice(0,8));input.disabled=locked;
          input.addEventListener('input',function(){hashes.set(item.id,input.value.trim());});card.append(input);
          action('Comprobar cancelación',function(){observe(item);},false);
          action('Revisar cambios',function(){var tx=hashes.get(item.id)||'';api.reviewReplacement(item.funding_id,tx);},false);
          action('Revisar otra cancelación',function(){select(parent);},!eligible(parent));
        }
      });
    }
    async function run(task){
      var s=snap();if(busy||walletPending||api.blocked()||!ready(s))return;
      var host=root,e=epoch,changed=false;busy=true;notify();
      function check(){if(!current(host,e,s))throw Error('La wallet o sesión cambió; actualiza el gas.');}
      try{changed=!!await task(s,check);}
      catch(error){if(current(host,e,s))message=error.status&&error.message?error.message:error.name==='Error'&&error.message?error.message:'Sin confirmar · Actualiza el gas.';}
      finally{if(current(host,e,s)){busy=false;notify();}}
      if(changed&&current(host,e,s))await api.onChanged();
    }
    function select(item,stored){
      if(!eligible(item))return;
      return run(async function(s,check){
        selected=item;review=null;pendingId=stored?stored.id:null;var recipient=api.originalRecipient(item.id)||'',profile=window.OrbisDexOperatorDraft&&window.OrbisDexOperatorDraft.verifiedDraft();
        if(!recipient&&profile)recipient=profile.operator_address;
        field('recipient').value=recipient;field('unknown').checked=false;field('maximum').value='0.0001';field('bump').value='10';field('price').value='1';
        var base=quantity(await read(s,'eth_gasPrice'));check();var original=api.originalHash(item.id)||'';
        if(hash(original)){
          var tx=await read(s,'eth_getTransactionByHash',[original]);check();
          if(tx){
            if(tx.blockNumber!==null&&tx.blockNumber!==undefined)throw Error('El envío original ya figura minado · Compruébalo.');
            field('recipient').value=address(tx.to);var signed=quantity(tx.maxFeePerGas||tx.gasPrice);if(signed>base)base=signed;
          }
        }
        records.filter(function(row){return row.funding_id===item.id&&row.request_attempted;}).forEach(function(row){var value=BigInt(row.gas_price);if(value>base)base=value;});
        field('price').value=units((base*110n+99n)/100n,9);
        if(stored){field('price').value=units(stored.gas_price,9);field('bump').value=String(stored.bump_percent);field('maximum').value=units(stored.max_gas_wei,18);field('unknown').checked=stored.accept_unknown_fee;review=stored;}
        message=stored?'Revisión recuperada · Comprueba el origen y el coste.':'Revisa la cancelación en tu wallet.';
      });
    }
    function prepare(event){event.preventDefault();return run(async function(s,check){
      var parent=selected;if(!eligible(parent))throw Error('Actualiza el envío original.');
      var recipient=address(field('recipient').value.trim()),price=decimal(field('price').value,9),maximum=decimal(field('maximum').value,18),bump=Number(field('bump').value);
      if(price<=0n||price>1000000000000n||maximum<=0n||maximum>100000000000000000n||price*21000n>maximum||!Number.isSafeInteger(bump)||bump<1||bump>1000)throw Error('Revisa el precio, aumento y máximo solicitado.');
      review=null;await wallet(s);check();var original=api.originalHash(parent.id)||null;if(original&&!hash(original))throw Error('Revisa el hash original del envío.');
      var result=await post(s,'',{request_id:api.uuid(),funding_id:parent.id,recipient:recipient,original_hash:original,gas_price:price.toString(),bump_percent:bump,
        max_gas_bnb:field('maximum').value.trim().replace(',','.'),accept_unknown_fee:field('unknown').checked});check();
      save(result);review=result;pendingId=result.id;message='Cancelación revisada · Confirma en tu wallet.';
    });}
    async function inspect(s,item,tx){
      if(!tx||address(tx.from)!==address(s.wallet)||address(tx.to)!==address(s.wallet)||tx.chainId!=='0x38'||tx.type!=='0x0'||tx.value!=='0x0'||tx.data!=='0x'
          ||quantity(tx.nonce)!==BigInt(item.nonce)||quantity(tx.gas)!==21000n||quantity(tx.gasPrice)!==BigInt(item.gas_price)||quantity(tx.gasPrice)*21000n>BigInt(item.max_gas_wei))throw Error('La cancelación no coincide con la revisión.');
      if(Date.now()>=Date.parse(item.expires_at))throw Error('La revisión de cancelación caducó.');
      await wallet(s);
      var values=await Promise.all([read(s,'eth_getCode',[s.wallet,'latest']),read(s,'eth_getTransactionCount',[s.wallet,'latest']),
        read(s,'eth_getTransactionCount',[s.wallet,'pending']),read(s,'eth_getBalance',[s.wallet,'latest'])]);
      var n=BigInt(item.nonce),pending=quantity(values[2]);
      if(values[0]!=='0x'||quantity(values[1])!==n||pending<n||pending>n+1n||quantity(values[3])<BigInt(item.max_gas_wei))throw Error('El nonce, código o saldo de la wallet cambió; comprueba el original.');
    }
    function sendReview(){return run(async function(s,check){
      var item=review;if(!item)return;var original=api.originalHash(item.funding_id)||null;
      if(item.original_hash_required&&!hash(original))throw Error('Añade el hash original en el envío de gas.');
      var recipient=address(field('recipient').value.trim());await wallet(s);check();review=null;message='Solicitando confirmación…';paint();
      var grant;
      try{grant=await post(s,'/'+encodeURIComponent(item.id)+'/claim',{recipient:recipient,original_hash:original,plan_digest:item.plan_digest,confirmation:'CANCELAR_FINANCIACION_BNB_EN_WALLET'});}
      catch(error){check();throw Error(error.status>=400&&error.status<500?error.message:'Concesión sin confirmar · Actualiza el gas.');}
      check();save(grant);if(!grant.claim_granted){message='Solicitud ya concedida · Comprueba el original.';return true;}
      await inspect(s,item,grant.transaction);check();
      walletPending=true;pendingChanged();var timeout;
      var sent=Promise.resolve().then(function(){check();return s.provider.request({method:'eth_sendTransaction',params:[grant.transaction]});});
      sent.finally(function(){walletPending=false;pendingChanged();}).catch(function(){});
      var txHash;
      try{txHash=await Promise.race([sent,new Promise(function(_,reject){timeout=setTimeout(function(){reject(Error('Wallet sin respuesta · Comprueba la cancelación; no se repetirá.'));},30000);})]);}
      catch(error){check();if(error&&Number(error.code)===4001){save(await post(s,'/'+encodeURIComponent(item.id)+'/rejected',{confirmation:'WALLET_RECHAZADA_4001'}));check();message='Cancelación rechazada en wallet.';return true;}throw error;}
      finally{clearTimeout(timeout);}
      check();if(!hash(txHash))throw Error('Hash sin confirmar · Comprueba la cancelación.');
      hashes.set(item.id,txHash);save(await post(s,'/'+encodeURIComponent(item.id)+'/hash',{tx_hash:txHash}));check();
      var done=await post(s,'/'+encodeURIComponent(item.id)+'/observe',{tx_hash:txHash});check();save(done);message=title(done);return true;
    });}
    function observe(item){return run(async function(s,check){var tx=hashes.get(item.id)||api.originalHash(item.funding_id)||'';
      if(!hash(tx))throw Error('Indica el hash de cancelación o del envío original.');
      var result=await post(s,'/'+encodeURIComponent(item.id)+'/observe',{tx_hash:tx});check();save(result);message=title(result);return result.status==='SETTLED';});}
    function reconcileKnown(item){return run(async function(s,check){var result=await post(s,'/'+encodeURIComponent(item.id)+'/reconcile',{});check();save(result);message=title(result);return true;});}
    function closeReview(item){return run(async function(s,check){var result=await post(s,'/'+encodeURIComponent(item.id)+'/cancel',{confirmation:'RETIRAR_REVISION_CANCELACION_WALLET_BNB'});check();save(result);if(review&&review.id===item.id)review=null;message='Revisión retirada.';});}
    function reconcile(rows,parents){records=rows;sources=parents;
      if(selected){selected=sources.find(function(item){return item.id===selected.id;});if(!eligible(selected)){selected=null;review=null;}}
      if(review){var actual=records.find(function(item){return item.id===review.id;});review=actual&&actual.status==='PREPARED'?actual:null;}
      var actual=records.find(function(item){return item.id===pendingId;});
      if(actual&&actual.status==='SETTLED'){message=title(actual);pendingId=null;review=null;}
      else if(actual&&actual.request_attempted&&message.indexOf('Concesión sin confirmar')===0){message=title(actual);review=null;}
      paint();}
    function clear(){++epoch;identity='';provider=null;busy=false;selected=null;review=null;records=[];sources=[];hashes.clear();pendingId=null;message='';if(root&&root.isConnected)root.querySelector('form').reset();paint();}
    function update(){var s=snap();if(identity!==key(s)||provider!==(s&&s.provider)){clear();identity=key(s);provider=s&&s.provider;}paint();}
    function mount(element,client){root=element;api=client;pendingChanged=notify;
      root.innerHTML='<strong data-wallet-cancel-source></strong><form class="row"><label class="field" data-help="Destinatario del envío original, sólo para comprobar su identidad. La cancelación siempre se solicita a tu propia wallet.">Operadora original<input name="dex_wallet_cancel_recipient" type="text" autocomplete="off" spellcheck="false" maxlength="42" placeholder="0x…" required></label>'+
        '<label class="field">Precio · gwei<input name="dex_wallet_cancel_price" type="text" inputmode="decimal" value="1" required></label>'+
        '<label class="field">Aumento mín. · %<input name="dex_wallet_cancel_bump" type="number" min="1" max="1000" value="10" required></label>'+
        '<label class="field">Máximo solicitado · BNB<input name="dex_wallet_cancel_maximum" type="text" inputmode="decimal" value="0.0001" required></label>'+
        '<label class="dex-run-permission" data-help="El original o alguna solicitud anterior pueden carecer de tarifa firmada verificable. El aumento no garantiza que el nodo acepte la sustitución."><input name="dex_wallet_cancel_unknown" type="checkbox">Aceptar tarifa desconocida</label>'+
        '<button type="submit" class="ghost" data-wallet-cancel-review>Revisar cancelación</button></form><p data-wallet-cancel-status class="state muted" role="status"></p><div data-wallet-cancel-preview></div><div data-wallet-cancel-history></div>';
      root.querySelector('form').addEventListener('submit',prepare);root.querySelector('form').addEventListener('input',function(){review=null;paint();});clear();identity=key(snap());provider=snap()&&snap().provider;paint();}
    return {mount:mount,update:update,clear:clear,reconcile:reconcile,select:select,isBusy:function(){return busy||walletPending;}};
  }
  window.OrbisDexFundingCancellation={create:create};
})();
