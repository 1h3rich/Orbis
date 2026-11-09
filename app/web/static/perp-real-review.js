/* A read-only comparison. No bot activation, credentials or account balance. */
(function () {
  'use strict';
  var reasons={NO_OBSERVED_CLOSED_TRADE:'Falta una operación PAPER cerrada',NO_OBSERVED_CANDLE:'Faltan velas observadas',PAPER_GAP:'PAPER tiene un intervalo sin observación',PAPER_HALTED:'PAPER requiere revisión',PAPER_LIQUIDATED:'PAPER fue liquidado',ESTIMATED_CLOSURE_EXCLUDED:'El cierre estimado queda excluido',SOURCE_ADVANCED:'PAPER avanzó durante la consulta; vuelve a revisar',MARGIN_MODE_MISMATCH:'El modo de margen difiere',LEVERAGE_MISMATCH:'El apalancamiento difiere',LEVERAGE_OUTSIDE_ACCOUNT_RANGE:'El perfil supera los límites de esta cuenta',PERP_EXECUTOR_NOT_IMPLEMENTED:'Ejecución REAL de este modelo pendiente'};
  function esc(v){return String(v==null?'':v).replace(/[&<>"']/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];});}
  function mount(root,request){
    var alive=true,connected=false,busy=false,epoch=0,offset=0,total=0,controller=new AbortController();
    root.innerHTML='<details data-perp-live-details><summary data-help="Reproduce los originales LAB y PAPER y compara margen y apalancamiento con GRVT. Las operaciones requieren una revisión de presupuesto y autorización separadas.">Revisar PAPER → REAL</summary>'+
      '<div class="row"><label class="field">Prueba PAPER<select data-perp-live-source aria-label="Prueba PAPER"></select></label><label class="field">Subcuenta<select data-perp-live-sub aria-label="Subcuenta GRVT"></select></label><button type="button" class="ghost" data-perp-live-review>Revisar</button></div>'+
      '<div class="row"><button type="button" class="ghost" data-perp-live-prev>Anterior</button><span data-perp-live-page></span><button type="button" class="ghost" data-perp-live-next>Siguiente</button><button type="button" class="ghost" data-perp-live-refresh>Actualizar pruebas</button></div>'+
      '<p class="state" role="status" data-perp-live-state></p><div data-perp-live-result></div></details>';
    root.hidden=true;
    function el(name){return root.querySelector('[data-perp-live-'+name+']');}
    function current(ticket){return alive&&connected&&root.isConnected&&ticket===epoch;}
    function clear(){epoch++;controller.abort();controller=new AbortController();busy=false;el('result').replaceChildren();el('state').textContent='';}
    function controls(){el('review').disabled=busy||!el('source').value||!el('sub').value;el('refresh').disabled=busy;el('prev').disabled=busy||offset===0;el('next').disabled=busy||offset+25>=total;el('source').disabled=busy;el('sub').disabled=busy;}
    async function sources(){
      if(!connected||busy)return;clear();var ticket=epoch;busy=true;controls();el('state').textContent='Consultando pruebas…';
      try{
        var response=await fetch('/paper/perp/observed/sessions?offset='+offset+'&limit=25',{cache:'no-store',credentials:'same-origin',signal:controller.signal});
        var page=await response.json();if(!current(ticket))return;
        if(!response.ok)throw new Error(page.detail||'Pruebas no disponibles');
        total=page.total;el('source').innerHTML='<option value="">Elige una prueba</option>'+page.items.map(function(row){return '<option value="'+esc(row.id)+'">'+esc(row.created_at.slice(0,16).replace('T',' '))+' · '+esc(row.id.slice(0,8))+'</option>';}).join('');
        el('page').textContent=total?(offset+1)+'–'+Math.min(offset+25,total)+' / '+total:'0 pruebas';el('state').textContent=total?'':'Primero prueba una estrategia en PAPER';
      }catch(error){if(current(ticket)&&error.name!=='AbortError'){el('source').replaceChildren();el('result').replaceChildren();el('state').textContent=error.message;}}
      finally{if(current(ticket)){busy=false;controls();}}
    }
    async function review(){
      if(busy||!el('source').value||!el('sub').value)return;clear();var ticket=epoch;busy=true;controls();el('state').textContent='Reproduciendo y comparando…';
      try{
        var data=await request('/exchanges/grvt/perp-review',{method:'POST',signal:controller.signal,body:JSON.stringify({session_id:el('source').value,sub_account_id:el('sub').value})});
        if(!current(ticket))return;
        var executable=false;
        try { var capabilities=await request('/exchanges/grvt/mainnet/bots/capabilities',{signal:controller.signal});executable=capabilities.supervised_perpetual===true; }
        catch (_) { /* El backend anterior conserva el aviso de ejecución pendiente. */ }
        if(!current(ticket))return;
        var config=data.configurations[1],profile=data.research.profile;
        el('state').textContent='Revisión completada · sólo lectura';
        el('result').innerHTML='<div class="tablewrap"><table><thead><tr><th>Configuración</th><th>Prueba</th><th>GRVT</th></tr></thead><tbody><tr><td>Margen</td><td>'+esc(profile.margin_mode)+'</td><td>'+esc(config.margin_type)+'</td></tr><tr><td>Apalancamiento</td><td>'+esc(profile.leverage)+'×</td><td>'+esc(config.leverage)+'×</td></tr></tbody></table></div>'+
          '<p><span class="statusbadge" data-help="Validación histórica experimental con reglas actuales; no demuestra ejecución histórica ni rentabilidad alcanzable.">Reglas actuales</span> '+esc(config.instrument)+' · '+esc(data.paper.closed_trades)+(data.paper.closed_trades===1?' operación PAPER cerrada':' operaciones PAPER cerradas')+'</p><ul>'+data.blockers.map(function(reason){return '<li>'+esc(reason==='PERP_EXECUTOR_NOT_IMPLEMENTED'&&executable?'Para operar, revisa y autoriza en Bot · MAINNET':reasons[reason]||reason)+'</li>';}).join('')+'</ul>'+
          '<details><summary>Procedencia</summary><pre>'+esc(JSON.stringify({validation:data.research.artifact_id,validation_hash:data.research.artifact_hash,session:data.paper.session_id,sequence:data.paper.sequence,head:data.paper.head_hash,review_hash:data.sha256,execution_authority:data.execution_authority},null,2))+'</pre></details>';
      }catch(error){if(current(ticket)&&error.name!=='AbortError'){el('result').replaceChildren();el('state').textContent=error.message;}}
      finally{if(current(ticket)){busy=false;controls();}}
    }
    el('details').addEventListener('toggle',function(){if(el('details').open&&!el('source').options.length)sources();});
    el('refresh').onclick=sources;el('review').onclick=review;
    el('prev').onclick=function(){offset=Math.max(0,offset-25);sources();};el('next').onclick=function(){offset+=25;sources();};
    ['source','sub'].forEach(function(name){el(name).onchange=function(){clear();controls();};});
    return {setConnected:function(value){connected=!!value;clear();root.hidden=!connected;if(!connected){el('source').replaceChildren();el('sub').replaceChildren();el('details').open=false;}controls();},
      setAccounts:function(accounts){var previous=el('sub').value;el('sub').innerHTML='<option value="">Elige subcuenta</option>'+accounts.map(function(row){return '<option value="'+esc(row.sub_account_id)+'">…'+esc(String(row.sub_account_id).slice(-4))+'</option>';}).join('');el('sub').value=previous;if(el('sub').value!==previous)clear();controls();},
      dispose:function(){alive=false;clear();root.replaceChildren();}};
  }
  window.OrbisPerpRealReview={mount:mount};
})();
