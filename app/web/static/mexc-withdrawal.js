/* Retirada puntual USDT/BSC. Nunca comparte la clave con el bot ni reenvía. */
(function () {
  "use strict";
  var root,api,epoch = 0,identity = "",busy = false,plan = null,payload = null,message = "Verifica control y consulta USDT en BNB Chain.";
  var historyEpoch = 0, maxEstimate = null, historyFocus;
  function snapshot() { return api && api.snapshot(); }
  function key(source) { return JSON.stringify(source && [source.accountId,source.wallet,source.chain,source.generation,source.verified,source.token && source.token.contract,source.token && source.token.decimals]); }
  function valid(source) { return source && source.accountId && source.provider && source.wallet && source.chain === "0x38" && source.verified && source.token; }
  function base(source) { return "/accounts/"+encodeURIComponent(source.accountId)+"/mexc-withdrawals"; }
  function current(host,issued,sourceKey) { return root === host && root.isConnected && epoch === issued && key(snapshot()) === sourceKey; }
  function clearSecrets(form) { if (form) form.querySelectorAll("input[type=password]").forEach(function (input) { input.value = ""; }); }
  function compact(raw) { return raw.includes(".") && !/[eE]/.test(raw) ? raw.replace(/0+$/,"").replace(/\.$/,"") : raw; }
  async function boundedRequest(localApi,path,options,milliseconds,passive) {
    var controller = new AbortController(),timer = setTimeout(function () { controller.abort(); },milliseconds);
    try { return await localApi.request(path,Object.assign({},options || {},{signal:controller.signal}),!!passive); }
    finally { clearTimeout(timer); }
  }
  function paint() {
    if (!root || !root.isConnected) return;
    var source = snapshot(); root.hidden = !source || !source.accountId;
    root.querySelector("[data-withdraw-prepare]").disabled = busy || !!plan || !valid(source);
    root.querySelector("[data-withdraw-estimate]").disabled = busy || !!plan || !valid(source) || source.hidden;
    root.querySelector("[data-withdraw-stop]").disabled = busy;
    root.querySelector("[data-withdraw-refresh]").disabled = busy;
    root.querySelectorAll("[data-withdraw-check-bnb]").forEach(function (button) {
      button.disabled = busy || !source || !source.wallet || source.chain !== "0x38" || !source.verified;
    });
    root.querySelectorAll("[data-withdraw-review]").forEach(function (button) {
      button.disabled = busy || !!plan || !valid(source);
    });
    root.querySelectorAll("[data-withdraw-plan] input").forEach(function (input) { input.disabled = busy || !!plan; });
    var confirm = root.querySelector("[data-withdraw-confirm]");
    confirm.hidden = !plan || plan.status !== "PREPARED" || Date.now() >= Date.parse(plan.expires_at);
    confirm.querySelectorAll("input,button").forEach(function (input) { input.disabled = busy || !valid(source); });
    root.querySelector("[data-withdraw-state]").textContent = message;
    root.querySelectorAll("[data-withdraw-private]").forEach(function (item) { item.textContent = source.hidden ? item.orbisMasked : item.orbisVisible; });
    var preview = root.querySelector("[data-withdraw-preview]"); preview.replaceChildren();
    var displayed = plan || maxEstimate;
    if (displayed) {
      var p = document.createElement("p"); p.className = "muted";
      p.textContent = source.hidden ? "Importes ocultos" :
        compact(displayed.amount)+" USDT · comisión observada "+compact(displayed.observed_fee)+" · previsión de débito hasta "+compact(displayed.max_debit)+" · recepción desde "+compact(displayed.min_receive);
      preview.append(p);
      if (source.wallet && plan && plan.status === "PREPARED") { var address = document.createElement("code"); address.className = "real-wallet-deposit-address"; address.textContent = source.wallet; preview.append(address); }
    }
  }
  async function history() {
    var source = snapshot(); if (busy || !source || !source.accountId || !root || !root.isConnected) return;
    var host = root,issued = epoch,sourceKey = key(source),localApi = api,reading = ++historyEpoch;
    var focus = historyFocus.request(), path = base(source)+(focus ? focus.query : "");
    try {
      var rows;
      for (var attempt = 0; attempt < 2; attempt += 1) {
        try { rows = await boundedRequest(localApi,path,null,2000,true); break; }
        catch (error) {
          if (!current(host,issued,sourceKey) || reading !== historyEpoch) return;
          if (attempt === 1) throw error;
        }
      }
      if (!current(host,issued,sourceKey) || reading !== historyEpoch) return;
      var list = root.querySelector("[data-withdraw-history]"); list.replaceChildren();
      var labels = {PREPARED:"Preparado",CANCELLED:"Plan descartado",SUBMISSION_UNKNOWN:"Resultado incierto · no reenviar",ACKNOWLEDGED:"Solicitud aceptada · recepción pendiente"};
      rows.forEach(function (row) {
        if (plan && plan.id === row.id) {
          if (plan.status !== "ACKNOWLEDGED" && row.status === "ACKNOWLEDGED") message = "Solicitud aceptada por MEXC · recepción y contabilidad pendientes.";
          plan = row;
        }
        var group = document.createElement("div");
        group.dataset.withdrawItem = group.dataset.transferId = row.id;
        group.tabIndex = -1; list.append(group);
        var p = document.createElement("p"); p.className = "muted";
        var prefix = row.id.slice(0,8)+" · "+row.created_at.slice(0,16).replace("T"," ")+" UTC · ";
        var statusLabel = row.settlement_status === "REJECTED_CONFIRMED" ? "Rechazo revisado · sin cargo" : row.booked ? "Recibido · contabilizado" : row.wallet_received ? "Recibido · sin contabilizar" : labels[row.status] || "Revisar";
        var rpcLabel = row.chain_source === "ORBIS_RPC_OBSERVED" ? " · RPC de ORBIS" : "";
        var suffix = " · "+statusLabel+rpcLabel+(row.status === "PREPARED" && row.expired ? " · caducado" : "");
        if (row.wallet_received) suffix += source.hidden ? "" : " · neto "+compact(row.received)+" · comisión "+compact(row.actual_fee);
        p.dataset.withdrawPrivate = ""; p.orbisVisible = prefix+compact(row.amount)+" USDT"+suffix; p.orbisMasked = prefix+"Importe oculto"+suffix;
        if (row.wallet_received) p.orbisMasked = prefix+"Importe oculto · "+statusLabel+rpcLabel;
        p.textContent = source.hidden ? p.orbisMasked : p.orbisVisible; group.append(p);
        if (row.status === "PREPARED" && !row.expired) {
          var review = document.createElement("button"); review.type = "button"; review.className = "ghost";
          review.textContent = "Revisar plan"; review.dataset.withdrawReview = row.id;
          review.orbisPlanDigest = row.plan_digest;
          review.dataset.help = "Requiere la wallet de destino verificada y MEXC View. Revisa el plan sin solicitar la retirada.";
          group.append(review);
        }
        if (row.status === "PREPARED") { var cancel = document.createElement("button"); cancel.type = "button"; cancel.className = "ghost";
          cancel.textContent = "Descartar plan"; cancel.dataset.withdrawCancel = row.id; cancel.disabled = busy; group.append(cancel); }
        if (["SUBMISSION_UNKNOWN","ACKNOWLEDGED"].includes(row.status) && !row.resolved) {
          var check = document.createElement("button"); check.type = "button"; check.className = "ghost"; check.textContent = "Comprobar recepción";
          check.dataset.withdrawCheck = row.id; check.disabled = busy;
          check.dataset.help = "Comprueba el neto recibido con View de la cuenta original y un recibo BNB finalizado. Puedes usar una clave renovada."; group.append(check);
          if (!row.wallet_received) {
            var bnb = document.createElement("button"); bnb.type = "button"; bnb.className = "ghost";
            bnb.textContent = "Consultar en BNB"; bnb.dataset.withdrawCheckBnb = row.id;
            bnb.dataset.help = "Comprueba en el RPC de ORBIS el hash de la retirada confirmada por MEXC. Requiere verificar la wallet de destino.";
            group.append(bnb);
          }
        }
        if (row.wallet_received && !row.booked) {
          var book = document.createElement("button"); book.type = "button"; book.className = "ghost"; book.textContent = "Contabilizar retirada";
          book.dataset.withdrawBook = row.id; book.disabled = busy || !source.wallet; group.append(book);
        }
        if (row.can_review_rejected) {
          var rejected = document.createElement("button"); rejected.type = "button"; rejected.className = "ghost";
          rejected.textContent = "Revisar rechazo"; rejected.dataset.withdrawReject = row.id; rejected.disabled = busy; group.append(rejected);
        }
      });
      if (plan && (plan.status === "CANCELLED" || plan.resolved)) {
        if (plan.booked) message = "Retirada contabilizada · neto y comisión separados. STOP se mantiene; revisa la apertura antes de operar.";
        else if (plan.resolved) message = "Rechazo revisado · sin cargo observado. STOP se mantiene; esta solicitud no se reutiliza.";
        plan = null; payload = null;
      }
      paint();
      historyFocus.finish(focus);
    } catch (_) {
      if (current(host,issued,sourceKey) && reading === historyEpoch) {
        root.querySelector("[data-withdraw-history]").textContent = "Historial de retiradas no verificable. Revisa la sesión REAL.";
        historyFocus.finish(focus);
      }
    }
  }
  function focusHistory(id) {
    return !busy && root && root.isConnected && !root.hidden && historyFocus.open(id, history);
  }
  async function stop() {
    if (busy) return;
    var source = snapshot(); if (!source || !source.accountId || !confirm("Detener nuevas órdenes y bots de esta cuenta antes de retirar. No cierra posiciones ni cancela órdenes ya enviadas. ¿Continuar?")) return;
    var host = root,issued = epoch,sourceKey = key(source),localApi = api;
    historyEpoch += 1; busy = true; paint();
    try {
      await localApi.request("/accounts/"+encodeURIComponent(source.accountId)+"/mexc-stop",{method:"POST",body:JSON.stringify({confirmation:"DETENER"})});
      if (current(host,issued,sourceKey)) message = "Operativa detenida · revisa órdenes pendientes y prepara la retirada.";
    } catch (error) { if (current(host,issued,sourceKey)) message = error.message || "No se confirmó la parada."; }
    finally { busy = false; paint(); }
  }
  async function estimate() {
    var source = snapshot(); if (busy || plan || !valid(source) || source.hidden) return;
    var host = root,issued = epoch,sourceKey = key(source),localApi = api;
    var form = host.querySelector("[data-withdraw-plan]");
    var body = {address:source.wallet,token_contract:source.token.contract,decimals:source.token.decimals,
      max_fee:form.elements.max_fee.value.trim() || null};
    busy = true; maxEstimate = null; message = "Calculando máximo…"; paint();
    try {
      var result = await localApi.request(base(source)+"/estimate-max",{method:"POST",body:JSON.stringify(body)});
      if (!current(host,issued,sourceKey) || snapshot().hidden) return;
      if (result.account_id !== source.accountId || result.token_contract !== source.token.contract.toLowerCase() ||
          result.chain_id !== "0x38" || result.asset !== "USDT" || result.fee_limit_exchange_enforced !== false)
        throw new Error("Cálculo de retirada no verificable.");
      form.elements.amount.value = result.amount;
      form.elements.max_fee.value = result.max_fee;
      payload = null; maxEstimate = result; message = "Máximo calculado · revisa y prepara.";
    } catch (error) { if (current(host,issued,sourceKey)) message = error.message || "No se pudo calcular el máximo."; }
    finally { busy = false; paint(); }
  }
  function showPrepared(prepared, text) {
    plan = prepared;
    message = text;
    setTimeout(function () {
      if (plan && plan.id === prepared.id) paint();
    }, Math.max(1, Date.parse(prepared.expires_at)-Date.now()+1));
  }
  async function review(event) {
    var button = event.target.closest("[data-withdraw-review]"), source = snapshot();
    if (!button || busy || plan || !valid(source)) return;
    var host = root, issued = epoch, sourceKey = key(source), localApi = api;
    var id = button.dataset.withdrawReview, digest = button.orbisPlanDigest;
    historyEpoch += 1; busy = true; message = "Revisando el plan guardado…"; paint();
    try {
      var reviewed = await localApi.request(base(source)+"/"+encodeURIComponent(id)+"/review", {
        method:"POST",body:JSON.stringify({address:source.wallet,token_contract:source.token.contract,
          decimals:source.token.decimals,plan_digest:digest})
      });
      if (!current(host,issued,sourceKey)) return;
      if (reviewed.id !== id || reviewed.account_id !== source.accountId || reviewed.plan_digest !== digest ||
          reviewed.status !== "PREPARED" || reviewed.expired || Date.now() >= Date.parse(reviewed.expires_at) ||
          reviewed.token_contract !== source.token.contract.toLowerCase() || reviewed.chain_id !== "0x38" ||
          reviewed.fee_limit_exchange_enforced !== false) throw new Error("El plan ya no está disponible para revisión.");
      var form = host.querySelector("[data-withdraw-plan]");
      form.elements.amount.value = compact(reviewed.amount);
      form.elements.max_fee.value = compact(reviewed.max_fee);
      clearSecrets(host);
      payload = null; maxEstimate = null;
      showPrepared(reviewed, "Plan revisado · confirma para solicitar.");
    } catch (error) {
      if (current(host,issued,sourceKey)) message = error.message || "Plan guardado no verificable.";
    } finally {
      busy = false;
      paint();
      if (current(host,issued,sourceKey) && plan && plan.id === id) {
        host.querySelector("[data-withdraw-confirm] [name=api_key]").focus();
      }
    }
  }
  async function prepare(event) {
    event.preventDefault(); var source = snapshot(); if (busy || plan || !valid(source)) return;
    var form = event.currentTarget,host = root,issued = epoch,sourceKey = key(source),localApi = api;
    historyEpoch += 1; busy = true; maxEstimate = null; message = "Revisando ruta, destino guardado, saldo y parada…"; paint();
    try {
      if (!payload) payload = {request_id:localApi.uuid(),address:source.wallet,token_contract:source.token.contract,
        decimals:source.token.decimals,amount:form.elements.amount.value.trim(),max_fee:form.elements.max_fee.value.trim()};
      var prepared = await localApi.request(base(source),{method:"POST",body:JSON.stringify(payload)});
      if (!current(host,issued,sourceKey)) return;
      if (prepared.id !== payload.request_id || prepared.token_contract !== source.token.contract.toLowerCase() ||
          prepared.fee_limit_exchange_enforced !== false || prepared.chain_id !== "0x38" || prepared.status !== "PREPARED")
        throw new Error("El plan no coincide o ya se utilizó. Consulta el historial; no se enviará.");
      showPrepared(prepared, "Plan listo · la clave de retirada sólo se utiliza al confirmar.");
    } catch (error) { if (current(host,issued,sourceKey)) message = error.message || "Plan no verificable; consulta el historial."; }
    finally { busy = false; paint(); history(); }
  }
  async function submit(event) {
    event.preventDefault(); var source = snapshot(); if (busy || !plan || plan.status !== "PREPARED" || !valid(source)) return;
    var original = plan,form = event.currentTarget,host = root,issued = epoch,sourceKey = key(source),localApi = api;
    if (Date.now() >= Date.parse(original.expires_at)) { message = "Plan caducado; descártalo antes de preparar otro."; paint(); return; }
    if (!confirm("RETIRADA REAL · USDT en BNB Chain\nDestino: "+source.wallet+"\nImporte API: "+original.amount+" USDT\nComisión observada: "+original.observed_fee+
      " USDT. El tope es una comprobación local previa: MEXC no permite fijar una comisión máxima en la API y puede cambiarla. Las previsiones no son una garantía del exchange.\nLa operativa seguirá detenida hasta comprobar la recepción y contabilizar. No uses esta cuenta desde otro cliente durante la retirada. Puede requerir verificaciones en MEXC. ¿Solicitar una sola vez?")) return;
    var body = {address:source.wallet,plan_digest:original.plan_digest,api_key:form.elements.api_key.value.trim(),
      secret_key:form.elements.secret_key.value.trim(),confirmation:"SOLICITAR_RETIRADA_REAL",fee_quote_only:true};
    historyEpoch += 1; busy = true; original.status = "SUBMISSION_UNKNOWN"; message = "Solicitud en curso · no repitas ni recargues."; paint();
    try {
      var result = await localApi.request(base(source)+"/"+encodeURIComponent(original.id)+"/submit",{method:"POST",body:JSON.stringify(body)});
      if (!current(host,issued,sourceKey)) return;
      plan = result; message = result.status === "ACKNOWLEDGED" ? "Solicitud aceptada por MEXC · recepción y contabilidad pendientes." :
        "Resultado incierto · revisa MEXC. No se reenviará ni se dará por recibida la retirada.";
    } catch (error) { if (current(host,issued,sourceKey)) message = error.message && !/failed to fetch|networkerror|load failed/i.test(error.message) ? error.message : "Respuesta no confirmada · consulta el historial y MEXC; no reenvíes."; }
    finally { body.api_key = body.secret_key = ""; clearSecrets(form); busy = false; paint(); history(); }
  }
  async function cancel(event) {
    var button = event.target.closest("[data-withdraw-cancel]"); if (!button || busy) return;
    var source = snapshot(),host = root,issued = epoch,sourceKey = key(source),localApi = api;
    historyEpoch += 1; busy = true; paint();
    try {
      await localApi.request(base(source)+"/"+encodeURIComponent(button.dataset.withdrawCancel)+"/cancel",
        {method:"POST",body:JSON.stringify({confirmation:"CANCELAR_PLAN_SIN_ENVIO"})});
      if (current(host,issued,sourceKey)) { if (plan && plan.id === button.dataset.withdrawCancel) plan = null; payload = null; message = "Plan descartado · ninguna solicitud de retirada enviada por ese plan."; }
    } catch (error) { if (current(host,issued,sourceKey)) message = error.message || "No se puede descartar una solicitud ya intentada."; }
    finally { busy = false; paint(); history(); }
  }
  async function rpc(provider,method,params) {
    var timer;
    try { return await Promise.race([Promise.resolve().then(function () { return provider.request({method:method,params:params}); }),
      new Promise(function (_resolve,reject) { timer = setTimeout(function () { reject(new Error("RPC sin respuesta; vuelve a consultar, no reenvíes dinero.")); },15000); })]); }
    finally { clearTimeout(timer); }
  }
  function withdrawalStatus(status) {
    if (status === "MEXC_REJECTED_NEEDS_REVIEW") return "MEXC comunica rechazo/cancelación · revisa si hubo cargo.";
    if (status === "BNB_PENDING") return "Recibo pendiente en BNB · vuelve a consultar.";
    return "MEXC aún no confirma esta retirada. Sin recepción ni asiento; no se reenvía.";
  }
  async function checkReceipt(event) {
    var button = event.target.closest("[data-withdraw-check], [data-withdraw-check-bnb]"); if (!button || busy) return;
    var source = snapshot(),host = root,issued = epoch,sourceKey = key(source),localApi = api;
    if (!source || !source.accountId) return;
    var server = button.hasAttribute("data-withdraw-check-bnb");
    var id = server ? button.dataset.withdrawCheckBnb : button.dataset.withdrawCheck;
    if (server && (!source.wallet || source.chain !== "0x38" || !source.verified)) return;
    var path = base(source)+"/"+encodeURIComponent(id);
    historyEpoch += 1; busy = true; message = server ? "Consultando MEXC y BNB…" : "Buscando la retirada en MEXC…"; paint();
    try {
      if (server) {
        var received = await boundedRequest(localApi,path+"/receipt-bnb",
          {method:"POST",body:JSON.stringify({address:source.wallet})},35000);
        if (current(host,issued,sourceKey)) message = received.wallet_received ?
          "Recepción comprobada · "+(received.chain_source === "ORBIS_RPC_OBSERVED" ? "RPC de ORBIS" : "RPC de la wallet") :
          withdrawalStatus(received.status);
        return;
      }
      var observed = await localApi.request(path+"/observe",{method:"POST"});
      if (!current(host,issued,sourceKey)) return;
      if (observed.status !== "MEXC_SUCCESS_PENDING_RECEIPT") {
        message = withdrawalStatus(observed.status); return;
      }
      if (!source.provider || !source.wallet || source.chain !== "0x38") { message = "MEXC confirma la solicitud · conecta la wallet original en BNB Chain para comprobar la recepción."; return; }
      if (!/^0x[0-9a-fA-F]{64}$/.test(observed.tx_hash || "")) throw new Error("MEXC no ofrece un hash inequívoco de esta retirada.");
      if (BigInt(await rpc(source.provider,"eth_chainId",[])) !== 56n) throw new Error("Selecciona BNB Chain en la wallet.");
      var pair = await Promise.all([rpc(source.provider,"eth_getTransactionByHash",[observed.tx_hash]),rpc(source.provider,"eth_getTransactionReceipt",[observed.tx_hash])]);
      if (!pair[0] || !pair[1] || !/^0x[0-9a-fA-F]+$/.test(pair[0].blockNumber || "")) throw new Error("Recibo aún no disponible o sin minar. No se repite la retirada.");
      var blocks = await Promise.all([rpc(source.provider,"eth_getBlockByNumber",[pair[0].blockNumber,false]),rpc(source.provider,"eth_getBlockByNumber",["finalized",false])]);
      if (BigInt(await rpc(source.provider,"eth_chainId",[])) !== 56n || !current(host,issued,sourceKey)) throw new Error("Cambió la red o sesión; vuelve a consultar.");
      var result = await localApi.request(path+"/receipt",{method:"POST",body:JSON.stringify({address:source.wallet,
        evidence:{chain_id:"0x38",tx_hash:observed.tx_hash,transaction:pair[0],receipt:pair[1],block:blocks[0],finalized:blocks[1]}})});
      if (current(host,issued,sourceKey)) message = result.wallet_received ? "Recepción comprobada · MEXC + RPC wallet observado. Revisa neto/comisión y contabiliza; STOP se mantiene." : "Recepción no confirmada.";
    } catch (error) { if (current(host,issued,sourceKey)) message = error.message || "No se pudo comprobar la recepción. No se reenvía."; }
    finally { busy = false; paint(); history(); }
  }
  async function showBooking(event) {
    var button = event.target.closest("[data-withdraw-book]"); if (!button || busy) return;
    var source = snapshot(),host = root,issued = epoch,sourceKey = key(source),localApi = api,id = button.dataset.withdrawBook;
    if (!source.wallet) return;
    historyEpoch += 1; busy = true; paint();
    try {
      var options = await localApi.request(base(source)+"/"+encodeURIComponent(id)+"/booking-options",{method:"POST",body:JSON.stringify({address:source.wallet})});
      if (!current(host,issued,sourceKey)) return;
      root.querySelectorAll("[data-withdraw-booking]").forEach(function (old) { old.remove(); });
      var form = document.createElement("form"); form.dataset.withdrawBooking = id; form.className = "row"; form.orbisOptions = options;
      var label = document.createElement("label"); label.className = "field"; label.textContent = "Destino contable";
      var select = document.createElement("select"); select.name = "destination"; select.required = true;
      function option(value,text) { var item = document.createElement("option"); item.value = value; item.textContent = text; select.append(item); }
      option("","Seleccionar…");
      options.accounts.forEach(function (item) { option("account:"+item.id,"Traslado interno · "+item.name); });
      options.events.forEach(function (item) { option("event:"+item.id,"Asiento ya registrado · "+item.occurred_at.slice(0,16).replace("T"," ")); });
      if (options.external_available) option("external","Fuera de ORBIS · retirada de capital");
      label.append(select); form.append(label);
      if (options.fee_filter_exceeded) {
        var warning = document.createElement("label"); warning.className = "field";
        var checkbox = document.createElement("input"); checkbox.type = "checkbox"; checkbox.name = "fee_override"; checkbox.required = true;
        warning.append(checkbox,document.createTextNode("Registrar comisión real superior al filtro previo")); form.append(warning);
      }
      var submit = document.createElement("button"); submit.type = "submit"; submit.textContent = "Registrar retirada"; form.append(submit);
      root.querySelector("[data-withdraw-history]").append(form);
      if (options.linked_to_source_account) message = "La wallet está vinculada al mismo libro MEXC. Separa las cuentas antes de registrar el traslado.";
    } catch (error) { if (current(host,issued,sourceKey)) message = error.message || "Opciones contables no verificables."; }
    finally { busy = false; paint(); }
  }
  async function bookReceipt(event) {
    var form = event.target.closest("[data-withdraw-booking]"); if (!form) return;
    event.preventDefault(); if (busy) return;
    var source = snapshot(),host = root,issued = epoch,sourceKey = key(source),localApi = api;
    var selected = form.elements.destination.value; if (!selected || !source.wallet) return;
    if (!confirm("Registrar neto y comisión reales, sin mover dinero. Un traslado interno conserva el capital; una retirada fuera de ORBIS es salida de capital, no pérdida del principal. STOP no se levanta automáticamente. ¿Confirmar?")) return;
    var mode = selected === "external" ? "NEW_EXTERNAL" : selected.startsWith("account:") ? "NEW_INTERNAL" : "EXISTING_EVENT";
    var eventId = mode === "EXISTING_EVENT" ? selected.slice(6) : null;
    var existing = eventId && form.orbisOptions.events.find(function (item) { return item.id === eventId; });
    var body = {address:source.wallet,mode:mode,target_account_id:mode === "NEW_INTERNAL" ? selected.slice(8) : existing && existing.target_account_id || null,
      event_id:eventId,accept_fee_override:!!(form.elements.fee_override && form.elements.fee_override.checked),confirmation:"CONTABILIZAR_RETIRADA_RECIBIDA"};
    historyEpoch += 1; busy = true; paint();
    try {
      var result = await boundedRequest(localApi,base(source)+"/"+encodeURIComponent(form.dataset.withdrawBooking)+"/book",{method:"POST",body:JSON.stringify(body)},3000);
      if (current(host,issued,sourceKey)) message = result.booked ? "Retirada contabilizada · neto y comisión separados, sin duplicados. Revisa la apertura antes de volver a operar." : "No contabilizada.";
    } catch (error) { if (current(host,issued,sourceKey)) message = error.name === "AbortError" || /failed to fetch|networkerror|load failed/i.test(error.message || "") ? "Respuesta no confirmada · consultando registro." : error.message || "No se pudo contabilizar."; }
    finally {
      busy = false; paint(); await history();
      if (current(host,issued,sourceKey) && localApi.onBooked) localApi.onBooked();
    }
  }
  async function reviewRejected(event) {
    var button = event.target.closest("[data-withdraw-reject]"); if (!button || busy) return;
    if (!confirm("Volver a verificar rechazo/cancelación MEXC, sin hash ni comisión y sin cargo en USDT. Cerrar el seguimiento no autoriza reenviar esta solicitud ni reanuda el bot. ¿Continuar?")) return;
    var source = snapshot(),host = root,issued = epoch,sourceKey = key(source),localApi = api;
    historyEpoch += 1; busy = true; paint();
    try {
      var result = await localApi.request(base(source)+"/"+encodeURIComponent(button.dataset.withdrawReject)+"/resolve-rejected",{method:"POST",body:JSON.stringify({confirmation:"CONFIRMAR_RECHAZO_SIN_CARGO"})});
      if (current(host,issued,sourceKey)) message = result.resolved ? "Rechazo revisado según MEXC y saldo · sin cargo observado. La solicitud no se reutiliza; STOP se mantiene." : "No resuelto.";
    } catch (error) { if (current(host,issued,sourceKey)) message = error.message || "El rechazo necesita más revisión."; }
    finally { busy = false; paint(); history(); }
  }
  function update() {
    if (!root || !root.isConnected) return;
    var next = key(snapshot());
    if (next !== identity) { identity = next; epoch += 1; plan = null; payload = null; maxEstimate = null; clearSecrets(root); historyFocus.reset();
      root.querySelector("[data-withdraw-plan]").reset();
      message = "Verifica control y consulta USDT en BNB Chain."; }
    paint();
  }
  function mount(element,client) {
    root = element; api = client; epoch += 1; identity = ""; plan = null; payload = null; maxEstimate = null;
    root.innerHTML = '<details><summary data-help="Retirada USDT/BSC hacia la wallet conectada. Requiere destino previamente guardado en MEXC, parada y permiso independiente. No se activa con el bot.">Retirar a wallet · BNB</summary>'+
      '<button type="button" class="ghost" data-withdraw-stop>Parar operativa</button><form class="row" data-withdraw-plan>'+
      '<label class="field">USDT<input name="amount" inputmode="decimal" autocomplete="off" required></label>'+
      '<label class="field" data-help="Filtro previo de la comisión observada en MEXC. No es un límite que el exchange garantice: la API no acepta un tope de comisión.">Filtro de comisión · USDT<input name="max_fee" inputmode="decimal" autocomplete="off" required></label>'+
      '<button type="button" class="ghost" data-withdraw-estimate data-help="Calcula el máximo con el saldo USDT conciliado, el filtro y los límites de BNB Chain.">Calcular máximo</button>'+
      '<button type="submit" data-withdraw-prepare>Preparar retirada</button></form><div data-withdraw-state class="state muted" role="status"></div><div data-withdraw-preview></div>'+
      '<form data-withdraw-confirm class="row" hidden><label class="field">API de retirada<input type="password" name="api_key" autocomplete="new-password" minlength="8" maxlength="128" required></label>'+
      '<label class="field">Secret de retirada<input type="password" name="secret_key" autocomplete="new-password" minlength="8" maxlength="256" required></label>'+
      '<button type="submit">Solicitar retirada</button></form><button type="button" class="ghost" data-withdraw-refresh>Consultar retiradas</button><div data-withdraw-history></div></details>';
    historyFocus = window.OrbisTransferHistoryFocus.create(root.querySelector("details"),
      root.querySelector("[data-withdraw-history]"));
    root.querySelector("[data-withdraw-stop]").addEventListener("click",stop);
    root.querySelector("[data-withdraw-estimate]").addEventListener("click",estimate);
    root.querySelector("[data-withdraw-plan]").addEventListener("submit",prepare);
    root.querySelector("[data-withdraw-confirm]").addEventListener("submit",submit);
    root.querySelector("[data-withdraw-history]").addEventListener("click",review);
    root.querySelector("[data-withdraw-history]").addEventListener("click",cancel);
    root.querySelector("[data-withdraw-history]").addEventListener("click",checkReceipt);
    root.querySelector("[data-withdraw-history]").addEventListener("click",showBooking);
    root.querySelector("[data-withdraw-history]").addEventListener("click",reviewRejected);
    root.querySelector("[data-withdraw-history]").addEventListener("submit",bookReceipt);
    root.querySelector("[data-withdraw-refresh]").addEventListener("click",history);
    root.querySelector("[data-withdraw-plan]").addEventListener("input",function () {
      if (plan || busy) return;
      payload = null;
      if (maxEstimate) { maxEstimate = null; message = "Revisa el importe y prepara."; paint(); }
    });
    root.querySelector("details").addEventListener("toggle",function () { if (this.open) history(); });
    update();
  }
  window.addEventListener("beforeunload",function (event) { if (busy) { event.preventDefault(); event.returnValue = ""; } });
  window.OrbisMexcWithdrawal = {mount:mount,update:update,focusHistory:focusHistory};
})();
