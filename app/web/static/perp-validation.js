/* Fixed-profile derivative research and handoff to fictitious PAPER. */
(function () {
  "use strict";
  function esc(v) {return String(v==null?'':v).replace(/[&<>"']/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];});}
  function amount(v) {var shown=v==null?'—':window.OrbisUI.fmtDecimal(String(v),2,2);return '<span data-exact="'+esc(v)+'" title="'+esc(v)+'">'+esc(shown==null?'—':shown)+'</span>';}
  function date(t) {return new Date(t*1000).toISOString().slice(0,16).replace('T',' ');}
  function pathLabel(path) {return path==='O_H_L_C'?'↑↓':'↓↑';}
  var methods={CHRONOLOGICAL_HOLDOUT:'Reserva',ROLLING_FIXED_VERSION:'Ventanas',PATH_SENSITIVITY:'Recorridos'};
  function label(record) {return record.optimization_origin ? 'Reserva optimizada' : methods[record.plan.method];}
  function mount(root,options) {
    options=options||{};
    var alive=true,generation=0,controller=new AbortController(),busy=false,bundle=null,offset=0,total=0,observer;
    root.innerHTML='<span class="statusbadge" data-help="Versión y parámetros fijos. Cada tramo empieza con capital nuevo; los recorridos OHLC siguen siendo hipótesis y no son límites de ejecución real.">Experimental</span>'+
      '<form id="perp-validation-form" class="perp-run-config"><label class="field">Perfil<select name="profile_id" required></select></label>'+
      '<label class="field">Datos<select name="dataset_id" required></select></label><label class="field">Método<select name="method"><option value="CHRONOLOGICAL_HOLDOUT">Reserva</option><option value="ROLLING_FIXED_VERSION">Ventanas sucesivas</option><option value="PATH_SENSITIVITY">Recorridos TRADE/MARK</option></select></label>'+
      '<label class="field" id="perp-validation-fraction">Desarrollo %<input name="fraction" type="number" min="50" max="80" step="1" value="70"></label>'+
      '<label class="field" id="perp-validation-folds" hidden>Ventanas<input name="folds" type="number" min="2" max="5" step="1" value="3"></label>'+
      '<div class="row"><button type="submit" disabled data-help="Guarda resultados de cuentas independientes con la misma versión. No reajusta parámetros ni activa bots.">Validar</button></div></form>'+
      '<p id="perp-validation-status" class="state" role="status"></p><div class="row"><label class="field">Validaciones guardadas<select id="perp-validation-selection"><option value="">Selecciona validación</option></select></label>'+
      '<button id="perp-validation-refresh" type="button" class="ghost">Actualizar</button><button id="perp-validation-prev" type="button" class="ghost" disabled>Anterior</button><span id="perp-validation-page"></span><button id="perp-validation-next" type="button" class="ghost" disabled>Siguiente</button></div>'+
      '<section id="perp-validation-result" hidden><div class="row"><span class="statusbadge">Reproducible · experimental</span><span id="perp-validation-small" class="statusbadge" data-help="Algún tramo tiene menos de 30 operaciones; las estadísticas pueden ser inestables." hidden>Muestra pequeña</span>'+
      '<button id="perp-validation-export" type="button" class="ghost">Exportar JSON</button><button id="perp-validation-reference" type="button" class="ghost" data-help="Exporta la identidad de la reserva; autoridad NONE. No acredita independencia estadística ni habilita operaciones.">Referencia OOS</button><button id="perp-validation-paper" type="button" class="ghost" hidden data-help="Verifica esta reserva y prepara una cuenta ficticia nueva. La activación requiere otra confirmación.">Probar en PAPER</button></div>'+
      '<div class="tablewrap" id="perp-validation-windows"></div><details><summary>Supuestos</summary><ul id="perp-validation-assumptions"></ul></details></section>';
    function el(id) {return root.querySelector('#'+id);}
    var form=el('perp-validation-form'),status=el('perp-validation-status'),selection=el('perp-validation-selection');
    function current(ticket) {return alive&&root.isConnected&&ticket===generation;}
    function begin() {controller.abort();controller=new AbortController();return ++generation;}
    function clear() {bundle=null;el('perp-validation-result').hidden=true;}
    function ready() {
      Array.from(form.elements).forEach(function(e){e.disabled=busy;});
      var method=form.elements.method.value;
      el('perp-validation-fraction').hidden=method!=='CHRONOLOGICAL_HOLDOUT';form.elements.fraction.disabled=busy||method!=='CHRONOLOGICAL_HOLDOUT';
      el('perp-validation-folds').hidden=method!=='ROLLING_FIXED_VERSION';form.elements.folds.disabled=busy||method!=='ROLLING_FIXED_VERSION';
      form.querySelector('button').disabled=busy||!form.elements.profile_id.value||!form.elements.dataset_id.value;
      root.querySelectorAll('[data-window]').forEach(function(e){e.disabled=busy;});
      el('perp-validation-paper').disabled=busy||!bundle;
    }
    async function api(path,options) {
      var r=await fetch('/lab/perp'+path,Object.assign({cache:'no-store',signal:controller.signal,headers:{'Content-Type':'application/json'}},options||{}));
      var body=await r.json();if(!r.ok)throw new Error(r.status===423?'LAB bloqueado · desbloquea en Ajustes.':typeof body.detail==='string'?body.detail:'No se pudo verificar la validación.');return body;
    }
    async function library(ticket,wanted) {
      var page=await api('/validations?offset='+offset+'&limit=25');if(!current(ticket))return;
      total=page.total;selection.innerHTML='<option value="">Selecciona validación</option>'+page.items.map(function(v){return '<option value="'+esc(v.id)+'">'+esc(label(v)+' · '+v.symbol+' · '+v.created_at.slice(0,19).replace('T',' '))+'</option>';}).join('');
      selection.value=wanted||'';el('perp-validation-prev').disabled=offset===0;el('perp-validation-next').disabled=offset+25>=total;
      el('perp-validation-page').textContent=total?(offset+1)+'–'+Math.min(offset+25,total)+' / '+total:'0';
    }
    async function load() {
      var ticket=begin();clear();busy=true;ready();status.textContent='Cargando…';
      try {
        var all=await Promise.all([api('/profiles'),api('/datasets'),fetch('/lab/strategy-versions',{cache:'no-store',signal:controller.signal}).then(function(r){if(!r.ok)throw new Error(r.status===423?'LAB bloqueado · desbloquea en Ajustes.':'LAB no disponible.');return r.json();})]);
        if(!current(ticket))return;
        form.elements.profile_id.innerHTML='<option value="">Selecciona perfil</option>'+all[0].map(function(row){var p=row.profile,v=all[2].find(function(v){return v.id===p.strategy_version_id;});return '<option value="'+esc(row.id)+'">'+esc((v?v.name+' · v'+v.version:p.strategy_version_id.slice(0,8))+' · '+(p.margin_mode==='ISOLATED'?'Aislado':'Cross')+' '+p.leverage+'× · '+(p.entry_order?'LIMIT · ':'')+(p.partial_targets?'Parciales · LAB · ':'')+'T '+pathLabel(p.trade_path)+' / M '+pathLabel(p.mark_path))+'</option>';}).join('');
        form.elements.dataset_id.innerHTML='<option value="">Selecciona datos</option>'+all[1].map(function(row){return '<option value="'+esc(row.id)+'">'+esc(row.symbol+' · '+row.timeframe+' · '+date(row.start))+'</option>';}).join('');
        await library(ticket);if(current(ticket))status.textContent=all[0].length?'Selecciona perfil y datos.':'Guarda primero un perfil en Pruebas de derivados.';
      } catch(error) {if(current(ticket)){form.elements.profile_id.innerHTML='';form.elements.dataset_id.innerHTML='';status.textContent=error.message;}}
      finally {if(current(ticket)){busy=false;ready();}}
    }
    function show(value) {
      if(['orbis.lab.perp.validation.bundle.v1','orbis.lab.perp.validation.bundle.v2','orbis.lab.perp.validation.bundle.v3'].indexOf(value.schema)<0||value.validation.verification!=='REPLAY_VERIFIED')throw new Error('La validación no está reproducida.');
      bundle=value;var record=value.validation,result=value.execution.result;
      if(!Array.from(selection.options).some(function(o){return o.value===record.id;}))selection.add(new Option(label(record)+' · '+record.symbol,record.id));selection.value=record.id;
      [['profile_id','Perfil archivado'],['dataset_id',record.symbol+' · '+record.timeframe]].forEach(function(row){var input=form.elements[row[0]];if(!Array.from(input.options).some(function(o){return o.value===record[row[0]];}))input.add(new Option(row[1],record[row[0]]));input.value=record[row[0]];});
      form.elements.method.value=record.plan.method;if(record.plan.train_fraction)form.elements.fraction.value=String(Math.round(Number(record.plan.train_fraction)*100));if(record.plan.folds)form.elements.folds.value=String(record.plan.folds);
      el('perp-validation-result').hidden=false;el('perp-validation-small').hidden=result.warnings.indexOf('SMALL_SAMPLE')<0;
      el('perp-validation-reference').hidden=record.plan.method==='PATH_SENSITIVITY';
      var reserves=result.windows.filter(function(w){return w.role==='RESERVE';});
      el('perp-validation-paper').hidden=!options.onPaper||!reserves.length||reserves.some(function(w){return w.status!=='COMPLETE'||w.metrics.open_position;});
      var role={DEVELOPMENT:'Desarrollo',RESERVE:'Reserva',SCENARIO:'Recorrido'};
      el('perp-validation-windows').innerHTML='<table><thead><tr><th>Tramo</th><th>Primera vela UTC</th><th>Última vela UTC</th><th>Velas</th><th>Operaciones</th><th>Capital final</th><th>Variación %</th><th>Drawdown %</th><th>Estado</th><th></th></tr></thead><tbody>'+result.windows.map(function(w){var m=w.metrics;return '<tr><td data-help="↑↓: máximo antes del mínimo. ↓↑: mínimo antes del máximo. TRADE y MARK siguen recorridos independientes.">'+esc(w.role==='SCENARIO'?'TRADE '+pathLabel(w.profile.trade_path)+' · MARK '+pathLabel(w.profile.mark_path):role[w.role]+(w.fold?' '+w.fold:''))+'</td><td>'+esc(date(w.from_t))+'</td><td>'+esc(date(w.to_t))+'</td><td>'+w.bars+'</td><td>'+m.trades+'</td><td>'+amount(m.final_equity)+'</td><td>'+amount(m.return_pct)+'</td><td>'+amount(m.max_drawdown_pct)+'</td><td>'+esc(w.status==='COMPLETE'?'Completo':'Interrumpido')+'</td><td><button type="button" class="ghost" data-window="'+w.index+'" data-help="Guarda o recupera la prueba exacta de este tramo y abre sus operaciones y marcas.">Ver</button></td></tr>';}).join('')+'</tbody></table>';
      el('perp-validation-assumptions').innerHTML=['Capital y posiciones reiniciados en cada tramo. No es una cartera continua.',
        'La reserva usa sólo el prefijo anterior para indicadores; parámetros fijos, sin reajuste.',
        'Reglas actuales y trayectorias OHLC hipotéticas. Cierre final artificial.',
        record.plan.method==='PATH_SENSITIVITY'?'Cuatro escenarios: no son fuera de muestra ni garantizan contener la ejecución real.':'Una estrategia diseñada mirando la reserva puede contaminar sus resultados; los tramos duran distinto.',
        'La referencia no activa PAPER ni concede autoridad real.'].map(function(t){return '<li>'+esc(t)+'</li>';}).join('');
      if(record.profile.entry_order)el('perp-validation-assumptions').insertAdjacentHTML('beforeend','<li>LIMIT: ejecución completa hipotética al tocar TRADE. Cada tramo cancela sus órdenes pendientes al terminar.</li>');
      if(record.profile.partial_targets)el('perp-validation-assumptions').insertAdjacentHTML('beforeend','<li>Salidas parciales · LAB y PAPER.</li>');
      status.textContent=label(record)+(record.profile.entry_order?' · Límite':'')+' · '+result.windows.length+' tramos';ready();
    }
    async function select(id) {
      var ticket=begin();clear();busy=false;ready();if(!id){status.textContent='Selecciona validación.';return;}
      status.textContent='Reproduciendo originales…';try {var value=await api('/validations/'+encodeURIComponent(id));if(current(ticket)&&selection.value===id)show(value);}catch(error){if(current(ticket)){clear();status.textContent=error.message;}}
    }
    form.addEventListener('change',function(){begin();clear();busy=false;ready();status.textContent='Configuración preparada.';});
    form.addEventListener('submit',async function(event){
      event.preventDefault();if(busy)return;var ticket=begin(),method=form.elements.method.value,plan={method:method};
      if(method==='CHRONOLOGICAL_HOLDOUT')plan.train_fraction='0.'+form.elements.fraction.value;
      if(method==='ROLLING_FIXED_VERSION')plan.folds=Number(form.elements.folds.value);
      var body={dataset_id:form.elements.dataset_id.value,profile_id:form.elements.profile_id.value,plan:plan};
      clear();busy=true;ready();status.textContent='Validando y guardando…';
      try {var saved=await api('/validations',{method:'POST',body:JSON.stringify(body)});if(!current(ticket))return;
        offset=0;await library(ticket,saved.id);if(!current(ticket))return;var value=await api('/validations/'+encodeURIComponent(saved.id));if(current(ticket))show(value);
      } catch(error){if(current(ticket)){clear();status.textContent=error.message;}}finally{if(current(ticket)){busy=false;ready();}}
    });
    selection.addEventListener('change',function(){select(selection.value);});el('perp-validation-refresh').addEventListener('click',load);
    [['prev',-25],['next',25]].forEach(function(row){el('perp-validation-'+row[0]).addEventListener('click',async function(){var ticket=begin();clear();busy=false;ready();offset=Math.max(0,offset+row[1]);try{await library(ticket);}catch(e){if(current(ticket))status.textContent=e.message;}});});
    async function download(reference) {
      if(!bundle)return;var id=bundle.validation.id,ticket=begin(),button=el('perp-validation-'+(reference?'reference':'export'));button.disabled=true;
      try {var value=await api('/validations/'+encodeURIComponent(id)+(reference?'/reference':'/export'));if(!current(ticket)||!bundle||bundle.validation.id!==id)return;
        var url=URL.createObjectURL(new Blob([JSON.stringify(value,null,2)],{type:'application/json'})),link=document.createElement('a');link.href=url;link.download='orbis-perp-'+(reference?'reference-':'validation-')+id+'.json';link.click();setTimeout(function(){URL.revokeObjectURL(url);},1000);
      } catch(error){if(current(ticket)){clear();status.textContent=error.message;}}finally{if(current(ticket))button.disabled=false;}
    }
    el('perp-validation-export').addEventListener('click',function(){download(false);});el('perp-validation-reference').addEventListener('click',function(){download(true);});
    el('perp-validation-paper').addEventListener('click',async function(){if(!bundle||busy||!options.onPaper)return;busy=true;ready();try{await options.onPaper(bundle.validation.id);}finally{if(alive){busy=false;ready();}}});
    el('perp-validation-windows').addEventListener('click',async function(event){
      var button=event.target.closest('[data-window]');if(!button||!bundle||busy)return;
      var id=bundle.validation.id,index=button.dataset.window,ticket=begin();busy=true;ready();status.textContent='Abriendo tramo…';
      try {var child=await api('/validations/'+encodeURIComponent(id)+'/windows/'+index+'/inspect',{method:'POST'});if(!current(ticket))return;
        if(options.onInspect&&!(await options.onInspect(child.run.id,function(){return current(ticket);})))throw new Error('No se pudo abrir el tramo.');if(current(ticket))status.textContent='Tramo abierto en Pruebas de derivados.';
      } catch(error){if(current(ticket)){clear();status.textContent=error.message;}}finally{if(current(ticket)){busy=false;ready();}}
    });
    var widget={load:load,dispose:function(){if(!alive)return;alive=false;generation+=1;controller.abort();if(observer)observer.disconnect();}};
    observer=new MutationObserver(function(){if(!root.isConnected)widget.dispose();});observer.observe(document.body,{childList:true,subtree:true});ready();return widget;
  }
  window.OrbisPerpValidation={mount:mount};
})();
