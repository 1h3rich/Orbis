/* Bots nativos REAL: opt-in, estado pasivo y parada; sin claves en este módulo. */
(function () {
  "use strict";
  var root = null, api = null, epoch = 0, sequence = 0, timer = null, busy = false;
  var candidates = [], drafts = [], workflow = null, lastSnapshot = "", journals = {}, bots = [], rearmTarget = null;
  var preparationIssue = null, controlsLoading = false, controlsSequence = 0, statusAvailable = false, listError = false;
  var handoff = null, workflowError = false, pendingActivation = null;
  var reviewedResearch = null, researchPending = null;
  function clear() {
    epoch += 1;
    if (window.OrbisMexcBotChart) window.OrbisMexcBotChart.clear();
    if (timer !== null) clearTimeout(timer);
    timer = null; root = null; api = null; candidates = []; drafts = []; workflow = null; busy = false; lastSnapshot = ""; journals = {}; bots = []; rearmTarget = null; preparationIssue = null; controlsLoading = false; controlsSequence = 0; statusAvailable = false; listError = false; handoff = null; workflowError = false; pendingActivation = null;
    reviewedResearch = null; researchPending = null;
  }
  function current(host, account, issued) {
    return root === host && host.isConnected && api && api.getAccountId() === account && epoch === issued;
  }
  function notice(message, error) {
    var target = root && root.querySelector("[data-bots-state]");
    if (target) { if (target.textContent !== message) target.textContent = message; target.hidden = !message; target.className = "state " + (error ? "error" : "muted"); }
  }
  function currentHelp(element,detail) {
    element.dataset.help = detail;
    element.title = detail;
    element.setAttribute("aria-description",detail);
  }
  function handoffSource() {
    return handoff && candidates.find(function (item) {
      return item.session_id === handoff.id && item.source_kind === handoff.kind;
    });
  }
  // Estado del formulario, ayuda y, si procede, acción y destino en Cartera.
  function accountIssue() {
    if (!workflow) return workflowError ? ["Cuenta · sin verificar","No se pudo comprobar el estado local de esta cuenta."] :
      ["Cuenta · comprobando","Espera a que se lea el estado local de esta cuenta."];
    if (workflow.view === "DISCONNECTED") return ["View · conectar","Conecta una clave View de MEXC.","Conectar View","view"];
    if (workflow.view === "OTHER_ACCOUNT") return ["View · otra cuenta","La conexión View pertenece a otro libro REAL.","Elegir cuenta","account"];
    if (workflow.view === "DIFFERENT_CONNECTION") return ["View · revisar vínculo","Usa la clave View vinculada a este libro o revisa su renovación.","Revisar vínculo","link"];
    if (workflow.view === "UNLINKED") {
      var canOpenBalance = workflow.book_empty && !api.amountsHidden();
      return ["View · vincular","Vincula este libro a la conexión MEXC.",
        canOpenBalance ? "Registrar saldo" : "Vincular View",canOpenBalance ? "opening" : "link"];
    }
    if (workflow.pending_orders) return ["Orden · pendiente","Resuelve las órdenes o reservas pendientes de la cuenta.","Revisar órdenes","orders"];
    if (workflow.withdrawal_pending) return ["Retirada · pendiente","Resuelve la retirada antes de volver a operar.","Revisar retirada","withdrawal"];
    if (workflow.stopped && !rearmTarget) return ["STOP activo","Reabre la operativa o reanuda un bot existente.","Abrir Orden Spot","spot"];
    if (workflow.bot_active && (!rearmTarget || !bots.some(function (item) {
      return item.id === rearmTarget.id && item.stored_status === "ACTIVE";
    }))) return ["Cuenta · bot activo","Detén el bot activo antes de autorizar otro."];
    if (!workflow.trade_verified) return ["Trade · verificar","Verifica Trade para la conexión View de este libro.","Configurar Trade","trade"];
    return null;
  }
  function preparationState() {
    var target = root.querySelector("[data-bots-form-state]"), form = root.querySelector("[data-bots-form]");
    var source = candidates.find(function (item) { return item.session_id === form.elements.paper.value; });
    var title, detail = "Sólo para activar o reanudar. El estado de los bots existentes aparece en sus tarjetas.";
    var proof = root.querySelector("[data-bots-handoff]"), carried = handoffSource();
    proof.hidden = !handoff;
    if (handoff) {
      var label = carried ? carried.name + " · v" + carried.version + " · " + carried.symbol + " · " + carried.timeframe :
        (handoff.kind === "DCA" ? "DCA PAPER" : "Spot PAPER") + " · " + handoff.id.slice(0, 8);
      proof.querySelector("strong").textContent = label;
      proof.querySelector("[data-bots-handoff-account]").textContent = api.accountLabel();
    }
    var limits = root.querySelector("[data-bots-limits]");
    var issue = accountIssue(), action = root.querySelector("[data-bots-account-action]");
    limits.hidden = !source || !source.paper_ready || !workflow || workflow.view !== "LINKED" || !!preparationIssue || controlsLoading;
    action.hidden = true;
    action.dataset.botsAccountAction = "";
    if (controlsLoading) {
      title = "Comprobando…"; detail += " Espera a que termine la lectura de pruebas, límites y Trade.";
    } else if (preparationIssue) {
      title = preparationIssue.scope + " · no disponible"; detail += " " + preparationIssue.detail + " Pulsa Actualizar para volver a comprobar.";
    } else if (!statusAvailable) {
      title = "Bots · sin verificar"; detail += " Actualiza el estado antes de solicitar otra autorización.";
    } else if (handoff && !carried) {
      title = "Sesión PAPER · no disponible";
      detail += " Comprueba que la sesión exacta siga activa y su archivo PAPER esté desbloqueado.";
    } else if (handoff && !carried.paper_ready) {
      title = "Observación PAPER · pendiente";
      detail += " " + (carried.reason || "Comprueba el origen de la sesión y su última observación.");
    } else if (issue) {
      title = issue[0]; detail += " " + issue[1];
      if (issue[3]) {
        action.textContent = issue[2];
        action.dataset.botsAccountAction = issue[3];
        action.hidden = false;
      }
    } else if (!source) {
      title = candidates.some(function (item) { return item.ready && compatibleSource(item); }) ?
        "Prueba PAPER · seleccionar" : "Prueba PAPER · pendiente";
      detail += " Selecciona una prueba compatible y verificable de la misma versión y temporalidad.";
    } else if (!form.elements.research.value) {
      title = "Reserva · seleccionar"; detail += " Guarda y elige una validación fuera de muestra compatible. Se repetirá al autorizar.";
    } else if (!form.elements.draft.value) {
      title = "Límites · seleccionar"; detail += " Selecciona límites revisados para esta estrategia y mercado.";
    } else {
      title = rearmTarget ? "Preparado para revisar" : "Preparado para activar";
      detail += " Se comprobarán de nuevo prueba, cuenta y límites al confirmar; esto no concede permiso de órdenes.";
    }
    if (target.textContent !== title) target.textContent = title;
    currentHelp(target,detail);
    target.className = preparationIssue ? "amber" : "muted";
  }
  function localDateTime(value) {
    var date = new Date(value);
    return new Date(date.getTime()-date.getTimezoneOffset()*60000).toISOString().slice(0,16);
  }
  function continuityDate() {
    if (!root) return;
    var form = root.querySelector("[data-bots-form]"), enabled = form.elements.permission.value === "CONTINUOUS";
    root.querySelector("[data-bots-until]").hidden = !enabled;
    var field = form.elements.continuous_until;
    field.required = enabled;
    if (!enabled) { field.setCustomValidity(""); return; }
    var draft = drafts.find(function (item) { return item.id === form.elements.draft.value; });
    if (!draft) { field.setCustomValidity("Selecciona límites vigentes."); return; }
    var maximum = Math.min(new Date(draft.expires_at).getTime(),Date.now()+30*86400000);
    field.min = localDateTime(Date.now()+60000); field.max = localDateTime(maximum);
    field.setCustomValidity(maximum <= Date.now()+60000 ? "Los límites han caducado; guarda otros." : "");
    if (!field.value) field.value = localDateTime(Math.min(maximum,Date.now()+7*86400000));
  }
  function selectedDrafts() {
    if (!root) return;
    var source = candidates.find(function (item) { return item.session_id === root.querySelector("[name=paper]").value; });
    var dca = source && source.source_kind === "DCA", form = root.querySelector("[data-bots-form]");
    var research = form.elements.research, previousResearch = research.value;
    research.replaceChildren(new Option("Selecciona reserva", ""));
    (source && source.research_options || []).forEach(function (item) {
      research.add(new Option(item.created_at.slice(0,10) + " · " + item.test_bars + " velas · " + item.id.slice(0,8),item.id));
    });
    var preferred = previousResearch || (rearmTarget && rearmTarget.research_binding && rearmTarget.research_binding.research.artifact_id);
    if ([].some.call(research.options,function (option) { return option.value === preferred; })) research.value = preferred;
    else if (research.options.length === 2) research.selectedIndex = 1;
    root.querySelector("[data-bots-research-link]").href = dca ? "#/dca" : "#/laboratorio";
    root.querySelector("[data-bots-dca-limits]").hidden = !dca;
    ["dca_total","dca_monthly"].forEach(function (name) {
      form.elements[name].required = !!dca; form.elements[name].readOnly = !!rearmTarget;
    });
    var select = root.querySelector("[name=draft]"), previous = select.value;
    select.replaceChildren(new Option("Selecciona límites", ""));
    drafts.filter(function (item) { return source && item.strategy_version_id === source.version_id &&
      item.symbol === source.symbol.replace("/", ""); }).forEach(function (item) {
      select.add(new Option(item.symbol + " · máx. " + api.format(item.max_order_quote) + " " + item.quote_asset, item.id));
    });
    if (previous && [].some.call(select.options, function (item) { return item.value === previous; })) select.value = previous;
    else if (select.options.length === 2) select.selectedIndex = 1;
    var scope = source && research.value && researchScope(source, research.value, rearmTarget);
    if (reviewedResearch && reviewedResearch.scope !== scope) clearResearch();
    root.querySelector("[data-bots-research-review]").disabled = busy || !!researchPending || !!form.dataset.payload || !scope;
    root.querySelector("[data-bots-start]").disabled = busy || !!researchPending || controlsLoading || !!preparationIssue || !statusAvailable || !!accountIssue() || !source || !source.ready || source.paper_ready === false || !research.value || !select.value ||
      (handoff && source !== handoffSource()) ||
      (rearmTarget && !compatibleSource(source));
    continuityDate();
    preparationState();
  }
  function compatibleSource(item) {
    return !rearmTarget || item.version_id === rearmTarget.strategy_version_id && item.symbol === rearmTarget.symbol &&
      item.timeframe === rearmTarget.timeframe;
  }
  function sources() {
    var select = root.querySelector("[name=paper]"), previous = select.value;
    select.replaceChildren(new Option("Selecciona prueba PAPER", ""));
    candidates.filter(compatibleSource).forEach(function (item) {
      var option = new Option((item.source_kind === "DCA" ? "DCA · " : "") + item.name + " · v" + item.version + " · " + item.symbol + " · " + item.timeframe +
        (item.ready ? "" : " · revisar origen"), item.session_id);
      option.disabled = item.paper_ready === false || (item.paper_ready === undefined && !item.ready);
      if (item.reason) option.title = item.reason;
      select.add(option);
    });
    var carried = handoffSource();
    if (handoff) select.value = carried && carried.paper_ready ? carried.session_id : "";
    else if ([].some.call(select.options, function (option) { return option.value === previous; })) select.value = previous;
    else if (rearmTarget && [].some.call(select.options, function (option) { return option.value === rearmTarget.paper_session_id; }))
      select.value = rearmTarget.paper_session_id;
    else if (rearmTarget && select.options.length === 2) select.selectedIndex = 1;
    selectedDrafts();
  }
  function mode(item) {
    handoff = null;
    rearmTarget = item;
    var form = root.querySelector("[data-bots-form]");
    delete form.dataset.payload; delete form.dataset.fingerprint;
    root.querySelector("[data-bots-mode]").textContent = item ? "Reanudar · " + item.symbol : "Nuevo bot";
    root.querySelector("[data-bots-new]").hidden = !item;
    root.querySelector("[data-bots-start]").textContent = item ? "Revisar y reanudar" : "Activar bot";
    if (item) form.elements.budget.value = item.entry_budget_quote;
    form.elements.permission.value = item ? item.execution_mode : "SESSION";
    form.elements.continuous_until.value = item && item.execution_mode === "CONTINUOUS" &&
      new Date(item.execution_permission.expires_at).getTime() > Date.now() ? localDateTime(item.execution_permission.expires_at) : "";
    form.elements.dca_total.value = item && item.dca_limits ? item.dca_limits.budget_total_quote : "";
    form.elements.dca_monthly.value = item && item.dca_limits ? item.dca_limits.monthly_limit_quote : "";
    sources();
  }
  function node(tag, text, className) {
    var element = document.createElement(tag);
    element.textContent = text;
    if (className) element.className = className;
    return element;
  }
  function researchScope(source, researchId, target) {
    return [api.getAccountId(), source.source_kind, source.session_id, researchId, target && target.id].join("/");
  }
  function clearResearch() {
    reviewedResearch = null;
    var panel = root.querySelector("[data-bots-origin]");
    panel.hidden = true;
    panel.replaceChildren();
  }
  function researchDay(stamp, time) {
    return new Date(stamp * 1000).toISOString().slice(0, time ? 16 : 10).replace("T", " ");
  }
  async function reviewResearch(source, researchId, target) {
    var host = root, account = api.getAccountId(), issued = epoch;
    var scope = researchScope(source, researchId, target);
    var params = new URLSearchParams({paper_session_id:source.session_id, research_validation_id:researchId,
      source_kind:source.source_kind || "STRATEGY"});
    if (target) params.set("bot_id", target.id);
    var proof = await api.request("/accounts/" + encodeURIComponent(account) + "/mexc-bots/research-review?" + params, null, true);
    if (!current(host, account, issued) || host.querySelector("[name=paper]").value !== source.session_id ||
        host.querySelector("[name=research]").value !== researchId || target !== rearmTarget) return null;
    if (!proof || !/^[0-9a-f]{64}$/.test(proof.sha256) || proof.paper?.session_id !== source.session_id ||
        !/^[0-9a-f]{64}$/.test(proof.paper.observation_digest) ||
        proof.paper.version_id !== source.version_id || proof.research?.artifact_id !== researchId ||
        proof.research.execution_authority !== "NONE" || !proof.research.windows?.length) {
      throw new Error("Origen LAB y PAPER no verificable.");
    }
    var panel = host.querySelector("[data-bots-origin]"), research = proof.research, paper = proof.paper;
    var summary = node("strong", "Origen LAB y PAPER · v" + source.version);
    summary.title = proof.sha256;
    var facts = node("ul", "", "muted");
    var period = researchDay(research.windows[0].from_t) + " → " + researchDay(research.windows.at(-1).to_t);
    var reserve = period + " · " + research.sample_bars + " velas";
    var last = researchDay(paper.observation_t, true) + " UTC";
    var reserveItem = node("li", "Reserva · " + reserve + (research.windows.length > 1 ? " · " + research.windows.length + " tramos" : ""));
    reserveItem.title = research.artifact_id;
    var paperItem = node("li", "Última vela PAPER · " + last);
    paperItem.title = paper.session_id;
    facts.append(reserveItem, paperItem);
    panel.replaceChildren(summary, facts);
    panel.hidden = false;
    reviewedResearch = {scope:scope, digest:proof.sha256, reserve:reserve, last:last};
    return reviewedResearch;
  }
  async function showResearch() {
    var host = root, account = api.getAccountId(), issued = epoch, form = host.querySelector("[data-bots-form]");
    var source = candidates.find(function (item) { return item.session_id === form.elements.paper.value; });
    if (!source || !form.elements.research.value || busy || researchPending) return;
    var ticket = researchPending = {};
    clearResearch();
    selectedDrafts();
    notice("Verificando origen LAB y PAPER…", false);
    try {
      await reviewResearch(source, form.elements.research.value, rearmTarget);
      if (current(host, account, issued)) notice("", false);
    } catch (error) {
      if (current(host, account, issued)) notice(error.message, true);
    } finally {
      if (researchPending === ticket) researchPending = null;
      if (current(host, account, issued)) selectedDrafts();
    }
  }
  function pausedReason(item) {
    if (item.status === "REARM_REQUIRED") return "Autorización pendiente";
    if (["WORKER_STOPPED","CYCLE_REVIEW_REQUIRED"].includes(item.reason) && item.worker.status === "PERMISSION_REVOKED_OR_EXPIRED")
      return "Permiso no vigente";
    return {CYCLE_REVIEW_REQUIRED:"Revisar ciclo",ORDER_REVIEW_REQUIRED:"Revisar orden",
      ORDER_PREPARATION_REJECTED:"Preparación rechazada",USER_STOP:"Parada manual",
      WORKER_STOPPED:"Seguimiento detenido",VAULT_LOCKED:"Bóveda bloqueada",VIEW_PROTECTED:"Vista protegida",
      WORKER_START_FAILED:"Seguimiento no iniciado",REARM_REVIEW_FAILED:"Revisar reanudación",
      EXECUTED_FEE_EXCEEDED:"Comisión excedida",EXECUTED_BUDGET_EXCEEDED:"Presupuesto excedido",
      CONNECTION_OR_MODE_CHANGED:"Conexión modificada"}[item.reason] || "Revisar operativa";
  }
  function render(items) {
    bots = items;
    var list = root.querySelector("[data-bots-list]");
    list.replaceChildren();
    root.querySelector("[data-bots-badge]").textContent = items.some(function (item) { return item.execution_enabled; }) ? "· activo" : "";
    items.forEach(function (item) {
      var card = document.createElement("section"); card.className = "state";
      var row = document.createElement("div"); row.className = "row";
      row.append(node("strong", (item.source_kind === "DCA" ? "DCA · " : "") + item.symbol + " · " + item.timeframe));
      var status = node("span", item.execution_enabled ? "Activo" : item.status === "REARM_REQUIRED" ? "Revisión requerida" : "Pausado", "statusbadge");
      status.dataset.help = item.reason === "EXECUTED_FEE_EXCEEDED" ? "La comisión observada superó el límite. Se conserva el fill real y se bloquean nuevos envíos." :
        item.reason === "EXECUTED_BUDGET_EXCEEDED" ? "El gasto observado superó el presupuesto. Revisa la ejecución; no se revierte un fill real." :
        "Estado del bot y su permiso temporal. Detener o pausar no liquida posiciones.";
      row.append(status);
      var signalWait = {DCA_MA_ZERO_AMOUNT:"Importe ×0", DCA_SMA_CONDITION_NOT_MET:"Filtro SMA sin señal",
        DCA_MA_WARMUP:"Media sin historial", DCA_SMA_WARMUP:"SMA sin historial",
        DCA_VOLUME_BELOW_MINIMUM:"Volumen por debajo del mínimo", DCA_VOLUME_WARMUP:"Volumen sin historial",
        DCA_VOLUME_BASELINE_ZERO:"Volumen medio cero"}[item.reason];
      if (item.execution_enabled && signalWait) row.append(node("small", signalWait, "muted"));
      var research = item.research_binding && item.research_binding.research;
      var proof = node("small",research ? "Reserva · " + research.artifact_id.slice(0,8) : "Legado · sin reserva","muted");
      proof.dataset.help = research ? "Versión, histórico y reserva comprobados al autorizar. Método " + research.method +
        (research.family === "DCA" ? ". La referencia DCA REAL empieza en la activación y se conserva al reanudar." : ".") +
        " No importa capital PAPER ni garantiza resultados." :
        "Autorización anterior al vínculo de investigación. Reanudar exige seleccionar una reserva compatible.";
      row.append(proof);
      if (item.execution_enabled) {
        var minutes = Math.ceil(item.worker.permission_remaining_seconds / 60);
        var permission = node("small", "Permiso · " + (minutes >= 1440 ? Math.ceil(minutes/1440)+" d" : minutes >= 60 ? Math.ceil(minutes/60)+" h" : minutes+" min"), "muted");
        permission.dataset.help = item.execution_mode === "CONTINUOUS" ?
          "Concesión con duración y límites fijos. Renueva la evidencia diaria, no la fecha ni el capital autorizado. Claves sólo en memoria; Bloquear o cerrar revoca el permiso." : item.execution_mode === "OPERATIONAL" ?
          "Concesión operativa independiente de la pantalla. Mantiene clave y credenciales en memoria hasta caducar la política (máximo 24 h). Bloquear y detener o cerrar el proceso la revoca; no liquida posiciones." :
          "Permiso temporal. El sondeo no lo renueva. Caducar o bloquear REAL/PAPER detiene nuevos envíos, sin vender la posición.";
        row.append(permission);
      }
      if (item.execution_permission) row.append(node("small",(item.execution_mode === "CONTINUOUS" ? "Continuo" : "Operativo") + " · hasta " + new Date(item.execution_permission.expires_at).toLocaleString(),"muted"));
      if (item.renewals) {
        var renewals = node("small","Renovaciones · " + item.renewals,"muted");
        renewals.dataset.help = "Aperturas causales dentro del mismo permiso. Evidencia vigente hasta " + new Date(item.policy_expires_at).toLocaleString() + ". No reinician presupuesto ni posiciones.";
        row.append(renewals);
      }
      var stop = node("button", "Detener bot", "ghost"); stop.type = "button"; stop.dataset.botStop = item.id;
      stop.disabled = !busy && item.status === "PAUSED" && !item.worker.running;
      stop.dataset.help = "Detiene nuevos envíos y cancela una revisión en curso. No cancela órdenes ni vende posiciones.";
      row.append(stop);
      if (!item.execution_enabled) {
        var resume = node("button", "Reanudar", "ghost"); resume.type = "button"; resume.dataset.botRearm = item.id;
        resume.disabled = busy || !!item.worker.running;
        resume.dataset.help = "Revisa saldo, órdenes, estrategia y límites. Conserva los fills propios. Los cambios de efectivo requieren confirmación y los de activos requieren revisar la asignación.";
        row.append(resume);
      }
      var diary = node("button", "Diario", "ghost"); diary.type = "button"; diary.dataset.botDiary = item.id; row.append(diary);
      var graph = node("button", "Gráfico", "ghost"); graph.type = "button"; graph.dataset.botChart = item.id;
      graph.dataset.help = "Precios y tiempos de fills REAL propios, finales y asentados. Una señal o una orden sin ejecutar no se dibuja como compra.";
      row.append(graph);
      card.append(row);
      if (item.dca_limits) card.append(node("p","Asignación ≤ " + api.format(item.dca_limits.budget_total_quote) +
        " · Mes ≤ " + api.format(item.dca_limits.monthly_limit_quote) + " USDT · sólo compras", "muted"));
      if (item.reauthorizations) {
        var review = node("small", "Revisiones: " + item.reauthorizations, "muted");
        review.dataset.help = "Cada reanudación conserva la política anterior, registra el hueco y guarda la posición revisada.";
        card.append(review);
      }
      if (item.position.verified) {
        var figures = node("p", (item.execution_enabled ? "Propio: " : "Registro del bot: ") + api.format(item.position.units) + " · Coste ≈ " + api.format(item.position.cost_quote) +
          " USDT · Realizado ≈ " + api.format(item.position.realized_pnl_quote) + " USDT · Fills: " + item.position.fill_count, "muted");
        figures.dataset.help = "Posición reconstruida con fills propios y ventas manuales atribuidas. El saldo libre de MEXC puede ser distinto.";
        card.append(figures);
        if (item.position.manual_sale_count) {
          card.append(node("small", "Fills de venta manual atribuidos: " + item.position.manual_sale_count, "muted"));
        }
      } else card.append(node("p", "Posición pendiente de verificación · revisa Órdenes MEXC.", "muted"));
      var result = item.worker.last_result;
      var actions = {WAIT:"En espera",BUY:"Compra preparada",SELL:"Venta preparada",FOLLOW_UP:"Seguimiento",PAUSE:"Revisar"};
      if (!item.execution_enabled) card.append(node("p", "Motivo · " + pausedReason(item), "amber"));
      else card.append(node("p", result ? "Último paso: " + (actions[result.action] || result.action) + (result.journal_status ? " · " + result.journal_status : "") :
        "Sin ciclo observado en este proceso.", "muted"));
      if (item.market_tracking === "OWN_REAL") {
        var market = item.market_observation;
        var tracking = node("small",market && market.verified ? "Mercado REAL · " +
          new Date(market.candle_t * 1000).toLocaleString() : "Mercado REAL · revisar",market && market.verified ? "muted" : "amber");
        tracking.dataset.help = "Velas cerradas observadas por este bot, archivadas en REAL y enlazadas a sus decisiones. No son compras PAPER ni amplían el permiso temporal. Bloquear sigue deteniendo el bot.";
        card.append(tracking);
      }
      var journal = node("div", "", "tablewrap"); journal.dataset.botJournal = item.id; journal.hidden = true; card.append(journal);
      if (Object.prototype.hasOwnProperty.call(journals, item.id)) renderJournal(journal, journals[item.id]);
      list.append(card);
    });
    if (!items.length) list.append(node("p", "Sin bots reales.", "muted"));
  }
  function renderJournal(target, items) {
    target.hidden = false; target.replaceChildren();
    if (!Array.isArray(items)) { target.append(node("p","Diario no verificable · vuelve a consultar.","amber")); return; }
    if (!items.length) target.append(node("p", "Sin decisiones de orden.", "muted"));
    items.forEach(function (item) {
      target.append(node("p", "Decisión: " + item.side + " · " + item.reason + " · " +
      api.format(item.quantity_base) + " · límite " + api.format(item.limit_price_quote) + " USDT · " +
      (item.legacy_fills_recovered ? "Orden conciliada" : item.journal_status === "NOT_RESERVED" ? "Sin reserva local" :
        item.journal_status === "LEGACY_CANCELED_WITHOUT_FILLS" ? "Cancelación archivada" : item.journal_status) +
      (item.observed_fills === null ? " · sin ejecución confirmada" : " · Fills observados: " + item.observed_fills), "muted"));
      var dca = item.dca;
      if (dca && (dca.ma_value != null || dca.sma_value != null)) {
        var average = dca.ma_value != null ? dca.ma_kind.toUpperCase() + " " + dca.ma_period : "SMA " + dca.sma_period;
        target.append(node("small", average + " · " + api.format(dca.ma_value || dca.sma_value) +
          (dca.amount_factor != null ? " · ×" + dca.amount_factor : "") +
          " · Solicitado " + api.format(dca.requested) + " USDT", "muted"));
      }
      if (dca && dca.volume_ratio != null) {
        target.append(node("small", "Volumen relativo · " + api.format(dca.volume_ratio) + "× · mínimo " +
          api.format(dca.volume_min_ratio) + "× · " + dca.volume_period + " velas", "muted"));
      }
      if (item.journal_status === "RESERVED" || item.journal_status === "SUBMISSION_UNKNOWN") {
        var discard = item.journal_status === "RESERVED";
        var button = node("button",discard ? "Parar y descartar" : "Seguir orden","ghost"); button.type = "button";
        button.dataset.botRecover = item.bot_id; button.dataset.intent = item.intent_id; button.dataset.mode = discard ? "discard" : "follow";
        button.disabled = busy;
        button.dataset.help = discard ? "Detiene el bot y libera sólo una reserva que nunca inició envío. No cancela pedidos externos ni vende posiciones." :
          "Detiene el bot y consulta orden/fills con View. No reenvía ni rearma. Si existe ejecución confirmada, se registra una sola vez.";
        target.append(button);
      } else if (item.journal_status === "NOT_RESERVED") {
        var lookup = node("button","Consultar en MEXC","ghost"); lookup.type = "button";
        lookup.dataset.botRecover = item.bot_id; lookup.dataset.intent = item.intent_id; lookup.dataset.mode = "lookup";
        lookup.disabled = busy;
        lookup.dataset.help = "Decisión antigua sin reserva local. Consulta la orden y sus fills con View. MEXC sólo ofrece hasta siete días por API.";
        target.append(lookup);
      }
    });
  }
  async function recover(event) {
    var button = event.target.closest("[data-bot-recover]"); if (!button || busy) return;
    var discard = button.dataset.mode === "discard";
    var lookup = button.dataset.mode === "lookup";
    if (!lookup && !confirm(discard ? "¿Parar este bot y descartar su preparación? Sólo se libera si nunca inició el envío. No cancela órdenes externas ni vende activos. Reanudar requiere una nueva revisión." :
        "¿Parar el bot y seguir esta orden con MEXC View? Se comprobarán orden y fills, sin reenviar. El bot no se reanudará solo.")) return;
    var host = root,account = api.getAccountId(),issued = epoch,localApi = api,identifier = button.dataset.botRecover,intent = button.dataset.intent;
    var base = "/accounts/"+encodeURIComponent(account)+"/mexc-bots/"+encodeURIComponent(identifier);
    busy = true; sequence += 1; button.disabled = true; selectedDrafts();
    try {
      if (lookup) {
        notice("Consultando decisión…",false);
        var reviewPath = base+"/decisions/"+encodeURIComponent(intent)+"/mexc-review";
        var review = await localApi.request(reviewPath,null,true);
        if (!current(host,account,issued)) return;
        var statuses = {NEW:"Abierta", FILLED:"Ejecutada", PARTIALLY_FILLED:"Ejecución parcial",
          CANCELED:"Cancelada", PARTIALLY_CANCELED:"Cancelada tras ejecución parcial"};
        var fillReview = /^[0-9a-f]{64}$/.test(review.fill_recovery_digest || "");
        if (fillReview || /^[0-9a-f]{64}$/.test(review.resolution_digest || "")) {
          var fees = fillReview ? review.fees_by_asset.map(function (fee) { return api.exact(fee[1])+" "+fee[0]; }).join(" · ") : "";
          var message = fillReview ? "Ejecuciones MEXC: "+review.fill_count+" · "+api.exact(review.executed_base)+
            " "+review.base_asset+" · "+api.exact(review.cumulative_quote)+" "+review.quote_asset+"\nComisiones: "+fees+"\n¿Conciliar la orden? El bot seguirá pausado." :
            "MEXC confirma la cancelación sin ejecuciones. ¿Guardar el cierre en el diario? El bot seguirá pausado.";
          if (!confirm(message)) {
            notice(fillReview ? "Ejecuciones verificadas · conciliación pendiente" : "Cancelada sin ejecuciones · pendiente de archivar",false);
            return;
          }
          review = await localApi.request(reviewPath+"/confirm", {method:"POST",body:JSON.stringify({
            confirmation:fillReview ? "CONCILIAR_EJECUCIONES_ANTIGUAS" : "ARCHIVAR_CANCELACION_SIN_FILLS",
            resolution_digest:fillReview ? review.fill_recovery_digest : review.resolution_digest})});
          if (!current(host,account,issued)) return;
        }
        if (review.state === "LEGACY_CANCELED_WITHOUT_FILLS") notice("Cancelación archivada · bot pausado",false);
        else if (review.state === "LEGACY_FILLS_RECONCILED") {
          notice("Orden conciliada · bot pausado",false);
          localApi.onControlChange();
        }
        else if (review.state === "MATCHED") notice(statuses[review.external_status]+" · Fills en MEXC: "+review.fill_count+" · Conciliación pendiente",false);
        else notice((review.state === "OUTSIDE_LOOKUP_WINDOW" ? "Fuera del historial de la API · consulta MEXC" :
          "Orden sin verificar · revisa el historial MEXC")+" · ID cliente "+review.client_order_id,true);
        return;
      }
      await localApi.request(base+"/stop",{method:"POST",body:JSON.stringify({confirmation:"DETENER_BOT"})});
      if (!current(host,account,issued)) return;
      var body = {account_id:account};
      if (discard) body.confirmation = "LIBERAR_RESERVA_NO_ENVIADA";
      var result = await localApi.request("/execution/mexc/intents/"+encodeURIComponent(intent)+(discard ? "/release-unsent" : "/synchronize"),
        {method:"POST",body:JSON.stringify(body)});
      if (!current(host,account,issued)) return;
      notice(discard ? "Preparación descartada · sin envío. Revisa antes de reanudar." :
        result.reservation_closed ? "Orden verificada y registrada · bot pausado. Revisa antes de reanudar." : "Orden todavía pendiente · bot pausado, no se reenviará.",false);
      localApi.onControlChange();
    } catch (error) { if (current(host,account,issued)) notice(lookup ? error.message :
      (error.message || "Revisa el diario antes de actuar")+" · no se reenvía ni reanuda automáticamente.",true); }
    finally {
      if (current(host,account,issued)) {
        try { var latest = await localApi.request(base+"/decisions",null,true); if (current(host,account,issued)) journals[identifier] = latest; }
        catch (_) { if (current(host,account,issued)) journals[identifier] = null; }
        if (current(host,account,issued)) {
          busy = false;
          var journal = host.querySelector('[data-bot-journal="'+CSS.escape(identifier)+'"]');
          if (journal && Object.prototype.hasOwnProperty.call(journals,identifier)) renderJournal(journal,journals[identifier]);
          selectedDrafts(); load(false);
        }
      }
    }
  }
  function schedule() {
    if (timer !== null) clearTimeout(timer);
    timer = setTimeout(function () { timer = null; load(false); }, 5000);
  }
  async function loadControls(host,account,issued) {
    var requestNumber = ++controlsSequence, base = "/accounts/"+encodeURIComponent(account), localApi = api;
    controlsLoading = true; selectedDrafts();
    try {
      var result = await Promise.all([
        localApi.request(base+"/mexc-bots/candidates",null,true).catch(function (error) { return {error:error.message}; }),
        localApi.request(base+"/mexc-policy-drafts",null,true)
      ]);
      if (!current(host,account,issued) || requestNumber !== controlsSequence) return;
      if ((!Array.isArray(result[0]) && (!result[0] || typeof result[0].error !== "string")) ||
          !Array.isArray(result[1])) {
        throw new Error("Respuesta de preparación no verificable.");
      }
      var receivedSources = Array.isArray(result[0]) ? result[0] : [];
      if (!receivedSources.every(function (item) {
        return item && ["session_id","version_id","symbol","timeframe","name"].every(function (key) {
          return typeof item[key] === "string" && item[key].length > 0;
        }) && typeof item.ready === "boolean" && Array.isArray(item.research_options) && item.research_options.every(function (option) {
          return option && typeof option.id === "string" && typeof option.created_at === "string" && Number.isInteger(option.test_bars) &&
            option.verification === "ON_ACTIVATION";
        }) && (!item.source_kind || ["STRATEGY","DCA"].includes(item.source_kind)) &&
          (item.paper_ready === undefined || typeof item.paper_ready === "boolean");
      }) || !result[1].every(function (item) {
        return item && ["id","strategy_version_id","symbol","quote_asset","max_order_quote","expires_at"].every(function (key) {
          return typeof item[key] === "string" && item[key].length > 0;
        }) && Number.isFinite(Date.parse(item.expires_at));
      })) throw new Error("Datos de preparación no verificables.");
      candidates = receivedSources;
      drafts = result[1];
      preparationIssue = result[0].error ? {scope:"PAPER",detail:result[0].error} : null;
      controlsLoading = false; sources();
    } catch (error) {
      if (current(host,account,issued) && requestNumber === controlsSequence) {
        controlsLoading = false;
        preparationIssue = {scope:"Preparación",detail:error.message}; selectedDrafts();
      }
    }
  }
  async function loadAccountState(host,account,issued,requestNumber) {
    var state = null;
    try {
      var result = await api.request("/accounts/"+encodeURIComponent(account)+"/mexc-workflow",null,true);
      if (result && ["DISCONNECTED","OTHER_ACCOUNT","DIFFERENT_CONNECTION","UNLINKED","LINKED"].includes(result.view) &&
          (result.view !== "UNLINKED" || typeof result.book_empty === "boolean") &&
          (result.view !== "LINKED" || ["trade_verified","pending_orders","withdrawal_pending","stopped","bot_active"].every(function (key) {
            return typeof result[key] === "boolean";
          }))) state = result;
    } catch (_) {}
    if (!current(host,account,issued) || requestNumber !== sequence) return;
    if (workflowError === !state && JSON.stringify(workflow) === JSON.stringify(state)) return;
    workflow = state;
    workflowError = !state;
    selectedDrafts();
  }
  async function load(controls) {
    if (!root || !root.isConnected || busy || !root.querySelector("details").open || !api.getAccountId()) return;
    var host = root, account = api.getAccountId(), issued = epoch, requestNumber = ++sequence;
    var base = "/accounts/" + encodeURIComponent(account);
    // La preparación tiene su propia secuencia: una lectura lenta de límites
    // no detiene el sondeo del bot ni permite que una respuesta vieja lo rearme.
    if (controls) loadControls(host,account,issued);
    loadAccountState(host,account,issued,requestNumber);
    try {
      var items = await api.request(base + "/mexc-bots", null, true);
      if (!current(host, account, issued) || requestNumber !== sequence) return;
      var localApi = api;
      var updates = await Promise.all(Object.keys(journals).filter(function (id) { return items.some(function (item) { return item.id === id; }); })
        .map(async function (id) {
          try { return [id,await localApi.request(base+"/mexc-bots/"+encodeURIComponent(id)+"/decisions",null,true)]; }
          catch (_) { return [id,null]; }
        }));
      if (!current(host,account,issued) || requestNumber !== sequence) return;
      updates.forEach(function (entry) { journals[entry[0]] = entry[1]; });
      var beforeIssue = accountIssue();
      render(items);
      var afterIssue = accountIssue();
      if (!statusAvailable || (beforeIssue && beforeIssue[0]) !== (afterIssue && afterIssue[0])) {
        statusAvailable = true; selectedDrafts();
      }
      if (controls || listError) notice("",false);
      if (pendingActivation) {
        var recovered = items.find(function (item) { return item.id === pendingActivation && item.account_id === account; });
        if (recovered) {
          pendingActivation = null;
          notice(recovered.execution_enabled ? "Autorización registrada · respuesta recuperada." :
            "Autorización pausada · revisa el bot antes de reanudar.",false);
          api.onControlChange();
        } else notice("Respuesta sin confirmar · revisa la autorización antes de continuar.",false);
      }
      listError = false;
      var snapshot = JSON.stringify(items.map(function (item) { return [item.id, item.last_decision_id, item.position.fill_count, item.status, item.reason]; }));
      if (lastSnapshot && snapshot !== lastSnapshot) api.onChange();
      if (lastSnapshot && snapshot !== lastSnapshot && window.OrbisMexcBotChart) window.OrbisMexcBotChart.refresh();
      lastSnapshot = snapshot;
    } catch (error) {
      if (current(host, account, issued) && requestNumber === sequence) {
        if (statusAvailable) { statusAvailable = false; selectedDrafts(); }
        listError = true;
        root.querySelector("[data-bots-badge]").textContent = "· sin verificar";
        root.querySelectorAll("[data-bots-list] .statusbadge").forEach(function (badge) {
          badge.textContent = "Sin verificar";
          currentHelp(badge,"No se ha podido actualizar el estado. No se deduce que el bot esté activo ni detenido. Puedes solicitar STOP.");
        });
        root.querySelectorAll("[data-bot-stop]").forEach(function (button) { button.disabled = false; });
        notice(error.message === "Not Found" ? "Reinicia ORBIS para cargar los bots." : error.message,true);
      }
    } finally {
      if (current(host, account, issued) && requestNumber === sequence && !busy) schedule();
    }
  }
  async function activate(event) {
    event.preventDefault();
    var form = event.currentTarget, host = root, account = api.getAccountId(), issued = epoch;
    var source = candidates.find(function (item) { return item.session_id === form.elements.paper.value; }), target = rearmTarget;
    var researchId = form.elements.research.value;
    if (busy || researchPending || controlsLoading || preparationIssue || !statusAvailable || !!accountIssue() || !source || !source.ready || source.paper_ready === false || !compatibleSource(source) || !form.elements.draft.value ||
        (handoff && source !== handoffSource()) ||
        !source.research_options.some(function (item) { return item.id === researchId; })) return;
    if (!form.reportValidity()) return;
    var budget = form.elements.budget.value.trim();
    var dca = source.source_kind === "DCA", total = form.elements.dca_total.value.trim(), monthly = form.elements.dca_monthly.value.trim();
    var permissionMode = form.elements.permission.value, operational = permissionMode === "OPERATIONAL", continuous = permissionMode === "CONTINUOUS";
    var until = continuous ? new Date(form.elements.continuous_until.value).toISOString() : null;
    var fingerprint = JSON.stringify([account, target && target.id, source.session_id, researchId, form.elements.draft.value, budget, dca, total, monthly,permissionMode,until]);
    if (form.dataset.fingerprint !== fingerprint) {
      form.dataset.fingerprint = fingerprint; delete form.dataset.payload;
    }
    busy = true; form.querySelector("button[type=submit]").disabled = true;
    root.querySelectorAll("[data-bot-stop]").forEach(function (button) { button.disabled = false; });
    form.querySelectorAll("select,input").forEach(function (input) { input.disabled = true; });
    root.querySelector("[data-bots-new]").disabled = true;
    notice("Verificando origen LAB y PAPER…", false);
    var activationId = null;
    try {
      var scope = researchScope(source, researchId, target);
      var proof = form.dataset.payload && reviewedResearch?.scope === scope ? reviewedResearch :
        await reviewResearch(source, researchId, target);
      if (!proof || !current(host, account, issued)) return;
      if (form.dataset.payload && JSON.parse(form.dataset.payload).research_review_digest !== proof.digest) {
        throw new Error("Consulta el estado de la autorización anterior antes de continuar.");
      }
      if (!confirm("¿" + (target ? "Reanudar" : "Activar") + " " + source.name + " en MEXC REAL con " + budget + " USDT por entrada?\n" +
        "Cuenta REAL: " + api.accountLabel() + "\n" +
        "Versión v" + source.version + " · reserva " + proof.reserve + "\nPAPER · " + proof.last + "\n\n" +
        (target ? "Se revisarán fills, saldo y órdenes. Aceptas el hueco sin vigilancia y los límites nuevos. Si había STOP, se reabrirá. No comprará señales pasadas.\n\n" : "") +
        (dca ? "DCA sólo compras. Asignación REAL " + total + " USDT · límite mensual " + monthly + " USDT. No vende ni coloca stops. " :
          "Ejecutará compras y ventas automáticamente dentro de los límites. ") +
        (continuous ? "PERMISO CONTINUO hasta " + new Date(until).toLocaleString() + ": autorizas renovar la evidencia diaria sin ampliar esta fecha, los límites ni el capital. La clave de la bóveda y credenciales siguen en memoria con la vista protegida y PAPER cerrado. Un error o hueco exige revisión. Detener y bloquear o cerrar ORBIS revoca el permiso, sin vender posiciones. " :
          operational ? "PERMISO OPERATIVO: autorizas trabajar con la vista protegida y PAPER cerrado. La clave de la bóveda y las credenciales de trading permanecen en memoria. Caduca con la política revisada (máximo 24 h), sin renovación automática. Detener y bloquear o cerrar ORBIS revoca el permiso, sin vender posiciones. " :
          "Bloquear o caducar REAL/PAPER detiene nuevos envíos, sin vender posiciones. ") +
        "Usa la cuenta sólo desde ORBIS. Una orden LIMIT no garantiza ejecución. No es un servicio permanente 24/7.")) {
        notice("", false);
        return;
      }
      notice("Verificando saldo y límites…", false);
      if (!form.dataset.payload) {
        if (target && target.stored_status === "ACTIVE") {
          await api.request("/accounts/" + encodeURIComponent(account) + "/mexc-bots/" + encodeURIComponent(target.id) + "/stop", {
            method:"POST", body:JSON.stringify({confirmation:"DETENER_BOT"})
          });
          if (!current(host, account, issued)) return;
        }
        var opening = await api.request("/accounts/" + encodeURIComponent(account) + "/mexc-spot-parity/observations", {
          method:"POST", body:JSON.stringify({request_id:api.uuid()})
        });
        if (!current(host, account, issued)) return;
        if (!opening.saved || opening.blockers.length) throw new Error("Revisa el inventario MEXC antes de activar.");
        var allocation = null;
        if (target) {
          allocation = await api.request("/accounts/" + encodeURIComponent(account) + "/mexc-bots/" +
            encodeURIComponent(target.id) + "/allocation-review", null, true);
          if (!current(host, account, issued)) return;
          if (allocation.state === "ASSET_REVIEW_REQUIRED")
            throw new Error("Asignación de activos pendiente. Revisa los movimientos de esta cuenta.");
          if (["CASH_ONLY", "MANUAL_PURCHASES", "MANUAL_SALES", "MANUAL_BOT_DEBIT"].includes(allocation.state)) {
            if (!/^[0-9a-f]{64}$/.test(allocation.review_digest)) throw new Error("Asignación no verificable.");
            var summary, assignsSale = allocation.state === "MANUAL_BOT_DEBIT";
            if (assignsSale) {
              summary = "Venta manual atribuible al bot · " + api.exact(allocation.assigned_units) + " " + allocation.base_asset + "\n" +
                "Posición " + api.exact(allocation.owned_units) + " → " + api.exact(allocation.owned_units_after) + " " + allocation.base_asset + "\n" +
                "Coste " + api.exact(allocation.owned_cost_quote) + " → " + api.exact(allocation.owned_cost_quote_after) + " " + allocation.quote_currency + "\n" +
                "Ingreso neto atribuido " + api.exact(allocation.assigned_net_quote) + " " + allocation.quote_currency;
            }
            else if (allocation.state === "MANUAL_SALES") {
              summary = "Venta manual conciliada · " + api.exact(allocation.manual_sale_units) + " " + allocation.base_asset + " fuera del bot\n" +
                "Inventario manual " + api.exact(allocation.outside_units_before) + " → " + api.exact(allocation.outside_units_after) + " " + allocation.base_asset;
            } else if (allocation.state === "MANUAL_PURCHASES") {
              summary = "Compra manual conciliada · " + api.exact(allocation.manual_purchase_units) + " " + allocation.base_asset + " fuera del bot";
            } else {
              summary = "Sólo cambió el efectivo · " + allocation.movement_count + " movimiento(s)";
            }
            var question = assignsSale ? "¿Atribuir esta venta ya conciliada y reanudar con la posición restante?" :
              "Variación " + api.exact(allocation.cash_delta_quote) + " " + allocation.quote_currency + "\n" +
              "Posición del bot " + api.exact(allocation.owned_units) + " " + target.symbol.split("/")[0] +
              " · coste " + api.exact(allocation.owned_cost_quote) + " " + allocation.quote_currency + "\n" +
              "¿Conservar esta posición y reanudar con los límites revisados?";
            if (!confirm(summary + "\n" + question)) {
              notice("Reanudación cancelada.",false);
              return;
            }
          } else if (allocation.state !== "UNCHANGED") throw new Error("Asignación no verificable.");
        }
        var payload = {request_id:api.uuid(), draft_id:form.elements.draft.value,
          anchor_id:opening.id, paper_session_id:source.session_id, research_validation_id:researchId, entry_budget_quote:budget,
          research_review_digest:proof.digest,
          confirmation:target ? (continuous ? "REANUDAR_BOT_CONTINUO" : operational ? "REANUDAR_BOT_OPERATIVO" : "REANUDAR_BOT_REAL") :
            (continuous ? "ACTIVAR_BOT_CONTINUO" : operational ? "ACTIVAR_BOT_OPERATIVO" : "ACTIVAR_BOT_REAL")};
        if (allocation && allocation.review_digest) payload.allocation_review_digest = allocation.review_digest;
        if (operational || continuous) payload.execution_mode = permissionMode;
        if (continuous) payload.continuous_until = until;
        if (dca && !target) { payload.source_kind = "DCA"; payload.dca_budget_total_quote = total; payload.dca_monthly_limit_quote = monthly; }
        form.dataset.payload = JSON.stringify(payload);
      }
      if (!target) activationId = JSON.parse(form.dataset.payload).request_id;
      var activated = await api.request("/accounts/" + encodeURIComponent(account) + "/mexc-bots" +
        (target ? "/" + encodeURIComponent(target.id) + "/rearm" : ""), {method:"POST",body:form.dataset.payload});
      if (!current(host, account, issued)) return;
      pendingActivation = null;
      notice(activated.execution_enabled ? "Autorización registrada · consulta el estado y diario." :
        "Autorización pausada · revisa el bot antes de reanudar.", false);
      if (target) mode(null);
      api.onControlChange();
    } catch (error) {
      if (current(host, account, issued)) {
        if (activationId && (!error.status || error.status >= 500)) {
          pendingActivation = activationId;
          notice("Respuesta sin confirmar · consultando la autorización.",false);
        } else {
          if (error.status === 409 || error.status === 422) {
            delete form.dataset.payload;
            clearResearch();
          }
          notice(error.message,true);
        }
      }
    } finally {
      if (current(host, account, issued)) {
        busy = false; form.querySelectorAll("select,input").forEach(function (input) { input.disabled = false; });
        root.querySelector("[data-bots-new]").disabled = false; selectedDrafts(); load(false);
      }
    }
  }
  async function action(event) {
    var button = event.target.closest("button[data-bot-stop],button[data-bot-diary],button[data-bot-rearm],button[data-bot-chart]");
    if (!button || !root.contains(button)) return;
    if (button.dataset.botChart) {
      var item = bots.find(function (bot) { return bot.id === button.dataset.botChart; });
      if (item && window.OrbisMexcBotChart) window.OrbisMexcBotChart.show(item);
      return;
    }
    if (button.dataset.botRearm) {
      if (busy) return;
      var selected = bots.find(function (item) { return item.id === button.dataset.botRearm; });
      if (selected) { mode(selected); root.querySelector("[data-bots-form]").scrollIntoView({block:"nearest"}); }
      return;
    }
    var host = root, account = api.getAccountId(), issued = epoch;
    var identifier = button.dataset.botStop || button.dataset.botDiary;
    var base = "/accounts/" + encodeURIComponent(account) + "/mexc-bots/" + encodeURIComponent(identifier);
    button.disabled = true;
    try {
      if (button.dataset.botStop) {
        if (!confirm("¿Detener este bot? No se cancelan órdenes ni se vende su posición.")) return;
        await api.request(base + "/stop", {method:"POST",body:JSON.stringify({confirmation:"DETENER_BOT"})});
        if (!current(host, account, issued)) return;
        api.onControlChange(); load(false);
      } else {
        var items = await api.request(base + "/decisions", null, true);
        if (!current(host, account, issued)) return;
        journals[identifier] = items;
        var journal = host.querySelector('[data-bot-journal="' + CSS.escape(identifier) + '"]');
        if (journal) renderJournal(journal, items);
      }
    } catch (error) {
      if (current(host,account,issued)) {
        notice(error.message,true);
        if (button.dataset.botDiary) {
          journals[identifier] = null;
          var unreadable = host.querySelector('[data-bot-journal="'+CSS.escape(identifier)+'"]');
          if (unreadable) renderJournal(unreadable,null);
        }
      }
    }
    finally { if (button.isConnected) button.disabled = false; }
  }
  function mount(element, client, prepared, keepOpen) {
    clear(); root = element; api = client; handoff = prepared || null;
    root.innerHTML = '<details><summary data-help="Estrategia LAB/PAPER probada, capital REAL propio y permiso temporal. Bloquear la bóveda detiene nuevos envíos; no cierra posiciones.">Bots · REAL <span data-bots-badge></span></summary>' +
      '<p data-bots-state class="state muted" role="status">Abre para consultar.</p><div class="row"><strong data-bots-mode>Nuevo bot</strong>' +
      '<small data-bots-form-state class="muted" role="status"></small>' +
      '<button type="button" class="ghost" data-bots-new hidden>Nuevo bot</button></div>' +
      '<div class="state row" data-bots-handoff hidden><span class="statusbadge off">PAPER → REAL</span><strong></strong>' +
      '<span class="muted" data-bots-handoff-account></span>' +
      '<button type="button" class="ghost" data-bots-discard-handoff>Quitar selección</button></div>' +
      '<form class="row" data-bots-form>' +
      '<label class="field">Prueba PAPER<select name="paper" required><option value="">Selecciona prueba PAPER</option></select></label>' +
      '<label class="field" data-help="Reserva histórica de la misma versión, mercado y temporalidad, anterior al tramo observado en PAPER. Se repite antes de activar o reanudar. DCA conserva una referencia REAL nueva desde su activación, independiente de la histórica.">Reserva<select name="research" required><option value="">Selecciona reserva</option></select></label>' +
      '<a data-bots-research-link href="#/laboratorio" data-help="Abre LAB o DCA para guardar una validación fuera de muestra.">Validar</a>' +
      '<button type="button" class="ghost" data-bots-research-review disabled>Verificar origen</button>' +
      '<label class="field">Límites<select name="draft" required><option value="">Selecciona límites</option></select></label>' +
      '<button type="button" class="ghost" data-bots-limits hidden>Crear límites</button>' +
      '<button type="button" class="ghost" data-bots-account-action hidden></button>' +
      '<label class="field">Entrada · USDT<input name="budget" inputmode="decimal" autocomplete="off" required></label>' +
      '<label class="field" data-help="Sesión: ligada a la pantalla. Operativo: hasta la política revisada (máximo 24 h). Continuo: fecha y límites fijos, con revisiones diarias. Operativo/Continuo mantienen claves en memoria al proteger la vista. No reanudan tras cerrar ORBIS.">Permiso<select name="permission"><option value="SESSION">Sesión</option><option value="OPERATIONAL">Operativo · hasta política</option><option value="CONTINUOUS">Continuo · con caducidad</option></select></label>' +
      '<label class="field" data-bots-until hidden data-help="Fecha final inmutable, hasta 30 días y nunca posterior a tus límites. Renovar evidencia no amplía el permiso.">Hasta<input name="continuous_until" type="datetime-local" step="60"></label>' +
      '<div class="row" data-bots-dca-limits hidden><label class="field" data-help="Capital máximo REAL asignado a este DCA; no se importa efectivo PAPER. Se conserva al reanudar.">Asignación DCA · USDT<input name="dca_total" inputmode="decimal" autocomplete="off"></label>' +
      '<label class="field" data-help="Límite mensual REAL, incluidas comisiones quote. También se respeta el techo mensual de la regla probada. No se reinicia al reanudar.">Mes DCA · USDT<input name="dca_monthly" inputmode="decimal" autocomplete="off"></label></div>' +
      '<button type="submit" data-bots-start disabled>Activar bot</button><button type="button" class="ghost" data-bots-refresh>Actualizar</button></form>' +
      '<div data-bots-origin hidden></div>' +
      '<div data-bots-list aria-live="polite"></div><section data-bots-chart class="real-bot-chart-panel" hidden></section></details>';
    if (window.OrbisMexcBotChart) window.OrbisMexcBotChart.mount(root.querySelector("[data-bots-chart]"),api);
    root.querySelector("details").addEventListener("toggle", function () {
      if (this.open) load(true); else if (timer !== null) { clearTimeout(timer); timer = null; }
    });
    root.querySelector("[name=paper]").addEventListener("change", function () {
      if (handoff && this.value !== handoff.id) handoff = null;
      selectedDrafts();
    });
    root.querySelector("[data-bots-discard-handoff]").addEventListener("click", function () {
      if (busy) return;
      handoff = null;
      root.querySelector("[name=paper]").value = "";
      selectedDrafts();
    });
    root.querySelector("[data-bots-limits]").addEventListener("click", function () {
      var id = root.querySelector("[name=paper]").value;
      var source = candidates.find(function (item) { return item.session_id === id && item.paper_ready; });
      if (source && !busy && !controlsLoading && !preparationIssue) api.prepareLimits(source);
    });
    root.querySelector("[data-bots-account-action]").addEventListener("click", function () {
      if (!busy && !controlsLoading && !this.hidden) api.openAccountAction(this.dataset.botsAccountAction);
    });
    root.querySelector("[name=research]").addEventListener("change", selectedDrafts);
    root.querySelector("[data-bots-research-review]").addEventListener("click", showResearch);
    root.querySelector("[name=draft]").addEventListener("change",selectedDrafts);
    root.querySelector("[name=permission]").addEventListener("change",continuityDate);
    root.querySelector("[data-bots-new]").addEventListener("click", function () { if (!busy) mode(null); });
    root.querySelector("[data-bots-form]").addEventListener("submit", activate);
    root.querySelector("[data-bots-refresh]").addEventListener("click", function () { load(true); });
    root.querySelector("[data-bots-list]").addEventListener("click", action);
    root.querySelector("[data-bots-list]").addEventListener("click",recover);
    if (handoff || keepOpen) {
      controlsLoading = true;
      selectedDrafts();
      root.querySelector("details").open = true;
    }
  }
  function accountChanged() {
    if (!root || !root.isConnected) return;
    mount(root, api, handoff, root.querySelector("details").open);
  }
  window.OrbisMexcBots = {mount:mount, clear:clear, accountChanged:accountChanged,
    preparedSource:function () { return handoff; }, refresh:function () { load(true); }};
})();
