/* Cartera REAL manual. Token y contraseña sólo en memoria; no localStorage. */
(function () {
  "use strict";
  var token = null;
  var accountId = null;
  var hiddenAmounts = false;
  var root = null;
  var afterRestoreMessage = null;
  var protectedOperationTimer = null;
  var afterUnlockMessage = null;
  var csvImportDraft = null;
  var csvImportEpoch = 0;
  var realRefreshEpoch = 0;
  var sessionEpoch = 0;
  var dashboardEpoch = 0;
  var realLoadEpoch = 0;
  var idleDeadline = 0;
  var idleTimer = null;
  var CLIENT_IDLE_MS = 895000; /* cinco segundos antes del límite del servidor */
  var mexcTradeEpoch = 0;
  var mexcArchiveEpoch = 0;
  var mexcTradeVerifyEpoch = 0;
  var mexcTransferEpoch = 0;
  var mexcExecutionEpoch = 0;
  var mexcAuditEpoch = 0;
  var mexcViewConnected = false;
  var mexcRouteState = null;
  var mexcRouteEpoch = 0;
  var mexcConnectionEpoch = 0;
  var grvtConnectionEpoch = 0;
  var mexcValueEpoch = 0;
  var mexcCompareEpoch = 0;
  var mexcOpeningEpoch = 0;
  var mexcOpeningDraft = null;
  var mexcIdentityEpoch = 0;
  var mexcLinkEpoch = 0;
  var mexcParityEpoch = 0;
  var mexcParityHistoryEpoch = 0;
  var mexcOpenOrdersEpoch = 0;
  var mexcOpenOrdersHistoryEpoch = 0;
  var mexcPolicyEpoch = 0;
  var mexcSupervisorEpoch = 0;
  var mexcSupervisorTimer = null;
  var mexcSupervisorActive = false;
  var lastMexcSupervisorRunning = null;
  var lastMexcRecoveryRequired = false;
  var lastMexcSupervisorStalled = false;
  var mexcSupervisorEverRunning = false;
  var mexcSupervisorUnreachable = false;
  var mexcSupervisorSnapshot = null;
  var grvtOrdersEpoch = 0;
  var grvtOrderEpoch = 0;
  var grvtFillsEpoch = 0;
  var grvtArchiveEpoch = 0;
  var grvtTestnetRecoveryEpoch = 0;
  var lastGrvtTestnetRecovery = null;
  var perpLiveReview = null;
  var portfolioHistory = null;
  var pendingMexcSource = null;
  var pendingGrvtSource = null;
  var pendingDexSource = null;
  var mexcLimitsFromSource = null;

  function clearPortfolioHistory() {
    if (portfolioHistory) portfolioHistory.dispose();
    portfolioHistory = null;
  }

  function forgetSession() {
    clearPortfolioHistory();
    pendingMexcSource = null;
    pendingGrvtSource = null;
    pendingDexSource = null;
    mexcLimitsFromSource = null;
    mexcConnectionEpoch += 1;
    mexcRouteEpoch += 1;
    mexcRouteState = null;
    grvtConnectionEpoch += 1;
    if (perpLiveReview) { perpLiveReview.dispose(); perpLiveReview = null; }
    if (protectedOperationTimer !== null) clearTimeout(protectedOperationTimer);
    protectedOperationTimer = null;
    if (window.OrbisGrvtTestnet) window.OrbisGrvtTestnet.clear();
    if (window.OrbisGrvtOco) window.OrbisGrvtOco.clear();
    if (window.OrbisGrvtBracket) window.OrbisGrvtBracket.clear();
    if (window.OrbisGrvtBot) window.OrbisGrvtBot.clear();
    if (window.OrbisGrvtMainnet) window.OrbisGrvtMainnet.clear();
    if (window.OrbisMexcTrading) window.OrbisMexcTrading.clear();
    if (window.OrbisMexcBots) window.OrbisMexcBots.clear();
    mexcSupervisorEpoch += 1;
    if (mexcSupervisorTimer !== null) window.clearTimeout(mexcSupervisorTimer);
    mexcSupervisorTimer = null;
    mexcSupervisorActive = false;
    lastMexcSupervisorRunning = null;
    lastMexcRecoveryRequired = false;
    lastMexcSupervisorStalled = false;
    mexcSupervisorEverRunning = false;
    mexcSupervisorUnreachable = false;
    mexcSupervisorSnapshot = null;
    var globalSupervisor = document.getElementById("real-mexc-supervisor-global");
    if (globalSupervisor) globalSupervisor.hidden = true;
    mexcTradeEpoch += 1;
    mexcArchiveEpoch += 1;
    mexcTradeVerifyEpoch += 1;
    mexcTransferEpoch += 1;
    mexcExecutionEpoch += 1;
    mexcAuditEpoch += 1;
    mexcViewConnected = false;
    mexcValueEpoch += 1;
    mexcCompareEpoch += 1;
    mexcOpeningEpoch += 1;
    mexcOpeningDraft = null;
    mexcIdentityEpoch += 1;
    mexcLinkEpoch += 1;
    mexcParityEpoch += 1;
    mexcParityHistoryEpoch += 1;
    mexcOpenOrdersEpoch += 1;
    mexcOpenOrdersHistoryEpoch += 1;
    mexcPolicyEpoch += 1;
    grvtOrdersEpoch += 1;
    grvtOrderEpoch += 1;
    grvtFillsEpoch += 1;
    grvtArchiveEpoch += 1;
    grvtTestnetRecoveryEpoch += 1;
    lastGrvtTestnetRecovery = null;
    csvImportDraft = null;
    csvImportEpoch += 1;
    realRefreshEpoch += 1;
    token = null;
    sessionEpoch += 1;
    idleDeadline = 0;
    if (idleTimer !== null) window.clearTimeout(idleTimer);
    idleTimer = null;
    if (window.OrbisWallet) window.OrbisWallet.clear();
  }
  function showLocked() {
    var summary = document.getElementById("home-real");
    if (summary) renderDashboard(summary);
    if (window.location.hash === "#/real" && root && root.isConnected) render(root);
  }
  function onIdle() {
    if (!token || !idleDeadline) return;
    if (Date.now() < idleDeadline) {
      idleTimer = window.setTimeout(onIdle, idleDeadline - Date.now());
      return;
    }
    var oldToken = token;
    afterRestoreMessage = { text: "Vista protegida por inactividad.", error: false };
    forgetSession();
    showLocked();
    fetch("/real/vault/protect-view", {
      method: "POST", cache: "no-store", credentials: "same-origin", referrerPolicy: "no-referrer",
      headers: { "Content-Type": "application/json", "X-Orbis-Vault-Token": oldToken }
    }).catch(function () { /* El servidor impone igualmente su propio vencimiento. */ });
  }
  function armIdle(startedAt) {
    if (!token) return;
    idleDeadline = Math.max(idleDeadline, startedAt + CLIENT_IDLE_MS);
    if (idleTimer !== null) window.clearTimeout(idleTimer);
    idleTimer = window.setTimeout(onIdle, Math.max(0, idleDeadline - Date.now()));
  }
  function checkIdle() {
    if (token && idleDeadline && Date.now() >= idleDeadline) onIdle();
  }
  document.addEventListener("visibilitychange", checkIdle);
  window.addEventListener("focus", checkIdle);

  function esc(value) {
    return String(value == null ? "" : value).replace(/[&<>"']/g, function (char) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[char];
    });
  }
  function show(value) { return hiddenAmounts ? "••••" : esc(value); }
  function showSummary(value) {
    if (hiddenAmounts) return "••••";
    var raw = String(value), formatter = window.OrbisUI && window.OrbisUI.fmtDecimal;
    if (!/^[+-]?\d+\.\d{9,}$/.test(raw) || !formatter) return esc(raw);
    var rounded = formatter(raw, 0, 8);
    if (rounded === "0" && /[1-9]/.test(raw)) return esc((raw[0] === "-" ? "−" : "") + "<0,00000001");
    return "≈ " + esc(rounded === null ? "—" : rounded);
  }
  function valuationGap(reason) {
    if (reason === "missing_or_stale_price") return "sin precio actual";
    if (reason === "negative_cash_or_missing_contribution") return "falta aportación o caja negativa";
    return "motivo no disponible";
  }
  function openValuationAllocations(container) {
    return new Set(Array.from(container.querySelectorAll("[data-valuation-allocation][open]"), function (item) {
      return item.dataset.valuationAllocation;
    }));
  }
  function valuationAllocation(item, open) {
    if (item.allocation === null) return '<p class="muted real-allocation-empty">Distribución pendiente · ' + valuationGap(item.reason) + '</p>';
    if (!Array.isArray(item.allocation) || !item.allocation.length) return "";
    var rows = hiddenAmounts ? '<p class="muted">Importes ocultos</p>' :
      '<ul class="real-allocation-rows">' + item.allocation.map(function (part) {
        var label = part.kind === "CASH" ? "Efectivo" : esc(part.asset);
        var percent = part.weight_bps ? (part.weight_bps / 100).toFixed(2).replace(".", ",") : "<0,01";
        return '<li><strong>' + label + '</strong><span class="num">' + showSummary(part.value) +
          ' ' + esc(item.currency) + '</span><span class="num">' + esc(percent) + ' %</span>' +
          '<meter aria-hidden="true" min="0" max="10000" value="' + esc(part.weight_bps) + '"></meter></li>';
      }).join("") + '</ul>';
    return '<details class="real-allocation" data-valuation-allocation="' + esc(item.currency) + '"' + (open ? ' open' : '') +
      '><summary data-help="Pesos sobre el patrimonio registrado de esta divisa, incluido el efectivo. Se redondean a 0,01 %.">Distribución · ' +
      esc(item.currency) + '</summary>' + rows + '</details>';
  }
  function csvCell(value, textField) {
    var raw = value == null ? "" : String(value);
    // El CSV puede abrirse en una hoja de cálculo: nunca ejecutar una nota
    // o identificador de texto como fórmula al abrir el archivo.
    if (textField && /^[\s\u0000-\u001f]*[=+\-@]/.test(raw)) raw = "'" + raw;
    return '"' + raw.replace(/"/g, '""') + '"';
  }
  function realEventsCsv(account, events) {
    var fields = ["schema_version", "environment", "account_id", "event_id", "request_id",
      "kind", "asset", "quote_currency", "quantity", "unit_price", "amount",
      "gross_amount", "fee", "fee_asset", "note", "occurred_at", "recorded_at"];
    var textFields = { account_id: true, event_id: true, request_id: true, kind: true,
      asset: true, quote_currency: true, fee_asset: true, note: true };
    var lines = [fields.join(",")];
    var version = events.some(function (item) { return item.kind === "FEE"; }) ? "3" : "2";
    events.forEach(function (item) {
      var row = Object.assign({ schema_version: version, environment: "REAL", account_id: account }, item);
      row.event_id = item.id;
      row.amount = ["CONTRIBUTION", "WITHDRAWAL", "CASH_TRANSFER_IN", "CASH_TRANSFER_OUT"].includes(item.kind) || item.kind === "FEE" && !item.asset ? item.gross_amount : "";
      lines.push(fields.map(function (field) { return csvCell(row[field], !!textFields[field]); }).join(","));
    });
    return "\uFEFF" + lines.join("\r\n") + "\r\n";
  }
  function exportEventsCsv() {
    if (!accountId || !token) return;
    var selectedAccount = accountId;
    var button = root.querySelector("#real-export-csv");
    button.disabled = true;
    request("/accounts/" + encodeURIComponent(selectedAccount) + "/events").then(function (events) {
      if (!token || selectedAccount !== accountId) throw new Error("La cuenta REAL cambió durante la exportación");
      var paired = events.some(function (item) { return ["TRANSFER_IN", "TRANSFER_OUT", "CASH_TRANSFER_IN", "CASH_TRANSFER_OUT"].includes(item.kind); });
      var accepted = events.some(function (item) { return !!item.accounting; });
      var warning = paired ? "Incluye traslados entre cuentas: este CSV individual sirve para consulta, pero NO se puede reimportar como traslado pareado. Usa una copia cifrada para restaurar el libro completo. " : "";
      if (accepted) warning = "CSV original: no incluye el criterio contable aceptado. Usa una copia cifrada para recuperarlo. " + warning;
      if (!window.confirm(warning + "El CSV contendrá el historial REAL sin cifrar. Guárdalo sólo en un lugar privado. ¿Descargarlo?")) return;
      var blob = new Blob([realEventsCsv(selectedAccount, events)], { type: "text/csv;charset=utf-8" });
      var url = URL.createObjectURL(blob);
      var link = document.createElement("a");
      link.href = url;
      link.download = "orbis-real-" + (accepted ? "original-" : "") + selectedAccount.slice(0, 8) + "-" + new Date().toISOString().slice(0, 10) + ".csv";
      document.body.appendChild(link);
      link.click();
      link.remove();
      window.setTimeout(function () { URL.revokeObjectURL(url); }, 1000);
      notice(events.length + " movimiento(s) exportados en CSV sin cifrar.", false);
    }).catch(function (error) { notice(error.message, true); })
      .finally(function () { if (button.isConnected) button.disabled = false; });
  }
  function resetCsvImport() {
    csvImportDraft = null;
    csvImportEpoch += 1;
    if (!root) return;
    var preview = root.querySelector("#real-csv-preview");
    var confirm = root.querySelector("#real-csv-confirm");
    var commit = root.querySelector("#real-csv-commit");
    if (preview) { preview.textContent = ""; preview.hidden = true; }
    if (confirm) confirm.value = "";
    if (commit) commit.disabled = true;
  }
  function previewCsvImport(event) {
    event.preventDefault();
    resetCsvImport();
    var file = root.querySelector("#real-csv-file").files[0];
    if (!file) return;
    if (file.size > 2_000_000) { notice("CSV superior a 2 MB.", true); return; }
    var selectedAccount = accountId, issuedEpoch = csvImportEpoch;
    var button = root.querySelector("#real-csv-check");
    button.disabled = true;
    file.text().then(function (content) {
      if (selectedAccount !== accountId || issuedEpoch !== csvImportEpoch || !token)
        throw new Error("Cuenta o sesión REAL cambiada");
      return request("/accounts/" + encodeURIComponent(selectedAccount) + "/csv-preview",
        { method: "POST", body: JSON.stringify({ csv_text: content }) })
        .then(function (result) { return { content: content, result: result }; });
    }).then(function (checked) {
      if (selectedAccount !== accountId || issuedEpoch !== csvImportEpoch || !token) return;
      csvImportDraft = { account: selectedAccount, content: checked.content, sha256: checked.result.sha256 };
      var preview = root.querySelector("#real-csv-preview");
      preview.textContent = checked.result.total + " movimientos · " + checked.result.new +
        " nuevos · " + checked.result.already_present + " ya presentes. Origen: " +
        checked.result.source_account_id.slice(0, 8) + ".";
      preview.hidden = false;
      root.querySelector("#real-csv-commit").disabled = checked.result.new === 0;
    }).catch(function (error) {
      if (issuedEpoch === csvImportEpoch) notice(error.message, true);
    }).finally(function () { if (button.isConnected) button.disabled = false; });
  }
  function commitCsvImport(event) {
    event.preventDefault();
    if (!csvImportDraft || csvImportDraft.account !== accountId) return;
    var confirmation = root.querySelector("#real-csv-confirm").value;
    if (confirmation !== "IMPORTAR") return;
    var draft = csvImportDraft, button = root.querySelector("#real-csv-commit");
    button.disabled = true;
    request("/accounts/" + encodeURIComponent(draft.account) + "/csv-import", {
      method: "POST", body: JSON.stringify({ csv_text: draft.content, sha256: draft.sha256,
        confirmation: confirmation })
    }).then(function (result) {
      resetCsvImport();
      var file = root.querySelector("#real-csv-file");
      if (file) file.value = "";
      notice(result.new + " movimiento(s) importados · " + result.already_present + " ya presentes.", false);
      return refresh();
    }).catch(function (error) {
      notice(error.message, true);
      if (csvImportDraft === draft && button.isConnected) button.disabled = false;
    });
  }
  function request(path, options, passive) {
    checkIdle();
    var issuedToken = token;
    var issuedEpoch = sessionEpoch;
    var startedAt = Date.now();
    var opts = options || {};
    opts.cache = "no-store";
    opts.credentials = "same-origin";
    opts.referrerPolicy = "no-referrer";
    opts.headers = Object.assign({ "Content-Type": "application/json" }, issuedToken ? { "X-Orbis-Vault-Token": issuedToken } : {},
      passive ? { "X-Orbis-Passive": "1" } : {}, opts.headers || {});
    var updatesMexcRoute = opts.method === "POST" && (
      /^\/exchanges\/mexc\/(?:connect|disconnect|trade\/(?:connect|disconnect))$/.test(path) ||
      /^\/accounts\/[^/]+\/(?:mexc-link(?:\/renew-view)?|mexc-stop|mexc-withdrawals(?:\/.*)?|mexc-policy-drafts\/[^/]+\/(?:activate|resume))$/.test(path) ||
      /^\/accounts\/[^/]+\/mexc-trading-sessions\/[^/]+\/orders$/.test(path) ||
      /^\/accounts\/[^/]+\/wallet-funding\/[^/]+\/(?:claim|hash|rejected|discover|check|check-bnb|book)$/.test(path) ||
      /^\/execution\/mexc\/intents(?:\/|$)/.test(path));
    var pending = fetch("/real" + path, opts).then(function (response) {
      return response.json().then(function (data) {
        if (issuedToken) checkIdle();
        if (issuedToken && (issuedEpoch !== sessionEpoch || token !== issuedToken)) throw new Error("Sesión REAL bloqueada");
        if (response.status === 401 && issuedToken) {
          forgetSession();
          showLocked();
        }
        if (!response.ok) {
          var error = new Error(data.detail || "No se pudo completar la operación");
          error.status = response.status;
          throw error;
        }
        if (issuedToken && !passive && path !== "/vault/lock") armIdle(startedAt);
        return data;
      });
    });
    return updatesMexcRoute ? pending.finally(function () {
      if (issuedToken && issuedEpoch === sessionEpoch && token === issuedToken) loadMexcRoute();
    }) : pending;
  }
  function notice(message, error) {
    var target = root && root.querySelector("#real-notice");
    if (!target) return;
    target.textContent = message;
    target.className = "state " + (error ? "error" : "ok");
    target.hidden = false;
  }
  function supervisorGlobalBadge() {
    var header = document.querySelector(".controles-globales");
    if (!header) return null;
    var badge = document.getElementById("real-mexc-supervisor-global");
    if (!badge) {
      badge = document.createElement("a");
      badge.id = "real-mexc-supervisor-global";
      badge.className = "real-supervisor-global";
      badge.href = "#/real";
      badge.hidden = true;
      badge.setAttribute("data-help", "Estado de la vigilancia local MEXC. Sólo funciona con ORBIS abierto y la bóveda desbloqueada; no es una orden protectora en el exchange.");
      header.appendChild(badge);
    }
    return badge;
  }
  function paintMexcSupervisor(data, unavailable) {
    mexcSupervisorSnapshot = { data: data, unavailable: unavailable };
    var running = !!(data && data.running);
    var monitoring = !!(data && data.monitoring_active);
    var recovery = !!(data && data.recovery_required);
    var stalled = !!(data && data.stalled);
    if (running) mexcSupervisorEverRunning = true;
    var stopped = data && data.status !== "NOT_STARTED" && !running;
    var failed = unavailable || !!(data && (
      data.status === "ERROR_NEEDS_REVIEW" || data.status === "STALLED_NEEDS_REVIEW" ||
      (running && !monitoring && !data.stop_requested && !stalled)));
    var mode = failed || recovery || stalled ? "error" : monitoring ? "active" :
      running || stopped ? "warning" : "idle";
    var label = unavailable ? "ESTADO NO VERIFICABLE" : recovery ? "PARADA SIN VIGILANCIA" :
      stalled ? "VIGILANCIA ESTANCADA" :
      failed ? "VIGILANCIA CAÍDA" :
      monitoring ? "VIGILANCIA LOCAL ACTIVA" :
      running ? (data.stop_requested ? "DETENIENDO VIGILANCIA" : "VIGILANCIA NO ACTIVA") :
      stopped ? "VIGILANCIA FINALIZADA" : "SIN VIGILANCIA";
    var detail = unavailable ? "No se puede comprobar el estado local; revisa MEXC." :
      recovery ? data.unmonitored_stopped_intents + " intento(s) inciertos en " +
        data.unmonitored_stopped_accounts + " cuenta(s); revisa MEXC. No se confirma cancelación." :
      stalled ? "El ciclo no progresa; revisa MEXC. No se confirma cancelación." :
      failed ? "Revisa la orden en MEXC; ORBIS no confirma una cancelación." :
      running ? "ORBIS abierto + bóveda desbloqueada · " + (data.cycles || 0) + " ciclos" :
      "La parada local no cancela órdenes en MEXC.";
    var panel = root && root.querySelector("#real-mexc-supervisor");
    if (panel) {
      panel.dataset.state = mode;
      panel.querySelector("strong").textContent = label;
      panel.querySelector("small").textContent = detail;
    }
    var stopControl = root && root.querySelector("#real-mexc-supervisor-stop");
    if (stopControl) stopControl.hidden = !running || !!(data && data.stop_requested);
    if (root) root.querySelectorAll(".real-exec-supervise").forEach(function (control) {
      control.hidden = running;
    });
    var badge = supervisorGlobalBadge();
    if (badge) {
      badge.hidden = mode === "idle" && !mexcSupervisorEverRunning;
      badge.dataset.state = mode;
      badge.textContent = "MEXC · " + label;
      badge.setAttribute("aria-label", "Parada MEXC: " + label + ". Abrir Cartera real.");
    }
    if (typeof window.showToast === "function") {
      if (recovery && !lastMexcRecoveryRequired) {
        window.showToast("error", "Parada MEXC sin vigilancia: hay órdenes inciertas. Revisa el exchange.");
      } else if (stalled && !lastMexcSupervisorStalled && !recovery) {
        window.showToast("error", "La vigilancia MEXC está estancada. Revisa el exchange.");
      } else if (lastMexcSupervisorRunning === true && (!running || unavailable) &&
          !mexcSupervisorUnreachable && !recovery && !lastMexcSupervisorStalled) {
        window.showToast("error", unavailable ?
          "No se puede comprobar la vigilancia MEXC. Revisa las órdenes en el exchange." :
          "La vigilancia MEXC ha terminado. Revisa las órdenes en el exchange.");
      }
    }
    if (!unavailable) {
      lastMexcSupervisorRunning = running;
      lastMexcRecoveryRequired = recovery;
      lastMexcSupervisorStalled = stalled;
    }
    mexcSupervisorUnreachable = !!unavailable;
  }
  function pollMexcSupervisor(epoch) {
    if (!token || epoch !== mexcSupervisorEpoch) return;
    request("/execution/mexc/stop-supervisor/status", null, true).then(function (data) {
      if (epoch === mexcSupervisorEpoch && token) paintMexcSupervisor(data, false);
    }).catch(function () {
      if (epoch === mexcSupervisorEpoch && token) paintMexcSupervisor(null, true);
    }).finally(function () {
      if (epoch !== mexcSupervisorEpoch || !token) return;
      mexcSupervisorTimer = window.setTimeout(function () { pollMexcSupervisor(epoch); },
        lastMexcSupervisorRunning ? 5000 : 10000);
    });
  }
  function startMexcSupervisorPoll() {
    if (mexcSupervisorActive || !token) return;
    mexcSupervisorActive = true;
    pollMexcSupervisor(++mexcSupervisorEpoch);
  }
  function heading(state) {
    return '<div class="pagina-cabecera"><div><p class="real-eyebrow">PATRIMONIO · REGISTRO LOCAL</p>' +
      '<h2 data-help="Libro patrimonial cifrado. Observar no autoriza operaciones: órdenes, bots y retiradas requieren sus permisos y confirmaciones separados.">Patrimonio</h2></div>' +
      '<span class="statusbadge ' + (state ? "on" : "off") + '">' + (state ? "DESBLOQUEADA" : "BLOQUEADA") + '</span></div>';
  }
  function shell(body, unlocked) {
    clearPortfolioHistory();
    if (perpLiveReview) { perpLiveReview.dispose(); perpLiveReview = null; }
    root.innerHTML = '<section class="card real-page">' + heading(unlocked) + body + '<p id="real-notice" class="state" role="status" hidden></p></section>';
  }
  function signIn(exists,operating) {
    shell('<div class="real-lock-grid"><div class="real-lock-mark" aria-hidden="true">◈</div><div>' +
      '<h3>' + (exists ? (operating && operating.active ? "Desproteger vista" : "Desbloquear bóveda") : "Crear bóveda privada") + '</h3>' +
      (pendingMexcSource ? '<p class="muted">Prueba PAPER seleccionada para MEXC.</p>' : '') +
      (pendingGrvtSource ? '<p class="muted">Prueba PAPER seleccionada para GRVT.</p>' : '') +
      (pendingDexSource ? '<p class="muted">Prueba PAPER seleccionada para Uniswap v3.</p>' : '') +
      '<p class="muted">' + (exists ? "Contraseña sólo en memoria." :
        "Guarda la contraseña: no hay recuperación automática.") + '</p>' +
      '<form id="real-auth-form" class="row"><label class="field">Contraseña principal' +
      '<input id="real-passphrase" type="password" minlength="12" required autocomplete="current-password"></label>' +
      (exists ? "" : '<label class="field">Confirmar contraseña<input id="real-passphrase-confirm" type="password" minlength="12" required autocomplete="new-password"></label>') +
      '<button type="submit">' + (exists ? "Desbloquear" : "Crear bóveda") + '</button></form></div></div>', false);
    if (operating && operating.active) {
      root.querySelector(".pagina-cabecera .statusbadge").textContent = "VISTA PROTEGIDA";
      var controls = document.createElement("div"); controls.className = "state";
      controls.innerHTML = '<span data-operational-status></span><button type="button" class="ghost" data-operational-stop>Detener y bloquear</button>';
      controls.querySelector("[data-operational-status]").textContent = "Operativa activa · " + operating.count + " · hasta " + new Date(operating.until).toLocaleString();
      controls.dataset.help = "El bot mantiene su clave de bóveda y credenciales sólo en memoria. La vista no permite acceder a importes sin contraseña. Detener no vende posiciones ni cancela pedidos ya enviados.";
      root.querySelector(".real-page").append(controls);
      controls.querySelector("button").addEventListener("click",function () {
        if (!confirm("¿Detener toda la operativa y cerrar la bóveda? No vende posiciones ni cancela órdenes pendientes.")) return;
        this.disabled = true;
        request("/operations/stop",{method:"POST",body:JSON.stringify({confirmation:"DETENER_Y_BLOQUEAR"})}).then(function (result) {
          afterRestoreMessage = result.review_required ?
            {text:"Operativa detenida · bóveda bloqueada; el archivo del bot requiere revisión antes de operar.",error:true} :
            {text:"Operativa detenida · bóveda bloqueada.",error:false};
          forgetSession(); showLocked();
        }).catch(function () { notice("Parada sin confirmar · revisa ORBIS antes de operar.",true); controls.querySelector("button").disabled = false; });
      });
      var protectedRoot = root;
      function pollProtected() {
        protectedOperationTimer = setTimeout(function () {
          if (token || root !== protectedRoot || !controls.isConnected) return;
          request("/vault/status",null,true).then(function (state) {
            if (token || root !== protectedRoot || !controls.isConnected) return;
            if (state.review_required && !afterRestoreMessage) afterRestoreMessage = {text:"El archivo del bot requiere revisión antes de operar.",error:true};
            if (!state.operating || !state.operating.active) { signIn(state.exists); return; }
            controls.querySelector("[data-operational-status]").textContent = "Operativa activa · " + state.operating.count + " · hasta " + new Date(state.operating.until).toLocaleString();
          }).catch(function () { if (controls.isConnected) controls.querySelector("[data-operational-status]").textContent = "Estado operativo sin confirmar"; })
            .finally(function () { if (!token && controls.isConnected) pollProtected(); });
        },5000);
      }
      pollProtected();
    }
    if (afterRestoreMessage) {
      notice(afterRestoreMessage.text, afterRestoreMessage.error);
      afterRestoreMessage = null;
    }
    root.querySelector("#real-auth-form").addEventListener("submit", function (event) {
      event.preventDefault();
      var form = event.currentTarget;
      var pass = root.querySelector("#real-passphrase");
      var password = pass.value;
      pass.value = "";
      if (!exists) {
        var confirmation = root.querySelector("#real-passphrase-confirm");
        var matches = confirmation.value === password;
        confirmation.value = "";
        if (!matches) { notice("Las contraseñas no coinciden.", true); return; }
      }
      var button = form.querySelector("button");
      button.disabled = true;
      var authStartedAt = Date.now();
      request(exists ? "/vault/unlock" : "/vault/create", {
        method: "POST", body: JSON.stringify({ passphrase: password })
      }).then(function (data) {
        token = data.token;
        sessionEpoch += 1;
        idleDeadline = 0;
        armIdle(authStartedAt);
        if (data.migrated_backup) afterUnlockMessage = "Bóveda actualizada. Copia cifrada anterior: " + data.migrated_backup;
        load();
      }).catch(function (error) {
        notice(error.message, true);
        button.disabled = false;
      });
    });
  }
  function grvtTestnetRecoveryPanel() {
    return '<section class="real-panel"><details id="real-grvt-mainnet"></details></section>' +
      '<section class="real-panel real-grvt-testnet"><div class="real-mexc-head"><h3 data-help="Consulta el diario cifrado. Auditar y cancelar usan sólo GRVT testnet; nunca registran fills como patrimonio real.">Recuperación GRVT</h3>' +
      '<span class="statusbadge off">TESTNET · REVISIÓN</span></div><details id="real-grvt-bot"></details><details id="real-grvt-bracket"></details><details id="real-grvt-testnet-send"></details><details id="real-grvt-oco"></details>' +
      '<form id="real-grvt-testnet-recovery-form" class="row"><label class="field" data-help="Elige una subcuenta testnet ya guardada o introduce su ID. Las sugerencias proceden sólo de la bóveda local.">Subcuenta testnet<input name="sub_account_id" list="real-grvt-testnet-subaccounts" inputmode="numeric" pattern="[0-9]+" maxlength="30" autocomplete="off" required></label>' +
      '<datalist id="real-grvt-testnet-subaccounts"></datalist>' +
      '<button type="submit" class="ghost">Consultar registro</button></form>' +
      '<div id="real-grvt-testnet-recovery-result" class="state muted" role="status">Introduce la subcuenta de prueba.</div>' +
      '<details id="real-grvt-testnet-actions"><summary data-help="La clave Trade testnet se usa sólo para la petición elegida, se borra del formulario y no se guarda. La clave View mainnet no sirve aquí.">Auditoría y parada testnet</summary>' +
      '<div class="row"><label class="field" data-help="Clave exclusiva de GRVT testnet. No pegues la clave View de mainnet ni una clave privada de wallet.">API key testnet<input id="real-grvt-testnet-key" type="password" autocomplete="off" spellcheck="false" maxlength="256"></label>' +
      '<button id="real-grvt-testnet-stop" type="button" class="ghost" data-help="Detiene nuevos envíos en esta subcuenta local. No cancela órdenes externas.">Parar subcuenta</button></div>' +
      '<div id="real-grvt-testnet-action-result" class="state muted" role="status" hidden></div></details></section>';
  }
  function bindGrvtTestnetRecovery() {
    var form = root.querySelector("#real-grvt-testnet-recovery-form");
    form.elements.sub_account_id.addEventListener('input',function(){this.dataset.edited='1';});
    if (window.OrbisGrvtMainnet) window.OrbisGrvtMainnet.mount(root.querySelector("#real-grvt-mainnet"), {
      request:request,canSend:function () { return !!token && !hiddenAmounts; }
    });
    if (pendingGrvtSource && window.OrbisGrvtMainnet && window.OrbisGrvtMainnet.prepare(pendingGrvtSource))
      pendingGrvtSource = null;
    if (window.OrbisGrvtBot) window.OrbisGrvtBot.mount(root.querySelector("#real-grvt-bot"), {
      request:request,canSend:function () { return !!token && !hiddenAmounts; }
    });
    if (window.OrbisGrvtBracket) window.OrbisGrvtBracket.mount(root.querySelector("#real-grvt-bracket"), {
      request:request,canSend:function () { return !!token && !hiddenAmounts; }
    });
    if (window.OrbisGrvtOco) window.OrbisGrvtOco.mount(root.querySelector("#real-grvt-oco"), {
      request:request,canSend:function () { return !!token && !hiddenAmounts; }
    });
    if (window.OrbisGrvtTestnet) window.OrbisGrvtTestnet.mount(root.querySelector("#real-grvt-testnet-send"), {
      request:request, canSend:function () { return !!token && !hiddenAmounts; },
      onAttempt:function (subaccount) {
        if (!root || !root.isConnected || !token) return;
        var recovery = root.querySelector("#real-grvt-testnet-recovery-form");
        recovery.elements.sub_account_id.value = subaccount;
        loadGrvtTestnetRecovery({preventDefault:function () {}, currentTarget:recovery});
      }
    });
    form.addEventListener("submit", loadGrvtTestnetRecovery);
    root.querySelector("#real-grvt-testnet-stop").addEventListener("click", stopGrvtTestnetSubaccount);
    root.querySelector("#real-grvt-testnet-recovery-result").addEventListener("click", function (event) {
      var button = event.target.closest("button[data-grvt-testnet-close], button[data-grvt-testnet-audit], button[data-grvt-testnet-cancel], button[data-grvt-testnet-reconcile]");
      if (!button || !this.contains(button)) return;
      if (button.hasAttribute("data-grvt-testnet-reconcile")) reconcileGrvtTestnet(button);
      else if (button.hasAttribute("data-grvt-testnet-close")) closeGrvtTestnetRejected(button);
      else if (button.hasAttribute("data-grvt-testnet-audit")) reviewGrvtTestnetOrder(button, false);
      else reviewGrvtTestnetOrder(button, true);
    });
    if (lastGrvtTestnetRecovery) {
      form.elements.sub_account_id.value = lastGrvtTestnetRecovery.sub_account_id;
      renderGrvtTestnetRecovery(lastGrvtTestnetRecovery,
        root.querySelector("#real-grvt-testnet-recovery-result"));
    }
    request("/exchanges/grvt/testnet/subaccounts").then(function (data) {
      if (data.network !== "TESTNET" || data.can_submit !== false) return;
      var list = root && root.querySelector("#real-grvt-testnet-subaccounts");
      if (!list) return;
      list.innerHTML = (data.sub_account_ids || []).map(function (id) {
        return '<option value="' + esc(id) + '"></option>';
      }).join("");
      var currentForm = root.querySelector("#real-grvt-testnet-recovery-form");
      var input=currentForm&&currentForm.elements.sub_account_id;
      if (input && !input.value && input.dataset.edited!=='1' && document.activeElement!==input && data.sub_account_ids.length === 1)
        input.value = data.sub_account_ids[0];
      if (window.OrbisGrvtOco && data.sub_account_ids.length === 1)
        window.OrbisGrvtOco.setSubaccount(data.sub_account_ids[0]);
      if (window.OrbisGrvtBracket && data.sub_account_ids.length === 1)
        window.OrbisGrvtBracket.setSubaccount(data.sub_account_ids[0]);
      if (window.OrbisGrvtBot && data.sub_account_ids.length === 1)
        window.OrbisGrvtBot.setSubaccount(data.sub_account_ids[0]);
    }).catch(function () { /* El ID manual sigue disponible sin sugerencias. */ });
  }
  function saveAccountTransfer(event) {
    event.preventDefault();
    var form = event.currentTarget, fields = new FormData(form);
    var sourceAccount = accountId;
    var targetAccount = String(fields.get("target_account_id") || "");
    if (!sourceAccount || !targetAccount || sourceAccount === targetAccount) return;
    if (hiddenAmounts) { notice("Muestra los importes antes de confirmar un traslado.", true); return; }
    var button = form.querySelector('button[type="submit"]');
    var requestId = form.dataset.requestId || uuid();
    form.dataset.requestId = requestId;
    var cashMode = fields.get("mode") === "cash";
    var body = {
      request_id: requestId, target_account_id: targetAccount,
      occurred_at: new Date(String(fields.get("occurred_at"))).toISOString(),
      note: String(fields.get("note") || "").trim() || null
    };
    if (cashMode) {
      body.currency = String(fields.get("currency") || "").trim().toUpperCase();
      body.amount = String(fields.get("amount") || "").trim();
    } else {
      body.asset = String(fields.get("asset") || "").trim().toUpperCase();
      body.quote_currency = String(fields.get("quote_currency") || "").trim().toUpperCase();
      body.quantity = String(fields.get("quantity") || "").trim();
      body.fee = String(fields.get("fee") || "0").trim();
    }
    var controls = Array.from(form.querySelectorAll("input, select, button"));
    var previouslyDisabled = controls.map(function (control) { return control.disabled; });
    controls.forEach(function (control) { control.disabled = true; });
    var path = "/accounts/" + encodeURIComponent(sourceAccount) +
      (cashMode ? "/cash-transfers" : "/transfers");
    request(path + "/preview", {method: "POST", body: JSON.stringify(body)}).then(function (preview) {
      if (sourceAccount !== accountId || !form.isConnected) return null;
      if (preview.status === "ALREADY_RECORDED") return preview;
      var targetLabel = form.elements.target_account_id.selectedOptions[0].textContent;
      var message = cashMode ?
        "Registrar " + body.amount + " " + body.currency + " hacia " + targetLabel +
        "\nDisponible en origen antes: " + preview.available_at_time + " " + body.currency +
        "\nQuedaría: " + preview.remaining_at_time + " " + body.currency +
        "\n\nSólo libro local. ¿Confirmar un movimiento externo ya realizado?" :
        "Registrar " + body.quantity + " " + body.asset + " hacia " + targetLabel +
        "\nComisión de red: " + body.fee + " " + body.asset +
        " · recibe: " + preview.received_quantity + " " + body.asset +
        "\nCoste trasladado: " + preview.cost_basis + " " + body.quote_currency +
        "\nDisponible en origen antes: " + preview.available_at_time + " " + body.asset +
        "\n\nSólo libro local. ¿Confirmar un movimiento externo ya realizado?";
      if (!window.confirm(message)) return null;
      return request(path, {method: "POST", body: JSON.stringify(Object.assign({}, body, {
        [cashMode ? "expected_source_balance" : "expected_cost_basis"]:
          cashMode ? preview.available_at_time : preview.cost_basis
      }))});
    }).then(function (result) {
      if (!result) return;
      if (sourceAccount !== accountId) return;
      delete form.dataset.requestId;
      if (cashMode) form.elements.amount.value = "";
      else { form.elements.quantity.value = ""; form.elements.fee.value = "0"; }
      form.elements.note.value = "";
      clearMexcComparison();
      notice(result.status === "ALREADY_RECORDED" ? "Traslado ya registrado." :
        "Traslado registrado en ambas cuentas. Ningún fondo se movió desde Orbis.", false);
      return refresh();
    }).catch(function (error) { notice(error.message, true); })
      .finally(function () {
        if (button.isConnected) controls.forEach(function (control, index) {
          control.disabled = previouslyDisabled[index];
        });
      });
  }
  function updateAccountTransferMode(form) {
    var cash = form.elements.mode.value === "cash";
    form.querySelectorAll("[data-transfer-mode]").forEach(function (label) {
      var active = label.dataset.transferMode === (cash ? "cash" : "asset");
      label.hidden = !active;
      label.querySelector("input").disabled = !active;
    });
  }
  function transferTargetOptions(accounts, sourceId) {
    return accounts.filter(function (account) { return account.id !== sourceId; })
      .map(function (account) { return '<option value="' + esc(account.id) + '">' + esc(account.name) + '</option>'; })
      .join("");
  }
  function openRealControl(id) {
    var destination = root.querySelector("#" + id);
    if (!destination || destination.hidden) return;
    var disclosure = destination.matches("details") ? destination : destination.querySelector(":scope > details");
    var focusTarget = disclosure ? disclosure.querySelector("summary") :
      destination.querySelector("#real-wallet-bnb-chain:not([hidden]):enabled") ||
      destination.querySelector("#real-wallet-proof:not([hidden]):enabled") ||
      destination.querySelector("#real-wallet-provider");
    if (disclosure) disclosure.open = true;
    if (focusTarget) focusTarget.focus({preventScroll:true});
    (disclosure || destination).scrollIntoView({block:"start"});
  }
  function openMexcPendingOrders(intentId) {
    var journal = root.querySelector("#real-mexc-execution");
    journal.dataset.pendingOnly = "1";
    if (intentId) journal.dataset.focusIntent = intentId;
    else delete journal.dataset.focusIntent;
    if (journal.open) loadMexcExecution();
    openRealControl(journal.id);
  }
  function openMexcTransfer(target) {
    var state = mexcRouteState && mexcRouteState.account === accountId ? mexcRouteState.data : {};
    var funding = target === "real-wallet-funding";
    var panel = funding ? window.OrbisWalletFunding : window.OrbisMexcWithdrawal;
    var id = funding ? state.funding_id : state.withdrawal_id;
    if (!panel || !panel.focusHistory(id)) openRealControl(target);
  }
  function openMexcJourneyStep(step) {
    var target = {SPOT:"real-mexc-trading", WITHDRAWAL:"real-wallet-withdrawal"}[step];
    var button = target && root.querySelector("#real-mexc-route [data-route-target='" + target + "']");
    if (button && !button.closest("[hidden]")) button.click();
  }
  function openMexcBotAccountAction(action) {
    var destination = {view:"real-mexc-connect",link:"real-mexc-link",opening:"real-mexc-opening",
      withdrawal:"real-wallet-withdrawal",spot:"real-mexc-trading",trade:"real-mexc-trade-verify"}[action];
    if (action === "account") root.querySelector("#real-account").focus();
    else if (action === "orders") openMexcPendingOrders();
    else if (action === "withdrawal") openMexcTransfer(destination);
    else if (destination) openRealControl(destination);
  }
  function validPaperSession(id) {
    return typeof id === "string" && /^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$/.test(id);
  }
  function prepareMexcSource(kind, id) {
    if (!["STRATEGY", "DCA"].includes(kind) || !validPaperSession(id)) return;
    checkIdle();
    pendingMexcSource = {kind:kind, id:id};
    window.location.hash = "#/real";
  }
  function prepareGrvtSource(kind, id) {
    if (!["LIMIT", "MARKET"].includes(kind) || !validPaperSession(id)) return;
    checkIdle();
    pendingGrvtSource = {kind:kind, id:id};
    window.location.hash = "#/real";
  }
  function prepareDexSource(kind, id) {
    if (!["STRATEGY", "DCA"].includes(kind) || !validPaperSession(id)) return;
    checkIdle();
    pendingDexSource = {kind:kind, id:id};
    window.location.hash = "#/real";
  }
  function prepareMexcLimits(source) {
    var pair = /^([A-Z0-9]{2,16})\/(USDT)$/.exec(source.symbol);
    if (!pair || !accountId) return;
    mexcLimitsFromSource = {account:accountId, version:source.version_id, base:pair[1], quote:pair[2]};
    var details = root.querySelector("#real-mexc-policy-drafts"), wasOpen = details.open;
    openRealControl("real-mexc-policy-drafts");
    if (wasOpen) loadMexcPolicyDrafts();
  }
  function paintMexcRoute() {
    var route = root && root.querySelector("#real-mexc-route");
    if (!route || route.hidden) return;
    var state = mexcRouteState && mexcRouteState.account === accountId ? mexcRouteState.data : {};
    var wallet = window.OrbisWallet ? window.OrbisWallet.mexcReadiness() : {};
    var view = {
      DISCONNECTED: "Conecta View",
      UNLINKED: "Vincula la cuenta",
      LINKED: state.view_renewed ? "Vínculo renovado" : "Vínculo local",
      OTHER_ACCOUNT: "Elige la cuenta vinculada",
      DIFFERENT_CONNECTION: "Usa la clave vinculada",
      UNAVAILABLE: "Estado sin comprobar"
    }[state.view] || "Comprobando…";
    var walletStatus = !wallet.connected ? "Conecta la wallet" : wallet.changingNetwork ? "Confirma en la wallet" :
      !wallet.onBnb ? "Selecciona BNB Chain" : !wallet.verified ? "Verifica el control" : "Control verificado";
    var walletVerified = wallet.connected && wallet.onBnb && wallet.verified;
    var needsUsdt = state.view === "LINKED" && walletVerified && !wallet.usdtObserved;
    var fundsStatus = state.view !== "LINKED" ? view : !walletVerified ? walletStatus :
      needsUsdt ? "Consulta USDT" : null;
    var tradeStatus = state.view !== "LINKED" ? view : state.stopped ? "STOP activo" :
      state.bot_active ? "Cuenta en uso por un bot" : !state.trade_verified ? "Verifica Trade" : "Revisa saldo y límites";
    var botStatus = state.view !== "LINKED" ? view : state.stopped ? "STOP activo" : state.bot_active ?
      "Bot activo" : !state.trade_verified ? "Verifica Trade" : "Revisa estrategia y límites";
    var spotAction = {
      SYNCHRONIZE: state.view === "LINKED" ? "Sincroniza la orden" : "Orden por sincronizar",
      REVIEW_RESERVATION: "Revisa la reserva"
    }[state.spot_order_action];
    var fundingAction = {SEARCH:"Busca en BNB",CHECK:"Comprueba el abono",BOOK:"Contabiliza el ingreso"}[state.funding_action];
    var withdrawalAction = {REVIEW_PLAN:"Revisa la retirada",CHECK:"Comprueba la recepción",
      REVIEW_REJECTION:"Revisa el rechazo",BOOK:"Contabiliza la retirada"}[state.withdrawal_action];
    var withdrawStatus = withdrawalAction || (state.view === "LINKED" && state.withdrawal_pending ? "Revisa la retirada" :
      fundsStatus || (!state.stopped ? "Detén la operativa" :
        state.pending_orders || state.bot_active ? "Revisa órdenes o bots pendientes" : "Revisa destino y saldo"));
    function set(id, label, observed) {
      var item = route.querySelector("#" + id);
      if (!item) return;
      if (item.textContent !== label) item.textContent = label;
      item.classList.toggle("observed", !!observed);
    }
    set("real-route-view-status", view, state.view === "LINKED");
    set("real-route-status-1", walletStatus, walletVerified);
    set("real-route-status-2", fundingAction || fundsStatus || "Revisa importe y destino");
    set("real-route-status-3", spotAction || tradeStatus);
    set("real-route-status-4", spotAction || botStatus, !spotAction && state.bot_active && !state.stopped);
    ["real-mexc-trading","real-mexc-bots"].forEach(function (id) {
      var step = route.querySelector("[data-route-target='" + id + "']");
      if (step) step.setAttribute("aria-controls", spotAction ? "real-mexc-execution" : id);
    });
    set("real-route-status-5", withdrawStatus);
    var viewControl = route.querySelector("[data-route-view]");
    var connected = !state.view || state.view === "UNAVAILABLE" ? mexcViewConnected : state.view !== "DISCONNECTED";
    viewControl.setAttribute("aria-controls", connected ? "real-mexc-link" : "real-mexc-connect");
    var funding = route.querySelector("[data-route-target='real-wallet-funding']");
    if (funding) funding.setAttribute("aria-controls",
      needsUsdt && !fundingAction ? "real-wallet-token-panel" : "real-wallet-funding");
    var opening = root.querySelector("[data-trading-opening]");
    if (opening) opening.hidden = hiddenAmounts || !mexcViewConnected || !state.book_empty ||
      (state.view !== "LINKED" && state.view !== "UNLINKED");
  }
  function loadMexcRoute() {
    var route = root && root.querySelector("#real-mexc-route");
    if (!route || route.hidden || !accountId || !token) return;
    var account = accountId, epoch = ++mexcRouteEpoch;
    function update(data) {
      if (!route.isConnected || account !== accountId || epoch !== mexcRouteEpoch) return;
      mexcRouteState = {account:account, data:data};
      paintMexcRoute();
    }
    paintMexcRoute();
    request("/accounts/" + encodeURIComponent(account) + "/mexc-workflow", null, true)
      .then(update, function () { update({view:"UNAVAILABLE"}); });
  }
  function mountMexcRoute() {
    var route = root.querySelector("#real-mexc-route");
    var steps = [
      ["Wallet", "BNB Chain", "real-wallet-root", "#real-wallet-provider"],
      ["Ingresar", "USDT → MEXC", "real-wallet-funding", "summary"],
      ["Orden Spot", "Compra / venta", "real-mexc-trading", "summary"],
      ["Bot", "LAB / PAPER", "real-mexc-bots", "summary"],
      ["Retirar", "USDT → wallet", "real-wallet-withdrawal", "summary"]
    ];
    var controls = steps.map(function (step, index) {
      var destination = root.querySelector("#" + step[2]);
      if (!destination || !destination.querySelector(step[3])) return "";
      var id = index + 1, label = "real-route-label-" + id, detail = "real-route-detail-" + id;
      var status = "real-route-status-" + id;
      return '<button type="button" class="real-route-step" data-route-target="' + step[2] +
        '" aria-labelledby="' + label + ' ' + detail + '" aria-describedby="' + status +
        '" aria-controls="' + step[2] + '"><span aria-hidden="true">0' + id +
        '</span><strong id="' + label + '">' + step[0] + '</strong><small id="' + detail + '">' + step[1] +
        '</small><small class="real-route-status" id="' + status + '"></small></button>';
    }).join("");
    if (!controls) return;
    route.innerHTML = '<div class="real-route-heading"><h3 id="real-mexc-route-title">Wallet ↔ MEXC</h3>' +
      '<div class="real-route-view"><small id="real-route-view-status" class="real-route-status"></small>' +
      '<button type="button" class="ghost" data-route-view aria-describedby="real-route-view-status" ' +
      'aria-controls="real-mexc-connect">Clave View</button>' +
      '<button type="button" class="ghost" data-route-target="real-mexc-direct-journeys" ' +
      'aria-controls="real-mexc-direct-journeys">Ingresos directos</button></div></div><div class="real-route-steps">' + controls + '</div>';
    route.hidden = false;
    route.addEventListener("click", function (event) {
      var control = event.target.closest("[data-route-target], [data-route-view]");
      if (!control) return;
      var target = control.getAttribute("aria-controls");
      if (target === "real-mexc-execution") openMexcPendingOrders();
      else if (target === "real-wallet-funding" || target === "real-wallet-withdrawal") openMexcTransfer(target);
      else openRealControl(target);
    });
  }
  function renderAccountPicker(accounts) {
    var selected = accounts.find(function (item) { return item.id === accountId; });
    var preparedSource = pendingMexcSource || (window.OrbisMexcBots && window.OrbisMexcBots.preparedSource());
    var options = accounts.map(function (account) {
      return '<option value="' + esc(account.id) + '">' + esc(account.name) + ' · ' + esc(account.base_currency) + '</option>';
    }).join("");
    shell('<div class="real-toolbar"><label class="field">Cuenta<select id="real-account">' + options + '</select></label>' +
      '<button id="real-hide" class="ghost" type="button" data-help="Oculta o muestra importes sólo en pantalla; no cambia el libro.">' + (hiddenAmounts ? "Mostrar importes" : "Ocultar importes") + '</button>' +
      '<button id="real-backup" class="ghost" type="button" data-help="Crea una copia local cifrada y verifica que puede abrirse.">Copia cifrada</button>' +
      '<button id="real-export-csv" class="ghost" type="button" data-help="Descarga los movimientos de esta cuenta como CSV local sin cifrar; requiere confirmación.">Exportar CSV</button>' +
      '<button id="real-protect" class="ghost" type="button" data-help="Protege la pantalla. Sólo bots con permiso Operativo siguen hasta su caducidad; su clave y credenciales permanecen en memoria. Sin esos permisos también cierra la bóveda.">Proteger vista</button>' +
      '<button id="real-lock" class="ghost" type="button" data-help="Detiene todos los bots y cierra la bóveda, incluidos los permisos operativos. No vende posiciones ni cancela órdenes pendientes.">Bloquear</button></div>' +
      '<nav id="real-mexc-route" class="real-mexc-route" aria-labelledby="real-mexc-route-title" hidden></nav>' +
      '<details id="real-add-account" class="real-recovery"><summary data-help="Crea otro libro REAL independiente, con alias local y divisa base. No conecta una wallet ni un exchange.">Nueva cuenta REAL</summary>' +
      '<form id="real-add-account-form" class="real-recovery-form"><label class="field">Alias<input name="name" maxlength="100" placeholder="Cartera 2" autocomplete="off" required></label>' +
      '<label class="field">Divisa base<input name="base_currency" maxlength="12" value="' + esc(selected.base_currency) + '" autocomplete="off" required></label>' +
      '<button type="submit" class="ghost">Crear cuenta</button></form></details>' +
      (accounts.length > 1 ? '<details id="real-paired-transfer" class="real-recovery"><summary data-help="Asiento contable local entre cuentas. Activos conservan coste; efectivo no genera aportación ni beneficio. No mueve fondos. Divisas distintas requieren asientos separados.">Traslado entre cuentas</summary>' +
      '<form id="real-paired-transfer-form" class="real-recovery-form">' +
      '<label class="field">Tipo<select name="mode"><option value="asset">Activo</option><option value="cash">Efectivo</option></select></label>' +
      '<label class="field">Destino<select name="target_account_id" required>' + transferTargetOptions(accounts, accountId) + '</select></label>' +
      '<label class="field" data-transfer-mode="asset">Activo<input name="asset" value="BTC" maxlength="12" required autocomplete="off"></label>' +
      '<label class="field" data-transfer-mode="asset">Divisa de coste<input name="quote_currency" value="' + esc(selected.base_currency) + '" maxlength="12" required autocomplete="off"></label>' +
      '<label class="field" data-transfer-mode="asset">Cantidad<input name="quantity" type="text" inputmode="decimal" required placeholder="0.025"></label>' +
      '<label class="field" data-transfer-mode="asset">Comisión en activo<input name="fee" type="text" inputmode="decimal" value="0" required data-help="Se descuenta de la cantidad enviada. El destino recibe el neto y conserva todo el coste de origen."></label>' +
      '<label class="field" data-transfer-mode="cash" hidden>Divisa<input name="currency" value="' + esc(selected.base_currency) + '" maxlength="12" required autocomplete="off" disabled></label>' +
      '<label class="field" data-transfer-mode="cash" hidden>Importe<input name="amount" type="text" inputmode="decimal" required placeholder="100" disabled></label>' +
      '<label class="field">Fecha y hora<input name="occurred_at" type="datetime-local" required></label>' +
      '<label class="field">Nota opcional<input name="note" maxlength="1000" autocomplete="off"></label>' +
      '<button type="submit" class="ghost">Registrar traslado</button></form>' +
      '<p class="muted">Libro local · sin envío de fondos ni direcciones guardadas.</p></details>' : '') +
      '<details id="real-recovery" class="real-recovery"><summary>Restaurar desde una copia cifrada</summary>' +
      '<div class="real-recovery-body"><p class="amber">Esta acción reemplaza el libro real actual por la copia elegida. Orbis guardará antes el estado actual como otra copia cifrada y bloqueará la sesión. No modifica PAPER.</p>' +
      '<form id="real-restore-form" class="real-recovery-form"><label class="field">Copia disponible<select id="real-restore-name" required><option value="">Cargando copias…</option></select></label>' +
      '<button id="real-verify-backup" class="ghost" type="button" disabled data-help="Comprueba una instantánea cifrada sin sustituir la bóveda ni modificar la copia elegida. Restaurar vuelve a verificarla.">Comprobar copia</button>' +
      '<label class="field">Escribe RESTAURAR para confirmar<input id="real-restore-confirm" required pattern="RESTAURAR" autocomplete="off" spellcheck="false"></label>' +
      '<button class="ghost danger" type="submit">Restaurar y bloquear</button></form>' +
      '<p id="real-backup-check" class="muted" role="status" hidden></p>' +
      '<p id="real-restore-empty" class="muted" hidden>No hay copias cifradas disponibles.</p></div></details>' +
      '<details id="real-csv-import" class="real-recovery"><summary>Importar CSV REAL</summary>' +
      '<div class="real-recovery-body"><form id="real-csv-check-form" class="real-recovery-form">' +
      '<label class="field">Archivo CSV<input id="real-csv-file" type="file" accept=".csv,text/csv" required></label>' +
      '<button id="real-csv-check" class="ghost" type="submit">Comprobar</button></form>' +
      '<p id="real-csv-preview" class="muted" role="status" hidden></p>' +
      '<form id="real-csv-commit-form" class="real-recovery-form"><label class="field">Escribe IMPORTAR para confirmar' +
      '<input id="real-csv-confirm" required pattern="IMPORTAR" autocomplete="off" spellcheck="false"></label>' +
      '<button id="real-csv-commit" class="ghost" type="submit" disabled>Importar movimientos</button></form>' +
      '<p class="muted">CSV REAL v1–v3 · validación completa, sin modificar PAPER.</p></div></details>' +
      '<div class="real-layout"><section class="real-panel"><h3 data-help="Compras, ventas, efectivo, transferencias propias y ajustes auditables.">Registrar movimiento</h3>' +
      '<form id="real-event-form" class="real-form">' +
      '<label class="field">Tipo<select name="kind" id="real-kind"><option value="BUY">Compra</option><option value="SELL">Venta</option><option value="CONTRIBUTION">Aportación de efectivo</option><option value="WITHDRAWAL">Retirada de efectivo</option><option value="TRANSFER">Traslado dentro de esta cuenta</option><option value="ADJUSTMENT_IN">Ajuste de entrada</option><option value="ADJUSTMENT_OUT">Ajuste de salida</option></select></label>' +
      '<label class="field" id="real-asset-field">Activo<input name="asset" value="BTC" required maxlength="12" autocomplete="off"></label>' +
      '<label class="field">Divisa de pago<input name="quote_currency" value="' + esc(selected.base_currency) + '" required maxlength="12" autocomplete="off"></label>' +
      '<label class="field real-asset-movement-field">Cantidad de activo<input name="quantity" type="text" inputmode="decimal" placeholder="0.025" required></label>' +
      '<label class="field real-price-field">Precio o coste por unidad<input name="unit_price" type="text" inputmode="decimal" placeholder="65000" required></label>' +
      '<label class="field real-cash-field" hidden>Importe de efectivo<input name="amount" type="text" inputmode="decimal" placeholder="1000"></label>' +
      '<label class="field real-fee-field">Comisión en divisa de pago<input name="fee" type="text" inputmode="decimal" value="0" required></label>' +
      '<label class="field real-note-field" data-help="Describe el motivo sin incluir direcciones completas de billetera." hidden>Motivo / nota<input name="note" maxlength="1000" autocomplete="off"></label>' +
      '<label class="field">Fecha y hora<input name="occurred_at" type="datetime-local" required></label>' +
      '<button type="submit" data-help="Añade un evento inmutable al libro real; no envía órdenes.">Guardar movimiento</button></form>' +
      '<p class="muted real-footnote">Coste medio informativo; no fiscal.</p></section>' +
      '<section class="real-panel"><h3>Posiciones registradas</h3><div id="real-positions" class="state loading">Cargando…</div>' +
      '<div id="real-market-values" class="state loading">Consultando precios de referencia…</div></section></div>' +
      '<details class="real-panel portfolio-history" id="real-valuation-history"><summary data-help="Reconstrucción del libro vigente con cierres diarios públicos. El criterio contable aceptado puede cambiar valores pasados. Al consultar se solicitan los pares al proveedor elegido.">Evolución patrimonial</summary>' +
      '<form class="portfolio-history-controls" hidden><label class="field">Periodo<select name="days"><option value="7">7 días</option>' +
      '<option value="30" selected>30 días</option><option value="90">90 días</option><option value="365">1 año</option>' +
      '<option value="5y">5 años</option><option value="10y">10 años</option></select></label>' +
      '<label class="field">Divisa<select name="currency"></select></label>' +
      '<label class="field">Fuente<select name="provider"><option value="kraken">Kraken</option><option value="mexc">MEXC</option>' +
      '<option value="binance">Binance</option><option value="bitstamp">Bitstamp</option></select></label>' +
      '<button type="submit" class="ghost">Consultar</button></form>' +
      '<p class="muted portfolio-history-status" role="status" data-history-status></p><div data-history-result></div></details>' +
      '<section class="real-panel real-mexc"><div class="real-mexc-head"><h3 data-help="Consulta de saldos, órdenes manuales y bots autorizados. View y Trade se conectan por separado; observar no concede permiso para operar.">MEXC Spot</h3>' +
      '<span class="statusbadge off">SPOT</span></div>' +
      '<div id="real-mexc-state" class="state loading">Comprobando conexión…</div>' +
      '<div id="real-mexc-supervisor" class="real-supervisor-state" data-state="idle" role="status" data-help="La parada local no cancela por sí sola. La vigilancia puede cancelar una orden propia sólo con ORBIS abierto y bóveda desbloqueada; no es protección garantizada por MEXC.">' +
      '<span class="real-supervisor-mark" aria-hidden="true"></span><div><strong>COMPROBANDO VIGILANCIA</strong><small>Estado del proceso local</small></div></div>' +
      '<details id="real-mexc-supervisor-stop" class="real-exec-stop" hidden><summary>Detener vigilancia</summary>' +
      '<form><p>Detiene el proceso local. Las órdenes inciertas siguen requiriendo revisión en MEXC.</p>' +
      '<label class="field">Escribe DETENER_VIGILANCIA<input name="confirmation" required pattern="DETENER_VIGILANCIA" autocomplete="off" spellcheck="false"></label>' +
      '<button type="submit" class="ghost">Detener vigilancia</button><p class="real-exec-result" role="status" hidden></p></form></details>' +
      '<div id="real-mexc-value" class="real-mexc-value" hidden><div class="real-mexc-value-head"><span data-help="Valor de mercado observado; no es coste, beneficio ni patrimonio contabilizado.">VALOR OBSERVADO · NO P/L</span>' +
      '<div class="row"><select id="real-mexc-value-quote" aria-label="Divisa de valoración MEXC"><option value="USDT">USDT</option><option value="EUR">EUR</option></select>' +
      '<button id="real-mexc-value-run" class="ghost" type="button" data-help="Consulta de nuevo todos los saldos Spot y precios bid públicos recientes. Si falta un precio, no calcula total.">Valorar</button></div></div>' +
      '<div id="real-mexc-value-state" class="muted" role="status">Elige divisa y pulsa Valorar.</div></div>' +
      '<div id="real-mexc-actions" class="row" hidden><button id="real-mexc-refresh" class="ghost" type="button">Actualizar saldos</button>' +
      '<button id="real-mexc-compare" class="ghost" type="button" data-help="Compara unidades de la cuenta REAL seleccionada con una lectura nueva de MEXC Spot. No verifica que sean la misma cuenta, no importa movimientos ni autoriza órdenes.">Comparar inventario</button>' +
      '<label class="field" data-help="MEXC permite consultar hasta 90 días, en ventanas de hasta 7. Se guarda sólo si el rango completo resulta válido.">Transferencias <select id="real-mexc-transfer-days" aria-label="Periodo de ingresos y retiros">' +
      '<option value="1">1 día</option><option value="7" selected>7 días</option><option value="30">30 días</option><option value="90">Hasta 90 días</option></select></label>' +
      '<button id="real-mexc-transfers" class="ghost" type="button" data-help="Consulta ingresos y retiros y guarda una instantánea cifrada; requiere SPOT_WITHDRAW_READ.">Consultar y guardar</button>' +
      '<button id="real-mexc-disconnect" class="ghost" type="button">Desconectar</button></div>' +
      '<div id="real-mexc-comparison" class="state muted" role="status" hidden></div>' +
      '<details id="real-mexc-opening" hidden><summary data-help="Copia una fotografía Spot completa a una cuenta REAL vacía. Debes declarar el coste de cada activo; no verifica compras históricas ni habilita órdenes.">Registrar saldo inicial</summary>' +
      '<p class="muted">Sólo cuenta vacía · coste declarado · no es una compra ni conciliación.</p>' +
      '<button id="real-mexc-opening-load" class="ghost" type="button">Leer saldos</button>' +
      '<div id="real-mexc-opening-fields" hidden></div>' +
      '<div id="real-mexc-opening-preview" class="state muted" role="status" hidden></div></details>' +
      '<div id="real-mexc-transfer-result" class="state muted" role="status" hidden></div>' +
      '<details id="real-mexc-identity" hidden><summary data-help="Consulta el UID mediante la clave View y lo compara en memoria con huellas cifradas de envíos anteriores. No muestra ni guarda el UID, no verifica Trade ni habilita órdenes.">Identidad MEXC</summary>' +
      '<div class="row"><button id="real-mexc-identity-check" class="ghost" type="button">Comprobar cuenta</button></div>' +
      '<div id="real-mexc-identity-result" class="state muted" role="status">Sin comprobar en esta sesión.</div></details>' +
      '<details id="real-mexc-link" class="real-mexc-link"><summary data-help="Asocia esta conexión MEXC View al libro REAL elegido. No comprueba saldos ni mueve fondos.">Vínculo con cuenta REAL <span class="statusbadge off">SIN MOVIMIENTO</span></summary>' +
      '<div id="real-mexc-link-state" class="state muted" role="status">Abre para comprobar el vínculo.</div>' +
      '<form id="real-mexc-link-form" class="row" hidden><label class="field" data-help="Este vínculo identifica la cuenta MEXC en la bóveda; no importa activos ni verifica que el libro coincida con el exchange.">Escribe VINCULAR<input name="confirmation" pattern="VINCULAR" autocomplete="off" spellcheck="false" required></label>' +
      '<button type="submit" class="ghost" data-help="Guarda sólo la relación local entre la identidad MEXC View y este libro REAL. No habilita Trade ni órdenes.">Vincular libro</button></form>' +
      '<button id="real-mexc-renew-view" class="ghost" type="button" data-help="Guarda la huella de esta clave de la misma cuenta. Aplica STOP y pide revisar de nuevo Trade y los límites." hidden>Renovar vínculo View</button>' +
      '<button id="real-mexc-parity" type="button" class="ghost" data-help="Hace dos lecturas Spot. Si todo coincide, conserva la evidencia cifrada en la bóveda. No verifica órdenes o transferencias en tránsito ni habilita operativa real." hidden>Comprobar y guardar</button>' +
      '<div id="real-mexc-parity-result" class="real-parity-result" role="status" hidden></div>' +
      '<button id="real-mexc-open-orders" type="button" class="ghost" data-help="Consulta órdenes Spot abiertas sin seleccionar par y archiva la respuesta saneada en la bóveda. MEXC no garantiza cobertura completa; nunca autoriza operativa real." hidden>Revisar y guardar órdenes</button>' +
      '<div id="real-mexc-open-orders-result" class="real-parity-result" role="status" hidden></div>' +
      '<details id="real-mexc-open-orders-history"><summary data-help="Revisiones de órdenes cifradas. Aunque no se hayan observado órdenes, la cobertura de la cuenta no está probada.">Órdenes observadas · historial</summary>' +
      '<div id="real-mexc-open-orders-history-list" class="state muted">Abre para consultar.</div>' +
      '<button id="real-mexc-open-orders-compare" class="ghost" type="button" hidden data-help="Compara las dos revisiones más recientes. Una orden desaparecida no se presume ejecutada ni cancelada; requiere consultar estado final y fills.">Comparar últimas 2</button>' +
      '<div id="real-mexc-open-orders-compare-result" class="real-parity-result" role="status" hidden></div></details>' +
      '<details id="real-mexc-parity-history"><summary data-help="Fotografías cifradas anteriores. Una observación caduca o un libro cambiado impiden usarla como estado actual; ninguna autoriza órdenes.">Comprobaciones guardadas</summary>' +
      '<div id="real-mexc-parity-history-list" class="state muted">Abre para consultar.</div></details></details>' +
      '<details id="real-mexc-policy-drafts" class="real-policy-drafts"><summary data-help="Define límites para una versión LONG o DCA probada. El borrador queda desactivado: no conecta Trade ni envía órdenes.">Límites de estrategia <span class="statusbadge off">BORRADOR · SIN ÓRDENES</span></summary>' +
      '<div id="real-mexc-policy-state" class="state muted" role="status">Abre para consultar los borradores.</div>' +
      '<form id="real-mexc-policy-form" class="real-policy-form" hidden>' +
      '<label class="field real-policy-strategy">Versión LONG / DCA<select name="strategy_version_id" required><option value="">Cargando versiones…</option></select></label>' +
      '<label class="field" data-help="Par Spot exacto en MEXC; por ejemplo BTCUSDT.">Par Spot<input name="symbol" value="BTCUSDT" pattern="[A-Z0-9]{3,30}" maxlength="30" autocomplete="off" required></label>' +
      '<label class="field">Activo<input name="base_asset" value="BTC" pattern="[A-Z0-9]{2,16}" maxlength="16" autocomplete="off" required></label>' +
      '<label class="field">Divisa<input name="quote_asset" value="USDT" pattern="[A-Z0-9]{2,16}" maxlength="16" autocomplete="off" required></label>' +
      '<label class="field">Máx. por orden<input name="max_order_quote" type="text" inputmode="decimal" value="100" required></label>' +
      '<label class="field" data-help="En operativa REAL limita nuevas compras. Las ventas que reducen inventario pueden superar este límite para permitir salir.">Máx. diario<input name="max_daily_turnover_quote" type="text" inputmode="decimal" value="200" required></label>' +
      '<label class="field">Máx. exposición<input name="max_exposure_quote" type="text" inputmode="decimal" value="300" required></label>' +
      '<label class="field">Pérdida diaria máx.<input name="max_daily_loss_quote" type="text" inputmode="decimal" value="25" required></label>' +
      '<label class="field" data-help="Un punto básico equivale a 0,01 %.">Comisión máx. · bps<input name="max_fee_bps" type="text" inputmode="decimal" value="20" required></label>' +
      '<label class="field" data-help="Deslizamiento máximo admitido, en puntos básicos.">Deslizamiento máx. · bps<input name="max_slippage_bps" type="text" inputmode="decimal" value="30" required></label>' +
      '<label class="field">Antigüedad datos · s<input name="max_data_age_seconds" type="number" min="1" max="300" step="1" value="60" required></label>' +
      '<label class="field">Vigencia · días<input name="valid_days" type="number" min="1" max="30" step="1" value="7" required></label>' +
      '<button type="submit" class="ghost" data-help="Guarda una política desactivada en la bóveda REAL. No activa ni envía órdenes.">Guardar borrador</button></form>' +
      '<div id="real-mexc-policy-list" class="real-policy-list" aria-live="polite"></div></details>' +
      '<div id="real-mexc-trading"></div>' +
      '<div id="real-mexc-bots"></div>' +
      '<form id="real-mexc-trades-form" class="row" hidden><label class="field" data-help="Un par Spot exacto, por ejemplo BTCUSDT. MEXC limita esta consulta al último mes.">Operaciones de un par' +
      '<input name="symbol" value="BTCUSDT" maxlength="30" pattern="[A-Z0-9]{3,30}" autocomplete="off" required></label>' +
      '<button type="submit" class="ghost" data-help="Consulta una página reciente de hasta 100 fills.">Última página</button>' +
      '<label class="field" data-help="Recorre el rango reciente por tiempo. Si MEXC llena demasiadas páginas, no archiva un resultado parcial.">Rango' +
      '<select name="days"><option value="1">1 día</option><option value="7" selected>7 días</option><option value="30">30 días</option></select></label>' +
      '<button id="real-mexc-window" type="button" class="ghost">Archivar rango</button></form>' +
      '<div id="real-mexc-trades" class="state muted" hidden></div>' +
      '<details id="real-mexc-connect"><summary>Conectar clave de lectura</summary>' +
      '<form id="real-mexc-form" class="real-form"><label class="field">API key<input name="api_key" type="password" required autocomplete="off" spellcheck="false"></label>' +
      '<label class="field">Secret key<input name="secret_key" type="password" required autocomplete="off" spellcheck="false"></label>' +
      '<button type="submit">Consultar MEXC</button></form></details>' +
      '<details id="real-mexc-trade-verify" class="real-mexc-link"><summary data-help="Comprueba mediante GET el permiso Spot Trade y que la clave corresponda a la misma cuenta View. La clave dura sólo esta sesión; no se guardan secretos ni se activan órdenes nuevas.">Verificar clave Trade <span class="statusbadge off">SOLO GET · SIN ÓRDENES</span></summary>' +
      '<div id="real-mexc-trade-state" class="state muted" role="status">Abre para comprobar el estado.</div>' +
      '<form id="real-mexc-trade-form" class="real-form" autocomplete="off" hidden>' +
      '<label class="field">API key Trade<input name="api_key" type="password" required autocomplete="off" spellcheck="false"></label>' +
      '<label class="field">Secret key Trade<input name="secret_key" type="password" required autocomplete="off" spellcheck="false"></label>' +
      '<button type="submit" class="ghost" data-help="Verifica permiso e identidad con consultas GET. No envía órdenes ni conserva la clave tras bloquear.">Verificar Trade</button></form>' +
      '<button id="real-mexc-trade-disconnect" type="button" class="ghost" hidden>Retirar clave Trade</button></details>' +
      '<details id="real-mexc-archive"><summary data-help="Consultas anteriores guardadas en la bóveda cifrada. Son fills observados, no compras contabilizadas ni historial completo.">Historial guardado</summary>' +
      '<div id="real-mexc-archive-list" class="state muted">Abre para consultar.</div><div id="real-mexc-archive-detail" hidden></div></details>' +
      '<details id="real-mexc-execution"><summary data-help="Revisa órdenes finales observadas e importa fills compatibles en la cuenta REAL elegida. No envía órdenes.">Órdenes y conciliación REAL</summary>' +
      '<button type="button" class="ghost" data-mexc-full-journal hidden>Ver historial completo</button>' +
      '<p id="real-mexc-execution-feedback" class="state error" role="status" hidden></p>' +
      '<div id="real-mexc-execution-list" class="state muted">Abre para revisar.</div></details>' +
      '<details id="real-mexc-transfers-archive"><summary data-help="Instantáneas de ingresos y retiros cifradas, sin direcciones y separadas del libro contable.">Transferencias guardadas</summary>' +
      '<div class="row"><button id="real-mexc-transfer-audit" class="ghost" type="button" data-help="Revisa todas las capturas de la conexión vinculada a la cuenta REAL seleccionada. No concilia saldos ni habilita órdenes.">Auditar cuenta</button></div>' +
      '<div id="real-mexc-transfer-audit-result" class="state muted" role="status" hidden></div>' +
      '<form id="real-mexc-deposit-tx-form" class="row" autocomplete="off"><label class="field" data-help="Busca un txId EVM de ingreso en capturas MEXC con UID View verificado. El hash se usa sólo durante la consulta local y no se guarda.">Hash de ingreso EVM' +
      '<input name="tx_hash" type="text" maxlength="66" minlength="66" pattern="0x[0-9a-fA-F]{64}" spellcheck="false" autocomplete="off" placeholder="0x…" required></label>' +
      '<button type="submit" class="ghost">Buscar ingreso</button></form>' +
      '<div id="real-mexc-deposit-tx-result" class="state muted" role="status" hidden></div>' +
      '<div id="real-mexc-transfers-list" class="state muted">Abre para consultar.</div><div id="real-mexc-transfers-detail" hidden></div></details>' +
      '<details id="real-mexc-direct-journeys"></details>' +
      '<p class="muted real-footnote">Clave View: SPOT_ACCOUNT_READ; transferencias: SPOT_WITHDRAW_READ. Consulta e importación con confirmación.</p></section>' +
      '<section class="real-panel real-grvt"><div class="real-mexc-head"><h3 data-help="Consulta puntual de cuenta GRVT mainnet. Saldos y posiciones observados; no modifica el libro ni ejecuta órdenes.">GRVT · solo lectura</h3>' +
      '<span class="statusbadge off" data-help="GRVT no devuelve los permisos de la clave en esta consulta. Usa exclusivamente una clave View; ORBIS sólo llama a endpoints de lectura.">VIEW NO VERIFICABLE</span></div>' +
      '<div id="real-grvt-state" class="state loading">Comprobando conexión…</div>' +
      '<div id="real-grvt-actions" class="row" hidden><button id="real-grvt-refresh" class="ghost" type="button">Actualizar</button>' +
      '<button id="real-grvt-disconnect" class="ghost" type="button">Desconectar</button></div>' +
      '<details id="real-grvt-connect"><summary>Conectar clave View</summary>' +
      '<form id="real-grvt-form" class="real-form"><label class="field" data-help="La clave se envía sólo al proceso local y se descarta del formulario. La cookie GRVT queda en memoria hasta bloquear la bóveda.">API key<input name="api_key" type="password" required autocomplete="off" spellcheck="false"></label>' +
      '<button type="submit">Consultar GRVT</button></form></details>' +
      '<section id="real-perp-live-review" hidden></section>' +
      '<details id="real-grvt-orders" hidden><summary data-help="Foto de órdenes GRVT abiertas. Puede no incluir órdenes enviadas pero aún en tránsito; nunca autoriza operativa automática.">Órdenes abiertas · observar</summary>' +
      '<div id="real-grvt-orders-state" class="state muted">Abre para consultar.</div>' +
      '<form id="real-grvt-order-form" class="real-grvt-order-form"><label class="field">Subcuenta<input name="sub_account_id" inputmode="numeric" pattern="[0-9]+" maxlength="30" required></label>' +
      '<label class="field">ID cliente<input name="client_order_id" inputmode="numeric" pattern="[0-9]+" maxlength="20" required></label>' +
      '<button type="submit" class="ghost" data-help="Vuelve a consultar una orden concreta, incluso si ya no aparece entre las abiertas.">Comprobar orden</button>' +
      '<button id="real-grvt-fills-check" type="button" class="ghost" data-help="Recorre fills desde la creación de la orden, compara tamaños y archiva el resultado saneado en la bóveda cifrada. No habilita trading.">Ver y guardar ejecuciones</button></form>' +
      '<div id="real-grvt-order-detail" class="state muted" role="status">Introduce los identificadores para comprobar una orden.</div>' +
      '<div id="real-grvt-fills-detail" class="state muted" role="status" hidden></div></details>' +
      '<details id="real-grvt-archive"><summary data-help="Evidencia cifrada de órdenes y fills observados, independiente de la cartera contable. Disponible sin conexión GRVT.">Evidencia guardada</summary>' +
      '<div id="real-grvt-archive-list" class="state muted">Abre para consultar.</div>' +
      '<div id="real-grvt-archive-detail" class="state muted" hidden></div></details>' +
      '<p class="muted real-footnote">Observación separada · sin importación ni órdenes</p></section>' +
      grvtTestnetRecoveryPanel() +
      '<div id="real-wallet-root"></div>' +
      '<section class="real-panel"><h3 data-help="Los eventos no se editan ni se borran silenciosamente.">Historial</h3><div id="real-history" class="state loading">Cargando…</div></section>' +
      '<p class="amber">Sin precio válido o efectivo completo, no se muestra patrimonio total.</p>', true);
    portfolioHistory = window.OrbisPortfolioHistory.mount(
      root.querySelector("#real-valuation-history"), function (params, signal) {
        return request("/accounts/" + encodeURIComponent(accountId) + "/valuation-history?" + params, {signal: signal});
      }, selected.base_currency, hiddenAmounts
    );
    root.querySelector("#real-account").value = accountId;
    root.querySelector("#real-account").addEventListener("change", function (event) {
      accountId = event.target.value;
      if (window.OrbisWallet) window.OrbisWallet.setBookSnapshot(null);
      clearMexcOpening();
      clearMexcComparison();
      clearMexcParity();
      clearMexcOpenOrdersSurvey();
      delete root.querySelector("#real-mexc-open-orders").dataset.requestId;
      delete root.querySelector("#real-mexc-parity").dataset.requestId;
      resetCsvImport();
      var policyForm = root.querySelector("#real-mexc-policy-form");
      mexcLimitsFromSource = null;
      policyForm.reset();
      delete policyForm.dataset.requestId;
      delete policyForm.dataset.requestPayload;
      var next = accounts.find(function (item) { return item.id === accountId; });
      if (portfolioHistory) portfolioHistory.selectAccount(next.base_currency);
      var form = root.querySelector("#real-event-form");
      form.reset();
      form.elements.quote_currency.value = next.base_currency;
      var transferForm = root.querySelector("#real-paired-transfer-form");
      if (transferForm) {
        transferForm.elements.target_account_id.innerHTML = transferTargetOptions(accounts, accountId);
        transferForm.elements.quote_currency.value = next.base_currency;
        transferForm.elements.currency.value = next.base_currency;
        transferForm.elements.quantity.value = "";
        transferForm.elements.fee.value = "0";
        transferForm.elements.amount.value = "";
        delete transferForm.dataset.requestId;
      }
      var localNow = new Date(Date.now() - new Date().getTimezoneOffset() * 60000);
      form.elements.occurred_at.value = localNow.toISOString().slice(0, 16);
      updateKind();
      refresh();
      root.querySelector("#real-mexc-execution-feedback").hidden = true;
      if (root.querySelector("#real-mexc-execution").open) loadMexcExecution();
      if (root.querySelector("#real-mexc-link").open) loadMexcAccountLink();
      if (root.querySelector("#real-mexc-parity-history").open) loadMexcParityHistory();
      if (root.querySelector("#real-mexc-open-orders-history").open) loadMexcOpenOrdersHistory();
      if (root.querySelector("#real-mexc-policy-drafts").open) loadMexcPolicyDrafts();
      if (window.OrbisMexcTrading) window.OrbisMexcTrading.refresh();
      if (window.OrbisMexcBots) window.OrbisMexcBots.accountChanged();
    });
    var transferForm = root.querySelector("#real-paired-transfer-form");
    if (transferForm) {
      transferForm.addEventListener("submit", saveAccountTransfer);
      transferForm.addEventListener("input", function () { delete transferForm.dataset.requestId; });
      transferForm.elements.mode.addEventListener("change", function () {
        delete transferForm.dataset.requestId;
        updateAccountTransferMode(transferForm);
      });
      updateAccountTransferMode(transferForm);
      var transferLocal = new Date(Date.now() - new Date().getTimezoneOffset() * 60000);
      transferForm.elements.occurred_at.value = transferLocal.toISOString().slice(0, 16);
    }
    root.querySelector("#real-hide").addEventListener("click", function () {
      hiddenAmounts = !hiddenAmounts;
      clearMexcOpening();
      clearPortfolioHistory();
      load();
    });
    root.querySelector("#real-lock").addEventListener("click", function () {
      request("/vault/lock", { method: "POST" }).then(function (result) {
        afterRestoreMessage = result.review_required ? {text:"Bloqueado · el archivo del bot requiere revisión antes de operar.",error:true} : null;
        forgetSession();
        showLocked();
      }).catch(function () {
        afterRestoreMessage = {text:"Vista protegida · no se pudo confirmar el bloqueo del servidor.",error:true};
        forgetSession(); showLocked();
      });
    });
    root.querySelector("#real-protect").addEventListener("click", protectRealView);
    root.querySelector("#real-backup").addEventListener("click", function () {
      var button = this;
      button.disabled = true;
      request("/vault/backup", { method: "POST" }).then(function (result) {
        notice("Copia cifrada creada en " + result.path, false);
        if (root.querySelector("#real-recovery").open) loadBackups();
      }).catch(function (error) { notice(error.message, true); })
        .finally(function () { button.disabled = false; });
    });
    root.querySelector("#real-export-csv").addEventListener("click", exportEventsCsv);
    root.querySelector("#real-add-account-form").addEventListener("submit", function (event) {
      event.preventDefault();
      var form = event.currentTarget, fields = new FormData(form);
      var button = form.querySelector('button[type="submit"]');
      button.disabled = true;
      request("/accounts", { method: "POST", body: JSON.stringify({
        name: String(fields.get("name")).trim(),
        base_currency: String(fields.get("base_currency")).trim().toUpperCase()
      }) }).then(function (account) {
        accountId = account.id;
        load("Cuenta REAL creada y seleccionada.");
      }).catch(function (error) { notice(error.message, true); })
        .finally(function () { if (button.isConnected) button.disabled = false; });
    });
    root.querySelector("#real-csv-file").addEventListener("change", resetCsvImport);
    root.querySelector("#real-csv-check-form").addEventListener("submit", previewCsvImport);
    root.querySelector("#real-csv-commit-form").addEventListener("submit", commitCsvImport);
    root.querySelector("#real-recovery").addEventListener("toggle", function () {
      if (this.open) loadBackups();
    });
    root.querySelector("#real-restore-form").addEventListener("submit", restoreBackup);
    root.querySelector("#real-verify-backup").addEventListener("click", verifyBackup);
    root.querySelector("#real-restore-name").addEventListener("change", function () {
      var status = root.querySelector("#real-backup-check");
      status.hidden = true; status.textContent = "";
      root.querySelector("#real-verify-backup").disabled = !this.value;
    });
    root.querySelector("#real-mexc-form").addEventListener("submit", connectMexc);
    root.querySelector("#real-mexc-supervisor-stop form").addEventListener("submit", stopMexcSupervision);
    root.querySelector("#real-mexc-trade-verify").addEventListener("toggle", function () {
      if (this.open) loadMexcTradeStatus();
    });
    root.querySelector("#real-mexc-trade-form").addEventListener("submit", connectMexcTrade);
    root.querySelector("#real-mexc-trade-disconnect").addEventListener("click", disconnectMexcTrade);
    root.querySelector("#real-mexc-refresh").addEventListener("click", refreshMexc);
    root.querySelector("#real-mexc-compare").addEventListener("click", compareMexc);
    root.querySelector("#real-mexc-opening-load").addEventListener("click", loadMexcOpening);
    root.querySelector("#real-mexc-identity-check").addEventListener("click", checkMexcIdentity);
    root.querySelector("#real-mexc-link").addEventListener("toggle", function () {
      if (this.open) loadMexcAccountLink();
    });
    root.querySelector("#real-mexc-link-form").addEventListener("submit", linkMexcAccount);
    root.querySelector("#real-mexc-renew-view").addEventListener("click", renewMexcView);
    root.querySelector("#real-mexc-parity").addEventListener("click", checkMexcParity);
    root.querySelector("#real-mexc-open-orders").addEventListener("click", checkMexcOpenOrdersSurvey);
    root.querySelector("#real-mexc-open-orders-history").addEventListener("toggle", function () {
      if (this.open) loadMexcOpenOrdersHistory();
    });
    root.querySelector("#real-mexc-open-orders-history-list").addEventListener("click", function (event) {
      var button = event.target.closest("button[data-orders-index]");
      if (!button || !this.contains(button) || !this._reports) return;
      var report = this._reports[Number(button.dataset.ordersIndex)];
      if (report) renderMexcOpenOrdersSurvey(report, root.querySelector("#real-mexc-open-orders-result"), true);
    });
    root.querySelector("#real-mexc-open-orders-compare").addEventListener("click", compareMexcOpenOrdersHistory);
    root.querySelector("#real-mexc-parity-history").addEventListener("toggle", function () {
      if (this.open) loadMexcParityHistory();
    });
    root.querySelector("#real-mexc-parity-history-list").addEventListener("click", function (event) {
      var button = event.target.closest("button[data-parity-index]");
      if (!button || !this.contains(button) || !this._reports) return;
      var report = this._reports[Number(button.dataset.parityIndex)];
      if (report) renderMexcParity(report, root.querySelector("#real-mexc-parity-result"), true);
    });
    root.querySelector("#real-mexc-policy-drafts").addEventListener("toggle", function () {
      if (this.open) loadMexcPolicyDrafts();
    });
    if (window.OrbisMexcTrading) window.OrbisMexcTrading.mount(root.querySelector("#real-mexc-trading"), {
      request: request, getAccountId: function () { return accountId; }, uuid: uuid,
      amountsHidden: function () { return hiddenAmounts; },
      viewConnected: function () { return mexcViewConnected; },
      openOpening: function () { openRealControl("real-mexc-opening"); },
      openTransfers: function () { root.querySelector("#real-mexc-transfers").focus(); },
      renderParity: function (data, target) { renderMexcParity(data, target, false); },
      openPending: openMexcPendingOrders,
      renderProgress: function (item, target) { target.innerHTML = executionProgress(item); },
      onChange: function () { refresh(); if (root.querySelector("#real-mexc-execution").open) loadMexcExecution(); }
    });
    if (window.OrbisMexcBots) window.OrbisMexcBots.mount(root.querySelector("#real-mexc-bots"), {
      request: request, getAccountId: function () { return accountId; }, uuid: uuid,
      accountLabel: function () { return root.querySelector("#real-account").selectedOptions[0].textContent; },
      prepareLimits: prepareMexcLimits,
      openAccountAction: openMexcBotAccountAction,
      amountsHidden: function () { return hiddenAmounts; },
      exact: function (value) { return hiddenAmounts ? "••••" : String(value); },
      format: function (value) {
        if (hiddenAmounts) return "••••";
        var formatter = window.OrbisUI && window.OrbisUI.fmtDecimal;
        var shown = formatter && formatter(String(value), 0, 8);
        if (shown === "0" && /[1-9]/.test(String(value))) return String(value)[0] === "-" ? "−<0,00000001" : "<0,00000001";
        return shown === null || shown === undefined ? "—" : shown;
      },
      onChange: function () { refresh(true); },
      onControlChange: function () { refresh(); if (window.OrbisMexcTrading) window.OrbisMexcTrading.refresh(); }
    }, preparedSource);
    if (pendingMexcSource) {
      pendingMexcSource = null;
      openRealControl("real-mexc-bots");
    }
    root.querySelector("#real-mexc-policy-list").addEventListener("submit", testMexcDraftOrder);
    root.querySelector("#real-mexc-policy-list").addEventListener("click", inspectMexcDailyBookRisk);
    root.querySelector("#real-mexc-policy-list").addEventListener("click", inspectMexcSpotExposure);
    root.querySelector("#real-mexc-policy-list").addEventListener("toggle", function (event) {
      var details = event.target.closest("details.real-policy-tests");
      if (details && details.open) loadMexcOrderTests(details);
    }, true);
    var policyForm = root.querySelector("#real-mexc-policy-form");
    policyForm.addEventListener("submit", saveMexcPolicyDraft);
    policyForm.addEventListener("input", function () {
      delete policyForm.dataset.requestId;
      delete policyForm.dataset.requestPayload;
    });
    policyForm.addEventListener("change", function () {
      delete policyForm.dataset.requestId;
      delete policyForm.dataset.requestPayload;
    });
    root.querySelector("#real-mexc-value-run").addEventListener("click", valueMexc);
    root.querySelector("#real-mexc-transfers").addEventListener("click", queryMexcTransfers);
    root.querySelector("#real-mexc-disconnect").addEventListener("click", disconnectMexc);
    root.querySelector("#real-mexc-trades-form").addEventListener("submit", queryMexcTrades);
    root.querySelector("#real-mexc-window").addEventListener("click", queryMexcWindow);
    root.querySelector("#real-mexc-archive").addEventListener("toggle", function () {
      if (this.open) loadMexcArchive();
    });
    var mexcJournal = root.querySelector("#real-mexc-execution");
    mexcJournal.addEventListener("toggle", function () {
      if (this.open) loadMexcExecution();
      else { delete this.dataset.pendingOnly; delete this.dataset.focusIntent; mexcExecutionEpoch += 1; }
    });
    mexcJournal.querySelector("[data-mexc-full-journal]").addEventListener("click", function () {
      delete mexcJournal.dataset.pendingOnly;
      delete mexcJournal.dataset.focusIntent;
      loadMexcExecution();
      mexcJournal.querySelector("summary").focus();
    });
    root.querySelector("#real-mexc-transfers-archive").addEventListener("toggle", function () {
      if (this.open) loadMexcTransferArchive();
    });
    root.querySelector("#real-mexc-transfer-audit").addEventListener("click", loadMexcTransferAudit);
    root.querySelector("#real-mexc-deposit-tx-form").addEventListener("submit", lookupMexcDepositTx);
    root.querySelector("#real-grvt-form").addEventListener("submit", connectGrvt);
    root.querySelector("#real-grvt-refresh").addEventListener("click", refreshGrvt);
    root.querySelector("#real-grvt-disconnect").addEventListener("click", disconnectGrvt);
    root.querySelector("#real-grvt-orders").addEventListener("toggle", function () {
      if (this.open) loadGrvtOrders();
    });
    root.querySelector("#real-grvt-order-form").addEventListener("submit", checkGrvtOrder);
    root.querySelector("#real-grvt-fills-check").addEventListener("click", checkGrvtFills);
    root.querySelector("#real-grvt-archive").addEventListener("toggle", function () {
      if (this.open) loadGrvtArchive();
    });
    root.querySelector("#real-grvt-archive-list").addEventListener("click", function (event) {
      var button = event.target.closest("button[data-grvt-evidence]");
      if (button && this.contains(button)) loadGrvtEvidence(button.dataset.grvtEvidence);
    });
    bindGrvtTestnetRecovery();
    root.querySelector("#real-grvt-orders-state").addEventListener("click", function (event) {
      var button = event.target.closest("button[data-grvt-order]");
      if (!button || !this.contains(button)) return;
      var form = root.querySelector("#real-grvt-order-form");
      form.elements.sub_account_id.value = button.dataset.subAccount;
      form.elements.client_order_id.value = button.dataset.clientOrder;
      form.requestSubmit();
    });
    if (window.OrbisWallet) window.OrbisWallet.mount(
      root.querySelector("#real-wallet-root"), hiddenAmounts,
      function (path, options, passive) { return request(path, options, passive); },
      function () { return accountId; },
      function () { refresh(); },
      paintMexcRoute,
      openMexcJourneyStep
    );
    if (pendingDexSource && window.OrbisWalletDexBot && window.OrbisWalletDexBot.prepare(pendingDexSource)) {
      pendingDexSource = null;
      openRealControl("real-wallet-dex-bot");
    }
    var journeyAccount = accountId;
    window.OrbisMexcDirectJourneys.mount(root.querySelector("#real-mexc-direct-journeys"), {
      accountId:journeyAccount, request:request, current:function () { return accountId === journeyAccount; },
      hidden:function () { return hiddenAmounts; }, openStep:openMexcJourneyStep
    });
    mountMexcRoute();
    var dateField = root.querySelector('#real-event-form [name="occurred_at"]');
    var local = new Date(Date.now() - new Date().getTimezoneOffset() * 60000);
    dateField.value = local.toISOString().slice(0, 16);
    root.querySelector("#real-kind").addEventListener("change", updateKind);
    updateKind();
    root.querySelector("#real-event-form").addEventListener("submit", saveEvent);
    refresh();
    loadMexc();
    if (window.OrbisPerpRealReview) perpLiveReview = window.OrbisPerpRealReview.mount(root.querySelector('#real-perp-live-review'), request);
    loadGrvt();
    if (mexcSupervisorSnapshot) paintMexcSupervisor(
      mexcSupervisorSnapshot.data, mexcSupervisorSnapshot.unavailable);
  }
  function mexcState(text, error) {
    var target = root && root.querySelector("#real-mexc-state");
    if (!target) return;
    target.textContent = text;
    target.className = "state " + (error ? "error" : "muted");
  }
  function renderMexcBalances(data, valuation) {
    var target = root && root.querySelector("#real-mexc-state");
    if (!target) return;
    var valued = {};
    if (valuation) valuation.balances.forEach(function (item) { valued[item.asset] = item; });
    var rows = data.balances.map(function (item) {
      return '<tr><td>' + esc(item.asset) + '</td><td class="num">' + show(item.free) + '</td><td class="num">' + show(item.locked) +
        '</td><td class="num">' + show(item.total) + '</td>' +
        (valuation ? '<td class="num">' + (valued[item.asset] && valued[item.asset].value_quote !== null ? show(valued[item.asset].value_quote) : '—') + '</td>' : '') + '</tr>';
    }).join("");
    target.className = "";
    target.innerHTML = '<p class="muted">Observado: ' + esc(new Date(data.observed_at).toLocaleString()) + ' · MEXC Spot · no importado</p>' +
      (rows ? '<div class="tablewrap"><table><thead><tr><th>Activo</th><th>Disponible</th><th>Bloqueado</th><th>Total</th>' +
        (valuation ? '<th>Valor ' + esc(valuation.quote_asset) + '</th>' : '') + '</tr></thead><tbody>' + rows + '</tbody></table></div>' :
        '<p class="muted">Sin saldos distintos de cero.</p>');
    var value = root.querySelector("#real-mexc-value-state");
    if (!valuation) {
      value.className = "muted";
      value.textContent = "Elige divisa y pulsa Valorar.";
    } else if (valuation.complete) {
      value.className = "real-mexc-value-total";
      value.innerHTML = '<strong>' + show(valuation.total_quote) + '</strong><span>' + esc(valuation.quote_asset) +
        ' · ' + esc(new Date(valuation.valued_at).toLocaleString()) + '</span>';
    } else {
      value.className = "amber";
      value.textContent = "Total no disponible · sin precio verificable: " + valuation.unpriced_assets.join(", ");
    }
  }
  function mexcConnected(connected) {
    mexcAuditEpoch += 1;
    mexcViewConnected = connected;
    mexcTradeVerifyEpoch += 1;
    var actions = root && root.querySelector("#real-mexc-actions");
    var trades = root && root.querySelector("#real-mexc-trades-form");
    var tradeResult = root && root.querySelector("#real-mexc-trades");
    var transferResult = root && root.querySelector("#real-mexc-transfer-result");
    var details = root && root.querySelector("#real-mexc-connect");
    var value = root && root.querySelector("#real-mexc-value");
    var identity = root && root.querySelector("#real-mexc-identity");
    var opening = root && root.querySelector("#real-mexc-opening");
    if (!actions || !details) return;
    root.querySelectorAll(".real-exec-audit, .real-exec-transfer-audit").forEach(function (form) {
      var button = form.querySelector('button[type="submit"]');
      var result = form.querySelector(".real-exec-result");
      if (button) button.disabled = !connected;
      if (result) {
        result.hidden = !connected;
        result.className = "real-exec-result muted";
        result.textContent = connected ? "Vuelve a auditar esta conexión." : "";
      }
    });
    root.querySelectorAll('.real-exec-sync button[type="submit"]').forEach(function (button) {
      button.disabled = !connected;
    });
    actions.hidden = !connected;
    trades.hidden = !connected;
    value.hidden = !connected;
    identity.hidden = !connected;
    opening.hidden = !connected || hiddenAmounts;
    if (!connected) {
      clearMexcComparison();
      clearMexcOpening();
      clearMexcIdentity();
      mexcValueEpoch += 1;
      value.querySelector("#real-mexc-value-state").textContent = "";
      mexcTradeEpoch += 1;
      mexcTransferEpoch += 1;
      tradeResult.hidden = true;
      tradeResult.replaceChildren();
      transferResult.hidden = true;
      transferResult.replaceChildren();
    }
    details.hidden = connected;
    if (connected) details.open = false;
    paintMexcRoute();
    var journal = root.querySelector("#real-mexc-execution");
    if (journal) {
      journal.querySelector("#real-mexc-execution-feedback").hidden = true;
      if (journal.open && journal.dataset.pendingOnly === "1") loadMexcExecution();
    }
    var policies = root.querySelector("#real-mexc-policy-drafts");
    if (policies && policies.open) loadMexcPolicyDrafts();
    var link = root.querySelector("#real-mexc-link");
    if (link && link.open) loadMexcAccountLink();
    var tradeVerification = root.querySelector("#real-mexc-trade-verify");
    if (tradeVerification && tradeVerification.open) loadMexcTradeStatus();
    if (window.OrbisMexcTrading) window.OrbisMexcTrading.refresh();
    if (window.OrbisMexcBots) window.OrbisMexcBots.refresh();
  }
  function paintMexcTradeStatus(data) {
    var details = root && root.querySelector("#real-mexc-trade-verify");
    if (!details) return;
    var state = details.querySelector("#real-mexc-trade-state");
    var form = details.querySelector("#real-mexc-trade-form");
    var disconnect = details.querySelector("#real-mexc-trade-disconnect");
    var verified = data && data.verified === true && data.execution_enabled === false;
    state.className = "state " + (data && data.execution_enabled !== false ? "error" : "muted");
    state.textContent = !mexcViewConnected ? "Conecta primero MEXC View. Sin órdenes nuevas." :
      data && data.execution_enabled !== false ? "Estado Trade inesperado. No operar." :
      verified ? "Trade comprobada · autorización de operativa separada." :
      "Sin clave Trade verificada · no se enviarán órdenes.";
    form.hidden = !mexcViewConnected || verified || data && data.execution_enabled !== false;
    disconnect.hidden = !verified;
    if (!mexcViewConnected) form.reset();
    if (window.OrbisMexcTrading) window.OrbisMexcTrading.refresh();
    if (window.OrbisMexcBots) window.OrbisMexcBots.refresh();
    if (root.querySelector("#real-mexc-execution").open) loadMexcExecution();
  }
  function loadMexcTradeStatus() {
    var details = root && root.querySelector("#real-mexc-trade-verify");
    if (!details || !details.open || !token) return;
    var epoch = ++mexcTradeVerifyEpoch;
    var state = details.querySelector("#real-mexc-trade-state");
    state.className = "state loading";
    state.textContent = "Comprobando estado Trade…";
    request("/exchanges/mexc/trade/status", null, true).then(function (data) {
      if (epoch === mexcTradeVerifyEpoch && details.isConnected) paintMexcTradeStatus(data);
    }).catch(function (error) {
      if (epoch !== mexcTradeVerifyEpoch || !details.isConnected) return;
      state.className = "state error";
      state.textContent = error.message;
    });
  }
  function connectMexcTrade(event) {
    event.preventDefault();
    if (!mexcViewConnected || !token) return;
    var form = event.currentTarget, epoch = ++mexcTradeVerifyEpoch;
    var body = JSON.stringify({ api_key: form.elements.api_key.value,
                                secret_key: form.elements.secret_key.value });
    form.reset();
    var button = form.querySelector('button[type="submit"]');
    var state = root.querySelector("#real-mexc-trade-state");
    button.disabled = true;
    state.className = "state loading";
    state.textContent = "Verificando permisos e identidad con GET…";
    request("/exchanges/mexc/trade/connect", { method: "POST", body: body })
      .then(function (data) {
        if (epoch === mexcTradeVerifyEpoch && form.isConnected) paintMexcTradeStatus(data);
      }).catch(function (error) {
        if (epoch !== mexcTradeVerifyEpoch || !form.isConnected) return;
        state.className = "state error";
        state.textContent = error.message;
      }).finally(function () { if (button.isConnected && epoch === mexcTradeVerifyEpoch) button.disabled = false; });
  }
  function disconnectMexcTrade(event) {
    var button = event.currentTarget, epoch = ++mexcTradeVerifyEpoch;
    var state = root.querySelector("#real-mexc-trade-state");
    button.disabled = true;
    state.className = "state loading";
    state.textContent = "Retirando clave Trade de memoria…";
    request("/exchanges/mexc/trade/disconnect", { method: "POST" })
      .then(function (data) {
        if (epoch === mexcTradeVerifyEpoch && button.isConnected) paintMexcTradeStatus(data);
      }).catch(function (error) {
        if (epoch !== mexcTradeVerifyEpoch || !button.isConnected) return;
        state.className = "state error";
        state.textContent = error.message;
      }).finally(function () { if (button.isConnected && epoch === mexcTradeVerifyEpoch) button.disabled = false; });
  }
  function loadMexcAccountLink() {
    var details = root && root.querySelector("#real-mexc-link");
    if (!details || !details.open || !accountId || !token) return;
    var selected = accountId, epoch = ++mexcLinkEpoch;
    var state = details.querySelector("#real-mexc-link-state");
    var form = details.querySelector("#real-mexc-link-form");
    var renew = details.querySelector("#real-mexc-renew-view");
    var parity = details.querySelector("#real-mexc-parity");
    var openOrders = details.querySelector("#real-mexc-open-orders");
    clearMexcParity();
    clearMexcOpenOrdersSurvey();
    form.hidden = true;
    renew.hidden = true;
    parity.hidden = true;
    openOrders.hidden = true;
    state.className = "state loading";
    state.textContent = "Comprobando vínculo local…";
    request("/accounts/" + encodeURIComponent(selected) + "/mexc-link", null, true)
      .then(function (status) {
        if (epoch !== mexcLinkEpoch || selected !== accountId || !details.isConnected) return;
        var invalid = ["KEY_UID_CHANGED", "ROTATED_KEY_MATCH", "AMBIGUOUS_LOCAL_IDENTITY"];
        renew.hidden = status.can_renew_view !== true;
        state.className = "state " + (!status.can_renew_view && (status.assigned_elsewhere ||
          status.account_has_other_link || invalid.includes(status.identity_status)) ? "error" : "muted");
        state.textContent = !status.connected ? "Conecta MEXC View para vincular." :
          status.session_renewed ? "Vínculo View guardado." :
          status.can_renew_view ? "Clave nueva de la misma cuenta MEXC." :
          status.assigned_elsewhere ? "Esta identidad MEXC ya pertenece a otro libro REAL." :
          status.account_has_other_link ? "Este libro REAL ya tiene otra identidad MEXC." :
          invalid.includes(status.identity_status) ? "Identidad MEXC en conflicto; revisa antes de vincular." :
          status.linked && status.identity_status === "SAME_KEY" ?
            "Vínculo local guardado · saldos sin conciliar · sin órdenes." :
          status.linked ? "Vínculo antiguo: confirma de nuevo la identidad View." :
          "Sin vínculo · no se importarán activos ni movimientos.";
        form.hidden = !status.connected || status.assigned_elsewhere || status.account_has_other_link ||
          invalid.includes(status.identity_status) ||
          (status.linked && status.identity_status === "SAME_KEY");
        parity.hidden = !status.linked || status.identity_status !== "SAME_KEY" ||
          status.account_has_other_link;
        openOrders.hidden = parity.hidden;
      }).catch(function (error) {
        if (epoch !== mexcLinkEpoch || selected !== accountId || !details.isConnected) return;
        state.className = "state error";
        state.textContent = error.message;
      });
  }
  function linkMexcAccount(event) {
    event.preventDefault();
    var form = event.currentTarget, selected = accountId;
    if (!selected || !token || !mexcViewConnected) return;
    var state = root.querySelector("#real-mexc-link-state");
    var button = form.querySelector('button[type="submit"]');
    button.disabled = true;
    state.className = "state loading";
    state.textContent = "Guardando vínculo local…";
    request("/accounts/" + encodeURIComponent(selected) + "/mexc-link",
      { method: "POST", body: JSON.stringify({ confirmation: form.elements.confirmation.value }) })
      .then(function () {
        if (!form.isConnected || selected !== accountId) return;
        form.reset();
        loadMexcAccountLink();
        if (root.querySelector("#real-mexc-policy-drafts").open) loadMexcPolicyDrafts();
        notice("Cuenta REAL vinculada a MEXC View. No se movieron fondos ni se activaron órdenes.", false);
      }).catch(function (error) {
        if (!form.isConnected || selected !== accountId) return;
        state.className = "state error";
        state.textContent = error.message;
      }).finally(function () { if (button.isConnected) button.disabled = false; });
  }
  function renewMexcView(event) {
    var button = event.currentTarget, selected = accountId, connection = mexcConnectionEpoch;
    if (!selected || !token || button.hidden || button.disabled ||
        !window.confirm("¿Renovar el vínculo View y detener la operativa de esta cuenta MEXC?")) return;
    var state = root.querySelector("#real-mexc-link-state");
    button.disabled = true;
    state.className = "state loading";
    state.textContent = "Verificando View y aplicando STOP…";
    function current() { return button.isConnected && selected === accountId && connection === mexcConnectionEpoch; }
    request("/accounts/" + encodeURIComponent(selected) + "/mexc-link/renew-view",
      {method:"POST",body:JSON.stringify({confirmation:"RENOVAR_VIEW_Y_DETENER"})})
      .then(function () {
        if (!current()) return;
        clearMexcOpening();
        clearMexcIdentity();
        clearMexcComparison();
        mexcConnected(true);
        refresh();
        notice("Vínculo renovado · verifica Trade y revisa la operativa.", false);
      }).catch(function (error) {
        if (!current()) return;
        state.className = "state error";
        state.textContent = error.message;
      }).finally(function () { if (button.isConnected) button.disabled = false; });
  }
  function clearMexcParity() {
    mexcParityEpoch += 1;
    var result = root && root.querySelector("#real-mexc-parity-result");
    var button = root && root.querySelector("#real-mexc-parity");
    if (result) { result.hidden = true; result.replaceChildren(); }
    if (button) button.disabled = false;
  }
  function clearMexcOpenOrdersSurvey() {
    mexcOpenOrdersEpoch += 1;
    var result = root && root.querySelector("#real-mexc-open-orders-result");
    var button = root && root.querySelector("#real-mexc-open-orders");
    if (result) { result.hidden = true; result.replaceChildren(); }
    if (button) button.disabled = false;
  }
  function checkMexcOpenOrdersSurvey(event) {
    if (!accountId || !token) return;
    var selected = accountId, epoch = ++mexcOpenOrdersEpoch;
    var button = event.currentTarget;
    var result = root.querySelector("#real-mexc-open-orders-result");
    var requestId = button.dataset.requestId || uuid();
    button.dataset.requestId = requestId;
    button.disabled = true;
    result.hidden = false;
    result.className = "real-parity-result loading";
    result.textContent = "Consultando órdenes Spot…";
    request("/accounts/" + encodeURIComponent(selected) + "/mexc-open-orders-survey/observations",
      { method: "POST", body: JSON.stringify({ request_id: requestId }) })
      .then(function (data) {
        if (epoch !== mexcOpenOrdersEpoch || selected !== accountId || !result.isConnected) return;
        delete button.dataset.requestId;
        renderMexcOpenOrdersSurvey(data, result, false);
        if (root.querySelector("#real-mexc-open-orders-history").open) loadMexcOpenOrdersHistory();
      }).catch(function (error) {
        if (epoch !== mexcOpenOrdersEpoch || selected !== accountId || !result.isConnected) return;
        result.className = "real-parity-result error";
        result.textContent = error.message;
      }).finally(function () { if (button.isConnected && epoch === mexcOpenOrdersEpoch) button.disabled = false; });
  }
  function renderMexcOpenOrdersSurvey(data, result, archived) {
    var historical = data.identity_current === false || data.recent === false;
    result.hidden = false;
    result.className = "real-parity-result blocked";
    result.innerHTML = '<div class="real-parity-head"><strong>' +
      (data.observed_open_orders ? "ÓRDENES OBSERVADAS" : "NINGUNA OBSERVADA") +
      '</strong><span>COBERTURA NO PROBADA · SIN ÓRDENES NUEVAS</span></div>' +
      '<small>' + (archived ? "Guardada" : "Guardada ahora") + ' · ' +
      esc(new Date(data.observed_at).toLocaleString()) +
      ' · MEXC no garantiza que la consulta sin par incluya todas las órdenes.</small>' +
      (historical ? '<small>' + (data.identity_current === false ? "Vínculo de cuenta cambiado." :
        "Lectura caducada.") + ' Repite la revisión.</small>' : '') +
      (data.symbols.length ? '<div class="real-parity-flags">' + data.symbols.map(function (item) {
        return '<span>' + esc(item.symbol) + ' · ' + esc(item.count) + '</span>';
      }).join("") + '</div>' : '');
  }
  function loadMexcOpenOrdersHistory() {
    var details = root && root.querySelector("#real-mexc-open-orders-history");
    if (!details || !details.open || !accountId || !token) return;
    var selected = accountId, epoch = ++mexcOpenOrdersHistoryEpoch;
    var target = details.querySelector("#real-mexc-open-orders-history-list");
    var compareButton = details.querySelector("#real-mexc-open-orders-compare");
    var comparison = details.querySelector("#real-mexc-open-orders-compare-result");
    target._reports = null;
    compareButton.hidden = true;
    comparison.hidden = true;
    comparison.replaceChildren();
    target.className = "state loading";
    target.textContent = "Leyendo órdenes archivadas…";
    request("/accounts/" + encodeURIComponent(selected) + "/mexc-open-orders-survey/observations", null, true)
      .then(function (reports) {
        if (epoch !== mexcOpenOrdersHistoryEpoch || selected !== accountId || !target.isConnected) return;
        target._reports = reports;
        compareButton.hidden = reports.length < 2;
        target.className = "real-parity-history-list";
        target.innerHTML = reports.length ? reports.slice(0, 10).map(function (item, index) {
          return '<button class="ghost" type="button" data-orders-index="' + index + '"><span>' +
            esc(new Date(item.observed_at).toLocaleString()) + ' · ' +
            esc(item.observed_open_orders) + ' observadas</span><small>' +
            (item.identity_current === false ? "CUENTA CAMBIADA" : item.recent ? "RECIENTE" : "CADUCADA") +
            ' · COBERTURA NO PROBADA</small></button>';
        }).join("") + (reports.length > 10 ? '<small>Últimas 10 de ' + esc(reports.length) +
          ' revisiones; API para historial completo.</small>' : '') :
          '<p class="muted">Todavía no hay revisiones guardadas.</p>';
      }).catch(function (error) {
        if (epoch !== mexcOpenOrdersHistoryEpoch || selected !== accountId || !target.isConnected) return;
        target.className = "state error";
        target.textContent = error.message;
      });
  }
  function compareMexcOpenOrdersHistory(event) {
    var selected = accountId, epoch = mexcOpenOrdersHistoryEpoch;
    var list = root.querySelector("#real-mexc-open-orders-history-list");
    var reports = list && list._reports;
    if (!selected || !reports || reports.length < 2) return;
    var button = event.currentTarget;
    var result = root.querySelector("#real-mexc-open-orders-compare-result");
    button.disabled = true;
    result.hidden = false;
    result.className = "real-parity-result loading";
    result.textContent = "Comparando revisiones cifradas…";
    request("/accounts/" + encodeURIComponent(selected) + "/mexc-open-orders-survey/observations/" +
      encodeURIComponent(reports[1].id) + "/compare/" + encodeURIComponent(reports[0].id), null, true)
      .then(function (data) {
        if (epoch !== mexcOpenOrdersHistoryEpoch || selected !== accountId || !result.isConnected) return;
        var counts = data.counts;
        result.className = "real-parity-result blocked";
        result.innerHTML = '<div class="real-parity-head"><strong>' +
          (data.status === "INCONSISTENT" ? "PROGRESO INCOHERENTE" :
            data.status === "CHANGES_OBSERVED" ? "CAMBIOS OBSERVADOS" : "SIN CAMBIOS OBSERVADOS") +
          '</strong><span>COBERTURA NO PROBADA · SIN CONCILIACIÓN</span></div>' +
          '<div class="real-parity-flags"><span>Aparecen ' + esc(counts.new) + '</span>' +
          '<span>Desaparecidas ' + esc(counts.disappeared) + '</span>' +
          '<span>Avance ' + esc(counts.progressed) + '</span>' +
          '<span>Incoherentes ' + esc(counts.inconsistent) + '</span></div>' +
          '<small>Una desaparición no demuestra ejecución ni cancelación. Requiere estado final y fills; ' +
          'la consulta sin par no garantiza cobertura completa.</small>';
      }).catch(function (error) {
        if (epoch !== mexcOpenOrdersHistoryEpoch || selected !== accountId || !result.isConnected) return;
        result.className = "real-parity-result error";
        result.textContent = error.message;
      }).finally(function () { if (button.isConnected && epoch === mexcOpenOrdersHistoryEpoch) button.disabled = false; });
  }
  function checkMexcParity(event) {
    if (!accountId || !token) return;
    var selected = accountId, epoch = ++mexcParityEpoch;
    var button = event.currentTarget;
    var result = root.querySelector("#real-mexc-parity-result");
    var requestId = button.dataset.requestId || uuid();
    button.dataset.requestId = requestId;
    button.disabled = true;
    result.hidden = false;
    result.className = "real-parity-result loading";
    result.textContent = "Comprobando dos lecturas Spot…";
    request("/accounts/" + encodeURIComponent(selected) + "/mexc-spot-parity/observations",
      { method: "POST", body: JSON.stringify({ request_id: requestId }) })
      .then(function (data) {
        if (epoch !== mexcParityEpoch || selected !== accountId || !result.isConnected) return;
        delete button.dataset.requestId;
        renderMexcParity(data, result, false);
        if (data.saved && root.querySelector("#real-mexc-parity-history").open) loadMexcParityHistory();
      }).catch(function (error) {
        if (epoch !== mexcParityEpoch || selected !== accountId || !result.isConnected) return;
        result.className = "real-parity-result error";
        result.textContent = error.message;
      }).finally(function () { if (button.isConnected && epoch === mexcParityEpoch) button.disabled = false; });
  }
  function renderMexcParity(data, result, archived) {
    var labels = { BOOK_CHANGED_DURING_CHECK: "Libro cambiado",
      MEXC_CHANGED_DURING_CHECK: "Saldo MEXC cambiado",
      NO_UNITS_TO_COMPARE: "Sin unidades", NEGATIVE_BOOK_CASH: "Efectivo negativo",
      QUANTITY_DIFFERENCE: "Unidades distintas", MEXC_LOCKED_FUNDS: "Fondos bloqueados" };
    var differences = data.assets.filter(function (item) { return !item.quantity_equal; });
    var comparable = data.book_stable && data.mexc_balances_stable;
    var historical = data.saved && (data.book_current === false || data.recent === false);
    result.hidden = false;
    result.className = "real-parity-result " + (data.blockers.length || historical ? "blocked" : "observed");
    result.innerHTML = '<div class="real-parity-head"><strong>' +
      (data.blockers.length ? "COMPROBACIÓN BLOQUEADA" : archived ? "OBSERVACIÓN GUARDADA" :
        historical ? "OBSERVACIÓN HISTÓRICA" :
        data.saved ? "EVIDENCIA GUARDADA" : "UNIDADES COINCIDEN") +
      '</strong><span>NO CONCILIADO · SIN ÓRDENES</span></div>' +
      '<small>Dos lecturas Spot · ' + esc(new Date(data.second_observed_at).toLocaleString()) +
      ' · no cubre órdenes o transferencias externas.</small>' +
      (historical ? '<small>' + (!data.book_current ? "El libro cambió." : "La observación caducó.") +
        ' Repite la comprobación para un estado actual.</small>' : '') +
      (data.blockers.length ? '<div class="real-parity-flags">' + data.blockers.map(function (code) {
        return '<span>' + esc(labels[code] || code) + '</span>';
      }).join("") + '</div>' : '') +
      (data.locked_assets.length ? '<p class="muted">Bloqueado: ' + data.locked_assets.map(function (item) {
        return esc(item.asset) + ' ' + show(item.locked);
      }).join(" · ") + '</p>' : '') +
      (!comparable ? '<small>Los datos cambiaron durante la comprobación. Repite la lectura para ver diferencias fiables.</small>' :
      differences.length ? '<div class="tablewrap"><table><thead><tr><th>Activo</th><th>Libro</th>' +
        '<th>MEXC</th><th>Diferencia</th></tr></thead><tbody>' + differences.slice(0, 30).map(function (item) {
          return '<tr><td>' + esc(item.asset) + '</td><td class="num">' + show(item.book_quantity) +
            '</td><td class="num">' + show(item.mexc_spot_quantity) + '</td><td class="num">' +
            show(item.difference_mexc_minus_book) + '</td></tr>';
        }).join("") + '</tbody></table></div>' +
        (differences.length > 30 ? '<small>Primeras 30 de ' + esc(differences.length) +
          ' diferencias; consulta la API para el informe completo.</small>' : '') : '');
  }
  function loadMexcParityHistory() {
    var details = root && root.querySelector("#real-mexc-parity-history");
    if (!details || !details.open || !accountId || !token) return;
    var selected = accountId, epoch = ++mexcParityHistoryEpoch;
    var target = details.querySelector("#real-mexc-parity-history-list");
    target._reports = null;
    target.className = "state loading";
    target.textContent = "Leyendo comprobaciones cifradas…";
    request("/accounts/" + encodeURIComponent(selected) + "/mexc-spot-parity/observations", null, true)
      .then(function (reports) {
        if (epoch !== mexcParityHistoryEpoch || selected !== accountId || !target.isConnected) return;
        target._reports = reports;
        target.className = "real-parity-history-list";
        target.innerHTML = reports.length ? reports.slice(0, 10).map(function (item, index) {
          return '<button class="ghost" type="button" data-parity-index="' + index + '"><span>' +
            esc(new Date(item.checked_at).toLocaleString()) + '</span><small>' +
            (!item.book_current ? "LIBRO CAMBIADO" : !item.recent ? "CADUCADA" : "RECIENTE") +
            ' · NO CONCILIADO</small></button>';
        }).join("") + (reports.length > 10 ? '<small>Últimas 10 de ' + esc(reports.length) +
          ' comprobaciones; API para historial completo.</small>' : '') :
          '<p class="muted">Todavía no hay comprobaciones guardadas.</p>';
      }).catch(function (error) {
        if (epoch !== mexcParityHistoryEpoch || selected !== accountId || !target.isConnected) return;
        target.className = "state error";
        target.textContent = error.message;
      });
  }
  function loadMexcPolicyDrafts() {
    var details = root && root.querySelector("#real-mexc-policy-drafts");
    if (!details || !details.open || !accountId || !token) return;
    var selected = accountId, epoch = ++mexcPolicyEpoch;
    var state = details.querySelector("#real-mexc-policy-state");
    var form = details.querySelector("#real-mexc-policy-form");
    var list = details.querySelector("#real-mexc-policy-list");
    state.className = "state loading";
    state.textContent = "Leyendo límites y versiones LAB…";
    form.hidden = true;
    Promise.all([
      request("/accounts/" + encodeURIComponent(selected) + "/mexc-policy-drafts", null, true),
      request("/accounts/" + encodeURIComponent(selected) + "/mexc-link", null, true),
      fetch("/lab/strategy-versions", { cache: "no-store", credentials: "same-origin",
        referrerPolicy: "no-referrer" }).then(function (response) {
        if (!response.ok) throw new Error("No se pudieron leer las estrategias LAB");
        return response.json();
      }).catch(function () { return []; }),
      request("/accounts/"+encodeURIComponent(selected)+"/mexc-bots/dca-versions",null,true).catch(function () { return []; })
    ]).then(function (results) {
      if (epoch !== mexcPolicyEpoch || selected !== accountId || !details.isConnected) return;
      var drafts = results[0], link = results[1], versions = results[2].filter(function (item) {
        return item.definition && item.definition.direction === "LONG";
      }).concat(results[3]);
      var names = {};
      versions.forEach(function (item) { names[item.id] = (item.kind === "DCA" ? "DCA · " : "") + item.name + " · v" + item.version; });
      var select = form.elements.strategy_version_id;
      var previousVersion = select.value;
      select.innerHTML = '<option value="">Selecciona versión LONG / DCA</option>' + versions.map(function (item) {
        return '<option value="' + esc(item.id) + '">' + esc(names[item.id]) + '</option>';
      }).join("");
      select.value = Object.hasOwn(names, previousVersion) ? previousVersion : "";
      var prepared = mexcLimitsFromSource;
      if (prepared && prepared.account === selected && Object.hasOwn(names, prepared.version)) {
        select.value = prepared.version;
        form.elements.symbol.value = prepared.base + prepared.quote;
        form.elements.base_asset.value = prepared.base;
        form.elements.quote_asset.value = prepared.quote;
        delete form.dataset.requestId;
        delete form.dataset.requestPayload;
        mexcLimitsFromSource = null;
      }
      list.innerHTML = drafts.length ? drafts.map(function (item) {
        return '<div class="real-policy-item"><div><strong>' + esc(item.symbol) + '</strong><span>' +
          esc(names[item.strategy_version_id] || "Versión LAB " + item.strategy_version_id.slice(0, 8)) +
          '</span></div><div class="real-policy-figures"><span>Orden ≤ ' + show(item.max_order_quote) +
          ' ' + esc(item.quote_asset) + '</span><span>Exposición ≤ ' + show(item.max_exposure_quote) +
          ' ' + esc(item.quote_asset) + '</span><span>Vence ' + esc(new Date(item.expires_at).toLocaleDateString()) +
          '</span></div><span class="statusbadge off">DESACTIVADO</span>' +
          '<div class="real-policy-risk"><button type="button" class="ghost" data-policy-risk="' + esc(item.id) +
          '" data-help="Compara volumen y P/L realizado del libro REAL con los límites. No incluye pérdidas latentes ni valida otros pares; no habilita órdenes.">Riesgo local</button>' +
          '<span class="muted" role="status" data-policy-risk-state>No consultado · sin conciliación</span>' +
          '<button type="button" class="ghost" data-policy-exposure="' + esc(item.id) +
          '" data-help="Valora todos los saldos Spot observados con precios MEXC recientes. Requiere par directo para cada activo; no concilia fills, costes ni habilita órdenes.">Exposición MEXC</button>' +
          '<span class="muted" role="status" data-policy-exposure-state>No consultada</span></div>' +
          '<form class="real-policy-test" data-policy-id="' + esc(item.id) + '">' +
          '<label class="field">Cantidad ' + esc(item.base_asset) +
          '<input name="quantity_base" type="text" inputmode="decimal" autocomplete="off" required></label>' +
          '<label class="field">Precio límite ' + esc(item.quote_asset) +
          '<input name="limit_price_quote" type="text" inputmode="decimal" autocomplete="off" required></label>' +
          '<button type="submit" class="ghost" data-help="Prueba una compra límite BUY contra MEXC /order/test con la clave Trade verificada. No crea una orden negociable ni reserva fondos. No valida una señal ni activa el borrador.">Probar BUY</button>' +
          '<span class="state muted" role="status">Solo prueba · sin ejecución</span></form>' +
          '<details class="real-policy-tests" data-policy-id="' + esc(item.id) + '"><summary data-help="Ensayos anteriores guardados en la bóveda cifrada. Un estado incierto no vuelve a llamar a MEXC al consultarlo.">Ensayos guardados</summary>' +
          '<div class="state muted">Abre para consultar.</div></details></div>';
      }).join("") : '<p class="muted">Sin borradores en esta cuenta.</p>';
      state.className = "state muted";
      var linkReady = link.linked && link.identity_status === "SAME_KEY";
      state.textContent = !mexcViewConnected ? "Conecta MEXC View para preparar otro borrador." :
        !linkReady ? "Confirma primero el vínculo MEXC View con esta cuenta REAL." :
        !versions.length ? "Crea una versión LONG o desbloquea DCA." :
        "Límites desactivados · sin Trade ni órdenes.";
      form.hidden = !mexcViewConnected || !linkReady || !versions.length;
    }).catch(function (error) {
      if (epoch !== mexcPolicyEpoch || selected !== accountId || !details.isConnected) return;
      state.className = "state error";
      state.textContent = error.message;
      list.replaceChildren();
    });
  }
  function inspectMexcDailyBookRisk(event) {
    var list = root && root.querySelector("#real-mexc-policy-list");
    var button = event.target.closest("button[data-policy-risk]");
    if (!list || !button || !list.contains(button) || !accountId || !token) return;
    var card = button.closest(".real-policy-item");
    var target = card && card.querySelector("[data-policy-risk-state]");
    if (!target) return;
    var selected = accountId, epoch = mexcPolicyEpoch;
    button.disabled = true;
    target.className = "muted";
    target.textContent = "Calculando libro REAL…";
    request("/accounts/" + encodeURIComponent(selected) + "/mexc-policy-drafts/" +
      encodeURIComponent(button.dataset.policyRisk) + "/daily-book-risk", null, true).then(function (risk) {
      if (epoch !== mexcPolicyEpoch || selected !== accountId || !target.isConnected) return;
      var reached = risk.book_turnover_limit_reached || risk.book_realized_loss_limit_reached;
      var origin = {
        LOCAL_FILL_CHAIN_MATCHES: "fills vinculados",
        DECLARED_OR_TRANSFERRED_COST: "coste declarado",
        UNLINKED_OR_CHANGED_TRADE: "coste manual",
        BOOK_EVENT_CHANGED: "libro alterado",
        NO_POSITION_HISTORY: "sin historial",
        ACCOUNT_LINK_MISSING: "sin vínculo"
      }[risk.cost_provenance] || "origen no verificado";
      var averageText = risk.average_cost_quote_per_base == null ? "— (sin posición)" :
        risk.average_cost_quote_per_base + " " + risk.quote_asset;
      target.className = reached ? "amber" : "muted";
      target.textContent = (reached ? "LÍMITE LOCAL ALCANZADO · " : "LIBRO REAL · ") +
        (hiddenAmounts ? "••••" : risk.turnover_quote + "/" + risk.max_daily_turnover_quote) +
        " " + risk.quote_asset + " volumen · P/L realizado " +
        (hiddenAmounts ? "••••" : risk.realized_pnl_quote + " / límite -" + risk.max_daily_loss_quote) +
        " · " + risk.base_asset + " " + (hiddenAmounts ? "••••" : risk.position_quantity_base) +
        " · PM " + (hiddenAmounts ? "••••" : averageText) +
        " · " + origin + " · sin conciliar";
    }).catch(function (error) {
      if (epoch !== mexcPolicyEpoch || selected !== accountId || !target.isConnected) return;
      target.className = "state error";
      target.textContent = error.message;
    }).finally(function () {
      if (button.isConnected) button.disabled = false;
    });
  }
  function inspectMexcSpotExposure(event) {
    var list = root && root.querySelector("#real-mexc-policy-list");
    var button = event.target.closest("button[data-policy-exposure]");
    if (!list || !button || !list.contains(button) || !accountId || !token) return;
    var card = button.closest(".real-policy-item");
    var target = card && card.querySelector("[data-policy-exposure-state]");
    if (!target) return;
    var selected = accountId, epoch = mexcPolicyEpoch;
    button.disabled = true;
    target.className = "muted";
    target.textContent = "Leyendo saldos y precios…";
    request("/accounts/" + encodeURIComponent(selected) + "/mexc-policy-drafts/" +
      encodeURIComponent(button.dataset.policyExposure) + "/spot-exposure", null, true).then(function (result) {
      if (epoch !== mexcPolicyEpoch || selected !== accountId || !target.isConnected) return;
      target.className = result.base_limit_reached ? "amber" : "muted";
      target.textContent = (result.base_limit_reached ? "LÍMITE BASE ALCANZADO · " : "SPOT OBSERVADO · ") +
        result.base_asset + " " + (hiddenAmounts ? "••••" : result.base_exposure_quote + "/" + result.max_base_exposure_quote) +
        " " + result.quote_asset + " · otros activos " +
        (hiddenAmounts ? "••••" : result.other_assets_exposure_quote) + " " + result.quote_asset +
        " · sin conciliar";
    }).catch(function (error) {
      if (epoch !== mexcPolicyEpoch || selected !== accountId || !target.isConnected) return;
      target.className = "state error";
      target.textContent = error.message;
    }).finally(function () {
      if (button.isConnected) button.disabled = false;
    });
  }
  function loadMexcOrderTests(details) {
    var selected = accountId, policyId = details.dataset.policyId;
    var target = details.querySelector("div");
    if (!selected || !token || !policyId || !target) return;
    target.className = "state loading";
    target.textContent = "Leyendo ensayos…";
    request("/accounts/" + encodeURIComponent(selected) + "/mexc-policy-drafts/" +
      encodeURIComponent(policyId) + "/order-tests", null, true).then(function (rows) {
        if (!details.isConnected || selected !== accountId || !details.open) return;
        target.className = "real-policy-test-history";
        target.innerHTML = rows.length ? rows.map(function (row) {
          var status = row.status === "ACCEPTED" ? "ACEPTADA · SIN EJECUCIÓN" :
            row.status === "CLAIMED" ? "ESTADO INCIERTO" : "NO CONFIRMADA";
          return '<div class="real-policy-test-record"><time>' +
            esc(new Date(row.created_at).toLocaleString()) + '</time><span>' +
            esc(status) + '</span><small>' + show(row.quantity_base) + ' × ' +
            show(row.limit_price_quote) + '</small></div>';
        }).join("") : '<span class="state muted">Sin ensayos guardados.</span>';
      }).catch(function (error) {
        if (!details.isConnected || selected !== accountId || !details.open) return;
        target.className = "state error";
        target.textContent = error.message;
      });
  }
  function testMexcDraftOrder(event) {
    var form = event.target.closest("form.real-policy-test");
    if (!form || !event.currentTarget.contains(form)) return;
    event.preventDefault();
    var selected = accountId, policyId = form.dataset.policyId, epoch = mexcPolicyEpoch;
    if (!selected || !token || !policyId) return;
    var state = form.querySelector('[role="status"]');
    var button = form.querySelector('button[type="submit"]');
    var quantity = String(new FormData(form).get("quantity_base")).trim();
    var price = String(new FormData(form).get("limit_price_quote")).trim();
    var fingerprint = quantity + "|" + price;
    if (form.dataset.testFingerprint !== fingerprint) {
      form.dataset.testRequestId = uuid();
      form.dataset.testFingerprint = fingerprint;
    }
    button.disabled = true;
    state.className = "state loading";
    state.textContent = "Probando en MEXC…";
    request("/accounts/" + encodeURIComponent(selected) + "/mexc-policy-drafts/" +
      encodeURIComponent(policyId) + "/order-test", {
        method: "POST", body: JSON.stringify({
          request_id: form.dataset.testRequestId,
          quantity_base: quantity,
          limit_price_quote: price,
          confirmation: "PROBAR_SIN_EJECUTAR"
        })
      }).then(function (result) {
        if (!form.isConnected || selected !== accountId || epoch !== mexcPolicyEpoch) return;
        state.className = result.status === "ACCEPTED" ? "state ok" : "state error";
        state.textContent = result.status === "ACCEPTED" && !result.executed ?
          "MEXC aceptó la prueba · ninguna orden ejecutada · guardada." :
          "Ensayo guardado con estado incierto; no se repite el envío.";
        if (result.status === "ACCEPTED") {
          delete form.dataset.testRequestId;
          delete form.dataset.testFingerprint;
        }
        var history = form.parentElement.querySelector("details.real-policy-tests");
        if (history && history.open) loadMexcOrderTests(history);
      }).catch(function (error) {
        if (!form.isConnected || selected !== accountId || epoch !== mexcPolicyEpoch) return;
        state.className = "state error";
        state.textContent = error.message;
      }).finally(function () { if (button.isConnected) button.disabled = false; });
  }
  function saveMexcPolicyDraft(event) {
    event.preventDefault();
    var form = event.currentTarget, selected = accountId;
    if (!selected || !token || !mexcViewConnected) return;
    var fields = new FormData(form);
    var symbol = String(fields.get("symbol")).trim().toUpperCase();
    var base = String(fields.get("base_asset")).trim().toUpperCase();
    var quote = String(fields.get("quote_asset")).trim().toUpperCase();
    var state = root.querySelector("#real-mexc-policy-state");
    if (symbol !== base + quote || base === quote) {
      state.className = "state error";
      state.textContent = "El par debe coincidir con activo + divisa.";
      return;
    }
    var days = Number(fields.get("valid_days"));
    var requestId = form.dataset.requestId || uuid();
    var body = form.dataset.requestPayload ? JSON.parse(form.dataset.requestPayload) : { request_id: requestId,
      strategy_version_id: String(fields.get("strategy_version_id")),
      symbol: symbol, base_asset: base, quote_asset: quote,
      max_order_quote: String(fields.get("max_order_quote")).trim(),
      max_daily_turnover_quote: String(fields.get("max_daily_turnover_quote")).trim(),
      max_exposure_quote: String(fields.get("max_exposure_quote")).trim(),
      max_daily_loss_quote: String(fields.get("max_daily_loss_quote")).trim(),
      max_fee_bps: String(fields.get("max_fee_bps")).trim(),
      max_slippage_bps: String(fields.get("max_slippage_bps")).trim(),
      max_data_age_seconds: Number(fields.get("max_data_age_seconds")),
      expires_at: new Date(Date.now() + days * 86400000).toISOString() };
    form.dataset.requestId = requestId;
    form.dataset.requestPayload = JSON.stringify(body);
    var button = form.querySelector('button[type="submit"]');
    button.disabled = true;
    state.className = "state loading";
    state.textContent = "Guardando borrador desactivado…";
    request("/accounts/" + encodeURIComponent(selected) + "/mexc-policy-drafts",
      { method: "POST", body: JSON.stringify(body) }).then(function () {
      if (!form.isConnected || selected !== accountId) return;
      delete form.dataset.requestId;
      delete form.dataset.requestPayload;
      loadMexcPolicyDrafts();
      if (window.OrbisMexcBots) window.OrbisMexcBots.refresh();
      notice("Límites guardados como borrador desactivado. No se envían órdenes.", false);
    }).catch(function (error) {
      if (!form.isConnected || selected !== accountId) return;
      state.className = "state error";
      state.textContent = error.message;
    }).finally(function () { if (button.isConnected) button.disabled = false; });
  }
  function clearMexcIdentity() {
    mexcIdentityEpoch += 1;
    var details = root && root.querySelector("#real-mexc-identity");
    var result = root && root.querySelector("#real-mexc-identity-result");
    var button = root && root.querySelector("#real-mexc-identity-check");
    if (details) details.open = false;
    if (result) { result.className = "state muted"; result.textContent = "Sin comprobar en esta sesión."; }
    if (button) button.disabled = false;
  }
  function checkMexcIdentity(event) {
    var result = root.querySelector("#real-mexc-identity-result");
    var button = event.currentTarget;
    var epoch = ++mexcIdentityEpoch;
    button.disabled = true;
    result.className = "state loading";
    result.textContent = "Comprobando identidad…";
    request("/exchanges/mexc/identity").then(function (data) {
      if (!result.isConnected || epoch !== mexcIdentityEpoch) return;
      var labels = {
        SAME_KEY: "Clave conocida localmente.",
        ROTATED_KEY_MATCH: "Clave rotada de una cuenta conocida · revisar antes de operar.",
        KEY_UID_CHANGED: "UID distinto para esta clave · no operar.",
        AMBIGUOUS_LOCAL_IDENTITY: "Identidad local ambigua · revisar la bóveda.",
        NOT_RECORDED: "Sin huella local previa de esta cuenta."
      };
      result.className = "state " + (data.local_identity_status === "ROTATED_KEY_MATCH" ||
        data.local_identity_status === "KEY_UID_CHANGED" ||
        data.local_identity_status === "AMBIGUOUS_LOCAL_IDENTITY" ? "amber" : "muted");
      result.textContent = (labels[data.local_identity_status] || "Identidad no verificable.") +
        (data.local_stopped ? " Parada local activa." : "") +
        (data.unresolved_intents ? " " + data.unresolved_intents + " intento(s) pendientes." : "") +
        " UID oculto · Trade sin verificar · sin órdenes.";
    }).catch(function (error) {
      if (!result.isConnected || epoch !== mexcIdentityEpoch) return;
      result.className = "state error";
      result.textContent = error.message;
    }).finally(function () { if (button.isConnected && epoch === mexcIdentityEpoch) button.disabled = false; });
  }
  function clearMexcComparison() {
    mexcCompareEpoch += 1;
    var result = root && root.querySelector("#real-mexc-comparison");
    if (result) { result.hidden = true; result.replaceChildren(); }
    var button = root && root.querySelector("#real-mexc-compare");
    if (button) button.disabled = false;
  }
  function compareMexc(event) {
    if (!accountId) return;
    var selectedAccount = accountId;
    var epoch = ++mexcCompareEpoch;
    var result = root.querySelector("#real-mexc-comparison");
    var button = event.currentTarget;
    result.hidden = false;
    result.className = "state loading";
    result.textContent = "Comparando unidades…";
    button.disabled = true;
    request("/accounts/" + encodeURIComponent(selectedAccount) + "/mexc-spot-comparison")
      .then(function (data) {
        if (!result.isConnected || epoch !== mexcCompareEpoch || accountId !== selectedAccount) return;
        var rows = data.assets.map(function (item) {
          return '<tr><td>' + esc(item.asset) + '</td><td class="num">' + show(item.book_quantity) +
            '</td><td class="num">' + show(item.mexc_spot_quantity) + '</td><td class="num">' +
            show(item.difference_mexc_minus_book) + '</td><td>' +
            (item.quantity_equal ? 'Igual' : 'Diferente') + '</td></tr>';
        }).join("");
        result.className = "";
        result.innerHTML = '<p class="muted">Cuenta REAL seleccionada · MEXC Spot · ' +
          esc(new Date(data.observed_at).toLocaleString()) + '</p>' +
          (data.local_account_link === "LOCAL_ACCOUNT_LINK" ?
            '<p class="muted">Referencia y UID coinciden con la asignación local; titularidad no verificada.</p>' : '') +
          (data.local_account_link === "LOCAL_ACCOUNT_LINK_UID_UNVERIFIED" ?
            '<p class="amber">Asignación local antigua sin UID registrado; identidad no comprobada.</p>' : '') +
          '<p class="amber">Diagnóstico de unidades; vínculo de cuenta no verificado. Sin asientos ni órdenes.</p>' +
          (data.negative_book_cash_assets.length ? '<p class="amber">Efectivo negativo en libro: ' +
            esc(data.negative_book_cash_assets.join(", ")) + '.</p>' : '') +
          (rows ? '<div class="tablewrap"><table><thead><tr><th>Activo</th><th>Libro</th>' +
            '<th>MEXC</th><th>MEXC − libro</th><th>Unidades</th></tr></thead><tbody>' + rows +
            '</tbody></table></div>' : '<p class="muted">Sin unidades en el libro ni en MEXC.</p>');
      }).catch(function (error) {
        if (!result.isConnected || epoch !== mexcCompareEpoch || accountId !== selectedAccount) return;
        result.className = "state error";
        result.textContent = error.message;
      }).finally(function () { if (button.isConnected && epoch === mexcCompareEpoch) button.disabled = false; });
  }
  function clearMexcOpening() {
    mexcOpeningEpoch += 1;
    mexcOpeningDraft = null;
    var details = root && root.querySelector("#real-mexc-opening");
    if (!details) return;
    details.open = false;
    var fields = details.querySelector("#real-mexc-opening-fields");
    var preview = details.querySelector("#real-mexc-opening-preview");
    fields.hidden = true;
    fields.replaceChildren();
    preview.hidden = true;
    preview.replaceChildren();
    details.querySelector("#real-mexc-opening-load").disabled = false;
  }
  function openingBody(form) {
    var quote = form.elements.quote_currency.value;
    var costs = {};
    form.querySelectorAll("input[data-opening-asset]").forEach(function (input) {
      if (input.dataset.openingAsset !== quote) costs[input.dataset.openingAsset] = input.value.trim();
    });
    return { quote_currency: quote, costs: costs, costs_confirmed: true };
  }
  function loadMexcOpening(event) {
    if (!accountId) return;
    var button = event.currentTarget, selectedAccount = accountId;
    var details = root.querySelector("#real-mexc-opening");
    var fields = details.querySelector("#real-mexc-opening-fields");
    var preview = details.querySelector("#real-mexc-opening-preview");
    var epoch = ++mexcOpeningEpoch;
    mexcOpeningDraft = null;
    button.disabled = true;
    fields.hidden = true;
    fields.replaceChildren();
    preview.hidden = false;
    preview.className = "state loading";
    preview.textContent = "Leyendo saldos MEXC…";
    request("/exchanges/mexc/balances").then(function (data) {
      if (epoch !== mexcOpeningEpoch || accountId !== selectedAccount || !details.isConnected) return;
      var balances = data.balances || [];
      if (!balances.length) throw new Error("MEXC no muestra saldo Spot para registrar.");
      var defaultQuote = "USDT";
      var quotes = ["USDT", "USDC", "EUR", "USD", "DAI", "FDUSD"];
      fields.hidden = false;
      fields.innerHTML = '<form id="real-mexc-opening-form"><label class="field">Divisa de referencia<select name="quote_currency">' +
        quotes.map(function (quote) { return '<option value="' + quote + '"' + (quote === defaultQuote ? ' selected' : '') + '>' + quote + '</option>'; }).join("") +
        '</select></label><div class="tablewrap"><table><thead><tr><th>Activo</th><th>Saldo MEXC</th><th>Coste declarado por unidad</th></tr></thead><tbody>' +
        balances.map(function (item) { return '<tr><th scope="row">' + esc(item.asset) + '</th><td class="num">' + show(item.total) +
          '</td><td><input data-opening-asset="' + esc(item.asset) + '" type="text" inputmode="decimal" autocomplete="off"' +
          (item.asset === defaultQuote ? ' disabled placeholder="Efectivo"' : ' required placeholder="Coste en ' + esc(defaultQuote) + '"') +
          ' aria-label="Coste declarado de ' + esc(item.asset) + '"></td></tr>'; }).join("") +
        '</tbody></table></div><p class="muted">Introduce el coste real conocido, incluso 0 si fue gratuito. La fecha será la observación, no la de adquisición.</p>' +
        '<label><input type="checkbox" required name="costs_ack"> Conozco los costes declarados; 0 no significa desconocido.</label>' +
        '<button type="submit" class="ghost">Vista previa</button></form>';
      var form = fields.querySelector("form");
      form.elements.quote_currency.addEventListener("change", function () {
        var quote = this.value;
        form.querySelectorAll("input[data-opening-asset]").forEach(function (input) {
          input.disabled = input.dataset.openingAsset === quote;
          input.required = !input.disabled;
          input.placeholder = input.disabled ? "Efectivo" : "Coste en " + quote;
        });
      });
      form.addEventListener("input", function () {
        mexcOpeningEpoch += 1;
        mexcOpeningDraft = null;
        preview.hidden = true;
        preview.replaceChildren();
      });
      form.addEventListener("submit", previewMexcOpening);
      preview.hidden = false;
      preview.className = "state muted";
      preview.textContent = "Comprueba costes y pulsa Vista previa.";
    }).catch(function (error) {
      if (epoch !== mexcOpeningEpoch || accountId !== selectedAccount || !details.isConnected) return;
      preview.hidden = false;
      preview.className = "state error";
      preview.textContent = error.message;
    }).finally(function () { if (button.isConnected && epoch === mexcOpeningEpoch) button.disabled = false; });
  }
  function previewMexcOpening(event) {
    event.preventDefault();
    var form = event.currentTarget, selectedAccount = accountId;
    if (!form.reportValidity() || !selectedAccount) return;
    var details = root.querySelector("#real-mexc-opening");
    var output = details.querySelector("#real-mexc-opening-preview");
    var body = openingBody(form), epoch = ++mexcOpeningEpoch;
    mexcOpeningDraft = null;
    output.hidden = false;
    output.className = "state loading";
    output.textContent = "Validando fotografía y libro…";
    request("/accounts/" + encodeURIComponent(selectedAccount) + "/mexc-opening/preview", {
      method: "POST", body: JSON.stringify(body)
    }).then(function (data) {
      if (epoch !== mexcOpeningEpoch || selectedAccount !== accountId || !output.isConnected) return;
      mexcOpeningDraft = {account: selectedAccount, body: body, preview: data};
      output.className = "";
      output.innerHTML = '<p class="amber">Costes declarados por ti, no verificados por MEXC. No se habilitan órdenes ni se concilia la cuenta.</p>' +
        '<p class="muted">Observado ' + esc(new Date(data.observed_at).toLocaleString()) +
        ' · ' + esc(data.lines.length) + ' activo(s) · cuenta REAL vacía.</p>' +
        '<div class="tablewrap"><table><thead><tr><th>Activo</th><th>Unidades</th><th>Coste por unidad</th><th>Coste total declarado</th></tr></thead><tbody>' +
        data.lines.map(function (item) { return '<tr><th scope="row">' + esc(item.asset) + '</th><td class="num">' + show(item.quantity) +
          '</td><td class="num">' + (item.declared_unit_cost === null ? 'Efectivo' : show(item.declared_unit_cost)) +
          '</td><td class="num">' + (item.declared_total_cost === null ? '—' : show(item.declared_total_cost)) + '</td></tr>'; }).join("") +
        '</tbody></table></div><form id="real-mexc-opening-commit"><label class="field">Escribe IMPORTAR_SALDO_INICIAL' +
        '<input name="confirmation" required pattern="IMPORTAR_SALDO_INICIAL" autocomplete="off" spellcheck="false"></label>' +
        '<button type="submit" class="ghost">Registrar inventario</button>' +
        '<p class="state muted" data-opening-commit-state role="status"></p></form>';
      output.querySelector("form").addEventListener("submit", commitMexcOpening);
    }).catch(function (error) {
      if (epoch !== mexcOpeningEpoch || selectedAccount !== accountId || !output.isConnected) return;
      output.className = "state error";
      output.textContent = error.message;
    });
  }
  function commitMexcOpening(event) {
    event.preventDefault();
    var form = event.currentTarget, draft = mexcOpeningDraft;
    var button = form.querySelector('button[type="submit"]');
    if (!draft || button.disabled || !form.reportValidity() || draft.account !== accountId) return;
    var details = root.querySelector("#real-mexc-opening");
    var fields = details.querySelector("#real-mexc-opening-fields");
    var output = details.querySelector("#real-mexc-opening-preview");
    var status = form.querySelector("[data-opening-commit-state]");
    var read = details.querySelector("#real-mexc-opening-load");
    var epoch = ++mexcOpeningEpoch;
    if (!draft.commitBody) draft.commitBody = JSON.stringify(Object.assign({}, draft.body, {
      observed_at: draft.preview.occurred_at,
      preview_hash: draft.preview.preview_hash,
      confirmation: form.elements.confirmation.value
    }));
    button.disabled = true;
    read.disabled = true;
    form.elements.confirmation.readOnly = true;
    fields.hidden = true;
    status.className = "state muted";
    status.textContent = "Registrando inventario…";
    function current() {
      return epoch === mexcOpeningEpoch && draft.account === accountId && form.isConnected;
    }
    request("/accounts/" + encodeURIComponent(draft.account) + "/mexc-opening/commit", {
      method: "POST", body: draft.commitBody
    }).then(function (data) {
      if (!current()) return;
      clearMexcComparison();
      clearMexcOpening();
      notice(data.counts.new ? "Inventario inicial registrado: " + data.counts.new + " asiento(s)." :
        "Inventario confirmado: " + data.counts.already_present + " asiento(s) ya registrados.", false);
      return refresh();
    }).catch(function (error) {
      if (!current()) return;
      if (!error.status || error.status >= 500) {
        status.className = "state error";
        status.textContent = "Respuesta sin confirmar. Reintenta el mismo registro.";
        button.textContent = "Reintentar registro";
        button.disabled = false;
        return;
      }
      mexcOpeningDraft = null;
      output.className = "state error";
      output.textContent = error.message + " Genera otra vista previa.";
      fields.hidden = false;
      read.disabled = false;
    });
  }
  function queryMexcTransfers(event) {
    var button = event.currentTarget;
    var result = root.querySelector("#real-mexc-transfer-result");
    var days = root.querySelector("#real-mexc-transfer-days").value;
    var currentEpoch = ++mexcTransferEpoch;
    result.hidden = false;
    result.className = "state loading";
    result.textContent = "Consultando hasta " + days + " días…";
    button.disabled = true;
    request("/exchanges/mexc/transfers?days=" + encodeURIComponent(days)).then(function (data) {
      if (!result.isConnected || currentEpoch !== mexcTransferEpoch) return;
      result.className = "";
      var from = new Date(data.start_ms).toISOString().slice(0, 10);
      var to = new Date(data.end_ms).toISOString().slice(0, 10);
      result.innerHTML = mexcTransferTable(data, 'MEXC · ' + from + ' → ' + to + ' · ' + data.requests +
        ' consultas · ' + (data.identity_verified ? 'UID View verificado' : 'UID View sin vínculo local') +
        ' · instantánea cifrada, no contabilizada · sin direcciones.');
      showMexcExternalDeposits(result, data, data.archive.id);
      var auditResult = root.querySelector("#real-mexc-transfer-audit-result");
      if (auditResult) auditResult.hidden = true;
      var archive = root.querySelector("#real-mexc-transfers-archive");
      if (archive && archive.open) loadMexcTransferArchive();
    }).catch(function (error) {
      if (!result.isConnected || currentEpoch !== mexcTransferEpoch) return;
      result.className = "state error";
      result.textContent = error.message;
    }).finally(function () { if (button.isConnected) button.disabled = false; });
  }

  function showMexcExternalDeposits(container, data, snapshotId) {
    if (!data.identity_verified || !snapshotId) return;
    var eligible = data.deposits.filter(function (row) {
      return row.asset === "USDT" && row.status === 5 && row.network && row.source_ref && /^0+(?:\.0+)?$/.test(row.fee);
    }).slice(0, 100);
    if (!eligible.length) return;
    var panel = document.createElement("details");
    panel.innerHTML = '<summary>Registrar ingresos USDT</summary><form class="real-form">' +
      '<div class="tablewrap"><table><thead><tr><th>Elegir</th><th>USDT</th><th>Red</th><th>Fecha</th></tr></thead><tbody>' +
      eligible.map(function (row, index) {
        return '<tr><td><input type="checkbox" name="source" value="' + index +
          '" aria-label="Ingreso USDT ' + (index + 1) + '"></td><td class="num">' + show(row.amount) +
          '</td><td>' + esc(row.network) + '</td><td>' + esc(new Date(row.time_ms).toLocaleString()) + '</td></tr>';
      }).join("") + '</tbody></table></div><button type="submit" class="ghost">Revisar ingresos</button>' +
      '<div class="state muted" role="status" data-external-review hidden></div></form>';
    container.append(panel);
    var form = panel.querySelector("form"), output = panel.querySelector("[data-external-review]");
    var review = form.querySelector('button[type="submit"]');
    var revision = 0;
    form.addEventListener("change", function (event) {
      if (event.target.name !== "source") return;
      revision += 1;
      review.disabled = false;
      output.hidden = true;
      output.replaceChildren();
    });
    form.addEventListener("submit", async function (event) {
      event.preventDefault();
      var selected = accountId, ticket = ++revision;
      var refs = Array.from(form.querySelectorAll('[name="source"]:checked'), function (input) {
        return eligible[Number(input.value)].source_ref;
      });
      function current() { return panel.isConnected && selected === accountId && ticket === revision; }
      function status(message, error) {
        output.className = "state " + (error ? "error" : "muted");
        output.textContent = message;
      }
      output.hidden = false;
      if (!selected || !refs.length || refs.length > 20) { status("Selecciona de uno a veinte ingresos.", true); return; }
      var path = "/accounts/" + encodeURIComponent(selected) + "/mexc-external-deposits/";
      var body = {snapshot_id:snapshotId, source_refs:refs};
      review.disabled = true;
      status("Comprobando saldo…", false);
      try {
        var result = await request(path + "preview", {method:"POST", body:JSON.stringify(body)});
        if (!current()) return;
        if (result.status === "ALREADY_RECORDED") { status("Ingresos ya registrados.", false); return; }
        output.innerHTML = '<p>Saldo USDT · ' + show(result.book_before) + ' → ' + show(result.book_after) + '</p>' +
          '<p>Aportación · ' + show(result.amount) + ' USDT</p>' +
          '<label><input type="checkbox" name="external"> Fondos fuera de ORBIS</label> ' +
          '<button type="button" data-external-commit disabled>Registrar aportación</button>' +
          '<span role="status" data-external-status></span>';
        var confirm = output.querySelector('[name="external"]'), submit = output.querySelector('[data-external-commit]');
        var feedback = output.querySelector('[data-external-status]');
        confirm.addEventListener("change", function () { submit.disabled = !confirm.checked; });
        submit.addEventListener("click", async function () {
          if (!current() || !confirm.checked || submit.disabled) return;
          submit.disabled = true;
          review.disabled = true;
          confirm.disabled = true;
          form.querySelectorAll('[name="source"]').forEach(function (input) { input.disabled = true; });
          feedback.textContent = "Comprobando MEXC…";
          try {
            var saved = await request(path + "commit", {method:"POST", body:JSON.stringify(Object.assign({}, body, {
              preview_hash:result.preview_hash, confirmation:"REGISTRAR INGRESOS EXTERNOS"
            }))});
            if (!current()) return;
            status(saved.status === "RECORDED" ? "Aportación registrada." : "Aportación ya registrada.", false);
            form.querySelectorAll('[name="source"]:checked').forEach(function (input) { input.checked = false; });
            await refresh();
          } catch (error) {
            if (!current()) return;
            if (!error.status || error.status >= 500) {
              feedback.textContent = "Respuesta sin confirmar. Reintenta el mismo registro.";
              submit.textContent = "Reintentar registro";
              submit.disabled = false;
              return;
            }
            status(error.message, true);
          } finally {
            if (current() && !output.querySelector('[data-external-commit]')) {
              review.disabled = false;
              form.querySelectorAll('[name="source"]').forEach(function (input) { input.disabled = false; });
            }
          }
        });
      } catch (error) {
        if (current()) status(error.message, true);
      } finally {
        if (current()) review.disabled = false;
      }
    });
  }
  function mexcTransferTable(data, caption) {
      var rows = data.deposits.concat(data.withdrawals).sort(function (a, b) { return b.time_ms - a.time_ms; });
      var depositStates = {1:"Pequeño", 2:"Demora", 3:"Demora", 4:"Pendiente", 5:"Confirmado", 6:"En revisión", 7:"Rechazado", 8:"Reembolso", 9:"Preconfirmado", 10:"Inválido", 11:"Restringido", 12:"Completado"};
      var withdrawalStates = {1:"Solicitado", 2:"En revisión", 3:"En espera", 4:"Procesando", 5:"Preparando", 6:"Confirmando", 7:"Confirmado", 8:"Fallido", 9:"Cancelado", 10:"Manual"};
      return '<p class="muted">' + esc(caption) + '</p>' +
        (rows.length ? '<div class="tablewrap"><table><thead><tr><th>UTC</th><th>Tipo</th><th>Activo</th><th>Red</th><th>Importe</th><th>Comisión</th><th>Estado MEXC</th><th data-help="Huella local: permite reconocer la misma referencia en otra consulta. No acredita recepción ni titularidad.">Ref.</th></tr></thead><tbody>' +
          rows.map(function (item) { return '<tr><td>' + esc(new Date(item.time_ms).toISOString().slice(0, 16).replace("T", " ")) +
            '</td><td>' + (item.direction === "deposit" ? "Ingreso" : "Retiro") + '</td><td>' + esc(item.asset) +
            '</td><td>' + esc(item.network || "—") + '</td><td class="num">' + show(item.amount) +
            '</td><td class="num">' + show(item.fee) + '</td><td>' + esc((item.direction === "deposit" ? depositStates : withdrawalStates)[item.status] || ("Código " + item.status)) +
            '</td><td>' + (item.source_ref ? esc(item.source_ref.slice(0, 10)) + '…' : '—') + '</td></tr>'; }).join("") +
          '</tbody></table></div>' : '<p class="muted">Sin ingresos ni retiros en el rango consultado.</p>');
  }
  function loadMexcTransferAudit(event) {
    var button = event.currentTarget, selected = accountId;
    var output = root.querySelector("#real-mexc-transfer-audit-result");
    if (!selected || !output) return;
    button.disabled = true;
    output.hidden = false;
    output.className = "state loading";
    output.textContent = "Auditando capturas…";
    request("/accounts/" + encodeURIComponent(selected) + "/mexc-transfer-audit").then(function (report) {
      if (!output.isConnected || accountId !== selected) return;
      var counts = report.counts, snapshots = report.snapshots;
      output.className = "state";
      output.innerHTML = '<p class="amber">Auditoría de traslados · no concilia saldos ni habilita órdenes.</p>' +
        '<p data-help="La cobertura completa exige ±24 h alrededor del asiento REAL en capturas MEXC con UID verificado. Un asiento fuera de ella no se interpreta como ausente en MEXC.">' + esc(counts.confirmed_matches) + ' vinculados · ' +
        esc(counts.unmatched_final_references) + ' MEXC sin vínculo · ' +
        esc(counts.unlinked_local_events) + ' asientos REAL sin vínculo (' +
        esc(counts.unlinked_local_fully_covered) + ' con cobertura completa, ' +
        esc(counts.unlinked_local_partial_or_outside) + ' parcial/fuera).</p>' +
        '<p class="muted">' + esc(snapshots.verified) + '/' + esc(snapshots.total) +
        ' capturas con UID verificado · ' + esc(report.coverage_windows.length) + ' tramo(s) de cobertura' +
        (counts.conflicted_references ? ' · ' + esc(counts.conflicted_references) + ' referencias contradictorias' : '') +
        (counts.final_rows_without_reference ? ' · ' + esc(counts.final_rows_without_reference) + ' filas sin referencia' : '') +
        (counts.legacy_matches ? ' · ' + esc(counts.legacy_matches) + ' vínculos anteriores a verificación UID' : '') +
        '.</p>' +
        (report.coverage_windows.length ? '<p class="muted" data-help="Sólo intervalos solicitados de capturas verificadas; no demuestra un historial completo.">Rango verificado: ' +
          esc(new Date(report.coverage_windows[0].start_ms).toISOString().slice(0, 10)) + ' → ' +
          esc(new Date(report.coverage_windows[report.coverage_windows.length - 1].end_ms).toISOString().slice(0, 10)) +
          ' · ' + esc(report.coverage_windows.length) + ' tramo(s).</p>' : '');
    }).catch(function (error) {
      if (!output.isConnected || accountId !== selected) return;
      output.className = "state error";
      output.textContent = error.message;
    }).finally(function () { if (button.isConnected) button.disabled = false; });
  }
  function lookupMexcDepositTx(event) {
    event.preventDefault();
    var form = event.currentTarget, selected = accountId;
    var output = root.querySelector("#real-mexc-deposit-tx-result");
    if (!selected || !output || !form.reportValidity()) return;
    var txHash = form.elements.tx_hash.value.trim();
    form.elements.tx_hash.value = "";
    if (!/^0x[0-9a-fA-F]{64}$/.test(txHash)) {
      output.hidden = false;
      output.className = "state error";
      output.textContent = "Hash EVM inválido.";
      return;
    }
    var button = form.querySelector('button[type="submit"]');
    button.disabled = true;
    output.hidden = false;
    output.className = "state loading";
    output.textContent = "Buscando en capturas verificadas…";
    request("/accounts/" + encodeURIComponent(selected) + "/mexc-deposit-tx-lookup", {
      method: "POST", body: JSON.stringify({tx_hash: txHash})
    }).then(function (result) {
      if (!output.isConnected || accountId !== selected) return;
      output.className = "state";
      var label = result.status === "FINAL_MEXC_DEPOSIT" ? "Ingreso final observado en MEXC" :
        result.status === "NOT_FINAL_MEXC_DEPOSIT" ? "Ingreso todavía no final en MEXC" :
        result.status === "CONTRADICTORY_MEXC_DEPOSIT" ? "Capturas contradictorias; revisar" :
        "No consta en las capturas verificadas";
      var book = result.book_link_status === "VERIFIED_EXPLICIT_LINK" ? " · Asiento REAL vinculado y verificado" :
        result.book_link_status === "LEGACY_LINK_UNVERIFIED_IDENTITY" ? " · Vínculo antiguo sin identidad View verificada" :
        result.book_link_status === "INCONSISTENT_LINK" ? " · Vínculo REAL inconsistente" :
        result.book_link_status === "NO_EXPLICIT_LINK" ? " · Sin traslado REAL vinculado" : "";
      output.textContent = label + (result.asset ? " · " + result.amount + " " + result.asset +
        (result.network ? " · " + result.network : "") : "") + book +
        " · Sin prueba de control de wallet ni conciliación patrimonial.";
    }).catch(function (error) {
      if (!output.isConnected || accountId !== selected) return;
      output.className = "state error";
      output.textContent = error.message;
    }).finally(function () { if (button.isConnected) button.disabled = false; });
  }
  function loadMexcTransferArchive() {
    var list = root && root.querySelector("#real-mexc-transfers-list");
    var detail = root && root.querySelector("#real-mexc-transfers-detail");
    if (!list || !detail) return;
    list.className = "state loading";
    list.textContent = "Cargando instantáneas…";
    request("/exchanges/mexc/transfers/archive").then(function (items) {
      if (!list.isConnected) return;
      list.className = "";
      list.innerHTML = items.length ? '<p class="muted">Últimas 100 instantáneas; cada conexión se identifica por referencia local.</p>' + items.map(function (item) {
        return '<button class="ghost" type="button" data-id="' + esc(item.id) + '">' +
          esc(new Date(item.observed_at).toLocaleString()) + ' · conexión ' + esc(item.account_ref.slice(0, 8)) + '… · ' + esc(item.deposit_count) +
          ' ingresos · ' + esc(item.withdrawal_count) + ' retiros · ' +
          (item.identity_verified ? 'UID verificado' : 'UID sin verificar') + '</button>';
      }).join(" ") : '<p class="muted">Aún no hay instantáneas guardadas.</p>';
      list.querySelectorAll("button[data-id]").forEach(function (button, index) {
        button.addEventListener("click", function () {
          var selectedSnapshot = items[index];
          var previous = items.slice(index + 1).find(function (candidate) {
            return candidate.account_ref === selectedSnapshot.account_ref &&
              candidate.observed_at < selectedSnapshot.observed_at &&
              candidate.start_ms <= selectedSnapshot.end_ms &&
              selectedSnapshot.start_ms <= candidate.end_ms;
          });
          detail.hidden = false;
          detail.className = "state loading";
          detail.textContent = "Abriendo instantánea…";
          request("/exchanges/mexc/transfers/archive/" + encodeURIComponent(button.dataset.id)).then(function (data) {
            if (!detail.isConnected) return;
            detail.className = "";
            detail.innerHTML = mexcTransferTable(data, 'Observado ' + data.observed_at + ' · ' +
              new Date(data.start_ms).toISOString().slice(0, 10) + ' → ' +
              new Date(data.end_ms).toISOString().slice(0, 10) + ' · ' + data.requests +
              ' consultas · ' + (data.identity_verified ? 'UID View verificado' : 'UID View sin verificar') +
              ' · no contabilizado') +
              '<div class="row"><button id="real-mexc-transfer-compare" class="ghost" type="button" ' +
              'data-help="Busca sólo candidatos por activo, importe y ±24 h. Supone que la cuenta REAL seleccionada representa MEXC; no verifica titularidad, txid ni llegada de fondos.">Comparar con cuenta seleccionada</button>' +
              (previous ? '<button id="real-mexc-transfer-diff" class="ghost" type="button" ' +
                'data-help="Compara referencias seudónimas de dos consultas de la misma conexión y cobertura compartida. No concilia fondos.">Comparar capturas</button>' : '') + '</div>' +
              '<div id="real-mexc-transfer-diff-result" class="state muted" role="status" hidden></div>' +
              '<div id="real-mexc-transfer-candidates" class="state muted" role="status" hidden></div>';
            showMexcExternalDeposits(detail, data, data.id);
            if (previous) detail.querySelector("#real-mexc-transfer-diff").addEventListener("click", function (event) {
              var diffButton = event.currentTarget;
              var diffOutput = detail.querySelector("#real-mexc-transfer-diff-result");
              diffButton.disabled = true;
              diffOutput.hidden = false;
              diffOutput.className = "state loading";
              diffOutput.textContent = "Comparando capturas…";
              request("/exchanges/mexc/transfers/archive/" + encodeURIComponent(previous.id) +
                "/compare/" + encodeURIComponent(data.id)).then(function (report) {
                if (!diffOutput.isConnected) return;
                var labels = {status_changed:"Estado cambió", finality_regression:"Final → no final",
                  fields_changed:"Datos cambiaron", not_returned:"Ya no aparece",
                  newly_returned:"Nueva en consulta", ambiguous_reference:"Referencia repetida"};
                var count = report.counts;
                diffOutput.className = "state";
                diffOutput.innerHTML = '<p class="amber">Solo observación · cobertura compartida · sin conciliación.</p>' +
                  '<p>' + esc(count.unchanged) + ' sin cambio · ' +
                  esc(count.status_changed + count.finality_regression + count.fields_changed) + ' cambiadas · ' +
                  esc(count.not_returned) + ' ya no aparecen · ' +
                  esc(count.newly_returned) + ' nuevas · ' +
                  esc(count.ambiguous_reference) + ' ambiguas.</p>' +
                  (report.unlinked_before || report.unlinked_after ? '<p class="muted">Sin referencia: ' +
                    esc(report.unlinked_before) + ' antes, ' + esc(report.unlinked_after) +
                    ' después; no comparadas.</p>' : '') +
                  (report.changes.length ? '<div class="tablewrap"><table><thead><tr><th>Tipo</th><th>Ref.</th><th>Cambio</th><th>Antes</th><th>Después</th></tr></thead><tbody>' +
                    report.changes.slice(0, 100).map(function (row) {
                      return '<tr><td>' + (row.direction === "deposit" ? "Ingreso" : "Retiro") +
                        '</td><td>' + esc(row.source_ref.slice(0, 10)) + '…</td><td>' +
                        esc(labels[row.outcome] || row.outcome) + '</td><td>' +
                        esc(row.before ? row.before.status : "—") + '</td><td>' +
                        esc(row.after ? row.after.status : "—") + '</td></tr>';
                    }).join("") + '</tbody></table></div>' : '<p class="muted">Sin cambios identificables en filas con referencia.</p>') +
                  (report.truncated || report.changes.length > 100 ? '<p class="muted">Detalle parcial; la API conserva el recuento completo.</p>' : '');
              }).catch(function (error) {
                if (!diffOutput.isConnected) return;
                diffOutput.className = "state error";
                diffOutput.textContent = error.message;
              }).finally(function () { if (diffButton.isConnected) diffButton.disabled = false; });
            });
            detail.querySelector("#real-mexc-transfer-compare").addEventListener("click", function (event) {
              var selected = accountId, output = detail.querySelector("#real-mexc-transfer-candidates");
              var compareButton = event.currentTarget;
              if (!selected) return;
              compareButton.disabled = true;
              output.hidden = false;
              output.className = "state loading";
              output.textContent = "Comparando asientos…";
              request("/accounts/" + encodeURIComponent(selected) + "/mexc-transfer-candidates/" +
                encodeURIComponent(data.id)).then(function (comparison) {
                if (!output.isConnected || accountId !== selected) return;
                if (comparison.conclusion === "ACCOUNT_REFERENCE_CONFLICT_NO_COMPARISON") {
                  output.className = "state error";
                  output.textContent = "Referencia MEXC distinta del vínculo local de esta cuenta. No se compararon movimientos.";
                  return;
                }
                var labels = {POSSIBLE_MATCH:"Posible", AMBIGUOUS:"Ambiguo",
                  FEE_HYPOTHESIS_ONLY:"Comisión: importe bruto/neto sin verificar",
                  NON_FINAL_CANDIDATE_ONLY:"Solo movimientos MEXC no finales",
                  NON_EXTERNAL_WITHDRAWAL_ONLY:"Retiro interno o ruta desconocida · sin comparación wallet",
                  NO_EXACT_CANDIDATE:"Sin candidato exacto", OUTSIDE_COVERAGE:"Fuera de cobertura",
                  PARTIAL_WINDOW_NO_CANDIDATE:"Ventana parcial"};
                var localRows = comparison.rows.slice().sort(function (a, b) {
                  return (a.result === "POSSIBLE_MATCH" ? 1 : 0) -
                    (b.result === "POSSIBLE_MATCH" ? 1 : 0) ||
                    b.occurred_at.localeCompare(a.occurred_at);
                });
                var observed = (comparison.observed_rows || []).slice().sort(function (a, b) {
                  return (a.result === "NO_PAIRED_LOCAL_CANDIDATE" ? -1 : 0) -
                    (b.result === "NO_PAIRED_LOCAL_CANDIDATE" ? -1 : 0) ||
                    b.observed_at.localeCompare(a.observed_at);
                });
                output.className = "";
                output.innerHTML = '<p class="amber">Coincidencias orientativas · ' +
                  (comparison.account_reference === "LOCAL_LINK_MATCH_NOT_OWNERSHIP_PROOF" ?
                    "vínculo local coincidente, titularidad no verificada" : "cuenta MEXC no vinculada") +
                  (comparison.identity_verified ? '' : ' · captura sin UID verificado; vuelve a consultar MEXC para vincular') +
                  ' · sin conciliación ni asientos.</p><h4>Libro REAL → MEXC</h4>' +
                  (localRows.length ? '<p class="muted">' + esc(localRows.length) + ' asiento(s) · primeros 100, pendientes primero.</p>' +
                    '<div class="tablewrap"><table><thead><tr><th>Movimiento</th><th>Activo</th><th>Importe</th><th>UTC</th><th>Resultado</th></tr></thead><tbody>' +
                    localRows.slice(0, 100).map(function (row) {
                      var candidate = row.candidates.length === 1 ? row.candidates[0] : null;
                      var canLink = comparison.identity_verified &&
                        comparison.account_reference === "LOCAL_LINK_MATCH_NOT_OWNERSHIP_PROOF" &&
                        row.result === "POSSIBLE_MATCH" && !row.confirmed_match_id &&
                        candidate && candidate.source_ref;
                      return '<tr><td>' + (row.direction === "deposit" ? "Entrada" : "Salida") +
                        '</td><td>' + esc(row.asset) + '</td><td class="num">' + show(row.amount) +
                        '</td><td>' + esc(row.occurred_at.slice(0, 16).replace("T", " ")) +
                        '</td><td>' + (row.confirmed_match_id ? 'Vinculado · no conciliado' : esc(labels[row.result] || row.result)) +
                        (row.candidates.length ? ' · ' + esc(row.candidates.length) + ' fila(s) MEXC' : '') +
                        (row.result === "FEE_HYPOTHESIS_ONLY" && candidate ?
                          ' · tasa ' + show(candidate.fee) + ' ' + esc(row.asset) +
                          ' · MEXC amount ' + (candidate.fee_hypothesis === "AMOUNT_IS_GROSS" ?
                            'bruto hipotético' : 'neto hipotético') : '') +
                        (canLink ? '<details class="real-mexc-link-candidate"><summary data-help="Fija una referencia MEXC seudónima a este asiento pareado. No mueve activos ni concilia la cuenta.">Vincular</summary>' +
                          '<form class="real-mexc-transfer-match" data-snapshot="' + esc(comparison.snapshot_id) +
                          '" data-event="' + esc(row.local_event_id) + '" data-direction="' + esc(row.direction) +
                          '" data-ref="' + esc(candidate.source_ref) + '">' +
                          '<label class="field">Escribe VINCULAR TRASLADO<input name="confirmation" required pattern="VINCULAR TRASLADO" autocomplete="off" spellcheck="false"></label>' +
                          '<button type="submit" class="ghost">Confirmar vínculo</button>' +
                          '<span class="state muted real-mexc-link-state" role="status" hidden></span></form></details>' : '') +
                        '</td></tr>';
                    }).join("") + '</tbody></table></div>' : '<p class="muted">Sin traslados pareados en la cuenta seleccionada.</p>') +
                  '<h4>MEXC → Libro REAL</h4>' +
                  (observed.length ? '<p class="muted">' + esc(observed.length) +
                    ' fila(s) observadas · primeras 100, sin candidato primero. «Sin candidato» no demuestra ausencia de asiento.</p>' +
                    '<div class="tablewrap"><table><thead><tr><th>Movimiento</th><th>Activo</th><th>Importe</th><th>UTC</th><th>Estado</th><th>Libro</th></tr></thead><tbody>' +
                    observed.slice(0, 100).map(function (row) {
                      var status = row.result === "NOT_FINAL_NO_COMPARISON" ? "No final · sin comparación" :
                        row.result === "INTERNAL_MEXC_NOT_WALLET" ? "Interno MEXC · no es retorno a wallet" :
                        row.result === "ROUTE_UNKNOWN_NO_COMPARISON" ? "Ruta desconocida · sin comparación wallet" :
                        row.result === "NO_PAIRED_LOCAL_CANDIDATE" ? "Sin traslado pareado candidato" :
                        row.result === "FEE_HYPOTHESIS_ONLY" ? "Comisión · sin semántica verificada" :
                        row.result === "AMBIGUOUS" ? "Ambiguo" : "Posible";
                      return '<tr><td>' + (row.direction === "deposit" ? "Ingreso" : "Retiro") +
                        '</td><td>' + esc(row.asset) + '</td><td class="num">' + show(row.amount) +
                        '</td><td>' + esc(row.observed_at.slice(0, 16).replace("T", " ")) +
                        '</td><td>Código MEXC ' + esc(row.status_code) + '</td><td>' + status + '</td></tr>';
                    }).join("") + '</tbody></table></div>' : '<p class="muted">Sin filas MEXC en esta instantánea.</p>');
                output.querySelectorAll(".real-mexc-transfer-match").forEach(function (form) {
                  form.addEventListener("submit", function (submitEvent) {
                    submitEvent.preventDefault();
                    if (!form.reportValidity()) return;
                    var button = form.querySelector('button[type="submit"]');
                    var state = form.querySelector(".real-mexc-link-state");
                    button.disabled = true;
                    state.hidden = false;
                    state.className = "state loading real-mexc-link-state";
                    state.textContent = "Verificando vínculo…";
                    request("/accounts/" + encodeURIComponent(selected) + "/mexc-transfer-matches", {
                      method: "POST", body: JSON.stringify({
                        snapshot_id: form.dataset.snapshot, event_id: form.dataset.event,
                        direction: form.dataset.direction, source_ref: form.dataset.ref,
                        confirmation: form.elements.confirmation.value
                      })
                    }).then(function (linked) {
                      if (!form.isConnected || accountId !== selected) return;
                      state.className = "state muted real-mexc-link-state";
                      state.textContent = linked.status === "ALREADY_LINKED" ?
                        "Ya vinculado · no conciliado" : "Vinculado · no conciliado";
                      var auditResult = root.querySelector("#real-mexc-transfer-audit-result");
                      if (auditResult) auditResult.hidden = true;
                      form.querySelector("input").value = "";
                      form.querySelector("input").disabled = true;
                      form.parentElement.querySelector("summary").textContent = "Vinculado";
                    }).catch(function (error) {
                      if (!form.isConnected || accountId !== selected) return;
                      state.className = "state error real-mexc-link-state";
                      state.textContent = error.message;
                      button.disabled = false;
                    });
                  });
                });
              }).catch(function (error) {
                if (!output.isConnected || accountId !== selected) return;
                output.className = "state error";
                output.textContent = error.message;
              }).finally(function () {
                if (compareButton.isConnected) compareButton.disabled = false;
              });
            });
          }).catch(function (error) {
            if (!detail.isConnected) return;
            detail.className = "state error";
            detail.textContent = error.message;
          });
        });
      });
    }).catch(function (error) {
      if (!list.isConnected) return;
      list.className = "state error";
      list.textContent = error.message;
    });
  }
  function queryMexcTrades(event) {
    event.preventDefault();
    var form = event.currentTarget;
    var symbol = form.elements.symbol.value.trim().toUpperCase();
    var result = root.querySelector("#real-mexc-trades");
    var button = form.querySelector("button");
    var tradeEpoch = ++mexcTradeEpoch;
    result.hidden = false;
    result.className = "state loading";
    result.textContent = "Consultando operaciones…";
    button.disabled = true;
    request("/exchanges/mexc/trades/" + encodeURIComponent(symbol)).then(function (data) {
      if (!result.isConnected || tradeEpoch !== mexcTradeEpoch) return;
      result.className = "";
      result.innerHTML = mexcTradeTable(data, esc(data.symbol) + ' · hasta ' + esc(data.limit) +
        ' fills recientes · historial parcial · no importado') +
        '<p class="muted">' + esc(data.archive.new_fills) + ' nuevos guardados en la bóveda cifrada.</p>';
      var archive = root.querySelector("#real-mexc-archive");
      if (archive && archive.open) loadMexcArchive();
    }).catch(function (error) {
      if (!result.isConnected || tradeEpoch !== mexcTradeEpoch) return;
      result.className = "state error";
      result.textContent = error.message;
    }).finally(function () { if (button.isConnected) button.disabled = false; });
  }
  function queryMexcWindow(event) {
    var form = event.currentTarget.closest("form");
    var symbol = form.elements.symbol.value.trim().toUpperCase();
    if (!form.elements.symbol.checkValidity()) { form.elements.symbol.reportValidity(); return; }
    var days = Number(form.elements.days.value);
    var result = root.querySelector("#real-mexc-trades");
    var button = event.currentTarget;
    var tradeEpoch = ++mexcTradeEpoch;
    result.hidden = false;
    result.className = "state loading";
    result.textContent = "Archivando rango…";
    button.disabled = true;
    request("/exchanges/mexc/trades/" + encodeURIComponent(symbol) + "/window", {
      method: "POST", body: JSON.stringify({ days: days })
    }).then(function (data) {
      if (!result.isConnected || tradeEpoch !== mexcTradeEpoch) return;
      result.className = "state";
      result.textContent = data.archive.observed_fills + " fills observados en " + days +
        " días · " + data.pages + " consultas · " + data.archive.new_fills +
        " nuevos cifrados. No es historial total ni contabilidad.";
      var archive = root.querySelector("#real-mexc-archive");
      if (archive && archive.open) loadMexcArchive();
    }).catch(function (error) {
      if (!result.isConnected || tradeEpoch !== mexcTradeEpoch) return;
      result.className = "state error";
      result.textContent = error.message;
    }).finally(function () { if (button.isConnected) button.disabled = false; });
  }
  function mexcTradeTable(data, caption) {
    var rows = data.trades.map(function (item) {
      return '<tr><td>' + esc(new Date(item.time_ms).toISOString().slice(0, 16).replace("T", " ")) +
        '</td><td>' + esc(item.side) + '</td><td class="num">' + show(item.quantity) +
        '</td><td class="num">' + show(item.price) + '</td><td class="num">' + show(item.quote_quantity) +
        '</td><td class="num">' + show(item.commission) + ' ' + esc(item.commission_asset) +
        '</td><td>' + esc(item.id) + '</td></tr>';
    }).join("");
    return '<p class="muted">' + caption + '</p>' +
      (rows ? '<div class="tablewrap"><table><thead><tr><th>UTC</th><th>Lado</th><th>Cantidad</th><th>Precio</th><th>Importe</th><th>Comisión</th><th>ID fill</th></tr></thead><tbody>' + rows + '</tbody></table></div>' :
        '<p class="muted">Sin operaciones en la página consultada.</p>');
  }
  function mexcCoverageTime(ms) {
    return new Date(ms).toISOString().slice(0, 16).replace("T", " ") + " UTC";
  }
  function mexcCoverageLabel(data) {
    var span = mexcCoverageTime(data.start_ms) + " – " + mexcCoverageTime(data.end_ms);
    if (data.fully_covered) return "Ventanas guardadas · sin huecos entre " + span +
      ". No acredita historial anterior ni conciliación.";
    var first = data.gaps[0];
    return "Ventanas guardadas · " + data.gaps.length +
      (data.gaps.length === 1 ? " hueco" : " huecos") + " entre " + span +
      ". Primer hueco: " + mexcCoverageTime(first.start_ms) + " – " +
      mexcCoverageTime(first.end_ms) + ". No conciliado.";
  }
  function loadMexcArchive() {
    var list = root && root.querySelector("#real-mexc-archive-list");
    var detail = root && root.querySelector("#real-mexc-archive-detail");
    if (!list || !detail) return;
    mexcArchiveEpoch += 1;
    detail.hidden = true;
    list.className = "state loading";
    list.textContent = "Cargando historial cifrado…";
    request("/exchanges/mexc/observations").then(function (items) {
      if (!list.isConnected) return;
      list.className = "";
      list.innerHTML = items.length ? items.map(function (item) {
        return '<button class="ghost" type="button" data-ref="' + esc(item.account_ref) +
          '" data-symbol="' + esc(item.symbol) + '">' + esc(item.symbol) + ' · conexión ' +
          esc(item.account_ref.slice(0, 10)) + '… · ' + esc(item.fill_count) + ' fills</button>';
      }).join(" ") : '<p class="muted">Todavía no hay consultas guardadas.</p>';
      list.querySelectorAll("button[data-ref]").forEach(function (button) {
        button.addEventListener("click", function () {
          var item = items.find(function (candidate) {
            return candidate.account_ref === button.dataset.ref &&
              candidate.symbol === button.dataset.symbol;
          });
          var archiveEpoch = ++mexcArchiveEpoch;
          detail.hidden = false;
          detail.className = "state loading";
          detail.textContent = "Abriendo historial…";
          request("/exchanges/mexc/observations/" + encodeURIComponent(button.dataset.ref) +
            "/" + encodeURIComponent(button.dataset.symbol)).then(function (data) {
            if (!detail.isConnected || archiveEpoch !== mexcArchiveEpoch) return;
            detail.className = "";
            detail.innerHTML = '<p class="muted" data-coverage>Comprobando cobertura…</p>' +
              mexcTradeTable(data, esc(data.symbol) + ' · ' + esc(data.total) +
              ' fills observados guardados · historial parcial · no contabilizado' +
              (data.truncated ? ' · sólo últimos 500 visibles' : ''));
            var coverageNode = detail.querySelector("[data-coverage]");
            if (item.complete_window_start_ms === null || item.complete_window_end_ms === null) {
              coverageNode.textContent = "Cobertura temporal desconocida · solo páginas guardadas.";
              return;
            }
            request("/exchanges/mexc/observations/" + encodeURIComponent(item.account_ref) +
              "/" + encodeURIComponent(item.symbol) + "/coverage?start_ms=" +
              encodeURIComponent(item.complete_window_start_ms) + "&end_ms=" +
              encodeURIComponent(item.complete_window_end_ms)).then(function (coverage) {
              if (!coverageNode.isConnected || archiveEpoch !== mexcArchiveEpoch) return;
              coverageNode.textContent = mexcCoverageLabel(coverage);
            }).catch(function () {
              if (!coverageNode.isConnected || archiveEpoch !== mexcArchiveEpoch) return;
              coverageNode.textContent = "Cobertura no verificable en este momento.";
            });
          }).catch(function (error) {
            if (!detail.isConnected || archiveEpoch !== mexcArchiveEpoch) return;
            detail.className = "state error";
            detail.textContent = error.message;
          });
        });
      });
    }).catch(function (error) {
      if (!list.isConnected) return;
      list.className = "state error";
      list.textContent = error.message;
    });
  }
  function cancelledMexcOrder(status, fillCount) {
    if (status !== "CANCELED" && status !== "PARTIALLY_CANCELED") return null;
    return fillCount > 0 ? "Resto cancelado · ejecución parcial registrada" : "Cancelada · reserva cerrada";
  }
  function executionStatus(item) {
    if (item.journal_reason === "ABANDONED_BEFORE_SUBMISSION") return "Reserva liberada · sin envío";
    if (item.stage === "RECONCILIATION_INCONSISTENT") return "Conciliación inconsistente · revisar";
    if (item.reservation_closed) {
      return cancelledMexcOrder(item.external_status, item.reconciliation.fill_count) || "Orden final · reserva cerrada";
    }
    if (item.booked_to_portfolio) return "Asentada · reserva pendiente";
    var cancelLabels = {
      ATTEMPT_OUTCOME_UNKNOWN: "Cancelación incierta · verificar",
      ACKNOWLEDGED_NEEDS_REOBSERVATION: "Cancelación acusada · verificar",
      OPEN_OBSERVED_AFTER_CANCEL: "Sigue abierta tras cancelar",
      FINAL_OBSERVED_AFTER_CANCEL: "Final tras cancelar · reserva pendiente"
    };
    if (cancelLabels[item.cancel_state]) return cancelLabels[item.cancel_state];
    if (item.stage === "ORDER_OBSERVED_OPEN" && item.external_status === "PARTIALLY_FILLED") {
      return "Parcial abierta · pendiente de asiento";
    }
    var labels = {
      RESERVED_NOT_SUBMITTED: "Reservada · no enviada",
      ORDER_UNVERIFIED: "Envío incierto · verificar en MEXC",
      ORDER_OBSERVED_OPEN: "Orden abierta · aún sin asiento",
      FINAL_NO_EXECUTION: "Finalizada sin ejecución",
      FINAL_OBSERVED_UNBOOKED: "Finalizada · pendiente de asiento",
      STOPPED_BEFORE_SUBMISSION: "Detenida antes de enviar",
      REJECTED_BEFORE_SUBMISSION: "Rechazada antes de enviar"
    };
    return labels[item.stage] || item.stage;
  }
  function executionProgress(item) {
    var progress = item.progress;
    if (!progress) return "";
    var asset = " " + esc(item.base_asset);
    var values = [["Solicitado", progress.requested_base], ["Ejecutado", progress.executed_base]];
    if (progress.unfilled_state !== "NONE") {
      values.push([progress.unfilled_state === "CANCELLED" ? "Resto cancelado" : "Sigue abierto", progress.unfilled_base]);
    }
    return '<div class="real-exec-deltas" aria-label="Cantidades de la orden">' + values.map(function (part) {
      return '<span class="real-exec-delta"><small>' + part[0] + '</small><strong>' + show(part[1]) + asset + '</strong></span>';
    }).join("") + '</div>';
  }
  function executionIssue(status) {
    var labels = {
      MULTI_ASSET_FEE_UNSUPPORTED: "Comisión en otro activo: requiere un modelo contable ampliado.",
      BASE_FEE_EXCEEDS_FILL: "La comisión en el activo supera la cantidad adquirida.",
      FILL_ARCHIVE_INCOMPLETE: "Faltan fills en el archivo observado.",
      FILL_TOTAL_MISMATCH: "Los fills no concuerdan con el total de la orden.",
      ORDER_NOT_FINAL: "La orden aún no es final.",
      ORDER_NOT_OBSERVED: "No existe observación externa verificada.",
      FINAL_WITHOUT_EXECUTION: "No hubo ejecución que contabilizar."
    };
    return labels[status] || "No se puede importar esta orden todavía.";
  }
  function tradeWindowAuditLabel(reason) {
    var labels = {
      WINDOW_ARCHIVE_AND_BOOKING_MATCH: "Ventana consistente",
      FILL_NOT_ARCHIVED: "Fill MEXC sin archivar",
      ARCHIVED_FILL_MISSING: "Falta un fill archivado",
      FILL_CONTENT_CHANGED: "Fill distinto al archivo",
      FILL_ORDER_NOT_OWNED: "Orden externa o sin vínculo",
      FILL_BOOKING_PENDING: "Asiento pendiente",
      FILL_BOOKING_MISMATCH: "Asiento distinto del fill o cuenta",
      TRADE_WINDOW_STALE: "Datos caducados",
      TRADE_WINDOW_INCOMPLETE: "Rango incompleto",
      TRADE_WINDOW_INVALID: "Rango no verificable",
      VIEW_UNAVAILABLE: "Lectura View no disponible"
    };
    return labels[reason] || "Revisión necesaria";
  }
  function auditMexcExecution(event) {
    event.preventDefault();
    var form = event.currentTarget;
    var button = form.querySelector('button[type="submit"]');
    var result = form.querySelector(".real-exec-result");
    var days = Number(form.elements.days.value);
    var executionEpoch = mexcExecutionEpoch;
    var auditEpoch = mexcAuditEpoch;
    button.disabled = true;
    result.hidden = false;
    result.className = "real-exec-result muted";
    result.textContent = "Contrastando fills…";
    request("/execution/mexc/policies/" + encodeURIComponent(form.dataset.policy) + "/trade-window-audit", {
      method: "POST", body: JSON.stringify({ days: days })
    }).then(function (data) {
      if (!form.isConnected || executionEpoch !== mexcExecutionEpoch || auditEpoch !== mexcAuditEpoch) return;
      result.className = "real-exec-result " + (data.consistent_for_window ? "muted" : "amber");
      result.textContent = tradeWindowAuditLabel(data.reason) + " · " + data.fill_count +
        " fills / " + days + " d · No autoriza operar.";
    }).catch(function (error) {
      if (!form.isConnected || executionEpoch !== mexcExecutionEpoch || auditEpoch !== mexcAuditEpoch) return;
      result.className = "real-exec-result error";
      result.textContent = error.message;
    }).finally(function () {
      if (button.isConnected && auditEpoch === mexcAuditEpoch) button.disabled = !mexcViewConnected;
    });
  }
  function transferWindowAuditLabel(reason) {
    var labels = {
      NO_TRANSFERS_IN_OBSERVED_WINDOW: "Sin traslados en el rango",
      TRANSFER_ACTIVITY_REQUIRES_RECONCILIATION: "Traslados por conciliar",
      TRANSFER_WINDOW_STALE: "Datos caducados",
      TRANSFER_WINDOW_INVALID: "Rango no verificable",
      VIEW_UID_CHANGED: "Identidad MEXC cambiada",
      VIEW_UID_UNVERIFIED: "Identidad MEXC sin vínculo",
      VIEW_ACCOUNT_MISMATCH: "Otra conexión MEXC",
      VIEW_UNAVAILABLE: "Lectura View no disponible"
    };
    return labels[reason] || "Revisión necesaria";
  }
  function auditMexcTransfers(event) {
    event.preventDefault();
    var form = event.currentTarget;
    var button = form.querySelector('button[type="submit"]');
    var result = form.querySelector(".real-exec-result");
    var days = Number(form.elements.days.value);
    var executionEpoch = mexcExecutionEpoch;
    var auditEpoch = mexcAuditEpoch;
    button.disabled = true;
    result.hidden = false;
    result.className = "real-exec-result muted";
    result.textContent = "Contrastando traslados…";
    request("/execution/mexc/policies/" + encodeURIComponent(form.dataset.policy) + "/transfer-window-audit", {
      method: "POST", body: JSON.stringify({ days: days })
    }).then(function (data) {
      if (!form.isConnected || executionEpoch !== mexcExecutionEpoch || auditEpoch !== mexcAuditEpoch) return;
      result.className = "real-exec-result " + (data.clear_for_window ? "muted" : "amber");
      result.textContent = transferWindowAuditLabel(data.reason) + " · " +
        (data.deposit_count + data.withdrawal_count) + " movimientos / " + days +
        " d · No autoriza operar.";
    }).catch(function (error) {
      if (!form.isConnected || executionEpoch !== mexcExecutionEpoch || auditEpoch !== mexcAuditEpoch) return;
      result.className = "real-exec-result error";
      result.textContent = error.message;
    }).finally(function () {
      if (button.isConnected && auditEpoch === mexcAuditEpoch) button.disabled = !mexcViewConnected;
    });
  }
  function executionCard(item, destination, tradeReady, viewReady, renewedView) {
    var projection = item.reconciliation;
    var origin = item.origin;
    var originMarkup = "";
    if (origin && origin.status === "ARCHIVED_PAPER") {
      var signalTime = String(origin.signal_at).slice(0, 16).replace("T", " ") + " UTC";
      originMarkup = '<div class="real-exec-origin" data-help="Señal PAPER archivada · versión ' +
        esc(origin.strategy_version_id) + ' · dataset SHA-256 ' + esc(origin.dataset_hash) +
        '. No demuestra ejecución en MEXC."><span>PAPER</span> <time datetime="' +
        esc(origin.signal_at) + '">' + esc(origin.timeframe) + ' · ' + esc(signalTime) +
        '</time></div>';
    } else if (origin && origin.status === "INVALID_SOURCE") {
      originMarkup = '<div class="real-exec-origin invalid" data-help="La procedencia archivada no supera la verificación local; no atribuyas esta intención a una estrategia.">Origen no verificable</div>';
    }
    var deltas = projection.deltas || {};
    var movements = Object.keys(deltas).map(function (asset) {
      return '<span class="real-exec-delta"><small>' + esc(asset) + '</small><strong>' + show(deltas[asset]) + '</strong></span>';
    }).join("");
    var importable = item.stage === "FINAL_OBSERVED_UNBOOKED" &&
      projection.status === "NEEDS_ACCOUNT_LINK_AND_BOOKING";
    var closable = item.stage === "FINAL_BOOKED_RESERVED" ||
      item.stage === "FINAL_NO_EXECUTION";
    var canStop = !item.account_stopped &&
      (item.journal_status === "RESERVED" || item.journal_status === "SUBMISSION_UNKNOWN");
    var releaseControl = item.account_id === accountId && item.journal_status === "RESERVED" &&
      item.journal_reason === "AUTHORIZED_FOR_RESERVATION" && !item.external_order_observed ?
      '<form class="real-exec-release" data-intent="' + esc(item.intent_id) + '" data-account="' + esc(accountId) + '">' +
      '<button type="submit" class="ghost" data-help="Libera sólo una reserva que nunca inició el envío. Conserva el ID y no consulta ni cancela órdenes en MEXC.">Liberar reserva</button>' +
      '<span class="real-exec-result" role="status" hidden></span></form>' : '';
    var stopControl = canStop ? '<details class="real-exec-stop"><summary>Detener cuenta local</summary>' +
      '<form data-intent="' + esc(item.intent_id) + '"><p>Impide nuevas intenciones y libera sólo reservas no enviadas. ' +
      'No cancela órdenes en MEXC.</p><label class="field">Escribe DETENER<input name="confirmation" required ' +
      'pattern="DETENER" autocomplete="off" spellcheck="false" aria-label="Confirmar parada local"></label>' +
      '<button type="submit" class="ghost">Detener</button><p class="real-exec-result" role="status" hidden></p>' +
      '</form></details>' : item.account_stopped ?
      '<p class="real-exec-stopped">Parada local activa · revisa las órdenes en MEXC</p>' : '';
    var observeControl = item.account_stopped && item.journal_status === "SUBMISSION_UNKNOWN" ?
      '<form class="real-exec-observe" data-intent="' + esc(item.intent_id) + '">' +
      '<button type="submit" class="ghost"' + (viewReady ? '' : ' disabled') +
      ' data-help="Consulta con tu clave MEXC View la orden y sus fills; no cancela ni libera la reserva.">Consultar MEXC</button>' +
      '<span class="real-exec-result" role="status" hidden></span></form>' : '';
    var superviseControl = item.account_stopped && item.journal_status === "SUBMISSION_UNKNOWN" ?
      '<details class="real-exec-stop real-exec-supervise"' +
      (mexcSupervisorSnapshot && mexcSupervisorSnapshot.data && mexcSupervisorSnapshot.data.running ? ' hidden' : '') +
      '><summary data-help="Vigila sólo esta cuenta detenida mientras ORBIS está abierto. Puede enviar una cancelación de una orden propia, pero no garantiza que MEXC la acepte.">Vigilar parada · posible cancelación</summary>' +
      '<form data-intent="' + esc(item.intent_id) + '"><p>Requiere claves View y Trade verificadas. Sólo vigila con ORBIS abierto y bóveda desbloqueada; puede cancelar una orden propia abierta en MEXC. No es una orden protectora.</p>' +
      '<label class="field">Escribe VIGILAR_PARADA<input name="confirmation" required pattern="VIGILAR_PARADA" autocomplete="off" spellcheck="false"></label>' +
      '<button type="submit" class="ghost">Iniciar vigilancia</button><p class="real-exec-result" role="status" hidden></p></form></details>' : '';
    var auditControl = '<form class="real-exec-audit" data-policy="' + esc(item.policy_id) + '">' +
      '<label class="field" data-help="Sólo un par y un periodo. Contrasta fills de MEXC con archivo y asientos locales; no calcula P/L ni autoriza órdenes.">Fills' +
      '<select name="days" aria-label="Periodo de auditoría de fills"><option value="1">1 d</option>' +
      '<option value="7">7 d</option><option value="30">30 d</option></select></label>' +
      '<button type="submit" class="ghost"' + (viewReady ? '' : ' disabled') +
      ' data-help="Requiere clave MEXC View conectada. Consulta el rango completo y señala fills externos, cambiados o sin asiento. No envía órdenes.">Auditar fills</button>' +
      '<span class="real-exec-result muted" role="status" hidden></span></form>' +
      '<form class="real-exec-transfer-audit" data-policy="' + esc(item.policy_id) + '">' +
      '<label class="field" data-help="Ingresos y retiradas de esta cuenta Spot. Una ventana vacía no acredita el pasado completo.">Traslados' +
      '<select name="days" aria-label="Periodo de auditoría de traslados"><option value="1">1 d</option>' +
      '<option value="7">7 d</option><option value="30">30 d</option><option value="90">90 d</option></select></label>' +
      '<button type="submit" class="ghost"' + (viewReady ? '' : ' disabled') +
      ' data-help="Requiere SPOT_WITHDRAW_READ. Detecta actividad externa; no contabiliza ni habilita órdenes.">Auditar traslados</button>' +
      '<span class="real-exec-result muted" role="status" hidden></span></form>';
    var manualAction = importable ? '<form class="real-exec-book" data-intent="' + esc(item.intent_id) +
      '" data-account="' + esc(accountId) + '"><p class="amber">Destino: ' + esc(destination) +
      '. Asienta todos los fills en esta cartera; no confirma saldo MEXC ni libera la reserva.</p>' +
      '<label class="field">Escribe IMPORTAR para confirmar<input name="confirmation" required pattern="IMPORTAR" ' +
      'autocomplete="off" spellcheck="false" aria-label="Confirmar importación"></label>' +
      '<button type="submit">Importar fills</button><p class="real-exec-result" role="status" hidden></p></form>' :
      closable ? '<form class="real-exec-close" data-intent="' + esc(item.intent_id) +
      '"><p class="amber">Conecta la clave View de la misma cuenta con permiso SPOT_DEAL_READ. ORBIS volverá a comprobar orden, fills y saldo antes de cerrar esta reserva local.</p>' +
      '<label class="field">Escribe CERRAR para confirmar<input name="confirmation" required pattern="CERRAR" ' +
      'autocomplete="off" spellcheck="false" aria-label="Confirmar cierre de reserva"></label>' +
      '<button type="submit" class="ghost">Reconfirmar y cerrar reserva</button><p class="real-exec-result" role="status" hidden></p></form>' :
      '<p class="muted real-exec-issue">' + esc(item.stage === "RECONCILIATION_INCONSISTENT" ?
        'El estado de la reserva, el asiento o los fills no concuerdan con el archivo. No importes ni cierres desde esta vista; revisa la bóveda y la cuenta MEXC.' :
        item.reservation_closed ?
        'Orden y fills reconfirmados; la reserva local se cerró. El saldo observado no sustituye una conciliación patrimonial completa.' :
        item.booked_to_portfolio ?
        'Los fills constan en el libro. La reserva y el saldo externo aún requieren conciliación.' :
        executionIssue(projection.status)) + '</p>';
    var synchronizable = item.account_id === accountId && item.journal_status === "SUBMISSION_UNKNOWN" &&
      item.stage !== "RECONCILIATION_INCONSISTENT";
    var cancelControl = synchronizable && item.account_stopped && item.stage === "ORDER_OBSERVED_OPEN" &&
      item.cancel_state === "NOT_ATTEMPTED" ?
      '<form class="real-exec-cancel" data-intent="' + esc(item.intent_id) + '" data-account="' + esc(accountId) +
      '" data-symbol="' + esc(item.symbol) + '">' +
      '<button type="submit" class="ghost"' + (viewReady && tradeReady ? '' : ' disabled') +
      ' data-help="Cancela el resto abierto de esta orden propia. Requiere View y Trade verificadas. La reserva se cierra al confirmar orden, ejecuciones y saldo.">Cancelar orden</button>' +
      '<span class="real-exec-result" role="status" hidden></span></form>' : '';
    var action = synchronizable ?
      '<form class="real-exec-sync" data-intent="' + esc(item.intent_id) + '" data-account="' + esc(accountId) + '"' +
      (renewedView ? ' data-renewed-view' : '') + '>' +
      '<button type="submit"' + (viewReady || renewedView ? '' : ' disabled') +
      ' data-help="Consulta la orden en MEXC, registra sus ejecuciones finales una sola vez y cierra la reserva si el saldo coincide con esta cartera. Las órdenes abiertas siguen pendientes.">' +
      (renewedView ? 'Recuperar con View renovada' : 'Sincronizar orden') + '</button>' +
      '<span class="real-exec-result" role="status" hidden></span></form>' +
      '<details class="real-exec-manual"><summary>Pasos manuales</summary>' + manualAction + '</details>' : manualAction;
    return '<article class="real-exec-card" tabindex="-1" data-intent="' + esc(item.intent_id) + '">' +
      '<div class="real-exec-head"><div><span class="real-exec-side">' + esc(item.side) + '</span> ' +
      '<strong>' + esc(item.symbol) + '</strong><small> · conexión ' + esc(item.account_ref_hint) + '</small></div>' +
      '<span class="statusbadge ' + (item.booked_to_portfolio ? 'on' : 'off') + '">' +
      esc(executionStatus(item)) + '</span></div>' +
      '<p class="muted real-exec-meta">' + esc(item.observed_at || item.created_at) +
      ' · ' + esc(projection.fill_count) + ' fills · reserva local ' +
      show(item.reserved_quote) + ' ' + esc(item.quote_asset) +
      (item.cancel_state && item.cancel_state !== 'NOT_ATTEMPTED' && item.external_status ?
        ' · MEXC: ' + esc(item.external_status) : '') + '</p>' + originMarkup + executionProgress(item) +
      (movements ? '<div class="real-exec-deltas" aria-label="Variación observada por activo">' + movements + '</div>' : '') +
      action + cancelControl + releaseControl + stopControl + observeControl + superviseControl + auditControl + '</article>';
  }
  function loadMexcExecution() {
    var list = root && root.querySelector("#real-mexc-execution-list");
    if (!list || !accountId) return;
    var panel = list.parentElement, pendingOnly = panel.dataset.pendingOnly === "1";
    var account = accountId, epoch = ++mexcExecutionEpoch;
    panel.querySelector("[data-mexc-full-journal]").hidden = !pendingOnly;
    list.className = "state loading";
    list.textContent = pendingOnly ? "Revisando órdenes pendientes…" : "Revisando órdenes archivadas…";
    var load = pendingOnly ? request("/accounts/" + encodeURIComponent(account) + "/mexc-pending-orders", null, true) :
      Promise.all([
        request("/execution/mexc/intents"),
        request("/exchanges/mexc/trade/status", null, true).catch(function () { return null; })
      ]).then(function (results) {
        return {orders:results[0], trade_ready:results[1] && results[1].verified === true, view_ready:mexcViewConnected};
      });
    load.then(function (data) {
      if (!list.isConnected || epoch !== mexcExecutionEpoch || account !== accountId) return;
      var items = data.orders, viewReady = data.view_ready, tradeReady = data.trade_ready;
      var selected = root.querySelector("#real-account");
      var destination = selected && selected.selectedOptions.length ? selected.selectedOptions[0].textContent : "cuenta no elegida";
      list.className = "real-exec-list";
      var renewedView = pendingOnly && !viewReady && mexcViewConnected;
      var needsView = pendingOnly && !viewReady && items.some(function (item) { return item.journal_status === "SUBMISSION_UNKNOWN"; });
      list.innerHTML = (needsView ? '<button class="ghost" type="button" data-mexc-recovery-view>' +
        (mexcViewConnected ? 'Cambiar clave View' : 'Conectar View') + '</button>' : '') +
        (items.length ? '<p class="muted">' + (pendingOnly ? 'Órdenes pendientes · cuenta seleccionada' :
          'Órdenes propias · ejecuciones y reservas.') + '</p>' +
          items.map(function (item) { return executionCard(item, destination, tradeReady, viewReady, renewedView); }).join("") :
          pendingOnly ? '<p class="muted">Sin órdenes pendientes en esta cuenta.</p>' :
          '<p class="muted">Aún no hay intenciones de orden REAL archivadas. Las operaciones PAPER y los fills consultados directamente en MEXC no son órdenes de ORBIS.</p>');
      var viewButton = list.querySelector("[data-mexc-recovery-view]");
      if (viewButton) viewButton.addEventListener("click", function () {
        if (mexcViewConnected) root.querySelector("#real-mexc-disconnect").focus();
        else openRealControl("real-mexc-connect");
      });
      list.querySelectorAll("form.real-exec-book").forEach(function (form) {
        form.addEventListener("submit", bookMexcExecution);
      });
      list.querySelectorAll("form.real-exec-close").forEach(function (form) {
        form.addEventListener("submit", closeMexcExecution);
      });
      list.querySelectorAll("form.real-exec-release").forEach(function (form) {
        form.addEventListener("submit", releaseMexcReservation);
      });
      list.querySelectorAll("form.real-exec-sync, form.real-exec-cancel").forEach(function (form) {
        form.addEventListener("submit", synchronizeMexcExecution);
      });
      list.querySelectorAll(".real-exec-stop form").forEach(function (form) {
        form.addEventListener("submit", stopMexcExecution);
      });
      list.querySelectorAll("form.real-exec-observe").forEach(function (form) {
        form.addEventListener("submit", observeStoppedMexcExecution);
      });
      list.querySelectorAll(".real-exec-supervise form").forEach(function (form) {
        form.addEventListener("submit", startMexcSupervision);
      });
      list.querySelectorAll("form.real-exec-audit").forEach(function (form) {
        form.addEventListener("submit", auditMexcExecution);
      });
      list.querySelectorAll("form.real-exec-transfer-audit").forEach(function (form) {
        form.addEventListener("submit", auditMexcTransfers);
      });
      if (panel.dataset.focusIntent) {
        var selectedOrder = list.querySelector('.real-exec-card[data-intent="' + CSS.escape(panel.dataset.focusIntent) + '"]');
        delete panel.dataset.focusIntent;
        if (selectedOrder) selectedOrder.focus();
      }
    }).catch(function (error) {
      if (!list.isConnected || epoch !== mexcExecutionEpoch) return;
      list.className = "state error";
      list.textContent = error.message;
    });
  }
  function releaseMexcReservation(event) {
    event.preventDefault();
    var form = event.currentTarget, selected = accountId, issued = mexcExecutionEpoch;
    if (form.dataset.account !== selected || !window.confirm("¿Liberar esta reserva no enviada? El ID quedará archivado y no se podrá enviar después.")) return;
    var button = form.querySelector('button[type="submit"]');
    var result = form.querySelector(".real-exec-result");
    button.disabled = true;
    result.hidden = false;
    result.textContent = "Liberando reserva…";
    request("/execution/mexc/intents/" + encodeURIComponent(form.dataset.intent) + "/release-unsent", {
      method: "POST", body: JSON.stringify({account_id: selected, confirmation: "LIBERAR_RESERVA_NO_ENVIADA"})
    }).then(function () {
      if (!form.isConnected || issued !== mexcExecutionEpoch || selected !== accountId) return;
      notice("Reserva liberada · sin envío a MEXC.", false);
      if (window.OrbisMexcTrading) {
        window.OrbisMexcTrading.resetOrderRequest();
        window.OrbisMexcTrading.refresh();
      }
      loadMexcExecution();
    }).catch(function (error) {
      if (!form.isConnected || issued !== mexcExecutionEpoch || selected !== accountId) return;
      if (form.closest("#real-mexc-execution").dataset.pendingOnly === "1") {
        notice(error.message, true);
        loadMexcExecution();
        return;
      }
      result.className = "real-exec-result error";
      result.textContent = error.message;
      button.disabled = false;
    });
  }
  function synchronizeMexcExecution(event) {
    event.preventDefault();
    var form = event.currentTarget;
    var journal = form.closest("#real-mexc-execution");
    var selectedAccount = accountId, connection = mexcConnectionEpoch, session = sessionEpoch;
    if (!mexcViewConnected || form.dataset.account !== selectedAccount) return;
    var cancelling = form.classList.contains("real-exec-cancel");
    var renewed = form.hasAttribute("data-renewed-view");
    if (renewed && !window.confirm("¿Comprobar esta orden con la clave View renovada? ORBIS verificará el UID archivado antes de conciliar.")) return;
    if (cancelling && !window.confirm("¿Cancelar el resto abierto de esta orden " + form.dataset.symbol +
      " en MEXC REAL? Las ejecuciones ya realizadas se conservan.")) return;
    var button = form.querySelector('button[type="submit"]');
    var result = form.querySelector(".real-exec-result");
    var feedback = journal.querySelector("#real-mexc-execution-feedback");
    feedback.hidden = true;
    button.disabled = true;
    result.hidden = false;
    result.className = "real-exec-result muted";
    result.textContent = cancelling ? "Solicitando cancelación…" : "Sincronizando…";
    var body = {account_id: selectedAccount};
    if (cancelling) body.confirmation = "CANCELAR_ORDEN_MEXC";
    if (renewed) body.confirmation = "RECUPERAR_CON_VIEW_RENOVADA";
    var action = cancelling ? "/cancel" : renewed ? "/synchronize-renewed-view" : "/synchronize";
    request("/execution/mexc/intents/" + encodeURIComponent(form.dataset.intent) + action, {
      method: "POST", body: JSON.stringify(body)
    }).then(function (response) {
      if (!journal.isConnected || session !== sessionEpoch || connection !== mexcConnectionEpoch ||
          selectedAccount !== accountId) return;
      var data = cancelling ? response.synchronization : response;
      if (!data) {
        notice("Resultado de cancelación pendiente · sincroniza la orden.", true);
      } else if (data.stage === "COMPLETED" || data.stage === "ALREADY_COMPLETED") {
        if (window.OrbisMexcTrading) window.OrbisMexcTrading.resetOrderRequest(form.dataset.intent);
        var terminal = cancelledMexcOrder(data.external_status, data.fill_count);
        notice(terminal || (cancelling ? "Orden ejecutada · reserva cerrada." : "Orden sincronizada · reserva cerrada."), false);
      } else if (data.stage === "ORDER_OPEN") {
        notice(cancelling ? "Orden aún abierta · sincroniza la cancelación." :
          data.external_status === "PARTIALLY_FILLED" ? "Orden parcialmente ejecutada · sigue abierta." :
          "Orden abierta · pendiente de ejecución.", false);
      } else {
        notice((data.booked ? "Ejecuciones registradas. " : "") + (data.detail || "Revisión pendiente."), true);
      }
      if (journal.open) loadMexcExecution();
      if (window.OrbisMexcTrading) window.OrbisMexcTrading.refresh();
      refresh();
    }).catch(function (error) {
      if (!journal.isConnected || session !== sessionEpoch || connection !== mexcConnectionEpoch ||
          selectedAccount !== accountId) return;
      if (renewed && error.status) {
        feedback.textContent = error.message;
        feedback.hidden = false;
        if (form.isConnected) {
          result.hidden = true;
          button.disabled = !mexcViewConnected;
        }
        return;
      }
      if (!form.isConnected || journal.dataset.pendingOnly === "1") {
        notice(error.message, true);
        if (journal.open) loadMexcExecution();
        if (window.OrbisMexcTrading) window.OrbisMexcTrading.refresh();
        return;
      }
      result.className = "real-exec-result error";
      result.textContent = error.message;
      button.disabled = !mexcViewConnected;
    });
  }
  function bookMexcExecution(event) {
    event.preventDefault();
    var form = event.currentTarget;
    var result = form.querySelector(".real-exec-result");
    if (form.dataset.account !== accountId || form.elements.confirmation.value !== "IMPORTAR") {
      result.hidden = false;
      result.className = "real-exec-result error";
      result.textContent = "Revisa la cuenta elegida y escribe IMPORTAR exactamente.";
      return;
    }
    var button = form.querySelector('button[type="submit"]');
    button.disabled = true;
    result.hidden = false;
    result.className = "real-exec-result muted";
    result.textContent = "Verificando y asentando…";
    request("/execution/mexc/intents/" + encodeURIComponent(form.dataset.intent) + "/book", {
      method: "POST", body: JSON.stringify({ account_id: accountId, confirmation: "IMPORTAR" })
    }).then(function (data) {
      if (!form.isConnected) return;
      notice(data.fill_count + " fills verificados asentados en la cuenta REAL.", false);
      loadMexcExecution();
      refresh();
    }).catch(function (error) {
      if (!form.isConnected) return;
      result.className = "real-exec-result error";
      result.textContent = error.message;
      button.disabled = false;
    });
  }
  function closeMexcExecution(event) {
    event.preventDefault();
    var form = event.currentTarget;
    var result = form.querySelector(".real-exec-result");
    if (form.elements.confirmation.value !== "CERRAR") return;
    var button = form.querySelector('button[type="submit"]');
    button.disabled = true;
    result.hidden = false;
    result.className = "real-exec-result muted";
    result.textContent = "Reconfirmando en MEXC…";
    request("/execution/mexc/intents/" + encodeURIComponent(form.dataset.intent) + "/close-reservation", {
      method: "POST", body: JSON.stringify({ confirmation: "CERRAR" })
    }).then(function () {
      if (!form.isConnected) return;
      notice("Orden final reconfirmada; reserva local cerrada.", false);
      loadMexcExecution();
    }).catch(function (error) {
      if (!form.isConnected) return;
      result.className = "real-exec-result error";
      result.textContent = error.message;
      button.disabled = false;
    });
  }
  function stopMexcExecution(event) {
    event.preventDefault();
    var form = event.currentTarget;
    var result = form.querySelector(".real-exec-result");
    if (form.elements.confirmation.value !== "DETENER") return;
    var button = form.querySelector('button[type="submit"]');
    button.disabled = true;
    result.hidden = false;
    result.className = "real-exec-result muted";
    result.textContent = "Guardando parada local…";
    request("/execution/mexc/intents/" + encodeURIComponent(form.dataset.intent) + "/stop-account", {
      method: "POST", body: JSON.stringify({ confirmation: "DETENER" })
    }).then(function (data) {
      if (!form.isConnected) return;
      notice("Parada local activa. " + data.released_unsent + " reservas no enviadas liberadas; " +
        data.unresolved_sent_or_unknown + " intenciones inciertas requieren revisión en MEXC.", false);
      loadMexcExecution();
      if (window.OrbisMexcTrading) window.OrbisMexcTrading.refresh();
    }).catch(function (error) {
      if (!form.isConnected) return;
      result.className = "real-exec-result error";
      result.textContent = error.message;
      button.disabled = false;
    });
  }
  function startMexcSupervision(event) {
    event.preventDefault();
    var form = event.currentTarget;
    var result = form.querySelector(".real-exec-result");
    if (form.elements.confirmation.value !== "VIGILAR_PARADA") return;
    var button = form.querySelector('button[type="submit"]');
    button.disabled = true;
    result.hidden = false;
    result.className = "real-exec-result muted";
    result.textContent = "Comprobando identidad y arrancando vigilancia…";
    request("/execution/mexc/intents/" + encodeURIComponent(form.dataset.intent) + "/supervise-stop", {
      method: "POST", body: JSON.stringify({ confirmation: "VIGILAR_PARADA" })
    }).then(function (data) {
      if (!form.isConnected) return;
      paintMexcSupervisor(data, false);
      notice("Vigilancia local iniciada. Comprueba la orden en MEXC; no se confirma cancelación.", false);
      loadMexcExecution();
    }).catch(function (error) {
      if (!form.isConnected) return;
      result.className = "real-exec-result error";
      result.textContent = error.message;
      button.disabled = false;
    });
  }
  function stopMexcSupervision(event) {
    event.preventDefault();
    var form = event.currentTarget;
    var result = form.querySelector(".real-exec-result");
    if (form.elements.confirmation.value !== "DETENER_VIGILANCIA") return;
    var button = form.querySelector('button[type="submit"]');
    button.disabled = true;
    result.hidden = false;
    result.className = "real-exec-result muted";
    result.textContent = "Deteniendo vigilancia…";
    request("/execution/mexc/stop-supervisor/stop", {
      method: "POST", body: JSON.stringify({ confirmation: "DETENER_VIGILANCIA" })
    }).then(function (data) {
      if (!form.isConnected) return;
      paintMexcSupervisor(data, false);
      notice("Vigilancia detenida. Revisa en MEXC las órdenes inciertas; no se confirma cancelación.", false);
      loadMexcExecution();
    }).catch(function (error) {
      if (!form.isConnected) return;
      result.className = "real-exec-result error";
      result.textContent = error.message;
      button.disabled = false;
    });
  }
  function observeStoppedMexcExecution(event) {
    event.preventDefault();
    var form = event.currentTarget;
    var result = form.querySelector(".real-exec-result");
    var button = form.querySelector('button[type="submit"]');
    button.disabled = true;
    result.hidden = false;
    result.className = "real-exec-result muted";
    result.textContent = "Consultando orden…";
    request("/execution/mexc/intents/" + encodeURIComponent(form.dataset.intent) + "/observe-stopped", {
      method: "POST"
    }).then(function (data) {
      if (!form.isConnected) return;
      notice("MEXC: " + data.external_status + ". Observación guardada; reserva sin cerrar.", false);
      loadMexcExecution();
    }).catch(function (error) {
      if (!form.isConnected) return;
      result.className = "real-exec-result error";
      result.textContent = error.message;
      button.disabled = false;
    });
  }
  function loadMexc() {
    var epoch = ++mexcConnectionEpoch, target = root.querySelector('#real-mexc-state');
    request("/exchanges/mexc/status").then(function (status) {
      if (epoch !== mexcConnectionEpoch || !target.isConnected) return;
      mexcConnected(status.connected);
      if (status.connected) return refreshMexc();
      mexcState("Sin conexión. Introduce una clave nueva, solo lectura.", false);
    }).catch(function (error) { if (epoch === mexcConnectionEpoch && target.isConnected) mexcState(error.message, true); });
  }
  function connectMexc(event) {
    event.preventDefault();
    var epoch = ++mexcConnectionEpoch;
    clearMexcComparison();
    clearMexcIdentity();
    mexcValueEpoch += 1;
    var form = event.currentTarget;
    var key = form.elements.api_key.value;
    var secret = form.elements.secret_key.value;
    form.reset();
    var button = form.querySelector('button[type="submit"]');
    button.disabled = true;
    mexcState("Consultando permisos y saldos…", false);
    request("/exchanges/mexc/connect", { method: "POST", body: JSON.stringify({ api_key: key, secret_key: secret }) })
      .then(function (data) { if (epoch === mexcConnectionEpoch && form.isConnected) { mexcConnected(true); renderMexcBalances(data); } })
      .catch(function (error) { if (epoch === mexcConnectionEpoch && form.isConnected) mexcState(error.message, true); })
      .finally(function () { if (epoch === mexcConnectionEpoch && form.isConnected) button.disabled = false; });
  }
  function refreshMexc() {
    mexcValueEpoch += 1;
    var epoch = mexcConnectionEpoch, target = root.querySelector('#real-mexc-state');
    mexcState("Consultando saldos…", false);
    var value = root && root.querySelector("#real-mexc-value-state");
    if (value) { value.className = "muted"; value.textContent = "La valoración anterior ya no está vigente."; }
    return request("/exchanges/mexc/balances").then(function (data) {
      if (epoch === mexcConnectionEpoch && target.isConnected) renderMexcBalances(data);
    }).catch(function (error) { if (epoch === mexcConnectionEpoch && target.isConnected) mexcState(error.message, true); });
  }
  function valueMexc() {
    var button = root.querySelector("#real-mexc-value-run");
    var target = root.querySelector("#real-mexc-value-state");
    var quote = root.querySelector("#real-mexc-value-quote").value;
    var epoch = ++mexcValueEpoch;
    button.disabled = true;
    target.className = "muted";
    target.textContent = "Valorando saldos y precios actuales…";
    return request("/exchanges/mexc/valuation?quote=" + encodeURIComponent(quote))
      .then(function (data) { if (epoch === mexcValueEpoch) renderMexcBalances(data, data); })
      .catch(function (error) {
        if (epoch === mexcValueEpoch) { target.className = "error"; target.textContent = error.message; }
      }).finally(function () { if (epoch === mexcValueEpoch) button.disabled = false; });
  }
  function disconnectMexc() {
    var epoch = ++mexcConnectionEpoch, target = root.querySelector('#real-mexc-state');
    clearMexcComparison();
    clearMexcIdentity();
    mexcValueEpoch += 1;
    mexcTradeEpoch += 1;
    mexcTransferEpoch += 1;
    var tradeResult = root && root.querySelector("#real-mexc-trades");
    if (tradeResult) { tradeResult.hidden = true; tradeResult.replaceChildren(); }
    var transferResult = root && root.querySelector("#real-mexc-transfer-result");
    if (transferResult) { transferResult.hidden = true; transferResult.replaceChildren(); }
    request("/exchanges/mexc/disconnect", { method: "POST" }).then(function () {
      if (epoch !== mexcConnectionEpoch || !target.isConnected) return;
      mexcConnected(false);
      mexcState("Desconectado. Clave retirada de la sesión.", false);
    }).catch(function (error) { if (epoch === mexcConnectionEpoch && target.isConnected) mexcState(error.message, true); });
  }
  function grvtState(message, error) {
    var target = root && root.querySelector("#real-grvt-state");
    if (!target) return;
    target.textContent = message;
    target.className = "state " + (error ? "error" : "muted");
  }
  function grvtConnected(connected) {
    if (perpLiveReview) perpLiveReview.setConnected(connected);
    var actions = root && root.querySelector("#real-grvt-actions");
    var details = root && root.querySelector("#real-grvt-connect");
    var orders = root && root.querySelector("#real-grvt-orders");
    if (!actions || !details || !orders) return;
    actions.hidden = !connected;
    details.hidden = connected;
    orders.hidden = !connected;
    if (!connected) {
      grvtOrdersEpoch += 1;
      grvtOrderEpoch += 1;
      grvtFillsEpoch += 1;
      orders.open = false;
      root.querySelector("#real-grvt-orders-state").textContent = "Abre para consultar.";
      root.querySelector("#real-grvt-order-detail").textContent = "Introduce los identificadores para comprobar una orden.";
      root.querySelector("#real-grvt-fills-detail").hidden = true;
    }
    if (connected) details.open = false;
  }
  function grvtTable(rows) {
    if (!rows.length) return '<p class="muted">Sin saldos distintos de cero.</p>';
    return '<div class="tablewrap"><table><thead><tr><th>Activo</th><th>Saldo</th></tr></thead><tbody>' +
      rows.map(function (row) { return '<tr><td>' + esc(row.asset) + '</td><td class="num">' + show(row.balance) + '</td></tr>'; }).join("") +
      '</tbody></table></div>';
  }
  function renderGrvtSnapshot(data) {
    if (perpLiveReview) perpLiveReview.setAccounts(data.trading_accounts || []);
    var target = root && root.querySelector("#real-grvt-state");
    if (!target) return;
    var accounts = (data.trading_accounts || []).map(function (account) {
      var positions = account.positions || [];
      return '<div class="real-grvt-account"><h4>Trading · …' + esc(String(account.sub_account_id).slice(-4)) +
        ' <span class="statusbadge off">' + esc(account.settle_currency) + '</span>' +
        (account.fresh ? '' : ' <span class="statusbadge off" data-help="La hora de evento GRVT supera 60 s de edad; no se presenta como estado actual.">DATO ANTIGUO</span>') + '</h4>' +
        '<p class="muted">Equity ' + show(account.total_equity) + ' · mantenimiento ' + show(account.maintenance_margin) +
        ' · P/L ' + show(account.unrealized_pnl) + '</p>' +
        (positions.length ? '<div class="tablewrap"><table><thead><tr><th>Perpetuo</th><th>Lado</th><th>Tamaño</th><th>Entrada</th><th>Mark</th><th>P/L</th></tr></thead><tbody>' +
          positions.map(function (position) { return '<tr><td>' + esc(position.instrument) + '</td><td>' + esc(position.side) +
            '</td><td class="num">' + show(position.size) + '</td><td class="num">' + show(position.entry_price) +
            '</td><td class="num">' + show(position.mark_price) + '</td><td class="num">' + show(position.unrealized_pnl) + '</td></tr>'; }).join("") +
          '</tbody></table></div>' : '<p class="muted">Sin posiciones abiertas.</p>') + '</div>';
    }).join("");
    target.className = "real-grvt-observation";
    target.innerHTML = '<p class="muted">Observado ' + esc(data.observed_at) + ' · no importado</p>' +
      '<div class="real-grvt-totals"><span>Equity agregada USD <strong>' + show(data.total_equity_usd) +
      '</strong></span><span>Trading incluido USD <strong>' + show(data.trading_equity_usd) + '</strong></span></div>' +
      '<h4>Saldos agregados</h4>' + grvtTable(data.spot_balances_aggregated || []) +
      (accounts || '<p class="muted">Sin subcuentas de trading accesibles.</p>');
  }
  function loadGrvtOrders() {
    var target = root && root.querySelector("#real-grvt-orders-state");
    if (!target || !root.querySelector("#real-grvt-orders").open) return;
    var epoch = ++grvtOrdersEpoch;
    target.className = "state loading";
    target.textContent = "Consultando órdenes GRVT…";
    return request("/exchanges/grvt/open-orders").then(function (data) {
      if (epoch !== grvtOrdersEpoch || !target.isConnected) return;
      var rows = data.orders || [];
      var content = rows.map(function (order) {
        return (order.legs || []).map(function (leg, index) {
          return '<tr><td>…' + esc(String(order.sub_account_id).slice(-4)) + '</td><td>' +
            esc(leg.instrument) + '</td><td>' + esc(leg.side) + '</td><td>' + esc(order.status) +
            '</td><td class="num">' + show(leg.size) + '</td><td class="num">' +
            show(leg.traded_size) + '</td><td class="num">' +
            (leg.limit_price === null ? 'Mercado' : show(leg.limit_price)) +
            '</td><td>' + (order.reduce_only ? 'Sí' : 'No') + '</td><td>' +
            (index ? '' : '<button class="ghost" type="button" data-grvt-order data-sub-account="' +
              esc(order.sub_account_id) + '" data-client-order="' + esc(order.client_order_id) +
              '">Comprobar</button>') + '</td></tr>';
        }).join("");
      }).join("");
      target.className = "real-grvt-observation";
      target.innerHTML = '<p class="amber">Foto puntual: puede omitir órdenes en tránsito. No es una conciliación apta para operar.</p>' +
        '<p class="muted">Observado ' + esc(data.observed_at) + ' · ' + rows.length + ' órdenes · no importado</p>' +
        (rows.length ? '<div class="tablewrap"><table><thead><tr><th>Cuenta</th><th>Instrumento</th><th>Lado</th><th>Estado</th><th>Tamaño</th><th>Ejecutado</th><th>Límite</th><th>Reducir</th><th></th></tr></thead><tbody>' +
          content + '</tbody></table></div>' : '<p class="muted">No se observaron órdenes abiertas en esta consulta.</p>');
    }).catch(function (error) {
      if (epoch !== grvtOrdersEpoch || !target.isConnected) return;
      target.className = "state error";
      target.textContent = error.message;
    });
  }
  function checkGrvtOrder(event) {
    event.preventDefault();
    var form = event.currentTarget;
    var sub = form.elements.sub_account_id.value;
    var client = form.elements.client_order_id.value;
    var detail = root.querySelector("#real-grvt-order-detail");
    var epoch = ++grvtOrderEpoch;
    grvtFillsEpoch += 1;
    root.querySelector("#real-grvt-fills-detail").hidden = true;
    detail.className = "state loading";
    detail.textContent = "Comprobando orden GRVT…";
    var query = new URLSearchParams({ sub_account_id: sub, client_order_id: client });
    request("/exchanges/grvt/order?" + query.toString()).then(function (data) {
      if (epoch !== grvtOrderEpoch || !detail.isConnected) return;
      var order = data.order;
      var legs = (order.legs || []).map(function (leg) {
        return '<li>' + esc(leg.instrument) + ' · ' + esc(leg.side) + ' · ' +
          show(leg.traded_size) + ' / ' + show(leg.size) + '</li>';
      }).join("");
      detail.className = "real-grvt-order-detail";
      detail.innerHTML = '<strong>' + esc(order.status) + '</strong> · …' +
        esc(String(order.sub_account_id).slice(-4)) + ' · ID ' + esc(order.client_order_id) +
        '<ul>' + legs + '</ul><small>Comprobación puntual; no confirma liquidación ni autoriza operativa.</small>';
    }).catch(function (error) {
      if (epoch !== grvtOrderEpoch || !detail.isConnected) return;
      detail.className = "state error";
      detail.textContent = error.message;
    });
  }
  function checkGrvtFills() {
    var form = root.querySelector("#real-grvt-order-form");
    if (!form.reportValidity()) return;
    var detail = root.querySelector("#real-grvt-fills-detail");
    var epoch = ++grvtFillsEpoch;
    detail.hidden = false;
    detail.className = "state loading";
    detail.textContent = "Consultando todas las páginas de fills GRVT…";
    var body = { sub_account_id: form.elements.sub_account_id.value,
                 client_order_id: form.elements.client_order_id.value };
    request("/exchanges/grvt/order-fills/archive", {
      method: "POST", body: JSON.stringify(body)
    }).then(function (data) {
      if (epoch !== grvtFillsEpoch || !detail.isConnected) return;
      var fills = data.fills || [];
      var matched = data.fill_sizes_match_order_state === true;
      var rows = fills.map(function (fill) {
        return '<tr><td>' + esc(fill.trade_id) + '</td><td>' + esc(fill.instrument) +
          '</td><td>' + esc(fill.side) + '</td><td>' + esc(fill.role) +
          '</td><td class="num">' + show(fill.size) + '</td><td class="num">' +
          show(fill.price) + '</td><td class="num">' + show(fill.fee) +
          '</td><td class="num">' + show(fill.builder_fee) +
          '</td><td class="num">' + show(fill.realized_pnl) + '</td><td>' +
          esc(fill.fee_currency) + '</td></tr>';
      }).join("");
      detail.className = "real-grvt-order-detail" + (matched ? "" : " mismatch");
      detail.innerHTML = '<strong>' + (matched ? 'TAMAÑOS COINCIDEN' : 'TAMAÑOS NO COINCIDEN') +
        '</strong> · ' + fills.length + ' fills · páginas agotadas · evidencia ' +
        (data.archive.archived ? 'guardada' : 'ya guardada') +
        '<p class="muted">Comparación con el estado observado de la orden; no acredita saldo, financiación ni liquidación.</p>' +
        (fills.length ? '<div class="tablewrap"><table><thead><tr><th>Trade ID</th><th>Instrumento</th><th>Lado</th><th>Rol</th><th>Tamaño</th><th>Precio</th><th>Comisión</th><th>Builder</th><th>P/L real.</th><th>Divisa</th></tr></thead><tbody>' +
          rows + '</tbody></table></div>' : '<p class="muted">No se observaron fills para esta orden.</p>');
      if (root.querySelector("#real-grvt-archive").open) loadGrvtArchive();
    }).catch(function (error) {
      if (epoch !== grvtFillsEpoch || !detail.isConnected) return;
      detail.className = "state error";
      detail.textContent = error.message;
    });
  }
  function loadGrvtArchive() {
    var target = root && root.querySelector("#real-grvt-archive-list");
    if (!target) return;
    var epoch = ++grvtArchiveEpoch;
    target.className = "state loading";
    target.textContent = "Leyendo evidencia cifrada…";
    request("/exchanges/grvt/evidence").then(function (rows) {
      if (epoch !== grvtArchiveEpoch || !target.isConnected) return;
      target.className = "real-grvt-observation";
      target.innerHTML = rows.length ? '<div class="tablewrap"><table><thead><tr><th>Observado</th><th>Cuenta</th><th>ID cliente</th><th>Estado</th><th>Fills</th><th>Comparación</th><th></th></tr></thead><tbody>' +
        rows.map(function (row) {
          return '<tr><td>' + esc(row.observed_at) + '</td><td>…' +
            esc(String(row.sub_account_id).slice(-4)) + '</td><td>' +
            esc(row.client_order_id) + '</td><td>' + esc(row.status) +
            '</td><td class="num">' + esc(row.fill_count) + '</td><td>' +
            (row.fill_sizes_match_order_state ? 'Coincide' : 'No coincide') +
            '</td><td><button type="button" class="ghost" data-grvt-evidence="' +
            esc(row.id) + '">Abrir</button></td></tr>';
        }).join("") + '</tbody></table></div>' : '<p class="muted">Sin evidencia GRVT guardada.</p>';
    }).catch(function (error) {
      if (epoch !== grvtArchiveEpoch || !target.isConnected) return;
      target.className = "state error";
      target.textContent = error.message;
    });
  }
  function loadGrvtEvidence(identifier) {
    var target = root && root.querySelector("#real-grvt-archive-detail");
    if (!target) return;
    var epoch = ++grvtArchiveEpoch;
    target.hidden = false;
    target.className = "state loading";
    target.textContent = "Abriendo evidencia…";
    request("/exchanges/grvt/evidence/" + encodeURIComponent(identifier)).then(function (data) {
      if (epoch !== grvtArchiveEpoch || !target.isConnected) return;
      target.className = "real-grvt-order-detail" + (data.fill_sizes_match_order_state ? "" : " mismatch");
      target.innerHTML = '<strong>' + esc(data.order.status) + '</strong> · ' +
        esc(data.fills.length) + ' fills · ' +
        (data.fill_sizes_match_order_state ? 'tamaños coinciden' : 'tamaños no coinciden') +
        '<p class="muted">Archivado ' + esc(data.observed_at) + ' · no importado ni conciliado.</p>' +
        (data.fills.length ? '<ul>' + data.fills.map(function (fill) {
          return '<li>' + esc(fill.trade_id) + ' · ' + esc(fill.instrument) + ' · ' +
            show(fill.size) + ' × ' + show(fill.price) + ' · comisión ' +
            show(fill.fee) + ' ' + esc(fill.fee_currency) + '</li>';
        }).join("") + '</ul>' : '<p class="muted">Sin fills.</p>');
    }).catch(function (error) {
      if (epoch !== grvtArchiveEpoch || !target.isConnected) return;
      target.className = "state error";
      target.textContent = error.message;
    });
  }
  function renderGrvtTestnetRecovery(report, target) {
      var labels = {FILL_TOTAL_MISMATCH:"Fills ≠ orden", PREFLIGHT_MISSING:"Sin estado previo",
        POSITION_DELTA_MISMATCH:"Posición distinta", EXTERNAL_FILLS:"Fills externos",
        FUNDING_ACTIVITY:"Financiación", TRANSFER_ACTIVITY:"Transferencias",
        WITHDRAWAL_ACTIVITY:"Retiros", DEPOSIT_CONTEXT:"Depósitos",
        INTEREST_ACTIVITY:"Interés"};
      var checks = {LOOKUP_CANCEL_AND_AUDIT:"Localizar cancelación",
        AUDIT_AFTER_CANCEL:"Auditar tras cancelación", LOOKUP_ORDER_BY_CLIENT_ID:"Localizar orden",
        AUDIT_ORDER_FILLS_ACCOUNT:"Auditar orden y cuenta",
        REVIEW_AUDIT_FINDINGS_NO_RELEASE:"Revisar hallazgos",
        REVIEW_AUDIT_NO_RELEASE:"Revisión manual", PREFLIGHT_OR_ABANDON_UNSENT:"Preparación sin envío",
        REJECTED_ZERO_FILL_ARCHIVED:"Rechazo sin fills cerrado",
        ISOLATED_TESTNET_RECONCILED:"Conciliada · TESTNET aislado",
        ARCHIVED_NO_POST:"Archivada sin envío"};
      var rows = (report.items || []).map(function (item) {
        var findings = item.current_audit_findings || [];
        var source = item.signal_source;
        var sourceLabel = source && source.kind === "GRVT_AUTO_PAPER" ?
          '<br><small class="muted" title="' + esc('Sesión ' + source.session_id +
          ' · versión ' + source.version_id + ' · dataset ' + source.dataset_id +
          ' · acción ' + source.action_request_id) + '">PAPER · ' +
          (source.action === "OPEN" ? "entrada" : "salida") +
          ' · v' + esc(String(source.version_id).slice(0, 8)) + '</small>' : '';
        var quote = item.price_guard;
        var quoteLabel = quote && quote.source === "GRVT_TESTNET_PUBLIC_TICKER" ?
          '<details class="real-testnet-quote"><summary>Precio previo</summary>' +
          '<small>Bid ' + show(quote.bid) + ' · Ask ' + show(quote.ask) +
          '<br>Límite ' + show(quote.limit_price) +
          ' · spread ≤' + esc(quote.max_spread_bps) +
          ' bps · desliz. ≤' + esc(quote.max_slippage_bps) +
          ' bps<br>Testnet · ' + esc(new Date(Number(quote.event_time_ns) / 1000000).toLocaleString("es-ES")) +
          ' · no acredita fills</small></details>' : '';
        var fills = item.fill_summary;
        var fees = fills && Array.isArray(fills.fees_by_currency) ?
          fills.fees_by_currency.map(function (entry) {
            return show(entry[1]) + ' ' + esc(entry[0]);
          }).join(' · ') : '';
        var fillsLabel = fills ?
          '<details class="real-testnet-quote real-testnet-fills"><summary>Fills ' +
          esc(fills.fill_count) + '</summary><small>' +
          (fills.sizes_match_order ? '' : 'Tamaño ≠ orden<br>') +
          'Ejecutado ' + show(fills.filled_size) + ' · PM ' +
          show(fills.weighted_average_price == null ? '—' : fills.weighted_average_price) +
          '<br>Comisión ' + (fees || '—') +
          '<br>' + (item.reconciliation ? 'Saldo y posición cotejados · sólo TESTNET' : 'Observado · saldo no conciliado') + '</small></details>' : '';
        var funding = item.funding_summary;
        var fundingLabel = funding ?
          '<details class="real-testnet-quote real-testnet-funding"><summary data-help="Financiación reportada por GRVT: positivo pagado; negativo cobrado. Neto de orden incluye sus comisiones y financiación, no las de otras órdenes.">Financiación</summary><small>' +
          (funding.linked_to_own_fills === true && funding.paid_usdt != null ?
            'Neto ' + show(funding.paid_usdt) + ' USDT' : 'No verificable · revisar en GRVT') +
          (item.reconciliation && item.reconciliation.result && item.reconciliation.result.net_realized_pnl_usdt != null ?
            '<br>Neto de orden ' + show(item.reconciliation.result.net_realized_pnl_usdt) + ' USDT' : '') +
          '<br>' + (item.reconciliation ? 'Conciliada · TESTNET' : 'Observada · sin conciliación') + '</small></details>' : '';
        var review = esc(checks[item.next_check] || item.next_check || "Revisión requerida") +
          (findings.length ? '<br><small>' + findings.map(function (code) { return esc(labels[code] || code); }).join(" · ") + '</small>' : '');
        if (item.can_reconcile === true && item.journal_status === "OBSERVED") {
          review += '<br><button type="button" class="ghost"' + (hiddenAmounts ? ' disabled' : '') + ' data-grvt-testnet-reconcile data-client-order="' +
            esc(item.client_order_id) + '" data-help="Verifica dos auditorías recientes, saldo y posición exactos y ausencia de actividad ajena. No levanta STOP, no asienta patrimonio REAL ni activa bots.">Conciliar · TESTNET</button>';
        }
        if (item.journal_status === "OBSERVED" && item.external_status === "REJECTED" &&
            /^0(?:\.0+)?$/.test(String(item.external_traded_size)) &&
            item.current_state_audited && item.verified_audit_count >= 2 && !findings.length) {
          review += '<br><button type="button" class="ghost" data-grvt-testnet-close data-client-order="' +
            esc(item.client_order_id) + '" data-help="Cierra sólo un rechazo testnet sin fills tras comprobar todas las auditorías, cuenta y movimientos. No consulta GRVT ni habilita trading real.">Cerrar rechazo</button>';
        }
        var sent = ["POST_CLAIMED", "ACKNOWLEDGED", "OBSERVED"].indexOf(item.journal_status) >= 0;
        if (sent) {
          review += '<div class="real-testnet-row-actions"><button type="button" class="ghost" data-grvt-testnet-audit data-client-order="' +
            esc(item.client_order_id) + '" data-help="Consulta en GRVT testnet la orden, sus fills y la cuenta; guarda una auditoría cifrada. No acredita beneficio real.">Auditar</button>';
          if (!item.cancel_status && ["FILLED", "REJECTED", "CANCELLED"].indexOf(item.external_status) < 0)
            review += '<button type="button" class="ghost danger" data-grvt-testnet-cancel data-client-order="' +
              esc(item.client_order_id) + '" data-help="Para esta subcuenta y reclama una única cancelación testnet. Un acuse no demuestra cierre; audita después.">Parar + cancelar</button>';
          review += '</div>';
        }
        return '<tr><td>' + esc(item.client_order_id) + sourceLabel + quoteLabel + fillsLabel + fundingLabel + '</td><td>' + esc(item.instrument) + ' · ' + esc(item.side) +
          '</td><td>' + esc(item.external_status || item.journal_status) + '</td><td class="num">' + show(item.external_traded_size == null ? '—' : item.external_traded_size) +
          ' / ' + show(item.intended_size) + '</td><td>' + review + '</td></tr>';
      }).join("");
      target.className = "real-grvt-observation";
      target.innerHTML = '<p class="muted">Subcuenta …' + esc(String(report.sub_account_id).slice(-4)) +
        ' · ' + (report.stopped ? 'detenida' : 'sin parada registrada') +
        ' · sin órdenes nuevas desde aquí</p>' + (rows ? '<div class="tablewrap"><table><thead><tr><th>ID cliente</th><th>Instrumento</th><th>Orden</th><th>Ejecutado / previsto</th><th>Revisar</th></tr></thead><tbody>' +
        rows + '</tbody></table></div>' : '<p class="muted">Sin intentos GRVT testnet guardados.</p>');
      var stop = root && root.querySelector("#real-grvt-testnet-stop");
      if (stop) stop.disabled = !!report.stopped;
  }
  function testnetActionState(message, error) {
    var box = root && root.querySelector("#real-grvt-testnet-action-result");
    if (!box) return;
    box.hidden = false;
    box.className = "state " + (error ? "error" : "ok");
    box.textContent = message;
  }
  function reviewGrvtTestnetOrder(button, cancel) {
    var report = lastGrvtTestnetRecovery;
    var form = root && root.querySelector("#real-grvt-testnet-recovery-form");
    var id = button.dataset.clientOrder;
    if (!report || !form || form.elements.sub_account_id.value.trim() !== report.sub_account_id ||
        !report.items.some(function (item) { return item.client_order_id === id; })) return;
    root.querySelector("#real-grvt-testnet-actions").open = true;
    var keyField = root.querySelector("#real-grvt-testnet-key");
    var key = keyField.value.trim();
    keyField.value = "";
    if (!key) { testnetActionState("Introduce una API key de GRVT testnet.", true); keyField.focus(); return; }
    if (cancel && !window.confirm("¿Parar esta subcuenta y enviar una sola cancelación a GRVT TESTNET? El acuse no confirma cierre. Después tendrás que auditar el ID.")) return;
    var issuedEpoch = grvtTestnetRecoveryEpoch;
    button.disabled = true;
    testnetActionState(cancel ? "Parando y cancelando en testnet…" : "Auditando testnet…", false);
    request(cancel ? "/exchanges/grvt/testnet/orders/stop-cancel" :
      "/exchanges/grvt/testnet/orders/audit", {
      method: "POST", body: JSON.stringify({api_key:key, sub_account_id:report.sub_account_id,
        client_order_id:id, confirmation: cancel ? "DETENER_Y_CANCELAR_GRVT_TESTNET" : "AUDITAR_GRVT_TESTNET"})
    }).then(function (result) {
      if (result.network !== "TESTNET" || result.client_order_id !== id ||
          result.mainnet_order_sent !== false || result.execution_unverified !== true)
        throw new Error("Resultado testnet no verificable");
      testnetActionState(cancel ? "Parada activa · estado externo " +
        (result.external_status || "sin verificar") + ". Audita el ID." :
        "Auditoría guardada · " + (result.external_status || "sin estado") +
        " · ejecutado " + show(result.traded_size == null ? "—" : result.traded_size), false);
    }).catch(function (error) {
      testnetActionState(error.message, true);
    }).finally(function () {
      if (issuedEpoch === grvtTestnetRecoveryEpoch && form.isConnected) form.requestSubmit();
      if (button.isConnected) button.disabled = false;
    });
  }
  function stopGrvtTestnetSubaccount(event) {
    var report = lastGrvtTestnetRecovery;
    var form = root && root.querySelector("#real-grvt-testnet-recovery-form");
    if (!report || !form || form.elements.sub_account_id.value.trim() !== report.sub_account_id) {
      testnetActionState("Consulta primero la subcuenta testnet.", true); return;
    }
    if (report.stopped) return;
    if (!window.confirm("¿Detener nuevos envíos GRVT testnet en esta subcuenta? No cancela órdenes externas.")) return;
    var button = event.currentTarget;
    button.disabled = true;
    request("/exchanges/grvt/testnet/subaccounts/" + encodeURIComponent(report.sub_account_id) + "/stop", {
      method:"POST", body:JSON.stringify({confirmation:"DETENER_GRVT_TESTNET"})
    }).then(function (result) {
      if (result.network !== "TESTNET" || result.stopped !== true ||
          result.external_orders_cancelled !== false || result.mainnet_order_sent !== false)
        throw new Error("Parada testnet no verificable");
      testnetActionState("Subcuenta detenida. Las órdenes externas no se cancelaron.", false);
    }).catch(function (error) {
      testnetActionState(error.message, true);
    }).finally(function () {
      if (form.isConnected) form.requestSubmit();
      if (button.isConnected) button.disabled = false;
    });
  }
  function reconcileGrvtTestnet(button) {
    var report = lastGrvtTestnetRecovery;
    var form = root && root.querySelector("#real-grvt-testnet-recovery-form");
    var id = button.dataset.clientOrder, issued = sessionEpoch;
    if (!report || !form || form.elements.sub_account_id.value.trim() !== report.sub_account_id ||
        hiddenAmounts || !report.items.some(function (item) { return item.client_order_id === id && item.can_reconcile; })) return;
    if (!confirm("¿Conciliar esta orden TESTNET con las dos últimas auditorías? Se exigirán fills, saldo y posición exactos, sin actividad ajena. No mueve fondos, no contabiliza patrimonio REAL ni quita STOP. Otra orden seguirá requiriendo una señal nueva y revisión de riesgo.")) return;
    button.disabled = true;
    request("/exchanges/grvt/testnet/orders/reconcile",{method:"POST",body:JSON.stringify({
      sub_account_id:report.sub_account_id,client_order_id:id,confirmation:"CONCILIAR_GRVT_TESTNET"})
    }).then(function (result) {
      if (issued !== sessionEpoch || !form.isConnected) return;
      if (result.network !== "TESTNET" || result.journal_status !== "RECONCILED" ||
          result.client_order_id !== id || result.can_submit !== false || result.new_order_sent !== false ||
          result.mainnet_order_sent !== false || !result.reconciliation || result.reconciliation.real_book_events_created !== false)
        throw new Error("Conciliación testnet no verificable");
      notice("Conciliación TESTNET guardada · sin órdenes nuevas ni cambios en cartera REAL.",false);
      form.requestSubmit();
    }).catch(function (error) {
      if (issued !== sessionEpoch || !form.isConnected) return;
      notice(error.message + " · consulta el registro antes de actuar.",true);
      form.requestSubmit();
    }).finally(function () { if (button.isConnected) button.disabled = false; });
  }
  function closeGrvtTestnetRejected(button) {
    var report = lastGrvtTestnetRecovery;
    var form = root && root.querySelector("#real-grvt-testnet-recovery-form");
    var id = button.dataset.clientOrder;
    if (!report || !form || form.elements.sub_account_id.value.trim() !== report.sub_account_id ||
        !report.items.some(function (item) { return item.client_order_id === id; })) return;
    if (!window.confirm("¿Cerrar este rechazo GRVT testnet sin ejecución? ORBIS verificará dos auditorías recientes y todo el historial local. No contactará GRVT ni enviará órdenes.")) return;
    button.disabled = true;
    request("/exchanges/grvt/testnet/rejected-zero-fill/close", {
      method: "POST", body: JSON.stringify({sub_account_id: report.sub_account_id,
        client_order_id: id, confirmation: "CERRAR_RECHAZO_TESTNET"})
    }).then(function (result) {
      if (result.network !== "TESTNET" || result.journal_status !== "REJECTED_CLOSED" ||
          result.can_submit !== false || result.new_order_sent !== false)
        throw new Error("Cierre testnet no verificable");
      lastGrvtTestnetRecovery = null;
      notice("Rechazo testnet archivado; no se envió ninguna orden nueva.", false);
      if (form.isConnected) form.requestSubmit();
    }).catch(function (error) {
      notice(error.message, true);
      if (button.isConnected) button.disabled = false;
    });
  }
  function loadGrvtTestnetRecovery(event) {
    event.preventDefault();
    var form = event.currentTarget;
    var target = root && root.querySelector("#real-grvt-testnet-recovery-result");
    if (!target) return;
    var subAccount = form.elements.sub_account_id.value.trim();
    var epoch = ++grvtTestnetRecoveryEpoch;
    lastGrvtTestnetRecovery = null;
    target.className = "state loading";
    target.textContent = "Leyendo registro cifrado…";
    request("/exchanges/grvt/testnet/recovery?sub_account_id=" + encodeURIComponent(subAccount)).then(function (report) {
      if (epoch !== grvtTestnetRecoveryEpoch) return;
      if (report.can_submit !== false) throw new Error("Informe testnet sin bloqueo de envío verificable");
      lastGrvtTestnetRecovery = report;
      var current = root && root.querySelector("#real-grvt-testnet-recovery-result");
      if (current) {
        root.querySelector("#real-grvt-testnet-recovery-form").elements.sub_account_id.value = report.sub_account_id;
        renderGrvtTestnetRecovery(report, current);
      }
    }).catch(function (error) {
      if (epoch !== grvtTestnetRecoveryEpoch) return;
      var current = root && root.querySelector("#real-grvt-testnet-recovery-result");
      if (current) { current.className = "state error"; current.textContent = error.message; }
    });
  }
  function loadGrvt() {
    var epoch = ++grvtConnectionEpoch, target = root.querySelector('#real-grvt-state');
    request("/exchanges/grvt/status").then(function (status) {
      if (epoch !== grvtConnectionEpoch || !target.isConnected) return;
      grvtConnected(status.connected);
      if (status.connected) return refreshGrvt();
      grvtState("Sin conexión. Introduce una clave View nueva.", false);
    }).catch(function (error) { if (epoch === grvtConnectionEpoch && target.isConnected) grvtState(error.message, true); });
  }
  function connectGrvt(event) {
    event.preventDefault();
    var epoch = ++grvtConnectionEpoch;
    var form = event.currentTarget;
    var key = form.elements.api_key.value;
    form.reset();
    grvtOrdersEpoch += 1;
    grvtOrderEpoch += 1;
    grvtFillsEpoch += 1;
    var orders = root.querySelector("#real-grvt-orders");
    orders.open = false;
    root.querySelector("#real-grvt-orders-state").textContent = "Abre para consultar.";
    root.querySelector("#real-grvt-order-detail").textContent = "Introduce los identificadores para comprobar una orden.";
    root.querySelector("#real-grvt-fills-detail").hidden = true;
    var button = form.querySelector('button[type="submit"]');
    button.disabled = true;
    grvtState("Consultando GRVT…", false);
    request("/exchanges/grvt/connect", { method: "POST", body: JSON.stringify({ api_key: key }) })
      .then(function (data) { if (epoch === grvtConnectionEpoch && form.isConnected) { grvtConnected(true); renderGrvtSnapshot(data); } })
      .catch(function (error) { if (epoch === grvtConnectionEpoch && form.isConnected) { grvtConnected(false); grvtState(error.message, true); } })
      .finally(function () { if (epoch === grvtConnectionEpoch && form.isConnected) button.disabled = false; });
  }
  function refreshGrvt() {
    var epoch = grvtConnectionEpoch, target = root.querySelector('#real-grvt-state');
    grvtState("Actualizando GRVT…", false);
    return request("/exchanges/grvt/snapshot").then(function (data) {
      if (epoch !== grvtConnectionEpoch || !target.isConnected) return;
      renderGrvtSnapshot(data);
      if (root.querySelector("#real-grvt-orders").open) loadGrvtOrders();
    })
      .catch(function (error) { if (epoch === grvtConnectionEpoch && target.isConnected) grvtState(error.message, true); });
  }
  function disconnectGrvt() {
    var epoch = ++grvtConnectionEpoch, target = root.querySelector('#real-grvt-state');
    request("/exchanges/grvt/disconnect", { method: "POST" }).then(function () {
      if (epoch !== grvtConnectionEpoch || !target.isConnected) return;
      grvtConnected(false);
      grvtState("Desconectado. Sesión GRVT retirada de memoria.", false);
    }).catch(function (error) { if (epoch === grvtConnectionEpoch && target.isConnected) grvtState(error.message, true); });
  }
  function loadBackups() {
    request("/vault/backups").then(function (backups) {
      var select = root.querySelector("#real-restore-name");
      if (!select) return;
      select.replaceChildren();
      var empty = document.createElement("option");
      empty.value = "";
      empty.textContent = backups.length ? "Elige una copia" : "No hay copias";
      select.appendChild(empty);
      backups.forEach(function (item) {
        var option = document.createElement("option");
        option.value = item.name;
        option.textContent = item.name + " · " + item.modified_at;
        select.appendChild(option);
      });
      root.querySelector("#real-verify-backup").disabled = true;
      var status = root.querySelector("#real-backup-check");
      status.hidden = true; status.textContent = "";
      root.querySelector("#real-restore-empty").hidden = backups.length !== 0;
    }).catch(function (error) { notice(error.message, true); });
  }
  function verifyBackup() {
    var select = root && root.querySelector("#real-restore-name");
    var button = root && root.querySelector("#real-verify-backup");
    var status = root && root.querySelector("#real-backup-check");
    if (!select || !button || !status || !select.value) return;
    var name = select.value;
    button.disabled = true;
    status.hidden = false;
    status.className = "state loading";
    status.textContent = "Comprobando copia cifrada…";
    request("/vault/backups/" + encodeURIComponent(name) + "/verify").then(function (result) {
      if (!status.isConnected || select.value !== name) return;
      status.className = "state ok";
      status.textContent = "Copia comprobada · " + result.accounts + (result.accounts === 1 ? " cuenta" : " cuentas") +
        " · " + result.events + (result.events === 1 ? " movimiento" : " movimientos") +
        ". Se verificará de nuevo al restaurar.";
    }).catch(function (error) {
      if (!status.isConnected || select.value !== name) return;
      status.className = "state error";
      status.textContent = "No se pudo comprobar la copia: " + error.message;
    }).finally(function () {
      if (button.isConnected) button.disabled = !select.value;
    });
  }
  function restoreBackup(event) {
    event.preventDefault();
    var name = root.querySelector("#real-restore-name").value;
    var confirmation = root.querySelector("#real-restore-confirm").value;
    if (!name || confirmation !== "RESTAURAR") {
      notice("Elige una copia y escribe RESTAURAR exactamente.", true);
      return;
    }
    var button = event.currentTarget.querySelector("button");
    button.disabled = true;
    request("/vault/restore", { method: "POST", body: JSON.stringify({ name: name, confirmation: confirmation }) })
      .then(function (result) {
        forgetSession();
        afterRestoreMessage = { text: "Copia restaurada. El estado anterior quedó cifrado como " + result.previous_vault_backup + ". Desbloquea de nuevo para revisar los datos.", error: false };
        showLocked();
      }).catch(function (error) {
        forgetSession();
        afterRestoreMessage = { text: "No se restauró la copia: " + error.message + ". Desbloquea para revisar el estado.", error: true };
        showLocked();
      });
  }
  function updateKind() {
    var kind = root.querySelector("#real-kind").value;
    var trade = kind === "BUY" || kind === "SELL";
    var movement = trade || kind === "TRANSFER" || kind === "ADJUSTMENT_IN" || kind === "ADJUSTMENT_OUT";
    var price = trade || kind === "ADJUSTMENT_IN";
    [[".real-asset-movement-field", movement], [".real-price-field", price], [".real-fee-field", trade]].forEach(function (item) {
      var element = root.querySelector(item[0]);
      element.hidden = !item[1];
      element.querySelector("input").required = item[1];
    });
    var cash = root.querySelector(".real-cash-field");
    cash.hidden = movement;
    cash.querySelector("input").required = !movement;
    var asset = root.querySelector("#real-asset-field");
    asset.hidden = !movement;
    asset.querySelector("input").required = movement;
    var note = root.querySelector(".real-note-field");
    note.hidden = !(kind === "TRANSFER" || kind === "ADJUSTMENT_IN" || kind === "ADJUSTMENT_OUT");
    note.querySelector("input").required = kind === "ADJUSTMENT_IN" || kind === "ADJUSTMENT_OUT";
  }
  function uuid() {
    if (crypto.randomUUID) return crypto.randomUUID();
    var bytes = crypto.getRandomValues(new Uint8Array(16));
    bytes[6] = (bytes[6] & 15) | 64;
    bytes[8] = (bytes[8] & 63) | 128;
    var hex = Array.from(bytes, function (item) { return item.toString(16).padStart(2, "0"); }).join("");
    return hex.slice(0, 8) + "-" + hex.slice(8, 12) + "-" + hex.slice(12, 16) + "-" + hex.slice(16, 20) + "-" + hex.slice(20);
  }
  function saveEvent(event) {
    event.preventDefault();
    var form = event.currentTarget;
    var fields = new FormData(form);
    var kind = fields.get("kind");
    var trade = kind === "BUY" || kind === "SELL";
    var movement = trade || kind === "TRANSFER" || kind === "ADJUSTMENT_IN" || kind === "ADJUSTMENT_OUT";
    var price = trade || kind === "ADJUSTMENT_IN";
    var annotated = kind === "TRANSFER" || kind === "ADJUSTMENT_IN" || kind === "ADJUSTMENT_OUT";
    var body = {
      request_id: uuid(), kind: kind,
      quote_currency: String(fields.get("quote_currency")).trim().toUpperCase(),
      occurred_at: new Date(String(fields.get("occurred_at"))).toISOString(),
      asset: movement ? String(fields.get("asset")).trim().toUpperCase() : null,
      quantity: movement ? String(fields.get("quantity")).trim() : null,
      unit_price: price ? String(fields.get("unit_price")).trim() : null,
      amount: movement ? null : String(fields.get("amount")).trim(),
      fee: trade ? String(fields.get("fee")).trim() : "0",
      note: annotated && fields.get("note") ? String(fields.get("note")).trim() : null
    };
    var button = form.querySelector('button[type="submit"]');
    button.disabled = true;
    request("/accounts/" + encodeURIComponent(accountId) + "/events", { method: "POST", body: JSON.stringify(body) })
      .then(function () {
        clearMexcComparison();
        notice("Movimiento guardado en la cartera real.", false);
        form.querySelector('[name="quantity"]').value = "";
        form.querySelector('[name="unit_price"]').value = "";
        form.querySelector('[name="amount"]').value = "";
        form.querySelector('[name="note"]').value = "";
        return refresh();
      }).catch(function (error) { notice(error.message, true); })
      .finally(function () { button.disabled = false; });
  }
  function refresh(passive) {
    if (!accountId) return Promise.resolve();
    loadMexcRoute();
    var selectedAccount = accountId, renderedRoot = root, epoch = ++realRefreshEpoch;
    var id = encodeURIComponent(selectedAccount);
    function current() {
      return epoch === realRefreshEpoch && accountId === selectedAccount &&
        root === renderedRoot && renderedRoot.isConnected;
    }
    return request("/accounts/" + id + "/snapshot?include_events=true", null, passive === true)
      .then(function (snapshot) {
        if (!current()) return false;
        var events = snapshot.events;
        if (portfolioHistory) portfolioHistory.setCurrencies(Object.keys(snapshot.cash || {}).sort());
        var accepted = events.some(function (item) { return !!item.accounting; });
        var exportButton = root.querySelector('#real-export-csv');
        if (exportButton) {
          exportButton.textContent = accepted ? 'CSV · Original' : 'Exportar CSV';
          exportButton.dataset.help = accepted ? 'Exporta los datos originales; el criterio aceptado se recupera con una copia cifrada.' :
            'Descarga los movimientos de esta cuenta como CSV local sin cifrar; requiere confirmación.';
        }
        if (window.OrbisWallet) window.OrbisWallet.setBookSnapshot(snapshot);
        var p = root.querySelector("#real-positions");
        if (p) {
          var rows = snapshot.positions.filter(function (item) { return item.quantity !== "0"; }).map(function (item) {
            return '<tr><td>' + esc(item.asset) + '</td><td class="num">' + show(item.quantity) + '</td><td class="num">' + showSummary(item.average_cost) +
              ' ' + esc(item.quote_currency) + '</td><td class="num">' + showSummary(item.realized_pnl) + ' ' + esc(item.quote_currency) + '</td></tr>';
          }).join("");
          p.className = "";
          p.innerHTML = rows ? '<div class="tablewrap"><table><thead><tr><th>Activo</th><th>Cantidad</th><th>Coste medio</th><th>Resultado realizado</th></tr></thead><tbody>' + rows + '</tbody></table></div>' : '<p class="muted">Sin posiciones abiertas.</p>';
          var flows = Object.keys(snapshot.contributions).map(function (currency) {
            return '<p class="muted">Aportaciones: <span class="num">' + show(snapshot.contributions[currency]) + ' ' + esc(currency) + '</span></p>';
          }).join("");
          var realized = Object.keys(snapshot.realized_pnl || {}).map(function (currency) {
            return '<p class="muted" data-help="Resultado neto de ventas y gastos registrados, incluidas posiciones ya cerradas. No cuenta aportaciones como beneficio.">Realizado: <span class="num">' +
              showSummary(snapshot.realized_pnl[currency]) + ' ' + esc(currency) + '</span></p>';
          }).join("");
          p.innerHTML += flows + realized;
        }
        var h = root.querySelector("#real-history");
        if (h) {
          var entries = events.slice().reverse().map(function (original) {
            var context = original.accounting;
            var item = context ? Object.assign({},original,{occurred_at:context.occurred_at,gross_amount:context.gross_amount}) : original;
            var dateHelp = context ? ' data-help="' + esc('Fecha original: '+original.occurred_at+' · Registrado: '+original.recorded_at) + '"' : '';
            var amountHelp = context && context.gross_amount !== original.gross_amount ?
              ' data-help="' + (hiddenAmounts ? 'Coste original oculto' : esc('Coste original: '+original.gross_amount+' '+original.quote_currency)) + '"' : '';
            var paired = ["TRANSFER_IN", "TRANSFER_OUT", "CASH_TRANSFER_IN", "CASH_TRANSFER_OUT"].includes(item.kind);
            var amountLabel = paired && item.asset ? "Coste " + show(item.gross_amount) + ' ' + esc(item.quote_currency) :
              item.kind === "TRANSFER" || item.kind === "ADJUSTMENT_OUT" || item.kind === "FEE" && item.asset ? "—" :
                show(item.gross_amount) + ' ' + esc(item.quote_currency);
            var feeLabel = item.kind === "FEE" ? show(item.asset ? item.quantity : item.gross_amount) + ' ' + esc(item.asset || item.quote_currency) : show(item.fee) + ' ' + esc(item.fee_asset || "");
            return '<tr data-event-id="' + esc(item.id) + '"><td' + dateHelp + '>' + esc(item.occurred_at.slice(0, 16).replace("T", " ")) + '</td><td>' + esc(item.kind === "FEE" ? "Comisión" : item.kind) + '</td><td>' + esc(item.asset || "—") + '</td>' +
              '<td class="num">' + show(item.quantity || "—") + '</td><td class="num"' + amountHelp + '>' + amountLabel + '</td><td class="num">' + feeLabel + '</td><td>' + esc(item.note || "—") + '</td></tr>';
          }).join("");
          h.className = "";
          h.innerHTML = entries ? (accepted ? '<span class="badge" data-accounting-method data-help="Fechas, orden y costes del criterio contable aceptado. Los datos originales se conservan.">Criterio aceptado</span>' : '') +
            '<div class="tablewrap"><table><thead><tr><th>' + (accepted ? 'Fecha efectiva UTC' : 'Fecha UTC') + '</th><th>Tipo</th><th>Activo</th><th>Cantidad</th><th>Importe</th><th>Comisión</th><th>Nota</th></tr></thead><tbody>' + entries + '</tbody></table></div>' : '<p class="muted">Todavía no hay movimientos.</p>';
        }
        return true;
      }).then(function (shouldValue) {
        if (!shouldValue || !current()) return;
        return request("/accounts/" + id + "/valuation", null, passive === true).then(function (valuation) {
          var target = root.querySelector("#real-market-values");
          if (!target || !current()) return;
          var totals = valuation.currencies.map(function (item) {
            var label = item.complete ? "Patrimonio " + esc(item.currency) + ": " + show(item.equity) :
              "Valoración " + esc(item.currency) + " incompleta · " + valuationGap(item.reason);
            var pnl = item.total_pnl == null ? '' :
              '<p class="num" data-help="Resultado de ventas y gastos más el no realizado de posiciones abiertas, según costes y precios registrados. No incluye aportaciones como beneficio ni es un cálculo fiscal.">Resultado registrado ' +
              esc(item.currency) + ': ' + showSummary(item.total_pnl) +
              ' <small>(realizado ' + showSummary(item.realized_pnl) +
              ' · no realizado ' + showSummary(item.unrealized_pnl) + ')</small></p>';
            return '<p class="num">' + label + '</p>' + pnl;
          }).join("");
          var references = valuation.positions.map(function (item) {
            var label = item.market_value === null ? "Sin precio actual" :
              showSummary(item.market_value) + ' ' + esc(item.quote_currency) + ' · no realizado ' + showSummary(item.unrealized_pnl);
            return '<p class="real-reference"><strong>' + esc(item.asset) + '</strong> · ' + label +
              '<br><small>Fuente: ' + esc(item.price_source || "no disponible") + ' · ' +
              esc(item.price_as_of || "sin fecha") + (item.price_stale ? " · obsoleto" : "") + '</small></p>';
          }).join("");
          var opened = openValuationAllocations(target);
          var allocations = valuation.currencies.map(function (item) {
            return valuationAllocation(item, opened.has(item.currency));
          }).join("");
          target.className = "real-valuation";
          target.innerHTML = totals + allocations + references;
        }).catch(function (error) {
          if (!current()) return;
          var target = root.querySelector("#real-market-values");
          if (target) { target.textContent = "Valoración no disponible: " + error.message; target.className = "state error"; }
        });
      }).catch(function (error) { if (current()) notice(error.message, true); });
  }
  function protectRealView() {
      request("/vault/protect-view",{method:"POST"}).then(function (result) {
        afterRestoreMessage = result.review_required ?
          {text:"Vista protegida · bóveda bloqueada; el archivo del bot requiere revisión antes de operar.",error:true} :
          {text:result.operating ? "Vista protegida · la operativa autorizada sigue en memoria hasta su caducidad." : "Vista protegida · bóveda bloqueada.",error:false};
        forgetSession(); showLocked();
      }).catch(function () {
        afterRestoreMessage = {text:"Vista protegida · estado operativo sin confirmar.",error:true};
        forgetSession(); showLocked();
      });
  }

  function renderCreateAccount() {
    shell('<div class="real-toolbar"><button id="real-hide" class="ghost" type="button">' +
      (hiddenAmounts ? 'Mostrar importes' : 'Ocultar importes') + '</button>' +
      '<button id="real-protect" class="ghost" type="button">Proteger vista</button>' +
      '<button id="real-lock" class="ghost" type="button">Bloquear</button></div>' +
      '<h3 data-help="La cuenta es un alias local; no guarda direcciones de billetera.">Primera cuenta</h3>' +
      '<form id="real-new-account" class="row"><label class="field" data-help="Usa un alias; las direcciones completas de billetera se rechazan.">Nombre<input name="name" maxlength="100" required placeholder="Principal"></label>' +
      '<label class="field">Divisa base<input name="base_currency" maxlength="12" value="EUR" required></label>' +
      '<button type="submit">Crear cuenta</button></form>' + grvtTestnetRecoveryPanel(), true);
    root.querySelector("#real-hide").addEventListener("click", function () {
      hiddenAmounts = !hiddenAmounts;
      renderCreateAccount();
    });
    root.querySelector("#real-lock").addEventListener("click", function () {
      request("/vault/lock", { method: "POST" }).then(function () {
        forgetSession();
        showLocked();
      }).catch(function (error) { notice(error.message, true); });
    });
    root.querySelector("#real-protect").addEventListener("click", protectRealView);
    bindGrvtTestnetRecovery();
    root.querySelector("#real-new-account").addEventListener("submit", function (event) {
      event.preventDefault();
      var fields = new FormData(event.currentTarget);
      request("/accounts", { method: "POST", body: JSON.stringify({
        name: String(fields.get("name")).trim(), base_currency: String(fields.get("base_currency")).trim().toUpperCase()
      }) }).then(function (account) { accountId = account.id; load(); })
        .catch(function (error) { notice(error.message, true); });
    });
  }
  function load(message) {
    var issuedLoad = ++realLoadEpoch;
    var issuedRoot = root;
    var issuedSession = sessionEpoch;
    function current() {
      return issuedLoad === realLoadEpoch && issuedRoot === root &&
        issuedRoot.isConnected && issuedSession === sessionEpoch &&
        window.location.hash === "#/real";
    }
    request("/accounts").then(function (accounts) {
      if (!current()) return;
      startMexcSupervisorPoll();
      if (!accounts.length) {
        if (!root.querySelector("#real-new-account")) renderCreateAccount();
        if (afterUnlockMessage) { notice(afterUnlockMessage, false); afterUnlockMessage = null; }
        return;
      }
      if (!accounts.some(function (item) { return item.id === accountId; })) accountId = accounts[0].id;
      renderAccountPicker(accounts);
      if (message) notice(message, false);
      if (afterUnlockMessage) { notice(afterUnlockMessage, false); afterUnlockMessage = null; }
    }).catch(function (error) {
      if (!current()) return;
      forgetSession();
      notice(error.message, true);
      showLocked();
    });
  }
  function render(container) {
    root = container;
    if (token) {
      shell('<p class="state loading" role="status">Cargando cuentas…</p>', true);
      return load();
    }
    shell('<p class="state loading">Comprobando la bóveda local…</p>', false);
    request("/vault/status").then(function (state) {
      if (state.review_required && !afterRestoreMessage) afterRestoreMessage = {text:"El archivo del bot requiere revisión antes de operar.",error:true};
      signIn(state.exists,state.operating);
    })
      .catch(function (error) { shell('<p class="state error">' + esc(error.message) + '</p>', false); });
  }
  function renderDashboard(container) {
    if (!container) return;
    var issuedDashboard = ++dashboardEpoch;
    if (!token) {
      container.innerHTML = '<p class="muted">Importes reales protegidos: abre la vista con tu contraseña.</p><p><a class="cta" href="#/real">Abrir cartera real</a></p>';
      return;
    }
    var issuedToken = token;
    var issuedSession = sessionEpoch;
    function current() {
      return container.isConnected && issuedDashboard === dashboardEpoch &&
        issuedSession === sessionEpoch && issuedToken === token;
    }
    request("/accounts", undefined, true).then(function (accounts) {
      if (!current()) return;
      if (!accounts.length) {
        container.innerHTML = '<p class="muted">Sin cuentas reales.</p><p><a href="#/real">Crear cuenta</a></p>';
        return;
      }
      var opened = new Map(Array.from(container.querySelectorAll("[data-real-dashboard-row]"), function (row) {
        return [row.dataset.realDashboardRow, openValuationAllocations(row)];
      }));
      container.innerHTML = accounts.map(function (account) {
        return '<div data-real-dashboard-row="' + esc(account.id) + '"><p><strong>' + esc(account.name) +
          '</strong> · REAL</p><p class="muted" role="status">Cargando valoración…</p></div>';
      }).join("") + '<p><a href="#/real">Ver cartera real</a></p>';
      var rows = container.querySelectorAll("[data-real-dashboard-row]");
      accounts.forEach(function (account, index) {
        request("/accounts/" + encodeURIComponent(account.id) + "/valuation", undefined, true).then(function (valuation) {
          if (!current() || !rows[index].isConnected) return;
          var previous = opened.get(account.id);
          var totals = valuation.currencies.map(function (item) {
            return '<li><strong>' + esc(item.currency) + '</strong> · ' + (item.complete ? 'Patrimonio ' + showSummary(item.equity) :
              'valoración incompleta · ' + valuationGap(item.reason)) +
              (item.total_pnl == null ? '' : ' · Resultado ' + showSummary(item.total_pnl)) +
              '<small class="real-dashboard-metric">Realizado ' + showSummary(item.realized_pnl) +
              (item.unrealized_pnl == null ? '' : ' · No realizado ' + showSummary(item.unrealized_pnl)) + '</small>' +
              '<small class="real-dashboard-metric">Aportado ' + showSummary(item.contributions) +
              ' · Retirado ' + showSummary(item.withdrawals) + '</small>' +
              valuationAllocation(item, previous && previous.has(item.currency)) + '</li>';
          }).join("");
          rows[index].querySelector('[role="status"]').outerHTML = totals ?
            '<ul class="miniops">' + totals + '</ul>' : '<p class="muted">Sin saldos valorables.</p>';
        }).catch(function (error) {
          if (current() && rows[index].isConnected) {
            rows[index].querySelector('[role="status"]').textContent =
              'Valoración no disponible: ' + error.message;
          }
        });
      });
    }).catch(function (error) {
      if (error.message === "Real vault is locked") {
        forgetSession();
      }
      if (current()) container.innerHTML = '<p class="muted">Resumen real no disponible: ' + esc(error.message) + '</p><p><a href="#/real">Abrir cartera</a></p>';
    });
  }
  window.addEventListener("hashchange", function () {
    if (window.location.hash !== "#/real") {
      clearPortfolioHistory();
      clearMexcOpening();
      pendingMexcSource = null;
      pendingGrvtSource = null;
      pendingDexSource = null;
      mexcLimitsFromSource = null;
      if (window.OrbisMexcBots) window.OrbisMexcBots.clear();
    }
  });
  window.OrbisReal = { render: render, renderDashboard: renderDashboard, prepareMexcSource: prepareMexcSource,
    prepareGrvtSource: prepareGrvtSource, prepareDexSource: prepareDexSource };
})();
