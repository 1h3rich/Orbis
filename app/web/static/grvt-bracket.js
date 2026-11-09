/* Entrada manual y salidas acotadas TESTNET. Sin autostart ni secretos persistidos. */
(function () {
  "use strict";
  var root = null, api = null, epoch = 0, timer = null, busy = false, reading = false;
  var prepared = null, closeReview = null, sources = [], closeSources = [], groups = [], attempted = new Set(), offset = 0;
  var renderedGroups = null;
  var exitReviews = new Map();
  var watchReview = null;
  var panelSnapshot = null, probe = null;
  var uncertainStarts = new Map(), mutationSerial = 0;
  var watchHistories = new Map(), watchEvents = new Map(), historyReading = new Set();
  var base = "/exchanges/grvt/testnet/brackets";
  function node(tag, text, cls) {
    var el = document.createElement(tag); el.textContent = text;
    if (cls) el.className = cls;
    return el;
  }
  function secrets(host) {
    if (host) host.querySelectorAll('input[type="password"],[name="expected_signer"]').forEach(function (el) { el.value = ""; });
  }
  function clear() {
    epoch += 1; clearProbe(); secrets(root);
    if (timer !== null) clearTimeout(timer);
    root = null; api = null; timer = null; prepared = null; busy = false; reading = false;
    sources = []; closeSources = []; groups = []; closeReview = null; attempted = new Set(); offset = 0;
    renderedGroups = null;
    exitReviews = new Map();
    watchReview = null;
    panelSnapshot = null;
    uncertainStarts = new Map(); mutationSerial += 1;
    watchHistories = new Map(); watchEvents = new Map(); historyReading = new Set();
  }
  function current(host, issued) { return root === host && host.isConnected && issued === epoch && api; }
  function sub() { return root.querySelector('[name="sub_account_id"]').value.trim(); }
  function selected() { return sources.find(function (s) { return s.client_order_id === root.querySelector('[name="source"]').value; }); }
  function notice(text, error) {
    var box = root.querySelector('[data-bracket-state]');
    box.textContent = text; box.className = "state " + (error ? "error" : "muted");
  }
  function previewId(p) { return p.client_order_id || p.source.client_order_id; }
  function validPreview() {
    if (watchReview) return sub() === watchReview.sub && watchReview.expires_at_ms > Date.now();
    if (closeReview) return sub() === closeReview.sub && closeReview.expires_at_ms > Date.now() &&
      (closeReview.existing || closeSources.some(function (source) {
        return source.candidate && source.action_request_id === closeReview.action_request_id && source.expires_at_ms > Date.now();
      }));
    if (!prepared || prepared.preview.expires_at_ms <= Date.now() || attempted.has(previewId(prepared.preview))) return false;
    return !!prepared.preview.parent_client_order_id || sources.some(function (s) {
      return s.client_order_id === prepared.preview.source.client_order_id && s.candidate && s.expires_at_ms > Date.now();
    });
  }
  function guards() {
    if (!root) return;
    var source = selected();
    root.querySelector('[data-bracket-controls]').disabled = busy || !api.canSend();
    root.querySelector('[data-bracket-prepare]').disabled = busy || !api.canSend() || !source || !source.candidate || source.expires_at_ms <= Date.now();
    root.querySelector('[data-bracket-send]').disabled = busy || !api.canSend() || !validPreview();
    var reviewed = watchReview || closeReview || (prepared && prepared.preview);
    root.querySelector('[data-bracket-expiry]').textContent = reviewed ?
      (validPreview() ? "Vigente · " + Math.max(0, Math.ceil((reviewed.expires_at_ms - Date.now()) / 1000)) + " s" : "Revisión no vigente") : "";
    ["close_attempts", "close_ttl", "watch_minutes"].forEach(function (name) { root.querySelector('[name="' + name + '"]').disabled = !!closeReview || !!watchReview; });
    root.querySelectorAll('[data-watch-stamp]').forEach(function (element) {
      var group = groups.find(function (g) { return g.parent_client_order_id === element.dataset.watchStamp; });
      var job = group && group.strategy_close && group.strategy_close.run, watch = job && job.watch;
      if (watch && watch.mode === "RECOVERY") element.textContent = "Recuperación";
      else if (watch) element.textContent = (watch.last_market_event_ns ? "Tick · " + new Date(deadlineMs(watch.last_market_event_ns)).toLocaleTimeString() :
        job.active && ["STARTING","WATCHING"].includes(job.status) ? "Esperando precios" : "Precios") +
        " · " + watch.observations + " observaciones";
    });
  }
  function invalidate() {
    prepared = null; closeReview = null; watchReview = null;
    if (!root) return;
    root.querySelector('[data-bracket-review]').hidden = true;
    root.querySelector('[data-bracket-preview]').replaceChildren();
    root.querySelector('[name="dedicated_private_key"]').value = "";
    guards();
  }
  function button(host, text, action, parent, disabled, cid) {
    var el = node("button", text, "ghost"); el.type = "button"; el.disabled = disabled;
    el.dataset.bracketAction = action; el.dataset.parent = parent;
    if (cid) el.dataset.clientId = cid;
    host.append(el);
  }
  function position(receipt) {
    return receipt.result.positions.length ? receipt.result.positions.map(function (p) { return p[0] + " · " + p[1]; }).join(" / ") : "Sin exposición conciliada";
  }
  function closeLabel(status) {
    return {STARTING:"Iniciando",CANCELLING:"Cancelando protecciones",WAITING_AUDIT:"Esperando auditoría",
      AUDITING_GROUP:"Auditando grupo",GROUP_RECONCILED:"Grupo conciliado",CLOSE_SENT:"Cierre enviado",
      AUDITING_CLOSE:"Auditando cierre",CLOSE_RECONCILED:"Cierre conciliado",RECOVERING:"Recuperando respuesta",
      STOP_REQUESTED:"Deteniendo",STOPPED:"Detenido · revisar posición",COMPLETE:"Completado",
      ATTEMPT_LIMIT:"Límite de intentos · revisar residuo",EXPIRED:"Permiso vencido",REVIEW_UNSENT:"Intento sin enviar · revisar",
      NEEDS_REVIEW:"Revisión necesaria",WATCHING:"Observando",GAP:"Datos interrumpidos · revisar posición",
      INTERRUPTED:"Interrumpido · revisar posición"}[status] || "Registrado";
  }
  function deadlineMs(value) { try { return Number(BigInt(value) / 1000000n); } catch (_) { return 0; } }
  function candidateClose(group) {
    return closeSources.find(function (source) {
      return source.candidate && source.expires_at_ms > Date.now() && source.session_id === group.signal_source.session_id &&
        source.version_id === group.signal_source.version_id;
    });
  }
  function confirmRecoveredState(parent, revision) {
    if (!sameRevision(revision, uncertainStarts.get(parent))) return;
    uncertainStarts.delete(parent);
    var box = root.querySelector('[data-bracket-state]');
    if (box.textContent === "Activación no confirmada · consultando estado.") notice("Estado recuperado");
  }
  function renderGroups(data) {
    panelSnapshot = data;
    groups = data.groups;
    var presentation = JSON.stringify({sub:sub(),offset:offset,stopped:data.stopped,has_more:data.has_more,
      groups:data.groups.map(function (group) {
        var strategy = group.strategy_close || {}, job = strategy.run, source = candidateClose(group);
        if (job) confirmRecoveredState(group.parent_client_order_id, job.permission_revision);
        return Object.assign({}, group, {
          last_observation:group.last_observation ? {orders:group.last_observation.orders} : null,
          strategy_close:{request:strategy.request || null,run:job ? {active:job.active,status:job.status,
            watch:job.watch ? {deadline_ns:job.watch.deadline_ns,max_attempts:job.watch.max_attempts,
              record_id:job.watch.record_id,archive_error:job.watch.archive_error,mode:job.watch.mode} : null} : null},
          close_candidate:source ? source.action_request_id : null,
          exit_review:exitReviews.get(group.parent_client_order_id) || null,
          unconfirmed:uncertainStarts.has(group.parent_client_order_id),
          watch_history:watchHistories.get(group.parent_client_order_id) || null,
          watch_events:watchEvents.get(group.parent_client_order_id) || null
        });
      })});
    if (renderedGroups === presentation) { guards(); return; }
    renderedGroups = presentation;
    var list = root.querySelector('[data-bracket-groups]'); list.replaceChildren();
    data.groups.forEach(function (group) {
      var parent = group.parent_client_order_id, posted = ["ACKNOWLEDGED", "POST_CLAIMED"].includes(group.status);
      var receipt = group.reconciliation, attempts = group.emergency_attempts || [];
      var strategy = group.strategy_close || {}, job = strategy.run, unconfirmed = uncertainStarts.has(parent);
      var closing = unconfirmed || !!(job && job.active);
      var latest = attempts.slice().reverse().find(function (a) { return a.reconciliation; });
      var outstanding = attempts.some(function (a) { return ["PREPARED", "POST_CLAIMED", "ACKNOWLEDGED", "OBSERVED"].includes(a.status); });
      var label = outstanding ? "Cierre pendiente" : latest ? "Cierre conciliado" : receipt ? "Grupo conciliado" :
        group.status === "ACKNOWLEDGED" ? "Acuse recibido" : group.status === "POST_CLAIMED" ? "Envío incierto" :
        group.status === "ABANDONED" ? "Descartada" : "Preparada";
      var card = node("article", "", "grvt-oco-group"); card.dataset.bracketGroup = parent;
      var head = node("div", "", "real-mexc-head");
      head.append(node("strong", group.instrument + " · " + group.signal_source.direction + " · " + group.legs[0].size), node("span", label, "statusbadge off"));
      card.append(head);
      var states = group.last_observation && group.last_observation.orders || [];
      group.legs.forEach(function (leg) {
        var actual = states.find(function (o) { return o.client_order_id === leg.client_order_id; });
        card.append(node("div", (leg.role === "ENTRY" ? "Entrada" : leg.role === "STOP_LOSS" ? "SL" : "TP") +
          " · " + (leg.trigger_price || leg.limit_price) + (leg.role === "ENTRY" ? " · FOK" : " → límite " + leg.limit_price) +
          (actual ? " · " + actual.status + " · " + actual.traded_size : " · sin observar"), "grvt-oco-leg"));
      });
      if (latest || receipt) card.append(node("p", position(latest ? latest.reconciliation : receipt), "grvt-oco-net"));
      if (receipt) card.append(node("small", "Neto del grupo · " + receipt.result.net_realized_pnl_usdt + " USDT", "muted"));
      var detail = document.createElement("details");
      detail.append(node("summary", "Registro · " + group.financial_audit_count + " auditorías"));
      group.legs.forEach(function (leg) { detail.append(node("small", leg.role + " · " + leg.client_order_id, "muted grvt-oco-id")); });
      if (posted) button(detail, "Muestra de parciales", "market-probe", parent, false);
      card.append(detail);
      var actions = node("div", "", "row");
      button(actions, "Consultar", "observe", parent, !posted || closing);
      if (group.can_review_strategy_exit) button(actions, "Evaluar salidas", "strategy-exit", parent, closing);
      if (group.can_recover_native && !closing && !strategy.request) button(actions, "Recuperar grupo", "recovery-review", parent, false);
      else if (group.can_review_strategy_exit && !closing && !strategy.request) button(actions, "Seguir salidas", "watch-review", parent, false);
      if (job && job.watch || watchHistories.has(parent)) button(actions, "Historial de seguimiento", "watch-history", parent, false);
      button(actions, "Auditar", "audit", parent, !posted || !!receipt || closing);
      button(actions, "Conciliar", "reconcile", parent, !group.can_reconcile || closing);
      if (group.status === "PREPARED") button(actions, "Descartar", "abandon", parent, false);
      var terminal = states.length === 3 && states.every(function (s) { return ["FILLED", "CANCELLED", "REJECTED"].includes(s.status); });
      if (posted && !receipt && !group.cancel_attempt && !terminal && !closing) button(actions, "Cancelar salidas", "cancel", parent, false);
      if (group.can_emergency && !outstanding && !closing) button(actions, "Cerrar restante", "emergency", parent, false);
      card.append(actions);
      var exitReview = exitReviews.get(parent);
      if (exitReview) {
        var decision = exitReview.decision, evaluated = node("section", "", "grvt-bracket-emergency");
        evaluated.dataset.strategyExitReview = parent;
        var reason = {STOP:"Stop",TRAILING_STOP:"Trailing",TARGET:"Objetivo",EXIT_RULE:"Regla",TIME_LIMIT:"Permanencia",POSITION_OPEN:"Mantener"}[decision.reason] || "Revisar";
        evaluated.append(node("strong", "Salidas calculadas · " + reason));
        evaluated.append(node("div", "Entrada ejecutada · " + decision.entry_price + " · " + decision.size));
        evaluated.append(node("div", "SL · " + decision.stop_price + " / TP · " + decision.target_price));
        evaluated.append(node("small", "Consulta puntual · " + new Date(deadlineMs(decision.market_event_time_ns)).toLocaleTimeString() + " · no ejecuta", "muted"));
        card.append(evaluated);
      }
      attempts.forEach(function (attempt) {
        var part = node("section", "", "grvt-bracket-emergency"); part.dataset.emergencyId = attempt.client_order_id;
        part.append(node("strong", "Cierre · " + attempt.side + " · " + attempt.size + " · " + attempt.status));
        part.append(node("small", attempt.client_order_id + " · " + attempt.audit_count + " auditorías", "muted grvt-oco-id"));
        if (attempt.reconciliation) {
          part.append(node("p", position(attempt.reconciliation)));
          if (attempt.reconciliation.result.net_realized_pnl_usdt !== undefined)
            part.append(node("small", "Neto del cierre · " + attempt.reconciliation.result.net_realized_pnl_usdt + " USDT", "muted"));
        }
        var row = node("div", "", "row");
        button(row, "Auditar cierre", "emergency-audit", parent, closing || !!attempt.reconciliation || attempt.status === "PREPARED", attempt.client_order_id);
        button(row, "Conciliar cierre", "emergency-reconcile", parent, closing || !attempt.can_reconcile, attempt.client_order_id);
        part.append(row); card.append(part);
      });
      var lastReceipt = latest ? latest.reconciliation : receipt, flat = !!lastReceipt && !lastReceipt.result.positions.length;
      if (strategy.request || job || unconfirmed) {
        var lastAttempt = attempts[attempts.length - 1], unsent = !!lastAttempt && lastAttempt.status === "PREPARED";
        var exhausted = !!strategy.request && attempts.length >= strategy.request.max_attempts && !!lastAttempt && !!lastAttempt.reconciliation;
        var blocked = unsent || exhausted || !!job && ["COMPLETE", "ATTEMPT_LIMIT", "REVIEW_UNSENT"].includes(job.status);
        var savedLabel = flat ? "Completado" : exhausted ? closeLabel("ATTEMPT_LIMIT") : unsent ? closeLabel("REVIEW_UNSENT") :
          strategy.request && deadlineMs(strategy.request.deadline_ns) <= Date.now() ? closeLabel("EXPIRED") : "Preparado · pendiente de firma";
        var execution = node("section", "", "grvt-bracket-emergency grvt-strategy-close"); execution.dataset.strategyClose = "";
        var filled = unconfirmed || !!(job && job.watch) || !!(strategy.request &&
          ["orbis.grvt.testnet.strategy.close.v2", "orbis.grvt.testnet.native.partial.close.v1", "orbis.grvt.testnet.native.recovery.close.v1"].includes(strategy.request.schema));
        execution.append(node("strong", (filled ? "Salidas · " : "Cierre de estrategia · ") + (unconfirmed ? "Activación sin confirmar" : job ? closeLabel(job.status) : savedLabel)));
        if (strategy.request && ["NATIVE_PARTIAL_STOP", "NATIVE_PARTIAL_TARGET"].includes(strategy.request.exit_reason))
          execution.append(node("small", strategy.request.exit_reason === "NATIVE_PARTIAL_STOP" ? "Stop parcial" : "Objetivo parcial", "muted"));
        if (strategy.request && strategy.request.schema === "orbis.grvt.testnet.native.recovery.close.v1") {
          var recoveryLabel = {ORPHANED_EXITS:flat ? "Sin ejecución · salidas retiradas" : "Salidas huérfanas",
            ENTRY_UNFILLED:flat ? "Sin ejecución" : "Entrada cancelada", EXIT_PROTECTION_EXPIRED:"Protección vencida",
            NATIVE_SIBLING_ACTIVE:flat ? "Salida retirada" : "Salida pendiente", NATIVE_PROTECTION_ENDED:"Protección finalizada"};
          execution.append(node("small", recoveryLabel[strategy.request.exit_reason] || "Recuperación", "muted"));
        }
        if (strategy.request) execution.append(node("small", "Hasta " + strategy.request.max_attempts + " intentos", "muted"));
        if (job && job.watch && !unconfirmed) {
          var stamp = node("small", "", "muted"); stamp.dataset.watchStamp = parent; execution.append(stamp);
          if (job.active) execution.append(node("small", "Permiso · " + new Date(deadlineMs(strategy.request ? strategy.request.deadline_ns : job.watch.deadline_ns)).toLocaleTimeString(), "muted"));
          if (job.watch.archive_error) execution.append(node("small", "Registro incompleto · revisar", "state error"));
        }
        if (closing) button(execution, filled ? "Detener seguimiento" : "Detener cierre", "strategy-stop", parent, false);
        else if (!flat && !blocked && strategy.request && deadlineMs(strategy.request.deadline_ns) > Date.now())
          button(execution, "Continuar cierre", filled ? "watch-review" : "strategy-review", parent, false);
        card.insertBefore(execution, card.children[1]);
      } else if (posted && !flat && !attempts.length && candidateClose(group)) {
        button(actions, "Cerrar por señal", "strategy-review", parent, false);
      }
      renderWatchHistory(card,parent);
      list.append(card);
    });
    if (!data.groups.length) list.append(node("p", "Sin entradas protegidas registradas.", "muted"));
    root.querySelector('[data-bracket-page]').textContent = "Página " + (offset / 50 + 1) + (data.stopped ? " · STOP activo" : "");
    root.querySelector('[data-bracket-prev]').disabled = offset === 0;
    root.querySelector('[data-bracket-next]').disabled = !data.has_more;
    var entry = root.querySelector('[data-bracket-entry]');
    if (data.groups.length && !prepared && !entry.contains(document.activeElement)) entry.open = false;
    guards();
  }
  function historyButton(host,text,action,parent,id,offset) {
    button(host,text,action,parent,false,id);
    host.lastChild.dataset.historyOffset = String(offset);
  }
  function renderWatchHistory(card,parent) {
    var history = watchHistories.get(parent);
    if (!history) return;
    var box = node("section", "", "grvt-bracket-emergency"); box.dataset.watchHistory = parent;
    box.append(node("strong", "Seguimiento · Historial"));
    if (!history.items.length) box.append(node("p", "Sin seguimientos guardados.", "muted"));
    history.items.forEach(function (item) {
      var row = node("div", "", "grvt-bracket-emergency");
      row.append(node("strong", new Date(deadlineMs(item.started_at_ns)).toLocaleString() + " · " + closeLabel(item.status)));
      row.append(node("p", item.observations + " observaciones · hasta " + item.max_attempts + " intentos", "muted"));
      if (item.unobserved_since_ns) row.append(node("small", "Sin seguimiento desde · " + new Date(deadlineMs(item.unobserved_since_ns)).toLocaleString(), "muted"));
      historyButton(row,"Observaciones","watch-events",parent,item.id,0); box.append(row);
    });
    var pages = node("div", "", "row");
    if (history.offset) historyButton(pages,"Seguimientos recientes","watch-history",parent,null,Math.max(0,history.offset-20));
    if (history.has_more) historyButton(pages,"Seguimientos anteriores","watch-history",parent,null,history.offset+20);
    box.append(pages);
    var record = watchEvents.get(parent);
    if (record) {
      var events = node("div", "", "grvt-bracket-emergency"); events.dataset.watchEvents = "";
      events.style.maxHeight = "20rem"; events.style.overflow = "auto";
      events.append(node("strong", "Observaciones · " + (record.offset+1) + "–" + (record.offset+record.events.length) + " / " + record.event_count));
      record.events.forEach(function (event) {
        var data = event.data, label = event.kind === "STARTED" ? "Inicio" : event.kind === "OBSERVATION" ?
          "Precio · " + data.mark_price + " · " + ({POSITION_OPEN:"Mantener",STOP:"Stop",TRAILING_STOP:"Trailing",TARGET:"Objetivo",EXIT_RULE:"Regla",TIME_LIMIT:"Permanencia"}[data.reason] || "Revisar") :
          event.kind === "STOP_REQUESTED" ? "Parada solicitada" : closeLabel(data.status);
        events.append(node("p", new Date(deadlineMs(event.at_ns)).toLocaleTimeString() + " · " + label, "muted"));
      });
      var more = node("div", "", "row");
      if (record.offset) historyButton(more,"Observaciones anteriores","watch-events",parent,record.id,Math.max(0,record.offset-100));
      if (record.has_more) historyButton(more,"Más observaciones","watch-events",parent,record.id,record.offset+100);
      box.append(events,more);
    }
    card.append(box);
  }
  async function loadWatchHistory(target) {
    var host = root, issued = epoch, local = api, parent = target.dataset.parent, account = sub();
    if (historyReading.has(parent)) return;
    var detail = target.dataset.bracketAction === "watch-events", skip = Number(target.dataset.historyOffset || 0);
    var path = base + "/" + parent + "/exit-watches" + (detail ? "/" + target.dataset.clientId : "") + "?offset=" + skip;
    historyReading.add(parent);
    try {
      var result = await watchRequest(local,path,null,true,5000);
      if (!current(host,issued) || sub() !== account) return;
      if (result.network !== "TESTNET" || result.authorization !== false || (detail ? !Array.isArray(result.events) : !Array.isArray(result.items)))
        throw new Error("Historial no verificable");
      (detail ? watchEvents : watchHistories).set(parent,result);
      if (panelSnapshot) renderGroups(panelSnapshot);
    } catch (error) { if (current(host,issued)) notice(error.message || "Historial no disponible",true); }
    finally { if (current(host,issued)) historyReading.delete(parent); }
  }
  async function refresh() {
    if (!root || busy || reading || !api.canSend()) return;
    var host = root, issued = epoch, local = api, account = sub(), operation = mutationSerial; reading = true;
    try {
      var results = await Promise.allSettled([
        watchRequest(local, "/exchanges/grvt/testnet/signals/candidates", null, true, 5000),
        account ? watchRequest(local, base + "?sub_account_id=" + encodeURIComponent(account) + "&offset=" + offset, null, true, 5000) : Promise.resolve(null)
      ]);
      if (!current(host, issued) || mutationSerial !== operation) return;
      var select = host.querySelector('[name="source"]'), previous = select.value;
      var data = results[0].status === "fulfilled" ? results[0].value : null;
      if (data && (data.network !== "TESTNET" || data.authorization !== false || !Array.isArray(data.items))) throw new Error("Señales no verificables");
      sources = data ? data.items.filter(function (s) { return s.action === "OPEN"; }) : [];
      closeSources = data ? data.items.filter(function (s) { return s.action === "CLOSE"; }) : [];
      select.replaceChildren(new Option(data ? "Señal PAPER" : "Señales no disponibles", ""));
      sources.forEach(function (s) {
        var option = new Option(s.direction + " · " + s.symbol + (s.candidate && s.expires_at_ms > Date.now() ? "" : " · caducada"), s.client_order_id);
        option.disabled = !s.candidate || s.expires_at_ms <= Date.now(); select.add(option);
      });
      if (sources.some(function (s) { return s.client_order_id === previous; })) select.value = previous;
      else if (sources.length === 1 && sources[0].candidate) select.value = sources[0].client_order_id;
      guards();
      if (account) {
        if (results[1].status !== "fulfilled") throw results[1].reason;
        var history = results[1].value;
        if (history.network !== "TESTNET" || history.authorization !== false) throw new Error("Registro no verificable");
        if (sub() === account) renderGroups(history);
      } else if (!data) throw results[0].reason;
    } catch (error) { if (current(host, issued)) notice(error.message || "Registro no disponible", true); }
    finally { if (current(host, issued)) reading = false; }
  }
  function showReview(data) {
    if (data.network !== "TESTNET" || data.can_submit !== false) throw new Error("Revisión no verificable");
    prepared = data;
    var p = data.preview, emergency = !!p.parent_client_order_id, box = root.querySelector('[data-bracket-preview]');
    box.replaceChildren(node("strong", emergency ? "Cierre de emergencia · TESTNET" : "Entrada protegida · TESTNET"));
    var legs = p.legs || [{role:"CIERRE",side:p.side,size:p.size,limit_price:p.limit_price}];
    legs.forEach(function (leg) {
      box.append(node("p", leg.role + " · " + leg.side + " · " + leg.size + " × " + leg.limit_price +
        (leg.trigger_price ? " · disparo " + leg.trigger_price : "") + " USDT"));
    });
    if (!emergency) box.append(node("small", "Protección manual · no activa las salidas automáticas de la estrategia.", "muted"));
    root.querySelector('[data-bracket-review]').hidden = false;
    if (emergency) root.querySelector('[data-bracket-entry]').open = false;
    root.querySelector('[data-bracket-send]').textContent = emergency ? "Firmar cierre · TESTNET" : "Firmar entrada · TESTNET";
    root.querySelector('[data-bracket-invalidate]').textContent = "Descartar revisión";
    guards();
    root.querySelector('[data-bracket-review]').scrollIntoView({block:"nearest"});
  }
  async function prepare(event) {
    event.preventDefault();
    if (busy || !api.canSend()) return;
    var source = selected(); if (!source || !source.candidate) return;
    var host = root, issued = epoch, local = api, values = new FormData(event.currentTarget);
    var body = {api_key:host.querySelector('[name="api_key"]').value.trim(),sub_account_id:sub(),session_id:source.session_id,
      action_request_id:source.action_request_id,ttl_minutes:Number(values.get("ttl_minutes")),
      max_spread_bps:Number(values.get("max_spread_bps")),max_slippage_bps:Number(values.get("max_slippage_bps"))};
    ["size","limit_price","entry_cap","exit_cap","stop_loss","take_profit"].forEach(function (name) { body[name] = String(values.get(name)); });
    secrets(host); invalidate(); mutationSerial += 1; busy = true; guards(); notice("Verificando señal, cuenta y protección…");
    try {
      var pending = local.request(base + "/preview", {method:"POST",body:JSON.stringify(body)}); body.api_key = "";
      var result = await pending; if (current(host, issued)) { showReview(result); notice("Revisión preparada · pendiente de firma."); }
    } catch (error) { if (current(host, issued)) notice(error.message, true); }
    finally { body.api_key = ""; if (current(host, issued)) { busy = false; guards(); } }
  }
  async function send(event) {
    event.preventDefault();
    if (watchReview) { await sendWatch(event); return; }
    if (closeReview) { await sendStrategyClose(event); return; }
    if (busy || !api.canSend() || !validPreview()) return;
    var host = root, issued = epoch, local = api, reviewed = prepared, p = reviewed.preview;
    var emergency = !!p.parent_client_order_id, id = previewId(p), values = new FormData(event.currentTarget);
    if (!confirm((emergency ? "Cerrar restante" : "Enviar entrada protegida") + " · GRVT TESTNET\n" +
        p.instrument + " · " + p.size + "\nUn único intento. ¿Firmar y enviar?")) return;
    if (!validPreview()) { secrets(host); guards(); return; }
    var body = {preview:p,preview_mac:reviewed.preview_mac,api_key:host.querySelector('[name="api_key"]').value.trim(),
      dedicated_private_key:String(values.get("dedicated_private_key")).trim(),expected_signer:String(values.get("expected_signer")).trim(),
      confirmation:emergency ? "FIRMAR_CIERRE_EMERGENCIA_GRVT_TESTNET" : "FIRMAR_ENTRADA_PROTEGIDA_GRVT_TESTNET"};
    attempted.add(id); secrets(host); mutationSerial += 1; busy = true; guards(); notice("Enviando · ID " + id);
    try {
      var pending = local.request(base + (emergency ? "/" + p.parent_client_order_id + "/emergency/send" : "/send"),
        {method:"POST",body:JSON.stringify(body)});
      body.api_key = ""; body.dedicated_private_key = ""; body.expected_signer = "";
      await pending;
      if (current(host, issued)) notice("Acuse registrado · consulta y audita las ejecuciones.");
    } catch (_) { if (current(host, issued)) notice("Envío bloqueado o incierto · consulta el ID " + id + ". No se reenvía.", true); }
    finally {
      secrets(host); body.api_key = ""; body.dedicated_private_key = "";
      if (current(host, issued)) { busy = false; invalidate(); await refresh(); }
    }
  }
  async function reviewStrategyClose(parent) {
    var group = groups.find(function (item) { return item.parent_client_order_id === parent; });
    if (!group) return;
    var saved = group.strategy_close && group.strategy_close.request, source = candidateClose(group);
    if (!saved && !source) { notice("No hay una señal de cierre vigente.", true); return; }
    var host = root, issued = epoch, local = api;
    var attempts = Number(root.querySelector('[name="close_attempts"]').value), ttl = Number(root.querySelector('[name="close_ttl"]').value);
    if (!saved && (!Number.isInteger(attempts) || attempts < 1 || attempts > 3 || !Number.isInteger(ttl) || ttl < 10 || ttl > 300)) {
      notice("Intentos: 1–3. Plazo: 10–300 s.", true); return;
    }
    invalidate();
    if (!saved) {
      secrets(host); mutationSerial += 1; busy = true; guards(); notice("Verificando señal de cierre…");
      try {
        var result = await local.request(base + "/" + parent + "/strategy-close/prepare", {method:"POST",body:JSON.stringify({
          action_request_id:source.action_request_id,max_attempts:attempts,ttl_seconds:ttl,confirmation:"PREPARAR_CIERRE_ESTRATEGIA_GRVT_TESTNET"})});
        if (!current(host, issued)) return;
        if (result.network !== "TESTNET" || !result.request) throw new Error("Preparación no verificable");
        saved = result.request;
      } catch (error) { if (current(host, issued)) notice(error.message, true); return; }
      finally { if (current(host, issued)) { busy = false; guards(); } }
    }
    if (!current(host, issued)) return;
    closeReview = {parent:parent, sub:sub(), existing:!!saved,
      action_request_id:saved ? saved.close_source.action_request_id : source.action_request_id,
      expires_at_ms:saved ? deadlineMs(saved.deadline_ns) : source.expires_at_ms,
      max_attempts:saved ? saved.max_attempts : 2,
      ttl_seconds:saved ? Math.ceil((deadlineMs(saved.deadline_ns) - deadlineMs(saved.created_at_ns)) / 1000) : 120};
    var box = root.querySelector('[data-bracket-preview]');
    box.replaceChildren(node("strong", "Cierre de estrategia · TESTNET"), node("p", group.instrument + " · " + group.signal_source.direction),
      node("small", "Cancelar protecciones → conciliar → reducir el residuo. STOP permanece activo.", "muted"));
    box.append(node("p", "Hasta " + closeReview.max_attempts + " intentos · plazo original · pendiente de firma."));
    root.querySelector('[name="close_attempts"]').value = closeReview.max_attempts;
    root.querySelector('[name="close_ttl"]').value = closeReview.ttl_seconds;
    root.querySelector('[data-bracket-review]').hidden = false;
    root.querySelector('[data-bracket-entry]').open = false;
    root.querySelector('[data-bracket-send]').textContent = "Ejecutar cierre · TESTNET";
    root.querySelector('[data-bracket-invalidate]').textContent = "Cerrar revisión";
    notice("Cierre preparado · pendiente de firma.");
    guards(); root.querySelector('[data-bracket-review]').scrollIntoView({block:"nearest"});
  }
  async function sendStrategyClose(event) {
    if (busy || !api.canSend() || !validPreview()) return;
    var host = root, issued = epoch, local = api, review = closeReview, values = new FormData(event.currentTarget);
    var attempts = review.existing ? review.max_attempts : Number(values.get("close_attempts"));
    var ttl = review.existing ? review.ttl_seconds : Number(values.get("close_ttl"));
    if (!confirm("Cierre de estrategia · GRVT TESTNET\nCancela SL/TP, audita y reduce sólo el residuo conciliado.\nHasta " + attempts +
        " intentos · " + (review.existing ? "plazo original" : ttl + " s") + ". STOP sigue activo. ¿Ejecutar?")) return;
    if (!validPreview()) { secrets(host); guards(); return; }
    var body = {action_request_id:review.action_request_id,max_attempts:attempts,ttl_seconds:ttl,
      api_key:host.querySelector('[name="api_key"]').value.trim(),dedicated_private_key:String(values.get("dedicated_private_key")).trim(),
      expected_signer:String(values.get("expected_signer")).trim(),confirmation:"EJECUTAR_CIERRE_ESTRATEGIA_GRVT_TESTNET"};
    secrets(host); mutationSerial += 1; busy = true; guards(); notice("Iniciando cierre…");
    try {
      var pending = local.request(base + "/" + review.parent + "/strategy-close/start", {method:"POST",body:JSON.stringify(body)});
      body.api_key = ""; body.dedicated_private_key = ""; body.expected_signer = "";
      await pending;
      if (current(host, issued)) notice("Cierre iniciado · consulta su progreso o detenlo desde el registro.");
    } catch (error) { if (current(host, issued)) notice("Cierre no confirmado · consulta su estado. " + error.message, true); }
    finally {
      body.api_key = ""; body.dedicated_private_key = ""; body.expected_signer = "";
      if (current(host, issued)) { busy = false; invalidate(); await refresh(); }
    }
  }
  function reviewWatch(parent, recovery) {
    var group = groups.find(function (g) { return g.parent_client_order_id === parent; });
    if (!group) return;
    var request = group.strategy_close && group.strategy_close.request;
    var revision = group.strategy_close && group.strategy_close.control_revision;
    if (!sameRevision(revision,revision)) { notice("Consulta de nuevo el registro.", true); return; }
    if (request && !["orbis.grvt.testnet.strategy.close.v2", "orbis.grvt.testnet.native.partial.close.v1", "orbis.grvt.testnet.native.recovery.close.v1"].includes(request.schema)) return;
    recovery = !!recovery || !!request && request.schema === "orbis.grvt.testnet.native.recovery.close.v1";
    var attempts = request ? request.max_attempts : Number(root.querySelector('[name="close_attempts"]').value);
    var ttl = request ? Math.max(30, Math.ceil((deadlineMs(request.deadline_ns) - Date.now()) / 1000)) :
      Math.round(Number(root.querySelector('[name="watch_minutes"]').value) * 60);
    if (recovery && !request) ttl = Number(root.querySelector('[name="close_ttl"]').value);
    if (!Number.isInteger(attempts) || attempts < 1 || attempts > 3 || !Number.isInteger(ttl) || ttl < 30 || ttl > (recovery ? 300 : 86400)) {
      notice("Revisa el plazo y los intentos.", true); return;
    }
    invalidate();
    watchReview = {parent:parent,sub:sub(),max_attempts:attempts,ttl_seconds:ttl,existing:!!request,
      expires_at_ms:request ? deadlineMs(request.deadline_ns) : Date.now() + 120000,instrument:group.instrument,
      permission_revision:revision.slice(),recovery_only:recovery};
    var box = root.querySelector('[data-bracket-preview]');
    box.replaceChildren(node("strong", request ? "Continuar salida · TESTNET" : recovery ? "Recuperar grupo · TESTNET" : "Seguir salidas · TESTNET"));
    box.append(node("p", group.instrument + " · " + group.signal_source.direction + " · " + group.legs[0].size));
    box.append(node("p", "Hasta " + attempts + " intentos · " + (request ? "plazo original" : ttl / 60 + " min")));
    box.append(node("small", "Sólo esta posición · sin nuevas entradas", "muted"));
    root.querySelector('[data-bracket-send]').textContent = recovery ? "Recuperar · TESTNET" : "Activar salidas · TESTNET";
    root.querySelector('[data-bracket-invalidate]').textContent = "Cerrar revisión";
    root.querySelector('[data-bracket-review]').hidden = false;
    guards(); root.querySelector('[data-bracket-review]').scrollIntoView({block:"nearest"});
  }
  function sameRevision(a, b) {
    return Array.isArray(a) && Array.isArray(b) && a.length === 2 && b.length === 2 &&
      a.every(function (value, i) { return Number.isSafeInteger(value) && value >= 0 && value === b[i]; });
  }
  async function watchRequest(local, path, options, passive, milliseconds) {
    var controller = new AbortController(), timeout = setTimeout(function () { controller.abort(); }, milliseconds);
    try { return await local.request(path, Object.assign({}, options || {}, {signal:controller.signal}), passive); }
    finally { clearTimeout(timeout); }
  }
  function showWatchState(review, result) {
    if (result.network !== "TESTNET" || result.authorization !== false || !result.run ||
        !sameRevision(result.run.permission_revision,review.permission_revision)) throw new Error("Estado no confirmado");
    confirmRecoveredState(review.parent, result.run.permission_revision);
    if (panelSnapshot && sub() === review.sub) renderGroups(Object.assign({}, panelSnapshot, {groups:panelSnapshot.groups.map(function (group) {
      return group.parent_client_order_id === review.parent ? Object.assign({}, group, {strategy_close:result}) : group;
    })}));
  }
  async function sendWatch(event) {
    if (busy || !api.canSend() || !watchReview || !validPreview()) return;
    var host = root, issued = epoch, local = api, review = watchReview, values = new FormData(event.currentTarget);
    if (!confirm((review.recovery_only ? "Recuperación" : "Salidas automáticas") + " · GRVT TESTNET\n" + review.instrument + " · hasta " + review.max_attempts +
        " intentos · " + (review.existing ? "plazo original" : review.ttl_seconds / 60 + " min") +
        ".\nLa parada no liquida la posición. ¿Activar?")) return;
    if (!validPreview()) { secrets(host); guards(); return; }
    var body = {api_key:host.querySelector('[name="api_key"]').value.trim(),
      dedicated_private_key:String(values.get("dedicated_private_key")).trim(),expected_signer:String(values.get("expected_signer")).trim(),
      max_attempts:review.max_attempts,ttl_seconds:review.ttl_seconds,
      confirmation:review.recovery_only ? "RECUPERAR_GRUPO_GRVT_TESTNET" : "ACTIVAR_SALIDAS_ESTRATEGIA_GRVT_TESTNET"};
    body.permission_revision = review.permission_revision.slice();
    secrets(host); mutationSerial += 1; var operation = mutationSerial; busy = true; guards(); notice("Activando salidas…");
    try {
      var pending = watchRequest(local, base + "/" + review.parent + (review.recovery_only ? "/native-recovery/start" : "/strategy-exit/start"), {method:"POST",body:JSON.stringify(body)}, false, 5000);
      body.api_key = ""; body.dedicated_private_key = ""; body.expected_signer = "";
      var result = await pending;
      if (current(host, issued)) {
        showWatchState(review, result);
        notice("Seguimiento activado · consulta el estado o detenlo desde el registro.");
      }
    } catch (error) {
      if (current(host, issued)) {
        notice("Activación no confirmada · consultando estado.", true);
        uncertainStarts.set(review.parent,review.permission_revision.slice());
        if (panelSnapshot) renderGroups(panelSnapshot);
        busy = false; invalidate();
        try {
          var recovered = await watchRequest(local, base + "/" + review.parent + "/strategy-close", null, true, 3000);
          if (current(host, issued) && mutationSerial === operation) showWatchState(review, recovered);
        } catch (_) { /* Sin reenviar. */ }
      }
    } finally {
      body.api_key = ""; body.dedicated_private_key = ""; body.expected_signer = "";
      if (current(host, issued)) {
        if (mutationSerial === operation) { busy = false; invalidate(); }
        await refresh();
      }
    }
  }
  function clearProbe() {
    if (probe && probe.controller) probe.controller.abort();
    probe = null;
    var box = root && root.querySelector("[data-bracket-probe]");
    if (!box) return;
    box.hidden = true;
    box.querySelector("form").reset();
    var comparison = box.querySelector("[data-probe-comparison]");
    comparison.open = false;
    var comparisonForm = comparison.querySelector("form");
    comparisonForm.reset();
    comparisonForm.elements.probe_source.disabled = false;
    setProbeSource(comparisonForm);
    comparison.querySelector("button").disabled = false;
    for (var name of ["saved_sample","saved_before","saved_after","saved_final"]) {
      box.querySelector('[name="' + name + '"]').replaceChildren(new Option("Selecciona muestra", ""));
    }
    box.querySelector("[data-probe-archive-state]").textContent = "";
    box.querySelector('[name="saved_sample"]').disabled = false;
    box.querySelector("[data-probe-open-saved]").disabled = true;
    comparison.querySelector("[data-comparison-result]").replaceChildren();
    box.querySelector("[data-probe-capture]").disabled = false;
    box.querySelector("[data-probe-result]").textContent = "";
    box.querySelector("[data-probe-download]").hidden = true;
  }
  function openProbe(parent) {
    var group = groups.find(function (item) { return item.parent_client_order_id === parent; });
    if (!group) return;
    clearProbe();
    probe = {parent:parent, sub:sub(), sample:null, controller:null, archiveSequence:0, pendingCapture:null};
    var box = root.querySelector("[data-bracket-probe]");
    box.hidden = false;
    box.querySelector("[data-probe-title]").textContent = group.instrument + " · " + group.signal_source.direction + " · TESTNET";
    box.querySelector("h3").focus();
    box.scrollIntoView({block:"nearest"});
    loadSavedProbes(probe, root, epoch);
  }
  function activeProbe(job, host, issued) {
    return current(host, issued) && probe === job && host.open && job.sub === sub() && api.canSend();
  }
  function setProbeSource(form) {
    var saved = form.elements.probe_source.value === "saved";
    var fromFiles = form.querySelector("[data-probe-from-files]");
    var fromSaved = form.querySelector("[data-probe-from-saved]");
    fromFiles.disabled = fromFiles.hidden = saved;
    fromSaved.disabled = fromSaved.hidden = !saved;
  }
  async function loadSavedProbes(job, host, issued) {
    var serial = ++job.archiveSequence, path = base + "/" + job.parent + "/market-samples";
    var status = host.querySelector("[data-probe-archive-state]");
    try {
      var archive = await api.request(path, null, true);
      if (!activeProbe(job, host, issued) || serial !== job.archiveSequence) return;
      if (archive.network !== "TESTNET" || archive.authorization !== false || !Array.isArray(archive.samples) ||
          archive.samples.length > 32 || archive.samples.some(function (row) {
            return !/^[0-9a-f-]{36}$/.test(row.id) || !/^[0-9]{1,19}$/.test(row.observed_at_ns) ||
              typeof row.has_ioc !== "boolean" || !/^[0-9]+(?:\.[0-9]+)?$/.test(row.remaining_size);
          })) throw new Error("Archivo TESTNET no verificable");
      for (var name of ["saved_sample","saved_before","saved_after","saved_final"]) {
        var select = host.querySelector('[name="' + name + '"]'), previous = select.value;
        select.replaceChildren(new Option("Selecciona muestra", ""));
        archive.samples.forEach(function (row) {
          var stamp = new Date(Number(BigInt(row.observed_at_ns) / 1000000n)).toISOString();
          select.add(new Option(stamp + " · " + (row.has_ioc ? "Con IOC" : "Inicial") + " · Posición " + row.remaining_size, row.id));
        });
        if (archive.samples.some(function (row) { return row.id === previous; })) select.value = previous;
      }
      host.querySelector("[data-probe-open-saved]").disabled = !host.querySelector('[name="saved_sample"]').value;
      status.textContent = archive.samples.length ? archive.samples.length + " guardadas" : "Sin muestras guardadas";
    } catch (error) {
      if (activeProbe(job, host, issued) && serial === job.archiveSequence)
        status.textContent = error.message || "Archivo TESTNET no disponible";
    }
  }
  function presentProbe(sample, job, host) {
    if (sample.schema !== "orbis.grvt.testnet.market-probe.v1" || sample.network !== "TESTNET" ||
        sample.authorization !== false || !/^[0-9]{1,19}$/.test(sample.observed_at_ns) ||
        !Array.isArray(sample.orders) || !sample.orders[0] || sample.orders[0].client_order_id !== job.parent ||
        sample.orders[0].sub_account_id !== job.sub) throw new Error("Muestra no verificable");
    job.sample = sample;
    host.querySelector("[data-probe-result]").textContent = "Posición restante · " + sample.remaining_size +
      " · Stop declarado · " + sample.stop_order_size +
      " · Tamaño en libro · " + (sample.stop_book_size === null ? "sin dato" : sample.stop_book_size) +
      (sample.ioc_terminal === null ? "" : " · IOC " + (sample.ioc_terminal ? "terminal" : "pendiente"));
    host.querySelector("[data-probe-download]").hidden = false;
  }
  async function openSavedProbe(event) {
    var job = probe, host = root, issued = epoch, button = event.currentTarget;
    if (!job || job.controller || !activeProbe(job, host, issued)) return;
    var select = host.querySelector('[name="saved_sample"]'), identifier = select.value;
    if (!identifier) return;
    var controller = new AbortController(), result = host.querySelector("[data-probe-result]");
    job.controller = controller; job.sample = null; button.disabled = true; select.disabled = true;
    host.querySelector("[data-probe-download]").hidden = true;
    result.textContent = "Abriendo muestra…";
    try {
      var sample = await api.request(base + "/" + job.parent + "/market-samples/" + encodeURIComponent(identifier),
        {signal:controller.signal}, true);
      if (activeProbe(job, host, issued)) presentProbe(sample, job, host);
    } catch (error) {
      if (activeProbe(job, host, issued)) result.textContent = error.message || "Muestra no disponible";
    } finally {
      if (activeProbe(job, host, issued)) { job.controller = null; button.disabled = false; select.disabled = false; }
    }
  }
  async function captureProbe(event) {
    event.preventDefault();
    if (!probe || probe.controller || !api.canSend() || probe.sub !== sub()) return;
    var job = probe, host = root, issued = epoch, form = event.currentTarget;
    var key = form.elements.probe_key, value = form.elements.probe_ioc.value.trim();
    var result = host.querySelector("[data-probe-result]"), download = host.querySelector("[data-probe-download]");
    var submit = form.querySelector("[data-probe-capture]"), body = {api_key:key.value.trim()};
    if (!body.api_key) { key.focus(); return; }
    var selector = value ? form.elements.probe_id_type.value : "";
    if (selector) body[selector] = value;
    if (!job.pendingCapture || job.pendingCapture.selector !== selector || job.pendingCapture.value !== value)
      job.pendingCapture = {selector:selector, value:value, id:crypto.randomUUID()};
    body.archive_id = job.pendingCapture.id;
    key.value = "";
    var controller = new AbortController();
    job.controller = controller; job.sample = null; submit.disabled = true; download.hidden = true;
    result.textContent = "Consultando GRVT Testnet…";
    function active() { return activeProbe(job, host, issued); }
    try {
      var pending = api.request(base + "/" + job.parent + "/market-sample",
        {method:"POST",body:JSON.stringify(body),signal:controller.signal});
      body.api_key = "";
      var sample = await pending;
      if (!active()) return;
      presentProbe(sample, job, host);
      job.pendingCapture = null;
    } catch (error) {
      if (active()) result.textContent = error.message || "Muestra no disponible.";
    } finally {
      body.api_key = "";
      if (active()) {
        job.controller = null; submit.disabled = false;
        loadSavedProbes(job, host, issued);
      }
    }
  }
  function downloadProbe() {
    if (!probe || !probe.sample || !api.canSend() || probe.sub !== sub()) return;
    var sample = probe.sample, href = URL.createObjectURL(new Blob([JSON.stringify(sample, null, 2) + "\n"],
      {type:"application/json"}));
    var link = document.createElement("a");
    link.href = href; link.download = "orbis-grvt-testnet-" + sample.observed_at_ns + ".json";
    link.click();
    setTimeout(function () { URL.revokeObjectURL(href); }, 1000);
  }
  function showComparison(result, view) {
    var table = node("table"), head = node("tr"), body = node("tbody");
    var timeline = !!view.middle;
    var columns = timeline ? ["Medida", "Primera", "Intermedia", "Final"] : ["Medida", "Antes", "Después"];
    columns.forEach(function (label) {
      var cell = node("th", label); cell.scope = "col"; head.append(cell);
    });
    table.append(node("thead"), body); table.tHead.append(head);
    function add(label, values) {
      var row = node("tr"), name = node("th", label);
      name.scope = "row"; row.append(name);
      values.forEach(function (value) { row.append(node("td", value)); });
      body.append(row);
    }
    var states = timeline ? [view.before,view.middle,view.after] : [view.before,view.after];
    var labels = {PENDING:"Pendiente",OPEN:"Abierto",CANCELLED:"Cancelado",REJECTED:"Rechazado",FILLED:"Ejecutado"};
    add("Posición restante", states.map(function (row) { return row.position_size; }));
    add("Stop", states.map(function (row) { return labels[row.stop_status] || row.stop_status; }));
    add("Tamaño declarado", states.map(function (row) { return row.stop_size; }));
    add("Tamaño en libro", states.map(function (row) { return row.stop_book_size === null ? "Sin dato" : row.stop_book_size; }));
    add("En órdenes abiertas", states.map(function (row) { return row.stop_listed ? "Sí" : "No"; }));
    add("Stop ejecutado", states.map(function (row) { return row.stop_filled; }));
    add("Motivo del stop", states.map(function (row) {
      if (row.stop_reason === "REDUCE_ONLY_LIMIT") return "Límite reduce-only";
      return row.stop_reason && row.stop_reason !== "UNSPECIFIED" ? row.stop_reason : "—";
    }));
    add("Objetivo ejecutado", states.map(function (row) { return row.target_filled; }));
    add("IOC ejecutado", states.map(function (row) {
      return row.ioc_filled === null ? "—" : row.ioc_filled + (row.ioc_terminal ? " · Terminal" : " · Pendiente");
    }));
    function money(interval, label) {
      return node("p", label + "Variación de saldo " + interval.cash_delta_usdt + " USDT · Fills netos " +
        interval.fill_delta_usdt + " USDT · Diferencia " + interval.unexplained_cash_delta_usdt + " USDT",
        interval.unexplained_cash_delta_usdt === "0" ? "muted" : "amber");
    }
    if (!timeline) {
      result.replaceChildren(table, money(view, ""));
      return;
    }
    var sequence = {
      NONE:"Sin fills de IOC ni stop", IOC_ONLY:"Solo IOC", STOP_ONLY:"Solo stop",
      IOC_THEN_STOP:"IOC → Stop", STOP_THEN_IOC:"Stop → IOC",
      INTERLEAVED_OR_TIED:"Fills intercalados o simultáneos"
    };
    result.replaceChildren(table, node("p", "Orden de fills · " + sequence[view.stop_ioc_fill_order]),
      money(view.intervals[0], "Primera → Intermedia · "), money(view.intervals[1], "Intermedia → Final · "));
  }
  async function compareProbes(event) {
    event.preventDefault();
    if (!probe || probe.controller || !api.canSend() || probe.sub !== sub()) return;
    var job = probe, host = root, issued = epoch, form = event.currentTarget;
    var saved = form.elements.probe_source.value === "saved";
    var first = saved ? form.elements.saved_before.value : form.elements.sample_before.files[0];
    var second = saved ? form.elements.saved_after.value : form.elements.sample_after.files[0];
    var final = saved ? form.elements.saved_final.value : form.elements.sample_final.files[0];
    var selected = final ? [first,second,final] : [first,second];
    var result = form.parentElement.querySelector("[data-comparison-result]"), submit = form.querySelector("button");
    if (selected.some(function (item) { return !item || !saved && item.size > 4 * 1024 * 1024; })) {
      result.textContent = saved ? "Elige dos o tres muestras guardadas" : "Elige dos o tres archivos JSON de hasta 4 MiB por archivo.";
      return;
    }
    var controller = new AbortController(), capture = host.querySelector("[data-probe-capture]");
    job.controller = controller; submit.disabled = true; capture.disabled = true;
    form.elements.probe_source.disabled = true;
    result.textContent = "Comparando muestras…";
    function active() { return activeProbe(job, host, issued); }
    try {
      var body;
      if (saved) {
        body = {before_id:first,after_id:second};
        if (final) body.final_id = final;
      } else {
        var texts = await Promise.all(selected.map(function (file) { return file.text(); }));
        body = {before:texts[0],after:texts[1]};
        if (final) body.final = texts[2];
      }
      if (!active()) return;
      var view = await api.request(base + "/" + job.parent + "/market-comparison", {method:"POST",
        body:JSON.stringify(body),signal:controller.signal});
      if (!active()) return;
      var schema = "orbis.grvt.testnet.market-comparison." + (final ? "v2" : "v1");
      if (view.schema !== schema || view.network !== "TESTNET" || view.authorization !== false ||
          (final && (!view.middle || !Array.isArray(view.intervals) || view.intervals.length !== 2)))
        throw new Error("Comparación no verificable");
      showComparison(result, view);
    } catch (error) {
      if (active()) result.textContent = error.message || "Comparación no disponible.";
    } finally {
      if (active()) {
        job.controller = null; submit.disabled = false; capture.disabled = false;
        form.elements.probe_source.disabled = false;
      }
    }
  }
  async function act(event) {
    var target = event.target.closest('button[data-bracket-action]');
    if (!target || busy || !api.canSend()) return;
    var action = target.dataset.bracketAction, parent = target.dataset.parent, cid = target.dataset.clientId;
    if (action === "market-probe") { openProbe(parent); return; }
    if (action === "watch-history" || action === "watch-events") { await loadWatchHistory(target); return; }
    if (action === "strategy-review") { await reviewStrategyClose(parent); return; }
    if (action === "watch-review") { reviewWatch(parent); return; }
    if (action === "recovery-review") { reviewWatch(parent, true); return; }
    var host = root, issued = epoch, local = api, body = {}, path = base + "/" + parent + "/" + action;
    if (["observe","audit","cancel","emergency","emergency-audit","strategy-exit"].includes(action)) {
      body.api_key = host.querySelector('[name="api_key"]').value.trim();
      if (!body.api_key) { notice("Introduce la API key TESTNET.", true); return; }
    }
    if (action === "reconcile") body.confirmation = "CONCILIAR_ENTRADA_PROTEGIDA_GRVT_TESTNET";
    if (action === "abandon") body.confirmation = "DESCARTAR_ENTRADA_PROTEGIDA_TESTNET";
    if (action === "strategy-stop") {
      path = base + "/" + parent + "/strategy-close/stop";
      body.confirmation = "DETENER_CIERRE_ESTRATEGIA_GRVT_TESTNET";
    }
    if (action === "cancel") {
      if (!confirm("Retirar protección · TESTNET\nActiva STOP y cancela las salidas. La posición seguirá abierta. ¿Continuar?")) return;
      body.confirmation = "RETIRAR_PROTECCIONES_ENTRADA_TESTNET"; body.acknowledge_unprotected_position = true;
    }
    if (action === "emergency") path = base + "/" + parent + "/emergency/preview";
    if (action === "strategy-exit") path = base + "/" + parent + "/strategy-exit/review";
    if (action === "emergency-audit" || action === "emergency-reconcile") {
      path = base + "/" + parent + "/emergencies/" + cid + "/" + (action === "emergency-audit" ? "audit" : "reconcile");
      if (action === "emergency-reconcile") body.confirmation = "CONCILIAR_CIERRE_EMERGENCIA_TESTNET";
    }
    var recoveryKey = action === "cancel" ? body.api_key : "";
    secrets(host); mutationSerial += 1; busy = true; guards(); notice("Verificando " + (cid || parent) + "…");
    try {
      var pending = action === "strategy-stop" ? watchRequest(local,path,{method:"POST",body:JSON.stringify(body)},false,5000) :
        local.request(path, {method:"POST",body:JSON.stringify(body)}); body.api_key = "";
      var result = await pending;
      if (current(host, issued)) {
        if (action === "strategy-stop" && result.network === "TESTNET") {
          uncertainStarts.delete(parent);
          if (panelSnapshot) renderGroups(Object.assign({}, panelSnapshot, {groups:panelSnapshot.groups.map(function (group) {
            return group.parent_client_order_id === parent ? Object.assign({}, group, {
              strategy_close:Object.assign({}, group.strategy_close, {run:result.run})
            }) : group;
          })}));
        }
        if (action === "emergency") showReview(result);
        if (action === "strategy-exit") {
          if (result.network !== "TESTNET" || result.review_only !== true || result.authorization !== false ||
              !result.decision || result.decision.execution_authorized !== false) throw new Error("Evaluación no verificable");
          exitReviews.set(parent, result);
        }
        notice(action === "strategy-exit" ? "Evaluación puntual · órdenes sin cambios." : "Registro actualizado.");
      }
    } catch (error) {
      if (current(host, issued) && action === "cancel" && recoveryKey && local.canSend()) {
        try {
          // Sólo lectura de los tres IDs: una respuesta perdida no permite
          // volver a enviar la cancelación ni afirmar que la posición cerró.
          await local.request(base + "/" + parent + "/observe", {method:"POST",body:JSON.stringify({api_key:recoveryKey})});
          if (current(host, issued)) notice("Estados recuperados · revisa la posición.");
        } catch (_) { if (current(host, issued)) notice("Cancelación incierta · consulta los tres IDs.", true); }
      } else if (current(host, issued)) notice(error.message, true);
    }
    finally { recoveryKey = ""; body.api_key = ""; if (current(host, issued)) { busy = false; guards(); await refresh(); } }
  }
  function schedule() {
    var issued = epoch;
    timer = setTimeout(async function () {
      timer = null;
      if (!root || epoch !== issued) return;
      if (!root.isConnected) { clear(); return; }
      guards(); if (root.open && !busy) await refresh();
      if (root && epoch === issued) schedule();
    }, 1500);
  }
  function mount(host, services) {
    clear(); root = host; api = services;
    host.innerHTML = '<summary>Entrada protegida · TESTNET</summary><p class="muted">Entrada manual · salidas automáticas opcionales.</p>' +
      '<fieldset data-bracket-controls class="grvt-oco-controls"><form data-bracket-form class="grvt-testnet-send-form" autocomplete="off"><div class="real-form">' +
      '<label class="field">Señal<select name="source" required><option value="">Señal PAPER</option></select></label>' +
      '<label class="field">Subcuenta<input name="sub_account_id" inputmode="numeric" pattern="[0-9]+" maxlength="30" required></label>' +
      '<label class="field">API key TESTNET<input name="api_key" type="password" autocomplete="off" maxlength="512" spellcheck="false"></label>' +
      '<label class="field">Cantidad<input name="size" inputmode="decimal" required></label><label class="field">Límite USDT<input name="limit_price" inputmode="decimal" required></label>' +
      '<label class="field">Stop<input name="stop_loss" inputmode="decimal" required></label><label class="field">Objetivo<input name="take_profit" inputmode="decimal" required></label>' +
      '<label class="field">Tope entrada<input name="entry_cap" value="100" inputmode="decimal" required></label><label class="field">Tope salidas<input name="exit_cap" value="100" inputmode="decimal" required></label></div>' +
      '<details><summary>Condiciones</summary><div class="real-form"><label class="field">Vigencia salidas · min<input name="ttl_minutes" type="number" value="60" min="2" max="1440" required></label>' +
      '<label class="field">Spread · pb<input name="max_spread_bps" type="number" value="25" min="0" max="100" required></label>' +
      '<label class="field">Deslizamiento · pb<input name="max_slippage_bps" type="number" value="25" min="0" max="100" required></label></div></details>' +
      '<div class="row"><button type="button" class="ghost" data-bracket-refresh>Consultar</button><button type="submit" data-bracket-prepare disabled>Revisar entrada</button></div></form>' +
      '<section data-bracket-review hidden><div data-bracket-preview class="grvt-oco-preview"></div><small class="muted" data-bracket-expiry></small>' +
      '<form data-bracket-sign autocomplete="off" class="grvt-testnet-send-form"><div class="real-form"><label class="field" data-help="Clave de firma dedicada exclusivamente a GRVT TESTNET. Nunca una semilla o clave de tu wallet patrimonial.">Firma TESTNET<input name="dedicated_private_key" type="password" autocomplete="off" spellcheck="false" required></label>' +
      '<label class="field">Firmante<input name="expected_signer" autocomplete="off" spellcheck="false" required></label></div>' +
      '<div class="row"><button type="submit" data-bracket-send disabled>Firmar entrada · TESTNET</button>' +
      '<button type="button" class="ghost" data-bracket-invalidate>Descartar revisión</button></div></form></section>' +
      '<section data-bracket-probe class="grvt-bracket-emergency" hidden aria-label="Muestra de parciales"><h3 tabindex="-1">Muestra de parciales</h3><p data-probe-title></p>' +
      '<form autocomplete="off"><div class="real-form"><label class="field">API View · TESTNET<input name="probe_key" type="password" autocomplete="off" spellcheck="false" minlength="8" maxlength="256" required></label>' +
      '<label class="field">Tipo de ID IOC<select name="probe_id_type"><option value="ioc_order_id">Exchange</option><option value="ioc_client_id">Cliente</option></select></label>' +
      '<label class="field" data-help="Vacío para tomar la muestra inicial. Después de reducir, identifica el IOC original.">ID IOC · opcional<input name="probe_ioc" maxlength="80" autocomplete="off" spellcheck="false" pattern="[A-Za-z0-9]+"></label></div>' +
      '<div class="row"><button type="submit" data-probe-capture data-help="Lee GRVT TESTNET y guarda la muestra en la bóveda cifrada.">Capturar</button><button type="button" class="ghost" data-probe-download hidden>Descargar JSON</button><button type="button" class="ghost" data-probe-close>Cerrar</button></div></form><p class="muted" role="status" data-probe-result></p>' +
      '<div class="row"><label class="field">Muestras guardadas<select name="saved_sample"></select></label>' +
      '<button type="button" class="ghost" data-probe-open-saved disabled>Abrir muestra</button>' +
      '<small class="muted" data-probe-archive-state role="status"></small></div>' +
      '<details data-probe-comparison><summary data-help="Ordena las capturas por fecha. Con tres, puedes revisar por separado el IOC y el stop.">Comparar muestras</summary><form>' +
      '<label class="field">Origen<select name="probe_source"><option value="saved">Muestras guardadas</option><option value="files">Archivos JSON</option></select></label>' +
      '<fieldset class="real-form grvt-oco-controls" data-probe-from-saved><label class="field">1 · Primera<select name="saved_before" required></select></label>' +
      '<label class="field">2 · Siguiente<select name="saved_after" required></select></label>' +
      '<label class="field">3 · Final · opcional<select name="saved_final"></select></label></fieldset>' +
      '<fieldset class="real-form grvt-oco-controls" data-probe-from-files hidden disabled>' +
      '<label class="field">1 · Primera<input name="sample_before" type="file" accept=".json,application/json" required></label>' +
      '<label class="field">2 · Siguiente<input name="sample_after" type="file" accept=".json,application/json" required></label>' +
      '<label class="field">3 · Final · opcional<input name="sample_final" type="file" accept=".json,application/json"></label></fieldset>' +
      '<button type="submit" class="ghost">Comparar</button></form><div data-comparison-result aria-live="polite"></div></details></section>' +
      '<div data-bracket-groups class="grvt-oco-groups"></div><div class="row"><button type="button" class="ghost" data-bracket-prev disabled>Más recientes</button><small data-bracket-page class="muted"></small>' +
      '<button type="button" class="ghost" data-bracket-next disabled>Anteriores</button></div></fieldset><div data-bracket-state class="state muted" role="status">Consulta una señal PAPER y tu subcuenta TESTNET.</div>';
    var controls = host.querySelector('[data-bracket-controls]'), form = host.querySelector('[data-bracket-form]');
    var toolbar = document.createElement("div"); toolbar.className = "real-form";
    toolbar.append(host.querySelector('[name="sub_account_id"]').closest("label"), host.querySelector('[name="api_key"]').closest("label"), host.querySelector('[data-bracket-refresh]'));
    controls.prepend(toolbar);
    var closeSettings = document.createElement("details"); closeSettings.dataset.bracketCloseLimits = "";
    closeSettings.innerHTML = '<summary>Cierre · límites</summary><div class="real-form"><label class="field">Intentos máximos<input name="close_attempts" type="number" value="2" min="1" max="3"></label>' +
      '<label class="field">Plazo · s<input name="close_ttl" type="number" value="120" min="10" max="300"></label>' +
      '<label class="field">Seguimiento · min<input name="watch_minutes" type="number" value="15" min="0.5" max="1440" step="0.5"></label></div>';
    controls.insertBefore(closeSettings, form);
    controls.insertBefore(host.querySelector('[data-bracket-groups]'), form);
    var entry = document.createElement("details"); entry.dataset.bracketEntry = ""; entry.open = true;
    entry.append(node("summary", "Nueva entrada")); controls.insertBefore(entry, form); entry.append(form);
    controls.insertBefore(host.querySelector('[data-bracket-review]'), host.querySelector('[data-bracket-groups]'));
    var capture = host.querySelector('[data-bracket-probe]');
    capture.querySelector("form").addEventListener("submit", captureProbe);
    var comparisonForm = capture.querySelector("[data-probe-comparison] form");
    comparisonForm.addEventListener("submit", compareProbes);
    comparisonForm.elements.probe_source.addEventListener("change", function () {
      setProbeSource(comparisonForm);
      capture.querySelector("[data-comparison-result]").replaceChildren();
    });
    capture.querySelector('[name="saved_sample"]').addEventListener("change", function () {
      capture.querySelector("[data-probe-open-saved]").disabled = !this.value;
    });
    capture.querySelector("[data-probe-open-saved]").addEventListener("click", openSavedProbe);
    capture.querySelector("[data-probe-download]").addEventListener("click", downloadProbe);
    capture.querySelector("[data-probe-close]").addEventListener("click", clearProbe);

    host.querySelector('[data-bracket-form]').addEventListener("submit", prepare);
    host.querySelector('[data-bracket-sign]').addEventListener("submit", send);
    host.querySelector('[data-bracket-refresh]').addEventListener("click", refresh);
    host.querySelector('[data-bracket-invalidate]').addEventListener("click", invalidate);
    host.querySelector('[data-bracket-groups]').addEventListener("click", act);
    host.querySelector('[data-bracket-prev]').addEventListener("click", function () { offset = Math.max(0, offset - 50); refresh(); });
    host.querySelector('[data-bracket-next]').addEventListener("click", function () { offset += 50; refresh(); });
    host.querySelector('[data-bracket-form]').addEventListener("input", function (event) { if (event.target.name !== "api_key") invalidate(); });
    host.querySelector('[name="sub_account_id"]').addEventListener("input", function () {
      clearProbe(); offset = 0; invalidate(); host.querySelector('[data-bracket-groups]').replaceChildren();
      renderedGroups = null;
      exitReviews = new Map();
      panelSnapshot = null;
      uncertainStarts = new Map();
      watchHistories = new Map(); watchEvents = new Map(); historyReading = new Set();
    });
    host.addEventListener("toggle", function () { if (host.open) refresh(); else clearProbe(); });
    guards(); schedule();
  }
  function setSubaccount(value) {
    if (root && !busy) {
      if (sub() !== value) clearProbe();
      root.querySelector('[name="sub_account_id"]').value = value;
      if (root.open) refresh();
    }
  }
  window.OrbisGrvtBracket = {mount:mount,clear:clear,setSubaccount:setSubaccount};
}());
