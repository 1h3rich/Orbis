/* Órdenes MEXC Spot manuales: apertura revisada y petición con ID estable. */
(function () {
  "use strict";
  var root = null, api = null, epoch = 0, sessions = [], executionState = null, selectedAccount = null;
  var reviewEpoch = 0;
  var activationDrafts = [], openingReview = null, openingEpoch = 0;
  var reviewControls = "[data-trading-review], [data-trading-budget-review], [data-trading-max-sale]";
  var limitFields = [
    ["max_order_quote", "Máx. por orden"],
    ["max_daily_turnover_quote", "Volumen diario máx."],
    ["max_exposure_quote", "Máx. exposición"],
    ["max_daily_loss_quote", "Pérdida diaria máx."],
    ["max_fee_bps", "Comisión máx. · bps"],
    ["max_slippage_bps", "Deslizamiento máx. · bps"]
  ];
  function clear() {
    epoch += 1; reviewEpoch += 1; root = null; api = null; sessions = [];
    executionState = null; selectedAccount = null;
    activationDrafts = []; openingReview = null; openingEpoch += 1;
  }
  function resetOrderRequest(intentId) {
    var form = root && root.querySelector("[data-trading-order]");
    if (!form || intentId && form.dataset.requestId !== intentId) return;
    if (intentId) form.elements.quantity.value = "";
    delete form.dataset.requestId;
    delete form.dataset.requestBody;
  }
  function notice(text, error) {
    var target = root && root.querySelector("[data-trading-state]");
    if (target) { target.textContent = text; target.className = "state " + (error ? "error" : "muted"); }
  }
  function current(host, account, issued) {
    return host === root && host.isConnected && api && account === api.getAccountId() && issued === epoch;
  }
  function options(select, items, placeholder) {
    select.replaceChildren(new Option(placeholder, ""));
    items.forEach(function (item) {
      select.add(new Option(item.symbol + " · " + (item.manual_only ? "Manual · " : "") + item.id.slice(0, 8), item.id));
    });
  }
  function labelOrder() {
    if (!root) return;
    var form = root.querySelector("[data-trading-order]");
    var policy = sessions.find(function (item) { return item.id === form.elements.session.value; });
    if (form.dataset.labeledSession !== form.elements.session.value) {
      clearCalculatedDraft(form);
      if (form.dataset.labeledSession) form.elements.budget.value = "";
      form.dataset.labeledSession = form.elements.session.value;
    }
    form.querySelector("[data-trading-base]").textContent = "Cantidad" + (policy ? " · " + policy.base_asset : "");
    form.querySelector("[data-trading-quote]").textContent = "Precio límite" + (policy ? " · " + policy.quote_asset : "");
    var selling = form.elements.side.value === "SELL";
    form.querySelector("[data-trading-budget-label]").textContent =
      (selling ? "Importe bruto" : "Presupuesto") + (policy ? " · " + policy.quote_asset : "");
    form.querySelector("[data-trading-max-sale]").hidden = !selling;
  }
  function clearCalculatedDraft(form) {
    if (!form.dataset.calculatedDraft) return;
    form.elements.quantity.value = "";
    form.elements.price.value = "";
    delete form.dataset.calculatedDraft;
    delete form.dataset.budgetCap;
  }
  function orderTerms(form) {
    return {side:form.elements.side.value, quantity_base:form.elements.quantity.value.trim(),
      limit_price_quote:form.elements.price.value.trim()};
  }
  function clearOrderReview(form) {
    reviewEpoch += 1;
    var status = form.querySelector("[data-trading-review-status]");
    status.hidden = true;
    status.textContent = "";
    var disabled = form.querySelector("[data-trading-send]").disabled || !executionState || executionState.bot_active;
    form.querySelectorAll(reviewControls).forEach(function (button) {
      button.disabled = disabled;
    });
  }
  function showLimits() {
    var review = root.querySelector("[data-trading-limits-review]");
    var selected = root.querySelector("[name=draft]").value;
    var limits = activationDrafts.find(function (item) { return item.id === selected; });
    review.hidden = !limits || api.amountsHidden();
    review.textContent = review.hidden ? "" : limitFields.map(function (entry, index) {
      return entry[1] + " " + limits[entry[0]] + (index < 4 ? " " + limits.quote_asset : "");
    }).join(" · ") + " · Vence " + new Date(limits.expires_at).toLocaleString();
  }
  function openingControls(form) {
    var ready = form.dataset.ready === "1" && !!form.elements.draft.value;
    form.querySelector("[data-trading-opening-review]").disabled = !ready || !!form.dataset.busy;
    var approve = form.querySelector("[data-trading-activate]");
    approve.hidden = !openingReview || form.dataset.canActivate !== "1";
    approve.disabled = !ready || approve.hidden || !!form.dataset.busy;
  }
  function resetOpeningReview(form) {
    openingEpoch += 1;
    openingReview = null;
    delete form.dataset.busy;
    form.elements.draft.disabled = false;
    var report = root.querySelector("[data-trading-opening-report]");
    report.replaceChildren();
    report.hidden = true;
    root.querySelector("[data-trading-direct-credit]").hidden = true;
    openingControls(form);
  }
  function renderPending(item) {
    var pending = root.querySelector("[data-trading-pending]");
    var action = pending.querySelector("button");
    action.dataset.intent = item ? item.intent_id : "";
    var status = pending.querySelector("[data-trading-pending-status]");
    status.textContent = !item ? "" : item.stage === "ORDER_OBSERVED_OPEN" ?
      (item.external_status === "PARTIALLY_FILLED" ? "Parcial abierta" : "Orden abierta") :
      item.journal_status === "RESERVED" ? "Reserva pendiente" : "Orden pendiente de revisión";
    api.renderProgress(item || {}, pending.querySelector("[data-trading-pending-quantities]"));
  }
  function load(preferredDraft, orderError) {
    if (!root || !api) return;
    var account = api.getAccountId();
    var orderForm = root.querySelector("[data-trading-order]");
    clearOrderReview(orderForm);
    var limitsForm = root.querySelector("[data-trading-limits]");
    var limitsDetails = limitsForm.parentElement;
    var privateMode = api.amountsHidden();
    var activationForm = root.querySelector("[data-trading-activation]");
    var pending = root.querySelector("[data-trading-pending]");
    pending.hidden = true;
    renderPending(null);
    delete activationForm.dataset.ready;
    delete activationForm.dataset.canActivate;
    resetOpeningReview(activationForm);
    limitsDetails.hidden = privateMode;
    activationForm.hidden = privateMode;
    orderForm.hidden = privateMode;
    if (account !== selectedAccount || privateMode) {
      limitsForm.reset();
      limitsDetails.open = false;
      delete limitsForm.dataset.pending;
      delete limitsForm.dataset.saving;
      limitsForm.querySelector("[data-trading-limits-status]").textContent = "";
      orderForm.reset();
      delete orderForm.dataset.calculatedDraft;
      delete orderForm.dataset.budgetCap;
      delete orderForm.dataset.labeledSession;
      resetOrderRequest();
      sessions = [];
      activationDrafts = [];
      executionState = null;
      selectedAccount = account;
    }
    showLimits();
    if (!root.querySelector("details").open) return;
    var host = root, issued = ++epoch;
    if (!account) return;
    host.querySelectorAll("[data-trading-send], [data-trading-stop], " + reviewControls).forEach(function (button) { button.disabled = true; });
    var limitsButton = limitsForm.querySelector('button[type="submit"]');
    limitsButton.disabled = true;
    notice("Consultando operativa…", false);
    Promise.all([
      api.request("/accounts/" + encodeURIComponent(account) + "/mexc-policy-drafts", null, true),
      api.request("/accounts/" + encodeURIComponent(account) + "/mexc-trading-sessions", null, true),
      api.request("/exchanges/mexc/trade/status", null, true),
      api.request("/accounts/" + encodeURIComponent(account) + "/mexc-execution-status", null, true),
      api.request("/accounts/" + encodeURIComponent(account) + "/mexc-manual-limits", null, true)
    ]).then(function (results) {
      if (!current(host, account, issued)) return;
      sessions = results[1].filter(function (item) { return item.execution_enabled && item.mode === "MANUAL"; });
      executionState = results[3];
      var draftSelect = host.querySelector("[name=draft]");
      var previousDraft = preferredDraft || draftSelect.value;
      var drafts = activationDrafts = results[4].concat(results[0]).filter(function (item) {
        return Date.parse(item.expires_at) > Date.now();
      });
      options(draftSelect, drafts, "Selecciona límites guardados");
      if (drafts.some(function (item) { return item.id === previousDraft; })) draftSelect.value = previousDraft;
      showLimits();
      var select = host.querySelector("[name=session]");
      var previous = select.value;
      options(select, sessions, "Selecciona operativa activa");
      if (sessions.some(function (item) { return item.id === previous; })) select.value = previous;
      else if (sessions.length) select.value = sessions[0].id;
      labelOrder();
      var ready = results[2].verified === true;
      limitsButton.disabled = !executionState.linked || !api.viewConnected() || !!limitsForm.dataset.saving;
      var canReview = !privateMode && executionState.linked && api.viewConnected();
      activationForm.dataset.ready = canReview ? "1" : "0";
      activationForm.dataset.canActivate = canReview && ready &&
        !executionState.bot_active && !executionState.pending_orders && !executionState.withdrawal_pending &&
        (!executionState.stopped || executionState.can_review_resume) ? "1" : "0";
      openingControls(activationForm);
      host.querySelector("[data-trading-activate]").textContent = executionState.stopped ? "Reabrir operativa" : "Habilitar operativa";
      host.querySelector("[data-trading-send]").disabled = privateMode || !ready || !sessions.length ||
        executionState.stopped || executionState.pending_orders > 0;
      clearOrderReview(orderForm);
      host.querySelector("[data-trading-stop]").disabled = !executionState.linked || executionState.stopped;
      if (executionState.pending_orders > 0) {
        notice("Orden pendiente", true);
        pending.hidden = false;
        api.request("/accounts/" + encodeURIComponent(account) + "/mexc-pending-orders", null, true)
          .then(function (data) {
            if (!current(host, account, issued) || !host.querySelector("details").open) return;
            if (data.orders.length === 1 && data.orders[0].account_id === account) renderPending(data.orders[0]);
          }).catch(function () {});
      } else if (executionState.withdrawal_pending) {
        notice("Retirada pendiente · abre la guía Wallet ↔ MEXC.", true);
      } else if (orderError) {
        notice(orderError, true);
      } else notice(executionState.bot_active ? "Bot activo · deténlo antes de operar manualmente." : executionState.stopped ?
        "Cuenta detenida · selecciona límites y revisa el saldo para reabrir." :
        !executionState.linked || !api.viewConnected() ? "Conecta y vincula MEXC View." :
        !ready ? "Verifica Trade para habilitar órdenes." :
        privateMode ? "Importes ocultos · muestra los importes para operar." :
        sessions.length ? "Operativa manual · activa. Cada orden se confirma antes de enviar." :
        drafts.length ? "Selecciona límites y revisa el saldo." : "Configura los límites manuales.", false);
      if (preferredDraft && draftSelect.value === preferredDraft) draftSelect.focus();
    }).catch(function (error) {
      if (current(host, account, issued)) notice(error.message === "Not Found" ?
        "Reinicia ORBIS para cargar la operativa nueva." : error.message, true);
    });
  }
  async function saveLimits(event) {
    event.preventDefault();
    var host = root, account = api.getAccountId(), client = api, form = event.currentTarget;
    var button = form.querySelector('button[type="submit"]');
    var status = form.querySelector("[data-trading-limits-status]");
    if (button.disabled || form.dataset.saving || !account || client.amountsHidden()) return;
    var base = form.elements.base_asset.value.trim().toUpperCase();
    var quote = form.elements.quote_asset.value.trim().toUpperCase();
    var hours = Number(form.elements.hours.value);
    var fields = {symbol: base + quote, base_asset: base, quote_asset: quote,
      max_data_age_seconds: Number(form.elements.max_data_age_seconds.value)};
    limitFields.forEach(function (entry) { fields[entry[0]] = form.elements[entry[0]].value.trim(); });
    var fingerprint = JSON.stringify([account, hours, fields]);
    var pending = form.dataset.pending && JSON.parse(form.dataset.pending);
    if (!pending || pending.fingerprint !== fingerprint) {
      pending = {fingerprint: fingerprint, body: Object.assign({}, fields, {
        request_id: client.uuid(), expires_at: new Date(Date.now() + hours * 3600000).toISOString()
      })};
      form.dataset.pending = JSON.stringify(pending);
    }
    var requestId = pending.body.request_id;
    form.dataset.saving = requestId;
    button.disabled = true;
    status.className = "state muted";
    status.textContent = "Guardando límites…";
    function stillSelected() {
      return root === host && form.isConnected && api === client && account === client.getAccountId() &&
        !client.amountsHidden() && form.dataset.saving === requestId;
    }
    try {
      var saved = await client.request("/accounts/" + encodeURIComponent(account) + "/mexc-manual-limits", {
        method: "POST", body: JSON.stringify(pending.body)
      });
      if (!stillSelected()) return;
      delete form.dataset.pending;
      form.reset();
      form.parentElement.open = false;
      delete form.dataset.saving;
      load(saved.id);
    } catch (error) {
      if (!stillSelected()) return;
      status.className = "state error";
      status.textContent = error.message;
    } finally {
      if (stillSelected()) {
        delete form.dataset.saving;
        button.disabled = !executionState || !executionState.linked || !client.viewConnected();
      }
    }
  }
  async function reviewOpening(event) {
    event.preventDefault();
    var host = root, account = api.getAccountId(), issued = epoch;
    var form = event.currentTarget;
    var draft = activationDrafts.find(function (item) { return item.id === form.elements.draft.value; });
    if (!draft || form.querySelector("[data-trading-opening-review]").disabled || api.amountsHidden()) return;
    resetOpeningReview(form);
    var reviewed = openingEpoch;
    var report = host.querySelector("[data-trading-opening-report]");
    form.dataset.busy = "1";
    openingControls(form);
    report.hidden = false;
    report.className = "state muted";
    report.textContent = "Comprobando saldo Spot…";
    function stillCurrent() {
      return current(host, account, issued) && reviewed === openingEpoch && !api.amountsHidden() &&
        form.elements.draft.value === draft.id;
    }
    try {
      var result = await api.request("/accounts/" + encodeURIComponent(account) + "/mexc-spot-parity/observations", {
        method: "POST", body: JSON.stringify({request_id: api.uuid()})
      });
      if (!stillCurrent()) return;
      if (!result.saved || result.blockers.length) {
        api.renderParity(result, report);
        var differences = result.assets.filter(function (item) { return !item.quantity_equal; });
        host.querySelector("[data-trading-direct-credit]").hidden = !(result.blockers.length === 1 &&
          result.blockers[0] === "QUANTITY_DIFFERENCE" && differences.length === 1 &&
          differences[0].asset === "USDT" && !differences[0].difference_mexc_minus_book.startsWith("-"));
        return;
      }
      var now = Date.now();
      var expires = Math.min(Date.parse(draft.expires_at), Date.parse(result.checked_at) + draft.max_data_age_seconds * 1000);
      if (result.book_current === false || result.recent === false || !(expires > now)) {
        throw new Error("Repite la revisión de saldo.");
      }
      openingReview = {draft:draft.id, anchor:result.id, requestId:api.uuid(), expires:expires,
        reopening:executionState.stopped};
      report.textContent = "Saldo Spot coincide · " + draft.symbol;
    } catch (error) {
      if (stillCurrent()) {
        report.className = "state error";
        report.textContent = error.message;
      }
    } finally {
      if (stillCurrent()) {
        delete form.dataset.busy;
        openingControls(form);
      }
    }
  }
  async function activate(event) {
    var host = root, account = api.getAccountId(), issued = epoch;
    var form = event.currentTarget.form, review = openingReview;
    if (!review || event.currentTarget.disabled || api.amountsHidden() || form.elements.draft.value !== review.draft) return;
    if (Date.now() >= review.expires) {
      resetOpeningReview(form);
      notice("La revisión caducó · revisa el saldo de nuevo.", true);
      return;
    }
    var reopening = review.reopening;
    if (!window.confirm(reopening ?
      "¿Revisar el saldo y reabrir MEXC con estos límites? Se creará una operativa nueva; las anteriores seguirán inhabilitadas." :
      "¿Habilitar órdenes reales MEXC con estos límites? La cuenta se usará exclusivamente desde ORBIS durante esta sesión. Esto no inicia un bot.")) return;
    form.dataset.busy = "1";
    form.elements.draft.disabled = true;
    openingControls(form);
    notice("Comprobando actividad y límites…", false);
    try {
      var policy = await api.request("/accounts/" + encodeURIComponent(account) + "/mexc-policy-drafts/" + encodeURIComponent(review.draft) + (reopening ? "/resume" : "/activate"), {
        method: "POST", body: JSON.stringify({request_id: review.requestId, anchor_id: review.anchor,
          confirmation: reopening ? "REABRIR_OPERATIVA_REAL" : "ACTIVAR_ORDENES_REALES"})
      });
      if (!current(host, account, issued)) return;
      host.querySelector("[name=session]").add(new Option(policy.symbol, policy.id));
      host.querySelector("[name=session]").value = policy.id;
      resetOrderRequest();
      load();
    } catch (error) {
      if (current(host, account, issued)) load(undefined, error.message);
    }
  }
  async function stop() {
    var host = root, account = api.getAccountId(), issued = epoch;
    if (!window.confirm("¿Detener nuevos envíos en esta cuenta MEXC? Las órdenes ya enviadas deberán revisarse en Órdenes MEXC.")) return;
    var button = host.querySelector("[data-trading-stop]");
    button.disabled = true;
    try {
      await api.request("/accounts/" + encodeURIComponent(account) + "/mexc-stop", {
        method: "POST", body: JSON.stringify({confirmation: "DETENER"})
      });
      if (!current(host, account, issued)) return;
      resetOrderRequest();
      api.onChange();
      load();
    } catch (error) {
      if (current(host, account, issued)) { notice(error.message, true); button.disabled = false; }
    }
  }
  async function review(event) {
    var host = root, account = api.getAccountId(), issued = epoch, button = event.currentTarget;
    var form = button.form, status = form.querySelector("[data-trading-review-status]");
    var policy = sessions.find(function (item) { return item.id === form.elements.session.value; });
    var byBudget = button.hasAttribute("data-trading-budget-review");
    var maxSale = button.hasAttribute("data-trading-max-sale");
    if (!policy || button.disabled || api.amountsHidden() || maxSale && form.elements.side.value !== "SELL" ||
        !byBudget && !maxSale && !form.reportValidity()) return;
    var terms = orderTerms(form);
    if (maxSale) {
      clearCalculatedDraft(form);
      form.elements.budget.value = "";
      form.elements.quantity.value = "";
      form.elements.price.value = "";
      terms = {side:"SELL", size_mode:"MAX_AVAILABLE"};
    } else if (byBudget) {
      var budget = form.elements.budget.value.trim();
      if (!budget) { form.elements.budget.focus(); return; }
      if (/^\d+,\d+$/.test(budget)) budget = budget.replace(",", ".");
      clearCalculatedDraft(form);
      terms = {side:form.elements.side.value, budget_quote:budget};
    }
    var reviewed = ++reviewEpoch;
    function stillCurrent() {
      return current(host, account, issued) && reviewed === reviewEpoch && !api.amountsHidden();
    }
    var reviewButtons = form.querySelectorAll(reviewControls);
    reviewButtons.forEach(function (control) { control.disabled = true; });
    status.hidden = false;
    status.className = "state muted";
    status.textContent = "Revisando orden…";
    try {
      var result = await api.request("/accounts/" + encodeURIComponent(account) + "/mexc-trading-sessions/" +
        encodeURIComponent(policy.id) + "/orders/review", {method:"POST", body:JSON.stringify(terms)});
      if (!stillCurrent()) return;
      var quote = result.quote_asset;
      var reasons = {
        MIN_NOTIONAL:"Mínimo " + result.min_order_quote + " " + quote,
        MAX_NOTIONAL:"Supera el máximo del mercado",
        BASE_STEP:"Incremento de cantidad " + result.base_step + " " + policy.base_asset,
        BASE_PRECISION:"Demasiados decimales en la cantidad", PRICE_PRECISION:"Demasiados decimales en el precio",
        EXCHANGE_PRICE_BOUND:"Precio fuera de la banda de MEXC", PRICE_BOUND:"Precio fuera del límite de deslizamiento",
        ORDER_LIMIT:"Supera el límite por orden", BUDGET_LIMIT:"Supera el presupuesto", FEE_LIMIT:"Supera el límite de comisión",
        EXPOSURE_LIMIT:"Supera el límite de exposición", DAILY_TURNOVER_LIMIT:"Supera el volumen diario",
        DAILY_LOSS_LIMIT:"Límite de pérdida diario alcanzado", PROJECTED_DAILY_LOSS_LIMIT:"Supera la pérdida permitida",
        INSUFFICIENT_QUOTE:"Saldo insuficiente en " + quote, INSUFFICIENT_BASE:"Saldo insuficiente en " + policy.base_asset,
        SELL_COST_BASIS_UNKNOWN:"Falta el coste de adquisición", SIDE_DISABLED:"MEXC tiene este lado deshabilitado",
        STALE_INSTRUMENT_OR_TRADE:"Datos de mercado caducados", STALE_DATA_OR_SIGNAL:"Datos caducados",
        POLICY_OR_SIGNAL_TIME_INVALID:"Operativa caducada"
      };
      status.className = "state " + (result.compatible ? "ok" : "error");
      status.textContent = (result.compatible ? "Compatible" : reasons[result.reason] || "Orden no compatible") +
        " · Importe " + result.notional_quote + " " + quote + " · Comisión máx. est. " +
        result.estimated_max_fee_quote + " " + quote;
      if (byBudget || maxSale) {
        var buying = terms.side === "BUY";
        status.textContent += " · " + (buying ? "Coste máximo est. " : "Neto est. ") +
          result.estimated_total_quote + " " + quote + " · Disponible " +
          (buying ? result.available_quote + " " + quote : result.available_base + " " + policy.base_asset);
        if (maxSale) status.textContent += " · Fuera de la orden " + result.base_outside_order + " " + policy.base_asset;
        if (result.compatible) {
          form.elements.quantity.value = result.quantity_base;
          form.elements.price.value = result.limit_price_quote;
          form.dataset.calculatedDraft = "1";
          if (buying) form.dataset.budgetCap = result.budget_quote;
        }
      }
    } catch (error) {
      if (stillCurrent()) { status.className = "state error"; status.textContent = error.message; }
    } finally {
      if (stillCurrent()) reviewButtons.forEach(function (control) { control.disabled = false; });
    }
  }
  async function send(event) {
    event.preventDefault();
    var host = root, account = api.getAccountId(), issued = epoch, form = event.currentTarget;
    var policy = sessions.find(function (item) { return item.id === form.elements.session.value; });
    var button = host.querySelector("[data-trading-send]");
    if (!policy || button.disabled || api.amountsHidden() || !executionState ||
        executionState.pending_orders > 0 || executionState.stopped) return;
    var payload = Object.assign(orderTerms(form), {confirmation:"ENVIAR_ORDEN_REAL"});
    if (payload.side === "BUY" && form.dataset.budgetCap) payload.max_spend_quote = form.dataset.budgetCap;
    if (!window.confirm((payload.side === "BUY" ? "Comprar " : "Vender ") + payload.quantity_base +
        " " + policy.base_asset + " a precio límite " + payload.limit_price_quote + " " + policy.quote_asset + " en MEXC REAL" +
        (payload.max_spend_quote ? " con presupuesto máximo " + payload.max_spend_quote + " " + policy.quote_asset : "") + "?")) return;
    var fingerprint = JSON.stringify([policy.id, payload]);
    if (form.dataset.requestBody !== fingerprint) {
      form.dataset.requestBody = fingerprint;
      form.dataset.requestId = api.uuid();
    }
    payload.request_id = form.dataset.requestId;
    button.disabled = true;
    clearOrderReview(form);
    notice("Verificando y enviando orden…", false);
    try {
      var result = await api.request("/accounts/" + encodeURIComponent(account) + "/mexc-trading-sessions/" + encodeURIComponent(policy.id) + "/orders", {
        method: "POST", body: JSON.stringify(payload)
      });
      if (!current(host, account, issued)) return;
      var outcome = result.synchronization;
      var terminal = "Orden final · consulta el diario.";
      if (outcome?.external_status === "FILLED") terminal = "Orden ejecutada y registrada.";
      if (["CANCELED", "PARTIALLY_CANCELED"].includes(outcome?.external_status)) {
        terminal = outcome.fill_count > 0 ? "Parcial registrada · resto cancelado." : "Cancelada sin ejecución.";
      }
      notice(result.journal_status === "RESERVATION_CLOSED" ? terminal :
        result.detail || "Orden registrada · consulta su ejecución en Órdenes MEXC.", result.stage === "REVIEW_REQUIRED");
      if (result.journal_status === "RESERVATION_CLOSED") {
        form.elements.quantity.value = "";
        if (form.dataset.calculatedDraft) {
          form.elements.budget.value = "";
          clearCalculatedDraft(form);
        }
        resetOrderRequest();
        button.disabled = false;
        clearOrderReview(form);
      } else load(undefined, result.detail);
      api.onChange();
    } catch (error) {
      if (current(host, account, issued)) load(undefined, error.message);
    }
  }
  function mount(element, client) {
    clear(); root = element; api = client;
    root.innerHTML = '<details class="real-trading-panel"><summary data-help="Órdenes LIMIT de compra y venta, con saldo revisado y límites guardados. Requiere claves View y Trade de la misma cuenta. Usa una cuenta dedicada a ORBIS mientras esté activa.">Operar MEXC · REAL</summary>' +
      '<p data-trading-state class="state muted" role="status">Abre para consultar.</p>' +
      '<div data-trading-pending hidden><strong data-trading-pending-status></strong><div data-trading-pending-quantities></div>' +
      '<button type="button" class="ghost" aria-controls="real-mexc-execution">Revisar orden pendiente</button></div>' +
      '<button type="button" class="ghost" data-trading-opening hidden aria-controls="real-mexc-opening" ' +
      'data-help="Registra fondos que ya tienes en MEXC. El coste lo declaras tú.">Registrar saldo inicial</button>' +
      '<details class="real-panel"><summary data-help="Guarda límites desactivados para órdenes manuales MEXC Spot. Cada orden necesita una confirmación.">Configurar límites manuales</summary>' +
      '<form class="row" data-trading-limits autocomplete="off">' +
      '<label class="field">Activo base<input name="base_asset" placeholder="BTC" maxlength="16" pattern="[A-Za-z0-9]{2,16}" required></label>' +
      '<label class="field">Divisa<input name="quote_asset" value="USDT" maxlength="16" pattern="[A-Za-z0-9]{2,16}" required></label>' +
      limitFields.map(function (entry) {
        return '<label class="field">' + entry[1] + '<input name="' + entry[0] + '" inputmode="decimal" maxlength="80" required></label>';
      }).join("") +
      '<label class="field">Antigüedad datos · s<input name="max_data_age_seconds" type="number" min="1" max="300" value="60" required></label>' +
      '<label class="field">Vigencia<select name="hours"><option value="1">1 hora</option><option value="4">4 horas</option><option value="24">24 horas</option></select></label>' +
      '<button type="submit" class="ghost" disabled>Guardar límites manuales</button>' +
      '<p data-trading-limits-status class="state muted" role="status"></p></form></details>' +
      '<form class="row" data-trading-activation><label class="field">Límites<select name="draft" required><option value="">Selecciona límites guardados</option></select></label>' +
      '<button type="submit" class="ghost" data-trading-opening-review disabled data-help="Compara dos lecturas MEXC con el libro. Sólo guarda una comprobación si coinciden.">Revisar saldo</button>' +
      '<button type="button" class="ghost" data-trading-activate hidden disabled>Habilitar operativa</button></form>' +
      '<p data-trading-limits-review class="muted" hidden></p>' +
      '<div data-trading-opening-report class="state muted" role="status" hidden></div>' +
      '<button type="button" class="ghost" data-trading-direct-credit aria-controls="real-mexc-transfers" hidden>Buscar ingreso MEXC</button>' +
      '<form class="row" data-trading-order><label class="field">Operativa<select name="session" required><option value="">Selecciona operativa activa</option></select></label>' +
      '<label class="field">Orden<select name="side"><option value="BUY">Comprar</option><option value="SELL">Vender</option></select></label>' +
      '<label class="field" data-help="Compra con comisión máxima estimada incluida. Venta por importe bruto al precio límite."><span data-trading-budget-label>Presupuesto</span><input name="budget" inputmode="decimal" maxlength="80" autocomplete="off"></label>' +
      '<button type="button" class="ghost" data-trading-budget-review disabled data-help="Propone cantidad y precio límite con la cotización de MEXC. No reserva fondos ni envía una orden.">Calcular orden</button>' +
      '<button type="button" class="ghost" data-trading-max-sale hidden disabled data-help="Calcula el máximo por orden al precio límite propuesto, con saldo conciliado y margen para una comisión en el activo vendido.">Máximo vendible</button>' +
      '<label class="field"><span data-trading-base>Cantidad</span><input name="quantity" inputmode="decimal" autocomplete="off" required></label>' +
      '<label class="field"><span data-trading-quote>Precio límite</span><input name="price" inputmode="decimal" autocomplete="off" required></label>' +
      '<button type="button" class="ghost" data-trading-review disabled data-help="Comprueba los datos actuales, sin reservar fondos. El envío vuelve a verificar la orden.">Revisar orden</button>' +
      '<button type="submit" data-trading-send disabled>Enviar orden real</button>' +
      '<p class="state muted" data-trading-review-status role="status" hidden></p></form>' +
      '<button type="button" class="ghost" data-trading-stop disabled data-help="Detiene nuevos envíos y libera reservas no enviadas. No cancela órdenes abiertas en MEXC.">Detener cuenta</button></details>';
    var activationForm = root.querySelector("[data-trading-activation]");
    root.querySelector("details").addEventListener("toggle", function () {
      if (!this.isConnected || root !== element) return;
      if (this.open) load();
      else resetOpeningReview(activationForm);
    });
    root.querySelector("[data-trading-opening]").addEventListener("click", api.openOpening);
    root.querySelector("[data-trading-pending] button").addEventListener("click", function () {
      api.openPending(this.dataset.intent);
    });
    root.querySelector("[data-trading-limits]").addEventListener("submit", saveLimits);
    activationForm.addEventListener("submit", reviewOpening);
    activationForm.querySelector("[data-trading-activate]").addEventListener("click", activate);
    activationForm.elements.draft.addEventListener("change", function () {
      resetOpeningReview(activationForm);
      showLimits();
    });
    root.querySelector("[data-trading-direct-credit]").addEventListener("click", api.openTransfers);
    root.querySelector("[data-trading-order]").addEventListener("submit", send);
    root.querySelector("[data-trading-order]").addEventListener("input", function (event) {
      if (event.target.name === "quantity" || event.target.name === "price") {
        if (this.dataset.calculatedDraft) this.elements.budget.value = "";
        delete this.dataset.calculatedDraft;
        delete this.dataset.budgetCap;
      } else clearCalculatedDraft(this);
      clearOrderReview(this);
    });
    root.querySelector("[data-trading-review]").addEventListener("click", review);
    root.querySelector("[data-trading-budget-review]").addEventListener("click", review);
    root.querySelector("[data-trading-max-sale]").addEventListener("click", review);
    root.querySelector("[name=session]").addEventListener("change", labelOrder);
    root.querySelector("[name=side]").addEventListener("change", labelOrder);
    root.querySelector("[data-trading-stop]").addEventListener("click", stop);
  }
  window.OrbisMexcTrading = {mount: mount, clear: clear, refresh: load, resetOrderRequest: resetOrderRequest};
})();
