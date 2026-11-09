/* LAB/PAPER preparation, encrypted binding and isolated owner setup. */
(function () {
  "use strict";
  var root=null, api=null, identity="", epoch=0, busy=false, candidates=[], preview=null, timer=null;
  var reviewedBody=null, saved=[], pendingBinding=null;
  var handoff=null;
  var message="Consulta tus estrategias probadas en PAPER.";
  function snapshot() { return api && api.snapshot(); }
  function key(value) { return value && value.accountId ? value.accountId+":"+value.generation : ""; }
  function current(host,issued,scope) { return root===host && root.isConnected && epoch===issued && key(snapshot())===scope; }
  function endpoint(value) { return "/accounts/"+encodeURIComponent(value.accountId)+"/dex-bots"; }
  function clearPreview() { preview=null;reviewedBody=null; if (timer) clearTimeout(timer); timer=null; }
  function candidateKey(item) { return item.source_kind+":"+item.session_id; }
  function selected() { return candidates.find(function (item) { return candidateKey(item)===root.querySelector('[name="source"]').value; }); }
  function researchChoices() {
    var item=selected(),select=root.querySelector('[name="research_validation_id"]');select.replaceChildren();
    var empty=document.createElement('option');empty.value='';empty.textContent='Elegir reserva';select.append(empty);
    (item&&item.research_options||[]).forEach(function(value){var option=document.createElement('option');option.value=value.id;
      option.textContent=(value.method==='ROLLING_FIXED_VERSION'?'Ventanas':'Reserva')+' · '+value.test_bars+' velas · '+value.id.slice(0,8);select.append(option);});
    if(select.options.length===2)select.selectedIndex=1;
    root.querySelector('[data-dex-research-link]').href=item&&item.source_kind==='DCA'?'#/dca':'#/laboratorio';
  }
  function units(text) {
    var value=String(text), whole=value.length>18 ? value.slice(0,-18) : "0";
    var fraction=value.padStart(19,"0").slice(-18).replace(/0+$/,"");
    return whole+(fraction ? ","+fraction : "");
  }
  function money(text,currency) { return snapshot().hidden ? "•••• "+currency : units(text)+" "+currency; }
  function text(parent,tag,value,className) {
    var node=document.createElement(tag);node.textContent=value;if (className) node.className=className;parent.append(node);return node;
  }
  function gasBusy() { return !!(window.OrbisDexGasFunding&&window.OrbisDexGasFunding.isBusy()); }
  function runBusy() { return !!(window.OrbisDexRuns&&window.OrbisDexRuns.isBusy()); }
  function returnBusy() { return !!(window.OrbisDexGasReturn&&window.OrbisDexGasReturn.isBusy()); }
  function blocked() { return busy || gasBusy() || runBusy() || returnBusy() || !!(window.OrbisDexSetup && window.OrbisDexSetup.isBusy()); }
  function paint() {
    if (!root || !root.isConnected) return;
    var locked=blocked();
    var source=snapshot(), item=selected(), valid=!!(source && source.accountId);
    root.hidden=!valid;
    root.querySelector('[data-dex-bot-sources]').disabled=locked || !valid;
    root.querySelector('[data-dex-bot-sources]').textContent=handoff ? "Verificar sesión seleccionada" : "Consultar PAPER";
    root.querySelector('[data-dex-bot-handoff]').hidden=!handoff;
    root.querySelector('[data-dex-discard-source]').disabled=locked || !valid;
    root.querySelectorAll('input,select').forEach(function (control) { if (!control.closest('[data-dex-owner],[data-dex-gas],[data-dex-runs],[data-dex-return],[data-dex-booking]')) control.disabled=locked || !valid; });
    var amountType=valid && source.hidden ? "password" : "text";
    root.querySelectorAll('.dex-bot-fields input, [data-dex-bot-bind-form] input[inputmode="decimal"]').forEach(function (input) {
      if (input.type!==amountType) input.type=amountType;
    });
    root.querySelector('[data-dex-bot-review]').disabled=locked || !valid || !item || !item.ready || !root.querySelector('[name="research_validation_id"]').value;
    root.querySelector('[data-dex-bot-monthly]').hidden=!item || item.source_kind!=="DCA";
    root.querySelector('[name="monthly_usdt"]').required=!!item && item.source_kind==="DCA";
    root.querySelector('[data-dex-bot-status]').textContent=message;
    var walletReady=valid && source.verified && source.chain==='0x38' && !!source.wallet;
    root.querySelector('[data-dex-bot-executors]').disabled=locked || !walletReady;
    root.querySelector('[data-dex-bot-save]').disabled=locked || !!pendingBinding || !walletReady || !preview || Date.now()>=Date.parse(preview.expires_at) || !root.querySelector('[name="deployment_id"]').value;
    root.querySelector('[data-dex-bot-recover]').hidden=!pendingBinding || locked;
    root.querySelector('[data-dex-bot-recover]').disabled=locked || !valid;
    root.querySelector('[data-dex-bot-history-refresh]').disabled=locked || !valid;
    root.querySelectorAll('[data-dex-binding-stop]').forEach(function (button) { button.disabled=locked || !valid; });
    var output=root.querySelector('[data-dex-bot-preview]');output.replaceChildren();
    if (preview) {
      var fresh=Date.now()<Date.parse(preview.expires_at);
      if (!fresh) text(output,"p","Revisión caducada · Comprueba de nuevo", "muted");
      text(output,"p",preview.signal_market+" → "+preview.execution_market,"muted");
      var list=document.createElement('dl');list.className="dex-bot-amounts";output.append(list);
      [["Entrada",preview.amounts.entry_units,"USDT"],["Presupuesto",preview.amounts.budget_units,"USDT"],
       ["Exposición máx.",preview.amounts.exposure_units,"USDT"],["Pérdida realizada máx.",preview.amounts.loss_units,"USDT"],
       ["Cuota de compras",preview.amounts.wbnb_quota_units,"WBNB"]].forEach(function (row) {
        var pair=document.createElement('div');text(pair,'dt',row[0],'muted');text(pair,'dd',money(row[1],row[2]));list.append(pair);
      });
      if (preview.dca) text(output,'p','Límite mensual efectivo · '+money(preview.dca.effective_monthly_units,'USDT'),'muted');
      var definition=preview.definition||{},average=definition.ma_adjustment||definition.sma_gate;
      if(average)text(output,'small','Cierre '+(average.condition==='above'?'sobre ':'bajo ')+
        (average.kind||'sma').toUpperCase()+' '+average.period+
        (average.kind?' · ×'+average.matched_factor+' / ×'+average.unmatched_factor:''),'muted');
      if(definition.volume_gate)text(output,'small','Volumen MEXC ≥ '+definition.volume_gate.min_ratio+
        '× · '+definition.volume_gate.period+' velas','muted');
      text(output,'small','Versión '+preview.source.version_id.slice(0,8)+' · Reserva '+preview.research_evidence.research.artifact_id.slice(0,8)+' · Señales MEXC','muted');
    }
    var history=root.querySelector('[data-dex-bot-history]');history.replaceChildren();
    saved.forEach(function (binding) {
      var card=document.createElement('section');card.className='real-panel';history.append(card);
      var run=window.OrbisDexRuns&&window.OrbisDexRuns.forBinding(binding.id);
      text(card,'strong',binding.status==='STOPPED' ? 'Vínculo retirado' : binding.grant_status==='STOPPED' ? 'Autorización retirada' : binding.expired ? 'Vínculo caducado' :
        run&&run.execution_enabled?'Bot activo':run&&run.runtime_status==='REARM_REQUIRED'?'Rearme necesario':run&&run.runtime_status==='STOPPED'?'Bot detenido':
        'Vinculado');
      text(card,'p',(binding.source_kind==='DCA'?'DCA':'Long')+' · '+binding.source.symbol+' · '+binding.source.timeframe+' · '+binding.id.slice(0,8),'muted');
      text(card,'small',binding.research_qualification?'Reserva '+binding.research_qualification.evidence.research.artifact_id.slice(0,8):'Legado · Sin reserva','muted');
      text(card,'p','Entrada máx. '+money(binding.effective_limits.max_buy_per_order,'USDT')+' · Gas autorizado '+money(binding.gas.gas_budget_wei,'BNB'),'muted');
      if (binding.status!=='STOPPED') {
        var button=text(card,'button','Retirar vínculo','ghost');button.type='button';button.dataset.dexBindingStop=binding.id;button.disabled=locked;
        button.addEventListener('click',function () { stopBinding(binding.id); });
      }
    });
    if (window.OrbisDexSetup) window.OrbisDexSetup.update();
    if (window.OrbisDexGasFunding) window.OrbisDexGasFunding.update();
    if (window.OrbisDexRuns) window.OrbisDexRuns.update();
    if (window.OrbisDexGasReturn) window.OrbisDexGasReturn.update();
    if(window.OrbisDexReceiptBooking)window.OrbisDexReceiptBooking.update();
  }
  async function load() {
    if (blocked() || !snapshot() || !snapshot().accountId) return;
    var host=root, issued=++epoch, scope=key(snapshot()), value=snapshot();
    var chosen=handoff, path=endpoint(value)+"/sources";
    if (chosen) {
      path+="/"+chosen.kind+"/"+encodeURIComponent(chosen.id);
      candidates=[];root.querySelector('[name="source"]').replaceChildren();
      researchChoices();
    }
    busy=true;clearPreview();message="Consultando LAB y PAPER…";paint();
    try {
      var result=await api.request(path,null,true);
      if (!current(host,issued,scope) || chosen!==handoff) return;
      if (!Array.isArray(result) || (chosen && (result.length>1 || result.some(function (item) {
        return !item || item.session_id!==chosen.id || item.source_kind!==chosen.kind ||
          item.preparation_only!==true || item.automatic_execution!==false || !Array.isArray(item.research_options);
      })))) throw new Error("No se pudo verificar la sesión seleccionada.");
      candidates=result;
      var select=root.querySelector('[name="source"]');select.replaceChildren();
      candidates.forEach(function (item) {
        var option=document.createElement('option');option.value=candidateKey(item);
        option.textContent=item.name+" · v"+(item.version || "?")+" · "+item.symbol+" · "+item.timeframe+(item.ready ? "" : " · Revisar");
        select.append(option);
      });
      var available=candidates.find(function (item) { return item.ready; });
      if (available) select.value=candidateKey(available);
      researchChoices();
      message=available ? "Elige los límites para revisar la preparación." : candidates.length ? candidates[0].reason :
        chosen ? "Sesión PAPER seleccionada · no disponible." : "Sin sesiones PAPER activas · Prueba BNB/USDT en Simulación.";
    } catch (error) {
      if (current(host,issued,scope)) {
        candidates=[];root.querySelector('[name="source"]').replaceChildren();researchChoices();
        message=error.message || "No se pudo consultar PAPER.";
      }
    }
    finally { if (current(host,issued,scope)) { busy=false;paint(); } }
  }
  async function review(event) {
    event.preventDefault();var item=selected();
    if (blocked() || !item || !item.ready || !event.currentTarget.reportValidity() ||
        handoff && (item.source_kind!==handoff.kind || item.session_id!==handoff.id)) return;
    var host=root, issued=++epoch, value=snapshot(), scope=key(value), data=new FormData(root.querySelector('form'));
    var body={source_kind:item.source_kind,paper_session_id:item.session_id,research_validation_id:String(data.get('research_validation_id'))};
    if(!(item.research_options||[]).some(function(option){return option.id===body.research_validation_id;}))return;
    ['entry_usdt','budget_usdt','exposure_usdt','loss_usdt','wbnb_quota'].forEach(function (name) { body[name]=String(data.get(name)).trim().replace(',','.'); });
    if (item.source_kind==='DCA') body.monthly_usdt=String(data.get('monthly_usdt')).trim().replace(',','.');
    busy=true;clearPreview();message="Repitiendo el backtest y comprobando PAPER…";paint();
    try {
      var result=await api.request(endpoint(value)+"/preview",{method:'POST',body:JSON.stringify(body)});
      if (!current(host,issued,scope)) return;
      if(result.protocol!=='orbis.dex.bot.preparation.v2'||result.source_kind!==body.source_kind||
          !result.source||result.source.session_id!==body.paper_session_id||
          !result.research_evidence||result.research_evidence.schema!=='orbis.dex.signal.research.v1'||
          result.research_evidence.research.artifact_id!==body.research_validation_id)throw Error('La revisión no coincide con la sesión y reserva seleccionadas.');
      preview=result;reviewedBody=body;message="LAB + PAPER verificados · Listo para vincular.";
      timer=setTimeout(function () { timer=null;if (root===host && root.isConnected && preview===result && key(snapshot())===scope) paint(); },Math.max(0,Date.parse(result.expires_at)-Date.now()+20));
    } catch (error) { if (current(host,issued,scope)) message=error.message || "No se pudo verificar la preparación."; }
    finally { if (current(host,issued,scope)) { busy=false;paint(); } }
  }
  async function executors() {
    if (blocked() || !snapshot().verified || snapshot().chain!=='0x38') return;
    var host=root,issued=++epoch,value=snapshot(),scope=key(value);busy=true;paint();
    try {
      var result=await api.request('/accounts/'+encodeURIComponent(value.accountId)+'/dex-deployments');
      if (!current(host,issued,scope)) return;
      var select=root.querySelector('[name="deployment_id"]');select.replaceChildren();
      result.filter(function (item) { return item.executor_verified; }).forEach(function (item) {
        var option=document.createElement('option');option.value=item.id;option.textContent='Ejecutor '+item.id.slice(0,8)+' · pool '+(item.fee/10000)+' %';select.append(option);
      });
      message=select.options.length ? 'Vincula la versión y los límites a su política confirmada.' : 'Sin ejecutores verificados · Prepara uno en «Ejecutor · BNB».';
    } catch (error) { if (current(host,issued,scope)) message=error.message || 'No se pudieron consultar ejecutores.'; }
    finally { if (current(host,issued,scope)) { busy=false;paint(); } }
  }
  async function history() {
    if (blocked() || !snapshot().accountId) return;
    var host=root,issued=++epoch,value=snapshot(),scope=key(value);busy=true;paint();
    try {
      var result=await api.request(endpoint(value)+'/bindings');
      if (!current(host,issued,scope)) return;
      saved=result;
      if (pendingBinding && saved.some(function (item) { return item.id===pendingBinding.request_id; })) pendingBinding=null;
      message=saved.length ? 'Vínculos recuperados.' : 'Sin vínculos guardados.';
    } catch (error) { if (current(host,issued,scope)) message=error.message || 'No se pudo recuperar el historial.'; }
    finally { if (current(host,issued,scope)) { busy=false;paint(); } }
  }
  async function saveBinding(event) {
    event.preventDefault();var value=snapshot();
    if (blocked() || !value.verified || value.chain!=='0x38' || !reviewedBody || !preview || Date.now()>=Date.parse(preview.expires_at)) return;
    if (pendingBinding) { message='Recupera el vínculo pendiente antes de crear otro.';paint();return; }
    var data=new FormData(root.querySelector('[data-dex-bot-bind-form]'));
    var body=Object.assign({},reviewedBody,{request_id:api.uuid(),deployment_id:String(data.get('deployment_id')),
      address:value.wallet,gas_budget_bnb:String(data.get('gas_budget_bnb')).trim().replace(',','.'),confirmation:'VINCULAR_BOT_DEX'});
    var host=root,issued=++epoch,scope=key(value);busy=true;pendingBinding=body;message='Verificando política y guardando procedencia…';paint();
    try {
      var result=await api.request(endpoint(value)+'/bindings',{method:'POST',body:JSON.stringify(body)});
      if (!current(host,issued,scope)) return;
      saved=[result].concat(saved.filter(function (item) { return item.id!==result.id; }));pendingBinding=null;
      message='Vínculo guardado.';
    } catch (error) {
      if (current(host,issued,scope)) {
        if (error.status>=400 && error.status<500) { pendingBinding=null;message=error.message; }
        else message=(error.message || 'Respuesta sin confirmar')+' · Recupera el vínculo antes de repetir.';
      }
    }
    finally { if (current(host,issued,scope)) { busy=false;paint(); } }
  }
  async function recoverBinding() {
    if (blocked() || !pendingBinding) return;
    var host=root,issued=++epoch,value=snapshot(),scope=key(value),body=pendingBinding;busy=true;paint();
    try {
      // Same immutable ID/body. A committed replay performs no RPC or PAPER
      // reads; a pre-commit failure may safely complete that same preparation.
      var result=await api.request(endpoint(value)+'/bindings',{method:'POST',body:JSON.stringify(body)});
      if (!current(host,issued,scope)) return;
      saved=[result].concat(saved.filter(function (item) { return item.id!==result.id; }));pendingBinding=null;
      message='Vínculo recuperado · Listo para activar.';
    } catch (error) { if (current(host,issued,scope)) message=error.message || 'Vínculo sin confirmar · conserva el mismo intento.'; }
    finally { if (current(host,issued,scope)) { busy=false;paint(); } }
  }
  async function stopBinding(identifier) {
    if (blocked() || !confirm('¿Retirar este vínculo y su autorización local de gas? No envía STOP a la cadena ni vende posiciones.')) return;
    var host=root,issued=++epoch,value=snapshot(),scope=key(value);busy=true;paint();
    try {
      var result=await api.request(endpoint(value)+'/bindings/'+encodeURIComponent(identifier)+'/stop',{method:'POST',body:JSON.stringify({confirmation:'RETIRAR_VINCULO_DEX'})});
      if (!current(host,issued,scope)) return;
      saved=saved.map(function (item) { return item.id===identifier ? result : item; });message='Vínculo retirado · Sin envío a cadena.';
    } catch (error) { if (current(host,issued,scope)) message=error.message || 'No se pudo confirmar la retirada.'; }
    finally { if (current(host,issued,scope)) { busy=false;paint(); } }
  }
  function clear(keepHandoff) {
    if (window.OrbisDexSetup) window.OrbisDexSetup.clear();
    if (window.OrbisDexGasFunding) window.OrbisDexGasFunding.clear();
    if (window.OrbisDexRuns) window.OrbisDexRuns.clear();
    if (window.OrbisDexGasReturn) window.OrbisDexGasReturn.clear();
    if(window.OrbisDexReceiptBooking)window.OrbisDexReceiptBooking.clear();
    if (!keepHandoff) handoff=null;
    ++epoch;identity="";busy=false;candidates=[];saved=[];pendingBinding=null;clearPreview();message="Consulta tus estrategias probadas en PAPER.";
    if (root && root.isConnected) { root.querySelector('[name="source"]').replaceChildren();root.querySelector('[name="research_validation_id"]').replaceChildren();root.querySelector('[name="deployment_id"]').replaceChildren();root.querySelector('[data-dex-bot-preview]').replaceChildren();root.querySelectorAll('form').forEach(function (form) { form.reset(); }); }
    paint();
  }
  function update() {
    var next=key(snapshot());
    if (identity!==next) {
      clear(true);identity=next;
      if (handoff && next && root.isConnected) load();
    }
    paint();
  }
  function prepare(source) {
    if (!root || !root.isConnected || !key(snapshot())) return false;
    handoff=source;
    load();
    return true;
  }
  function mount(element,client) {
    if (timer) clearTimeout(timer);
    root=element;api=client;identity="";++epoch;busy=false;candidates=[];saved=[];pendingBinding=null;preview=null;reviewedBody=null;timer=null;
    handoff=null;message="Consulta tus estrategias probadas en PAPER.";
    root.innerHTML='<details><summary data-help="Reproduce la versión LAB y su prueba PAPER de BNB/USDT. Prepara límites REAL independientes antes de vincular y activar el bot.">Bot DEX · Preparar</summary>'+
      '<div class="row" data-dex-bot-handoff hidden><span class="statusbadge off">PAPER → Uniswap v3</span>'+
      '<button type="button" class="ghost" data-dex-discard-source>Quitar selección</button></div>'+
      '<form><div class="row"><label class="field">Estrategia<select name="source" aria-label="Estrategia DEX"></select></label><button type="button" class="ghost" data-dex-bot-sources>Consultar PAPER</button></div>'+
      '<div class="row"><label class="field" data-help="Reserva histórica de la misma versión, anterior a PAPER. Se reproduce al revisar, vincular y activar. Estudia señales de MEXC; no incluye ejecuciones, gas ni costes DEX.">Reserva<select name="research_validation_id" required></select></label><a data-dex-research-link href="#/laboratorio">Validar</a></div>'+
      '<div class="dex-bot-fields"><label class="field">Entrada · USDT<input name="entry_usdt" inputmode="decimal" type="text" autocomplete="off" placeholder="10" required></label>'+
      '<label class="field">Presupuesto · USDT<input name="budget_usdt" inputmode="decimal" type="text" autocomplete="off" placeholder="100" required></label>'+
      '<label class="field">Exposición máx. · USDT<input name="exposure_usdt" inputmode="decimal" type="text" autocomplete="off" placeholder="100" required></label>'+
      '<label class="field" data-help="El límite de pérdidas realizadas acumuladas bloquea nuevas compras. Gas BNB y pérdidas no realizadas van aparte; no es un stop.">Pérdida realizada · USDT<input name="loss_usdt" inputmode="decimal" type="text" autocomplete="off" placeholder="10" required></label>'+
      '<label class="field" data-help="Cantidad acumulada máxima de WBNB que el ejecutor podrá comprar. Vender no repone esta cuota.">Cuota WBNB<input name="wbnb_quota" inputmode="decimal" type="text" autocomplete="off" placeholder="1" required></label>'+
      '<label class="field" data-dex-bot-monthly hidden>Límite mensual · USDT<input name="monthly_usdt" inputmode="decimal" type="text" autocomplete="off" placeholder="100"></label></div>'+
      '<button type="submit" class="ghost" data-dex-bot-review>Comprobar preparación</button></form>'+
      '<p data-dex-bot-status class="state muted" role="status"></p><div data-dex-bot-preview></div><div data-dex-owner></div>'+
      '<details data-dex-bot-binding><summary data-help="Guarda la estrategia probada junto al ejecutor y su política finalizada. Requiere wallet BNB verificada. No firma, financia gas ni arranca el bot.">Vincular ejecutor</summary>'+
      '<form data-dex-bot-bind-form class="row"><button type="button" class="ghost" data-dex-bot-executors>Consultar ejecutores</button><label class="field">Ejecutor<select name="deployment_id" required></select></label>'+
      '<label class="field" data-help="Presupuesto máximo local para gas del operador, incluida su reserva STOP. No transfiere BNB ni acredita saldo.">Gas autorizado · BNB<input type="text" name="gas_budget_bnb" inputmode="decimal" autocomplete="off" placeholder="0.002" required></label>'+
      '<button type="submit" class="ghost" data-dex-bot-save>Guardar vínculo</button></form></details>'+
      '<button type="button" class="ghost" data-dex-bot-recover hidden>Recuperar vínculo</button><div data-dex-gas></div><div data-dex-runs></div><div data-dex-return></div><div data-dex-booking></div>'+
      '<details data-dex-bot-history-panel><summary>Vínculos guardados</summary><button type="button" class="ghost" data-dex-bot-history-refresh>Actualizar vínculos</button><div data-dex-bot-history></div></details></details>';
    root.querySelector('[data-dex-bot-sources]').addEventListener('click',load);
    root.querySelector('[data-dex-discard-source]').addEventListener('click',function () {
      if (blocked()) return;
      handoff=null;candidates=[];clearPreview();
      root.querySelector('form').reset();
      root.querySelector('[name="source"]').replaceChildren();researchChoices();
      message="Consulta tus estrategias probadas en PAPER.";paint();
    });
    root.querySelector('form').addEventListener('submit',review);
    root.querySelector('[data-dex-bot-executors]').addEventListener('click',executors);
    root.querySelector('[data-dex-bot-bind-form]').addEventListener('submit',saveBinding);
    root.querySelector('[data-dex-bot-recover]').addEventListener('click',recoverBinding);
    root.querySelector('[data-dex-bot-history-refresh]').addEventListener('click',history);
    root.querySelector('[data-dex-bot-history-panel]').addEventListener('toggle',function () { if (this.open) history(); });
    root.querySelector('form').addEventListener('input',function () { clearPreview();message="Parámetros cambiados · Comprueba la preparación.";paint(); });
    root.querySelector('[name="source"]').addEventListener('change',function () { var item=selected();clearPreview();researchChoices();message=item && !item.ready ? item.reason : "Comprueba la preparación con tus límites.";paint(); });
    if (window.OrbisDexSetup) window.OrbisDexSetup.mount(root.querySelector('[data-dex-owner]'),{
      request:api.request,uuid:api.uuid,snapshot:api.snapshot,review:function () { return preview; },
      parentBusy:function () { return busy||gasBusy()||runBusy()||returnBusy(); },onBusyChange:paint
    });
    if(window.OrbisDexGasFunding)window.OrbisDexGasFunding.mount(root.querySelector('[data-dex-gas]'),{
      request:api.request,uuid:api.uuid,snapshot:api.snapshot,
      blocked:function(){return busy||runBusy()||returnBusy()||!!(window.OrbisDexSetup&&window.OrbisDexSetup.isBusy());},onBusyChange:paint
    });
    if(window.OrbisDexRuns)window.OrbisDexRuns.mount(root.querySelector('[data-dex-runs]'),{
      request:api.request,uuid:api.uuid,snapshot:api.snapshot,
      blocked:function(){return busy||gasBusy()||returnBusy()||!!(window.OrbisDexSetup&&window.OrbisDexSetup.isBusy());},onBusyChange:paint
    });
    if(window.OrbisDexGasReturn)window.OrbisDexGasReturn.mount(root.querySelector('[data-dex-return]'),{
      request:api.request,uuid:api.uuid,snapshot:api.snapshot,
      blocked:function(){return busy||gasBusy()||runBusy()||!!(window.OrbisDexSetup&&window.OrbisDexSetup.isBusy());},onBusyChange:paint
    });
    if(window.OrbisDexReceiptBooking)window.OrbisDexReceiptBooking.mount(root.querySelector('[data-dex-booking]'),{request:api.request,snapshot:api.snapshot,onBooked:api.onBooked});
    update();
  }
  window.OrbisWalletDexBot={mount:mount,prepare:prepare,update:update,clear:clear};
})();
