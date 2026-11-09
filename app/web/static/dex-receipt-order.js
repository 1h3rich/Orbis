/* Shared read-only block proof client. No wallet provider, signing or retries. */
(function(){
  'use strict';
  async function supported(client,gasOnly){
    try{var state=await client.request('/vault/status',null,true);
      return !!(state&&state.capabilities&&state.capabilities[gasOnly?'dex_gas_order_proof':'dex_receipt_order_proof']===true);
    }catch(_){return false;}
  }
  async function check(client,source,kind,id,passive,gasOnly){
    var kinds=gasOnly?['WALLET_SWAP','OPERATOR','CALL_RESOLUTION','DEPLOYMENT','CONTROL','GAS_FUNDING','GAS_RETURN','DEPLOYMENT_RESOLUTION','NATIVE_RESOLUTION']:['WALLET_SWAP','OPERATOR','CALL_RESOLUTION'];
    if(!kinds.includes(kind))throw new Error('Recibo no compatible.');
    var controller=new AbortController(),timer=setTimeout(function(){controller.abort();},15000);
    try{var result=await client.request('/accounts/'+encodeURIComponent(source.accountId)+'/dex-receipts/'+
        encodeURIComponent(kind)+'/'+encodeURIComponent(id)+(gasOnly?'/gas-order-proof':'/order-proof'),
        {method:'POST',body:JSON.stringify({address:source.wallet}),signal:controller.signal},!!passive);
      if(!result||result.saved!==true||result.source_kind!==kind||result.source_id!==id||!result.proof||
          typeof result.proof.transaction_index!=='string'||!/^(0|[1-9]\d{0,5})$/.test(result.proof.transaction_index)||
          Number(result.proof.transaction_index)>=100000||typeof result.proof.block_number!=='string'||
          !/^(0|[1-9]\d{0,77})$/.test(result.proof.block_number)||gasOnly&&
          (result.proof.schema!=='orbis.dex.gas.order.proof.v1'||result.proof.purpose!=='NATIVE_GAS_ONLY'))throw new Error('Prueba de bloque sin confirmar.');
      return result.proof;
    }finally{clearTimeout(timer);}
  }
  function title(proof,hidden){return hidden?'Bloque verificado':'Bloque '+proof.block_number+' · Posición '+String(Number(proof.transaction_index)+1);}
  window.OrbisDexReceiptOrder={supported:supported,check:check,title:title};
})();
