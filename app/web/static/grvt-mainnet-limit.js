/* Perpetuo LIMIT · revisión, activación y recuperación de un intento. */
(function () {
  "use strict";
  var base = "/exchanges/grvt/mainnet/bots/perp/limit";
  var root, api, timer, epoch = 0, busy = false, reading = false, review, uncertain;
  var items = [], job, rendered = "", historyOffset = 0, sourceOffset = 0;
  var historyMore = false, sourceMore = false, selectedAttempt, fixedResume;
  var handoffId = null, sourceSequence = 0;
  var statuses = {WARMING:"Preparando",WAITING_SIGNAL:"Esperando señal",SENDING:"Enviando entrada",
    VERIFYING_ORDER:"Verificando orden",WAIT_ENTRY:"Orden en espera",WAIT_TERMINAL:"Verificando cancelación",
    HOLD:"Posición abierta",VERIFYING_EXIT:"Verificando salida",COMPLETE:"Conciliado",REVIEW:"Revisión necesaria",
    INTERRUPTED:"Interrumpido",STOPPED:"Detenido",NO_SIGNAL:"Plazo sin señal",DATA_GAP:"Datos interrumpidos",
    ABANDONED:"Reserva liberada"};
  var reasons = {EXIT_RULE:"Regla de salida",TIME_LIMIT:"Permanencia",MAX_EXPOSURE_TIME:"Plazo de posición",
    STOP_OR_TRAILING:"Stop / trailing",TAKE_PROFIT:"Objetivo",DATA_GAP:"Datos interrumpidos",
    DATA_STALE:"Cotización antigua",ENTRY_LIMIT_BREACH:"Límite de entrada",PARTIAL_ENTRY:"Entrada parcial",
    ENTRY_EXPIRED:"Orden vencida",ENTRY_TERMINAL_EMPTY:"Sin ejecución",CANCEL_RECORDED:"Cancelación registrada"};

  function q(selector) { return root.querySelector(selector); }
  function field(name) { return q('[name="limit_' + name + '"]'); }
  function el(tag, text, cls) { var node = document.createElement(tag); node.textContent = text; if (cls) node.className = cls; return node; }
  function say(text, error) {
    var node = q("[data-limit-notice]"); node.textContent = text; node.className = "state " + (error ? "error" : "muted");
  }
  function valid(host, issued, scope) {
    return root === host && host.isConnected && epoch === issued && api.context() === scope;
  }
  function matchesReview(job, expected) {
    return !!(job && job.client_order_id === expected.clientOrderId && job.review_id === expected.reviewId);
  }
  function subReady() { return /^[1-9][0-9]{0,19}$/.test(api.sub()); }
  function expires(value) { return BigInt(value.review.expires_at_ns) <= BigInt(Date.now()) * 1000000n; }
  function invalidate() {
    review = null;
    if (!root) return;
    q("[data-limit-review]").hidden = true;
    q("[data-limit-values]").replaceChildren();
    q("[data-limit-evidence]").replaceChildren();
    field("authorize").checked = false;
    guard();
  }
  function resetScope() {
    if (!root) return;
    epoch += 1; busy = false; reading = false; uncertain = null; items = []; job = null; rendered = "";
    historyOffset = 0; historyMore = false; sourceOffset = 0; sourceMore = false; selectedAttempt = null; fixedResume = null;
    var select = field("session_id"), prepared = handoffId && select.value === handoffId && select.selectedOptions[0];
    select.replaceChildren(new Option("Elegir sesión", ""));
    if (prepared) { select.add(prepared); select.value = handoffId; }
    configureSource();
    field("dedicated_account_confirmed").checked = false;
    q("[data-limit-resume-form]").hidden = true;
    invalidate(); say(handoffId && !prepared ? "Carga la prueba PAPER seleccionada" : ""); render();
  }
  function clear() {
    epoch += 1; if (timer !== undefined) clearTimeout(timer);
    sourceSequence += 1;
    root = api = timer = review = uncertain = job = selectedAttempt = fixedResume = undefined;
    items = []; rendered = ""; busy = reading = historyMore = sourceMore = false; historyOffset = sourceOffset = 0;
    handoffId = null;
  }
  function guard() {
    if (!root) return;
    var visible = api.canSend();
    var editable = visible && subReady() && api.canSign() && !busy && !api.isBusy();
    root.querySelectorAll("[data-limit-form] input,[data-limit-form] select,[data-limit-resume-form] input")
      .forEach(function (input) { input.disabled = !visible || busy; });
    if (!visible && review) invalidate();
    if (!visible) {
      q("[data-limit-items]").replaceChildren();
      q("[data-limit-job]").hidden = true;
      q("[data-limit-resume-form]").hidden = true;
      selectedAttempt = null; rendered = "";
    }
    q("[data-limit-preview]").disabled = !editable || !field("session_id").value ||
      !!(handoffId && field("session_id").value !== handoffId) ||
      !field("dedicated_account_confirmed").checked || !!(job && job.active) || !!uncertain ||
      items.some(function (row) { return row.reserved; });
    q("[data-limit-confirm]").disabled = !editable || !review || expires(review) || !field("authorize").checked;
    q("[data-limit-recovery-preview]").disabled = !editable || !!(job && job.active) || !!uncertain;
    q("[data-limit-sources]").disabled = !visible || busy || api.isBusy();
    q("[data-limit-handoff]").hidden = !handoffId;
    q("[data-limit-discard-source]").disabled = busy || api.isBusy();
    q("[data-limit-sources]").textContent = handoffId ? "Verificar sesión seleccionada" : "Cargar pruebas PAPER";
    q("[data-limit-refresh]").disabled = !visible || !subReady() || busy || api.isBusy();
    q("[data-limit-source-prev]").hidden = sourceOffset === 0;
    q("[data-limit-source-next]").hidden = !sourceMore;
    q("[data-limit-history-prev]").hidden = historyOffset === 0;
    q("[data-limit-history-next]").hidden = !historyMore;
    root.querySelectorAll("[data-limit-source-prev],[data-limit-source-next],[data-limit-history-prev],[data-limit-history-next]")
      .forEach(function (button) { button.disabled = !visible || busy || api.isBusy(); });
    root.querySelectorAll("[data-limit-recover]").forEach(function (button) {
      button.disabled = !editable || !!(job && job.active) || !!uncertain;
    });
    root.querySelectorAll("[data-limit-abandon]").forEach(function (button) {
      button.disabled = !visible || busy || api.isBusy() || !!(job && job.active) || !!uncertain;
    });
  }
  function metric(container, label, value) {
    var cell = el("div", ""); cell.append(el("small", label, "muted"),el("strong",value)); container.append(cell);
  }
  function percentage(value) {
    return Number(value).toLocaleString("es-ES",{style:"percent",maximumSignificantDigits:16});
  }
  function beginRecovery(item) {
    api.invalidateManual(); invalidate();
    selectedAttempt = item.client_order_id;
    fixedResume = item.partial_limits ? item.partial_limits.max_attempts : null;
    field("resume_max").max = fixedResume ? "20" : "5";
    field("resume_max").readOnly = fixedResume !== null;
    field("resume_max").value = fixedResume || Math.min(5,Math.max(2,item.reductions+1));
    q("[data-limit-resume-title]").textContent = item.instrument + " · " + item.direction;
    q("[data-limit-resume-form]").hidden = false;
    q("[data-limit-recovery-preview]").focus(); guard();
  }
  function render() {
    if (!root) return;
    if (!api.canSend()) { guard(); return; }
    var signature = JSON.stringify({job:job && [job.status,job.active,job.client_order_id],uncertain:uncertain,
      items:items.map(function (item) {
        return [item.client_order_id,item.status,item.reserved,item.resumable,item.abandonable,item.reductions,item.entry && item.entry.filled_size,
          item.financial && item.financial.remaining_size,item.financial && item.financial.net_change_usdt,
          item.decision && (item.decision.close_reason || item.decision.reason)];
      })});
    if (rendered === signature) { guard(); return; }
    rendered = signature;
    var badge = q("[data-limit-job]");
    badge.hidden = !job && !uncertain;
    badge.textContent = uncertain ? "Activación pendiente de confirmar" : job ? (statuses[job.status] || "En seguimiento") : "";
    var list = q("[data-limit-items]");
    var openRecords = new Set(Array.from(list.querySelectorAll("details[open]"),function (detail) { return detail.dataset.limitRecord; }));
    list.replaceChildren();
    if (!items.length) list.append(el("p","Sin intentos registrados","muted"));
    items.forEach(function (item) {
      var card = el("article","","grvt-limit-attempt");
      var head = el("div","","real-mexc-head");
      var state = item.reserved && job && job.active && job.client_order_id === item.client_order_id ? job.status : item.status;
      head.append(el("strong",item.instrument + " · " + item.direction + (item.partial_limits ? " · Parciales" : "")),
        el("span",statuses[state] || "Registrado","statusbadge off")); card.append(head);
      var stats = el("div","","grvt-mainnet-metrics");
      metric(stats,"Ejecutado",item.entry ? item.entry.filled_size : "Pendiente");
      metric(stats,"Residuo verificado",item.financial ? item.financial.remaining_size : "Pendiente");
      metric(stats,"Resultado neto",item.financial && !item.reserved ? item.financial.net_change_usdt + " USDT" : "Pendiente");
      card.append(stats);
      var foot = el("div","","grvt-limit-foot");
      var details = el("details",""); details.dataset.limitRecord = item.client_order_id;
      details.open = openRecords.has(item.client_order_id); details.append(el("summary","Registro"),
        el("small","ID " + item.client_order_id,"grvt-oco-id"),
        el("small","Reducciones · " + item.reductions,"grvt-oco-id"));
      if (item.decision) {
        var reason = reasons[item.decision.close_reason || item.decision.reason];
        if (reason) details.append(el("small",reason,"grvt-oco-id"));
      }
      foot.append(details);
      if (item.entry && item.entry.filled_size !== "0" && api.showChart) {
        var chartButton = el("button","Ver gráfico","ghost");
        chartButton.type = "button";
        chartButton.addEventListener("click",function () { if (api.canSend()) api.showChart(item); });
        foot.append(chartButton);
      }
      if (item.resumable) {
        var button = el("button","Retomar intento","ghost"); button.type = "button"; button.dataset.limitRecover = item.client_order_id;
        button.addEventListener("click",function () { beginRecovery(item); }); foot.append(button);
      }
      if (item.abandonable) {
        var abandon = el("button","Liberar reserva","ghost"); abandon.type = "button";
        abandon.dataset.limitAbandon = item.client_order_id;
        abandon.addEventListener("click",function () {
          if (!window.confirm("¿Liberar la reserva local sin envío a GRVT?")) return;
          run(async function (current) {
            api.invalidateManual();
            await post("/attempts/abandon",{sub_account_id:api.sub(),client_order_id:item.client_order_id,
              confirmation:"LIBERAR_RESERVA_LIMIT_SIN_ENVIO"});
            if (current()) { say("Reserva liberada"); await refresh(); }
          });
        }); foot.append(abandon);
      }
      card.append(foot); list.append(card);
    });
    guard();
  }
  function scope() { return {sub_account_id:api.sub(),permission_revision:api.revision()}; }
  function post(path, body) { return api.request(base+path,{method:"POST",body:JSON.stringify(body)}); }
  async function refresh() {
    if (!api.canSend() || !subReady()) return;
    var host = root, issued = epoch, scopeId = api.context();
    var data = await api.read(base+"/attempts?sub_account_id="+encodeURIComponent(api.sub())+"&offset="+historyOffset,true);
    if (!valid(host,issued,scopeId) || !api.canSend()) return;
    job = data.job;
    if (!data.pending) { items = data.items; historyMore = data.has_more; }
    if (uncertain && matchesReview(job,uncertain)) {
      uncertain = null;
      q("[data-limit-resume-form]").hidden = true; selectedAttempt = null;
      say("Autorización registrada · respuesta recuperada");
    }
    render();
  }
  async function sources() {
    var host = root, issued = epoch, scopeId = api.context(), selected = handoffId;
    var serial = ++sourceSequence;
    function currentSource() {
      if (serial !== sourceSequence || selected !== handoffId) return false;
      // The exact PAPER lookup is local and independent of the edited subaccount.
      return selected ? root === host && host.isConnected && api.canSend() : valid(host,issued,scopeId) && api.canSend();
    }
    var query = selected ? "session_id="+encodeURIComponent(selected) : "offset="+sourceOffset;
    if (selected) {
      field("session_id").replaceChildren(new Option("Elegir sesión", ""));
      configureSource(); invalidate();
    }
    var data;
    try {
      data = await api.read(base+"/sources?"+query,true);
      if (!currentSource()) return;
      if (selected && (data.execution_authority !== "NONE" || !Array.isArray(data.items) ||
          data.items.length > 1 || data.items.some(function (item) { return !item || item.session_id !== selected; })))
        throw new Error("No se pudo verificar la sesión seleccionada");
    } catch (error) {
      if (!selected) throw error;
      if (currentSource()) say(error.message,true);
      return;
    }
    sourceMore = data.has_more;
    var select = field("session_id"); select.replaceChildren(new Option("Elegir sesión",""));
    data.items.forEach(function (item) {
      var option = new Option([item.symbol,item.direction,item.timeframe,item.margin_mode+" "+item.leverage+"×",
        item.partial_targets ? item.partial_targets+" parciales" : ""].filter(Boolean).join(" · "),item.session_id);
      option.dataset.partialTargets = String(item.partial_targets || 0);
      select.add(option);
    });
    if (data.items.length === 1) select.selectedIndex = 1;
    configureSource();
    invalidate();
    say(data.items.length ? "" : selected ? "Sesión PAPER LIMIT · no disponible" : "Completa y detén una prueba PAPER LIMIT");
  }
  function partialTargets() {
    var option = field("session_id").selectedOptions[0];
    return Number(option && option.dataset.partialTargets || 0);
  }
  function configureSource() {
    var targets = partialTargets();
    field("reserved_close_attempts").closest("label").hidden = !targets;
    field("reserved_close_attempts").value = "2";
    field("max_reductions").max = targets ? "20" : "5";
    field("max_reductions").value = targets ? String(targets+2) : "2";
  }
  async function run(task) {
    if (!api.canSend() || busy || api.isBusy()) return;
    var host = root, issued = ++epoch, scopeId = api.context();
    busy = true; guard(); api.guard();
    try { await task(function () { return valid(host,issued,scopeId) && api.canSend(); }); }
    catch (error) { if (valid(host,issued,scopeId)) say(error.message,true); }
    finally { if (root === host && epoch === issued) { busy = false; guard(); api.guard(); } }
  }
  function showReview(data, resume) {
    var value = data.review;
    var expected = resume ? "orbis.grvt.mainnet.perp.limit.resume.review.v1" : "orbis.grvt.mainnet.perp.limit.review.v1";
    if (!value || ![expected,expected.replace(/v1$/,"v2")].includes(value.schema) || value.sub_account_id !== api.sub() ||
        value.execution_authority !== "NONE" || value.network !== "MAINNET" ||
        typeof value.review_id !== "string" || !/^[a-zA-Z0-9_-]{32}$/.test(value.review_id) ||
        (resume && (!value.attempt || value.attempt.client_order_id !== selectedAttempt)) ||
        !resume && (!value.terms || !value.terms.paper || value.terms.paper.session_id !== field("session_id").value))
      throw new Error("Revisión LIMIT no verificable");
    var evidence = resume ? null : window.OrbisGrvtPerpReviewEvidence.create(
      value.lab_origin, value.terms.research, value.terms.paper);
    var values = q("[data-limit-values]"); values.replaceChildren();
    q("[data-limit-review-title]").textContent = resume ? "Retomar · REAL" : "Activar · REAL";
    q("[data-limit-confirm]").textContent = resume ? "Confirmar reanudación" : "Activar bot";
    metric(values,"Subcuenta",value.sub_account_id);
    if (resume) {
      metric(values,"Intento",value.attempt.client_order_id);
      metric(values,"Tope de salida",value.exit_notional_limit+" USDT");
      if (value.attempt.partial_limits) metric(values,"Intentos para cierre",String(value.attempt.partial_limits.reserved_close_attempts));
    } else {
      metric(values,"Mercado",value.terms.research.symbol+" · "+value.terms.definition.direction);
      metric(values,"Saldo consultado",value.account_equity+" USDT");
      metric(values,"Efectivo / colateral",value.terms.budget.entry_cash_limit+" / "+value.terms.budget.collateral_limit+" USDT");
      metric(values,"Nominal entrada / salida",value.terms.budget.entry_notional_limit+" / "+value.terms.budget.exit_notional_limit+" USDT");
      metric(values,"Esperar señal",value.wait_seconds+" s");
      metric(values,"Vigencia de la orden",value.terms.max_pending_seconds+" s");
      if (value.terms.reserved_close_attempts) {
        metric(values,"Objetivos parciales",String(value.terms.research.profile.partial_targets.length));
        value.terms.research.profile.partial_targets.forEach(function (target,index) {
          metric(values,"P"+(index+1),"+"+percentage(target.target)+" · "+percentage(target.fraction)+" de la posición");
        });
        metric(values,"Intentos para cierre",String(value.terms.reserved_close_attempts));
      }
    }
    metric(values,"Permiso",value.authorization_seconds+" s");
    metric(values,"Máx. reducciones",String(value.max_reductions));
    if (evidence) q("[data-limit-evidence]").replaceChildren(evidence);
    review = data;
    field("authorize").checked = false;
    q("[data-limit-review]").hidden = false; field("authorize").focus(); guard();
  }
  function fieldHtml(name, label, value, range) {
    return '<label class="field">'+label+'<input name="limit_'+name+'" value="'+value+'" required '+
      (range ? 'type="number" min="'+range[0]+'" max="'+range[1]+'"' : 'inputmode="decimal"')+'></label>';
  }
  function schedule() {
    timer = setTimeout(async function () {
      if (!root || !root.isConnected) { clear(); return; }
      var host = root;
      if (root.open && !reading && !busy && !api.isBusy() && api.canSend()) {
        reading = true;
        try { await refresh(); } catch (_) { /* El usuario puede consultar de nuevo el historial. */ }
        finally { if (root === host) reading = false; }
      }
      if (root === host) schedule();
    },3000);
  }
  function mount(host, services) {
    clear(); if (!host) return;
    root = host; api = services;
    root.innerHTML = '<summary>Bot LIMIT <span class="grvt-limit-tag">GRVT · REAL</span></summary><div class="grvt-mainnet-body grvt-limit-body">'+
      '<div class="grvt-limit-head"><strong>Una entrada por activación</strong><span data-limit-job class="statusbadge off" hidden></span></div>'+
      '<div class="row" data-limit-handoff hidden><span class="statusbadge off">PAPER → REAL · LIMIT</span>'+
      '<button type="button" class="ghost" data-limit-discard-source>Quitar selección</button></div>'+
      '<form data-limit-form><div class="grvt-mainnet-fields"><label class="field grvt-limit-source">Sesión PAPER<select name="limit_session_id" required><option value="">Elegir sesión</option></select></label>'+
      fieldHtml("collateral_limit","Colateral máx. · USDT","100")+fieldHtml("entry_cash_limit","Efectivo por entrada · USDT","25")+
      fieldHtml("entry_notional_limit","Nominal entrada · USDT","100")+fieldHtml("exit_notional_limit","Nominal salida · USDT","150")+'</div>'+
      '<div class="row"><button type="button" class="ghost" data-limit-sources>Cargar pruebas PAPER</button>'+
      '<button type="button" class="ghost" data-limit-source-prev hidden>Pruebas anteriores</button><button type="button" class="ghost" data-limit-source-next hidden>Más pruebas</button></div>'+
      '<details><summary>Plazos y ejecución</summary><div class="grvt-mainnet-fields">'+
      fieldHtml("wait_seconds","Esperar señal · s","60",[30,600])+fieldHtml("max_pending_seconds","Vigencia de orden · s","30",[5,120])+
      fieldHtml("exit_seconds","Permanencia máx. · s","120",[120,780])+fieldHtml("authorization_seconds","Permiso total · s","300",[185,840])+
      fieldHtml("max_reductions","Máx. reducciones","2",[1,5])+fieldHtml("fee_rate_ceiling","Techo de comisión","0.001")+
      fieldHtml("reserved_close_attempts","Intentos para cierre","2",[1,19]).replace("<label ","<label hidden ")+
      fieldHtml("max_spread_bps","Spread máx. · bps","25",[0,100])+fieldHtml("max_slippage_bps","Deslizamiento máx. · bps","25",[0,100])+'</div></details>'+
      '<label class="grvt-mainnet-check"><input name="limit_dedicated_account_confirmed" type="checkbox" required>Confirmo la subcuenta dedicada</label>'+
      '<button data-limit-preview>Revisar activación</button></form>'+
      '<section class="grvt-limit-history"><div class="grvt-limit-head"><h4>Historial LIMIT</h4><button type="button" class="ghost" data-limit-refresh>Consultar historial</button></div>'+
      '<div data-limit-items></div><div class="row"><button type="button" class="ghost" data-limit-history-prev hidden>Anterior</button><button type="button" class="ghost" data-limit-history-next hidden>Siguiente</button></div></section>'+
      '<form data-limit-resume-form hidden><strong data-limit-resume-title></strong><div class="grvt-mainnet-fields">'+
      fieldHtml("resume_seconds","Permiso de reanudación · s","180",[60,600])+fieldHtml("resume_max","Reducciones totales","3",[1,5])+
      '</div><button data-limit-recovery-preview>Revisar reanudación</button></form>'+
      '<div class="grvt-mainnet-review" data-limit-review hidden><strong data-limit-review-title></strong><div class="grvt-mainnet-metrics" data-limit-values></div><div data-limit-evidence></div>'+
      '<label class="grvt-mainnet-check"><input name="limit_authorize" type="checkbox">Autorizo operaciones reales con estos límites</label>'+
      '<div class="row"><button type="button" data-limit-confirm>Activar bot</button><button type="button" class="ghost" data-limit-discard>Descartar</button></div></div>'+
      '<p class="state muted" role="status" data-limit-notice></p></div>';
    q("[data-limit-sources]").onclick = function () { run(sources); };
    q("[data-limit-discard-source]").onclick = function () {
      if (!handoffId || busy) return;
      handoffId = null;
      field("session_id").replaceChildren(new Option("Elegir sesión", ""));
      configureSource(); invalidate(); say("");
    };
    q("[data-limit-refresh]").onclick = function () { run(refresh); };
    [["source",-25],["source",25],["history",-20],["history",20]].forEach(function (item) {
      q("[data-limit-"+item[0]+"-"+(item[1]<0?"prev":"next")+"]").onclick = function () {
        run(function () {
          if (item[0] === "source") { sourceOffset = Math.max(0,sourceOffset+item[1]); return sources(); }
          historyOffset = Math.max(0,historyOffset+item[1]); return refresh();
        });
      };
    });
    q("[data-limit-form]").oninput = q("[data-limit-resume-form]").oninput = invalidate;
    field("session_id").onchange = function () { configureSource(); invalidate(); };
    field("authorize").onchange = guard; q("[data-limit-discard]").onclick = invalidate;
    q("[data-limit-form]").onsubmit = function (event) {
      event.preventDefault(); if (q("[data-limit-preview]").disabled) return;
      run(async function (current) {
        api.invalidateManual(); invalidate();
        var body = scope(); body.session_id = field("session_id").value; body.dedicated_account_confirmed = true;
        ["collateral_limit","entry_cash_limit","entry_notional_limit","exit_notional_limit","fee_rate_ceiling"].forEach(function (name) {
          body[name] = field(name).value.trim().replace(",",".");
        });
        ["wait_seconds","max_pending_seconds","exit_seconds","authorization_seconds","max_reductions","max_spread_bps","max_slippage_bps"]
          .forEach(function (name) { body[name] = Number(field(name).value); });
        if (partialTargets()) body.reserved_close_attempts = Number(field("reserved_close_attempts").value);
        var data = await post("/preview",body); if (current()) showReview(data,false);
      });
    };
    q("[data-limit-resume-form]").onsubmit = function (event) {
      event.preventDefault(); if (q("[data-limit-recovery-preview]").disabled || !selectedAttempt) return;
      run(async function (current) {
        api.invalidateManual(); invalidate();
        var data = await post("/resume/preview",Object.assign(scope(),{client_order_id:selectedAttempt,
          authorization_seconds:Number(field("resume_seconds").value),max_reductions:Number(field("resume_max").value)}));
        if (current()) showReview(data,true);
      });
    };
    q("[data-limit-confirm]").onclick = function () {
      if (q("[data-limit-confirm]").disabled) return;
      run(async function (current) {
        var value = review, resume = !!value.review.attempt;
        var partial = /\.v2$/.test(value.review.schema);
        var expected = {clientOrderId:resume ? value.review.attempt.client_order_id : value.review.client_order_id,
          reviewId:value.review.review_id};
        invalidate(); uncertain = expected; render();
        var data;
        try {
          data = await post(resume ? "/resume/start" : "/start",Object.assign({},value,{permission_revision:api.revision(),
            confirmation:(resume ? "RETOMAR" : "ACTIVAR")+"_BOT_LIMIT"+(partial ? "_PARCIALES" : "")+"_GRVT_MAINNET"}));
        } catch (error) {
          if (!current()) return;
          if ([400,403,404,409,422,423,429].includes(error.status)) {
            uncertain = null;
            say(error.message,true); render();
          } else {
            say("Respuesta sin confirmar · consulta el historial",true);
            await refresh().catch(function () {});
          }
          return;
        }
        if (!current()) return;
        if (!matchesReview(data,expected)) {
          say("Respuesta sin confirmar · consulta el historial",true);
          await refresh().catch(function () {});
          return;
        }
        uncertain = null; job = data;
        q("[data-limit-resume-form]").hidden = true; selectedAttempt = null;
        say(resume ? "Verificando intento" : "Esperando señal nueva"); render();
      });
    };
    root.addEventListener("toggle",function () { if (root === host && host.open && api.canSend()) refresh().catch(function () {}); });
    guard(); render(); schedule();
  }
  function prepare(id) {
    handoffId = id;
    sourceOffset = 0; sourceMore = false;
    field("session_id").replaceChildren(new Option("Elegir sesión", ""));
    field("dedicated_account_confirmed").checked = false;
    configureSource(); invalidate(); say("Verificando sesión PAPER…");
    run(sources);
  }
  window.OrbisGrvtMainnetLimit = {mount:mount,prepare:prepare,clear:clear,guard:guard,invalidate:invalidate,
    changedSub:resetScope,isBusy:function () { return busy; },cancel:function () { epoch += 1; busy = false; reading = false; invalidate(); },
    ackStop:function () { if (root) { uncertain = null; refresh().catch(function () {}); } }};
}());
