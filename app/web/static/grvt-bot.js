/* Ciclos automáticos TESTNET acotados. Sin autostart ni secretos guardados. */
(function () {
  "use strict";
  var root = null, api = null, epoch = 0, serial = 0, timer = null, reading = false;
  var busy = false, unknown = false, revision = null, records = [], offset = 0;
  var rendered = null;
  var stopState = null;
  var fillChart = null, fillRequest = 0, selectedRun = null, chartLoading = false;
  var fillRoles = {ENTRY:["Entrada","#54d69a"],STOP_LOSS:["Stop","#ff817d"],
    TAKE_PROFIT:["Objetivo","#71b8ff"],IOC:["Reducción IOC","#f2be68"]};
  var base = "/exchanges/grvt/testnet/bots";
  var labels = {WAITING_SIGNAL:"Esperando señal",PREPARING_ENTRY:"Preparando entrada",SENDING_ENTRY:"Enviando entrada",
    STARTING:"Iniciando seguimiento",WATCHING:"Observando posición",CANCELLING:"Cancelando protecciones",
    WAITING_AUDIT:"Esperando auditoría",AUDITING_GROUP:"Auditando grupo",GROUP_RECONCILED:"Grupo conciliado",
    CLOSE_SENT:"Cierre enviado",AUDITING_CLOSE:"Auditando cierre",CLOSE_RECONCILED:"Cierre conciliado",
    RECOVERING:"Recuperando respuesta",STOP_REQUESTED:"Deteniendo",STOPPED:"Detenido",COMPLETE:"Ciclo completado",
    EXPIRED:"Permiso vencido",NEEDS_REVIEW:"Revisar operativa",GAP:"Datos interrumpidos",INTERRUPTED:"Interrumpido",
    ATTEMPT_LIMIT:"Límite de intentos",REVIEW_UNSENT:"Intento sin enviar",REARMING:"Preparando siguiente ciclo",
    ENTRY_LIMIT:"Entradas completadas",BUDGET_LIMIT:"Presupuesto agotado",LOSS_LIMIT:"Límite de pérdida neta"};
  function node(tag, text, cls) { var el = document.createElement(tag); el.textContent = text; if (cls) el.className = cls; return el; }
  function clearSecrets(host) { if (host) host.querySelectorAll('input[type="password"],[name="expected_signer"]').forEach(function (el) { el.value = ""; }); }
  function clear() {
    epoch += 1; serial += 1; clearSecrets(root); if (timer !== null) clearTimeout(timer);
    closeFillChart();
    root = api = timer = null; busy = unknown = reading = false; revision = null; records = []; offset = 0; rendered = null; stopState = null;
  }
  function current(host, at) { return root === host && host.isConnected && epoch === at && api; }
  function sub() { return root.querySelector('[name="sub_account_id"]').value.trim(); }
  function notice(message, error) { if (root) { var box = root.querySelector('[data-bot-notice]'); box.textContent = message; box.className = "state " + (error ? "error" : "muted"); } }
  function aggregateFields() {
    if (!root) return;
    var entries = Number(root.querySelector('[name="max_entries"]').value), bounded = entries > 1;
    root.querySelectorAll('[data-bot-aggregate]').forEach(function (label) {
      label.hidden = !bounded; label.querySelector('input').disabled = !bounded; label.querySelector('input').required = bounded;
    });
    root.querySelector('[data-bot-mode]').textContent = bounded ? "HASTA " + entries + " ENTRADAS" : "1 CICLO";
    root.querySelector('[data-bot-confirm-text]').textContent = bounded ?
      "Autorizar hasta " + entries + " entradas y sus salidas · TESTNET" : "Autorizar 1 entrada y sus salidas · TESTNET";
  }
  function guards() {
    if (!root) return;
    var live = records.some(function (record) { return record.active; });
    root.querySelector('[data-bot-fields]').disabled = busy || unknown || live || !api.canSend();
    root.querySelector('[name="sub_account_id"]').disabled = busy || unknown || live;
    root.querySelector('[data-bot-start]').disabled = busy || unknown || live || !revision || !!(stopState && stopState.stopped) || !api.canSend();
    root.querySelector('[data-bot-stop-all]').disabled = !/^\d{1,30}$/.test(sub()) || !api.canSend();
    root.querySelector('[data-bot-prev]').disabled = busy || offset === 0;
    root.querySelectorAll('[data-bot-rearm]').forEach(function (button) {
      button.disabled = busy || !revision || live || !api.canSend();
    });
    root.querySelector('[data-bot-subaccount-state]').textContent = stopState && stopState.stopped ?
      (stopState.reason === "AUTO_EXIT" ? "STOP · coordinador" : stopState.reason === "PARTIAL_EXIT" ? "STOP · salida parcial" : stopState.reason === "NATIVE_RECOVERY" ? "STOP · recuperación" : "STOP · revisión necesaria") : "";
  }
  async function request(local, path, options, passive) {
    var controller = new AbortController(), timeout = setTimeout(function () { controller.abort(); }, 5000);
    try { return await local.request(path, Object.assign({},options || {}, {signal:controller.signal}), passive); }
    finally { clearTimeout(timeout); }
  }
  function closeFillChart() {
    fillRequest += 1; selectedRun = null; chartLoading = false;
    if (fillChart) fillChart.remove();
    fillChart = null;
    var panel = root && root.querySelector("[data-bot-fill-panel]");
    if (panel) { panel.hidden = true; panel.replaceChildren(); }
  }
  async function readFillArchive(path, stillSelected, options) {
    var readyBy = performance.now() + 5000;
    while (reading && stillSelected() && performance.now() < readyBy) {
      await new Promise(function (resolve) { setTimeout(resolve,100); });
    }
    var retryDelays = [200,400,800,1600,1600];
    for (var attempt = 0; attempt <= retryDelays.length && stillSelected(); attempt += 1) {
      try { return await request(api,path,options,true); }
      catch (error) {
        if (error.status !== 503 || attempt === retryDelays.length) throw error;
        await new Promise(function (resolve) { setTimeout(resolve,retryDelays[attempt]); });
      }
    }
    return null;
  }
  async function exportFillResearch(run, account, panel, stillSelected) {
    var button = panel.querySelector("[data-bot-fill-export]");
    if (button.disabled || !stillSelected() || !window.confirm("¿Descargar el episodio TESTNET como JSONL sin cifrar?")) return;
    var issued = fillRequest;
    var state = panel.querySelector("[data-bot-export-status]");
    state.hidden = false;
    state.className = "state muted";
    state.textContent = "Verificando exportación…";
    button.disabled = true;
    chartLoading = true;
    try {
      var data = await readFillArchive(base+"/"+encodeURIComponent(run)+"/research-export",stillSelected,{
        method:"POST", body:JSON.stringify({sub_account_id:account,confirmation:"EXPORTAR_JSONL_TESTNET"})
      });
      if (!stillSelected()) return;
      if (data.schema !== "orbis.grvt.testnet.bot-research.v1" || !Number.isInteger(data.record_count) ||
          data.record_count < 1 || data.record_count > 20 || !/^[0-9a-f]{64}$/.test(data.sha256) ||
          typeof data.jsonl !== "string" || !data.jsonl.endsWith("\n") ||
          data.jsonl.split("\n").length !== data.record_count + 1) throw new Error("Archivo TESTNET no verificable");
      var bytes = new TextEncoder().encode(data.jsonl);
      var digest = await crypto.subtle.digest("SHA-256",bytes);
      if (!stillSelected()) return;
      var sha = Array.from(new Uint8Array(digest),function (value) { return value.toString(16).padStart(2,"0"); }).join("");
      if (sha !== data.sha256) throw new Error("La huella del archivo TESTNET no coincide");
      var url = URL.createObjectURL(new Blob([bytes],{type:"application/x-ndjson"}));
      var link = document.createElement("a");
      link.href = url;
      link.download = "orbis-grvt-testnet-"+sha.slice(0,12)+".jsonl";
      document.body.append(link);
      link.click();
      link.remove();
      setTimeout(function () { URL.revokeObjectURL(url); },1000);
      state.textContent = data.record_count + (data.record_count === 1 ? " ciclo" : " ciclos") + " · JSONL descargado";
    } catch (error) {
      if (stillSelected()) { state.className = "state error"; state.textContent = error.message; }
    } finally {
      if (fillRequest === issued) chartLoading = false;
      if (stillSelected()) button.disabled = false;
    }
  }
  function drawFills(panel, data) {
    var state = panel.querySelector("[data-bot-fill-state]");
    if (!data.total) { state.textContent = "Sin fills auditados."; return; }
    var lw = window.LightweightCharts, canvas = panel.querySelector("[data-bot-fill-canvas]");
    if (!lw || !lw.LineSeries || !lw.createSeriesMarkers) throw new Error("Gráfico local no disponible");
    var select = panel.querySelector("[data-bot-fill-select]"), detail = panel.querySelector("[data-bot-fill-detail]");
    var grouped = new Map(), rangeTimes = new Set();
    data.executions.forEach(function (fill, index) {
      var role = fillRoles[fill.role];
      if (!Object.hasOwn(fillRoles,fill.role) || !["BUY","SELL"].includes(fill.side) || !["AUDITED","RECONCILED"].includes(fill.basis) ||
          typeof fill.event_time_ns !== "string" || !/^[1-9][0-9]{0,18}$/.test(fill.event_time_ns) ||
          !Number.isFinite(Number(fill.price)) || Number(fill.price) <= 0) throw new Error("Fill TESTNET incoherente");
      var time = Number(BigInt(fill.event_time_ns) / 1000000000n);
      rangeTimes.add(time);
      var points = grouped.get(fill.role) || new Map();
      points.set(time, {time:time,value:Number(fill.price),side:fill.side});
      grouped.set(fill.role,points);
      var date = new Date(time*1000).toISOString().slice(0,19).replace("T"," ");
      select.add(new Option(date + " UTC · " + role[0] + " · Ciclo " + fill.cycle, String(index)));
    });
    canvas.hidden = false;
    fillChart = lw.createChart(canvas,{autoSize:true,
      layout:{background:{type:"solid",color:"#101b18"},textColor:"#c9d6ce"},
      grid:{vertLines:{color:"#24362f"},horzLines:{color:"#24362f"}},
      timeScale:{timeVisible:true,secondsVisible:true,lockVisibleTimeRangeOnResize:true,minBarSpacing:0.05}});
    grouped.forEach(function (points, role) {
      var series = fillChart.addSeries(lw.LineSeries,{lineVisible:false,crosshairMarkerVisible:false,
        priceLineVisible:false,lastValueVisible:false});
      var marks = Array.from(points.values()).sort(function (a,b) { return a.time-b.time; });
      series.setData(marks.map(function (point) { return {time:point.time,value:point.value}; }));
      lw.createSeriesMarkers(series,marks.map(function (point) {
        var buy = point.side === "BUY";
        return {time:point.time,position:buy ? "belowBar" : "aboveBar",shape:buy ? "arrowUp" : "arrowDown",
          color:fillRoles[role][1],text:fillRoles[role][0]};
      }));
    });
    if (rangeTimes.size > 1 && window.OrbisChartRange) {
      window.OrbisChartRange.mount(panel.querySelector("[data-bot-fill-range]"), fillChart,
        Array.from(rangeTimes, function (time) { return {time:time}; }), {kind:"fills",selected:"all",lastCounts:[100,500]});
    } else {
      fillChart.timeScale().fitContent();
    }
    canvas.dataset.fillCount = String(data.executions.length);
    state.textContent = data.executions.length + " fills TESTNET · sin velas" +
      (data.total > data.executions.length ? " · últimos " + data.executions.length + " de " + data.total : "");
    function inspect() {
      var fill = data.executions[Number(select.value)];
      var time = Number(BigInt(fill.event_time_ns) / 1000000000n);
      var exactTime = new Date(time*1000).toISOString().slice(0,19).replace("T"," ") + "." + fill.event_time_ns.slice(-9);
      detail.textContent = (fill.side === "BUY" ? "Compra" : "Venta") + " · " + fill.size + " @ " + fill.price +
        " · Comisión " + fill.fee + " " + fill.fee_currency +
        " · " + (fill.basis === "RECONCILED" ? "Conciliado" : "Auditado") + " · " + exactTime + " UTC";
    }
    select.closest("label").hidden = false;
    select.addEventListener("change",inspect);
    select.value = String(data.executions.length - 1);
    inspect();
  }
  async function openFillChart(run) {
    var record = records.find(function (item) { return item.id === run; });
    if (!record || !api.canSend()) return;
    closeFillChart();
    selectedRun = run;
    chartLoading = true;
    var host = root, at = epoch, issued = fillRequest, account = sub();
    var panel = host.querySelector("[data-bot-fill-panel]");
    panel.innerHTML = '<div class="row"><strong data-bot-fill-title></strong><span class="statusbadge">TESTNET</span>' +
      '<button type="button" class="ghost" data-bot-fill-refresh>Actualizar gráfico</button>' +
      '<button type="button" class="ghost" data-bot-fill-export disabled data-help="Descarga un JSONL local con el origen y los fills auditados de este episodio.">Exportar JSONL</button>' +
      '<button type="button" class="ghost" data-bot-fill-close>Cerrar gráfico</button></div>' +
      '<p class="state muted" role="status" data-bot-fill-state>Verificando fills…</p>' +
      '<p class="state muted" role="status" data-bot-export-status hidden></p>' +
      '<div data-bot-fill-range></div>' +
      '<div class="real-bot-chart" data-bot-fill-canvas role="img" aria-label="Precio y hora de ejecuciones GRVT TESTNET" hidden></div>' +
      '<label class="field" data-help="Agrupa en el último precio los fills del mismo tipo y segundo. Cada fill se puede seleccionar aquí." hidden>' +
      'Ejecución<select data-bot-fill-select aria-label="Ejecución TESTNET"></select></label>' +
      '<p data-bot-fill-detail aria-live="polite"></p>';
    panel.querySelector("[data-bot-fill-title]").textContent = record.source.symbol + " · " + record.source.definition.direction;
    panel.querySelector("[data-bot-fill-refresh]").addEventListener("click",function () { openFillChart(run); });
    panel.querySelector("[data-bot-fill-close]").addEventListener("click",closeFillChart);
    panel.hidden = false;
    function stillSelected() {
      return current(host,at) && api.canSend() && sub() === account && selectedRun === run && fillRequest === issued;
    }
    var exportButton = panel.querySelector("[data-bot-fill-export]");
    exportButton.addEventListener("click",function () { exportFillResearch(run,account,panel,stillSelected); });
    try {
      var path = base+"/"+encodeURIComponent(run)+"/executions?sub_account_id="+encodeURIComponent(account);
      var data = await readFillArchive(path,stillSelected);
      if (!stillSelected()) return;
      if (data.network !== "TESTNET" || data.run_id !== run || data.sub_account_id !== account ||
          data.symbol !== record.source.symbol || data.direction !== record.source.definition.direction ||
          !Number.isInteger(data.total) || !Array.isArray(data.executions) ||
          data.executions.length !== Math.min(5000,data.total) || data.total < 0) throw new Error("Fills TESTNET no verificables");
      drawFills(panel,data);
      exportButton.disabled = false;
    } catch (error) {
      if (!stillSelected()) return;
      if (fillChart) fillChart.remove();
      fillChart = null;
      panel.querySelector("[data-bot-fill-canvas]").hidden = true;
      panel.querySelector("[data-bot-fill-state]").textContent = error.message || "Gráfico no disponible";
    } finally {
      if (fillRequest === issued) chartLoading = false;
    }
  }
  function render(data) {
    if (data.network !== "TESTNET" || data.authorization !== false || !Array.isArray(data.items) ||
        !Array.isArray(data.control_revision) || data.control_revision.length !== 2) throw new Error("Registro no verificable");
    if (data.history_pending) {
      var merged = new Map(records.map(function (r) { return [r.id, r]; }));
      data.items.forEach(function (r) { merged.set(r.id, r); }); records = Array.from(merged.values());
    } else records = data.items;
    revision = data.history_pending ? null : data.control_revision;
    if (!data.history_pending) stopState = data.subaccount_stop || null;
    if (records.some(function (record) { return record.active; })) unknown = false;
    root.querySelector('[data-bot-next]').disabled = busy || data.history_pending || !data.has_more;
    var presentation = JSON.stringify({stop:stopState && stopState.stopped ? {
      reason:stopState.reason,parent:stopState.parent_client_order_id} : null,items:records.map(function (record) {
      var stable = Object.assign({},record); delete stable.permission_remaining_seconds; return stable;
    })});
    if (rendered === presentation) { guards(); return; }
    rendered = presentation;
    var list = root.querySelector('[data-bot-records]'); list.replaceChildren();
    records.forEach(function (record) {
      var card = node("article", "", "grvt-oco-group"); card.dataset.grvtBotRun = record.id;
      var head = node("div", "", "real-mexc-head");
      var ended = ["COMPLETE","ENTRY_LIMIT","BUDGET_LIMIT","LOSS_LIMIT"].includes(record.status);
      var status = ended ? (record.active ? "Guardando resultado" :
        record.archive_error ? "Revisar registro final" : labels[record.status]) : labels[record.status] || "Registrado";
      head.append(node("strong", record.source.symbol + " · " + record.source.definition.direction),
        node("span", status, "statusbadge " + (record.active ? "on" : "off")));
      card.append(head, node("small", (record.max_entries > 1 ? record.completed_entries + "/" + record.max_entries + " entradas" : "1 entrada") +
        " · " + record.entry_cap + " USDT · " + record.source.timeframe, "muted"));
      if (record.entry_execution && record.entry_execution.status === "UNFILLED")
        card.append(node("small", "Sin ejecución · intento conciliado", "muted"));
      if (record.max_entries > 1) {
        card.append(node("small", "Reservado · " + record.entry_budget_reserved + " / " + record.total_entry_cap + " USDT", "muted grvt-oco-id"));
        card.append(node("small", "Neto conciliado · " + record.net_realized_pnl_usdt + " USDT", "muted grvt-oco-id"));
      }
      if (record.selection) card.append(node("small", "Entrada · " + record.selection.parent_client_order_id, "muted grvt-oco-id"));
      if (record.selection) {
        var chart = node("button", "Gráfico de fills", "ghost");
        chart.type = "button"; chart.dataset.botFillRun = record.id; card.append(chart);
      }
      if (!record.active && !ended && record.selection && (!record.cycles || record.completed_entries !== record.entry_count)) card.append(node("small", "Revisar posición y protecciones", "muted"));
      if (record.archive_error) card.append(node("small", "Registro final pendiente", "muted"));
      if (record.active || record.status === "INTERRUPTED") {
        var stop = node("button", record.active ? "Detener ciclo" : "Retirar permiso", "ghost");
        stop.type = "button"; stop.dataset.botStop = record.id; stop.disabled = !api.canSend(); card.append(stop);
      }
      if (ended && !record.active && !record.archive_error && stopState && stopState.stopped && stopState.reason === "AUTO_EXIT" && record.selection &&
          record.selection.parent_client_order_id === stopState.parent_client_order_id) {
        var rearm = node("button", "Rearmar subcuenta", "ghost"); rearm.type = "button"; rearm.dataset.botRearm = record.id;
        rearm.disabled = busy || !revision || records.some(function (r) { return r.active; }) || !api.canSend();
        card.append(rearm);
      }
      list.append(card);
    });
    guards();
  }
  async function refresh(withSources) {
    if (!root || reading || !api.canSend()) return;
    var host = root, at = epoch, stamp = serial, account = sub(), local = api;
    reading = true;
    try {
      if (withSources) {
        var available = await request(local, base + "/sources", null, true);
        if (!current(host,at) || stamp !== serial) return;
        if (available.network !== "TESTNET" || available.authorization !== false || !Array.isArray(available.items)) throw new Error("Sesiones no verificables");
        var select = host.querySelector('[name="session_id"]'), selected = select.value;
        select.replaceChildren(node("option", available.items.length ? "Elegir sesión PAPER" : "Sin sesiones PAPER activas"));
        select.firstChild.value = "";
        available.items.forEach(function (source) {
          var option = node("option", source.symbol + " · " + source.direction + " · " + source.timeframe + " · " + source.session_id.slice(0,8));
          option.value = source.session_id; select.append(option);
        });
        select.value = selected;
      }
      if (/^\d{1,30}$/.test(account)) {
        var data = await request(local, base + "?sub_account_id=" + encodeURIComponent(account) + "&offset=" + offset, null, true);
        if (current(host,at) && stamp === serial && sub() === account) {
          render(data);
          if (data.history_pending) notice("Registro en uso · estado en directo");
        }
      }
    } catch (error) {
      if (current(host,at) && stamp === serial) { revision = null; notice("Consulta pendiente · " + error.message, true); }
    } finally { if (current(host,at)) { reading = false; guards(); } }
  }
  async function start(event) {
    event.preventDefault(); if (busy || unknown || !revision || !api.canSend()) return;
    var host = root, at = epoch, local = api, form = event.currentTarget;
    var data = new FormData(form), body = {sub_account_id:sub(),permission_revision:revision.slice(),
      confirmation:"ACTIVAR_UN_CICLO_AUTOMATICO_GRVT_TESTNET"};
    ["session_id","api_key","dedicated_private_key","expected_signer","entry_cap","exit_cap"].forEach(function (key) { body[key] = String(data.get(key) || "").trim(); });
    ["wait_seconds","exit_seconds","max_attempts","max_spread_bps","max_slippage_bps"].forEach(function (key) { body[key] = Number(data.get(key)); });
    body.max_entries = Number(data.get("max_entries"));
    if (body.max_entries > 1) {
      body.total_entry_cap = String(data.get("total_entry_cap") || "").trim();
      body.loss_cap = String(data.get("loss_cap") || "").trim();
    }
    form.querySelector('input[type="checkbox"]').checked = false;
    busy = true; unknown = true; revision = null; serial += 1; guards(); clearSecrets(host); notice("Activando un ciclo · TESTNET");
    var issuedSerial = serial;
    try {
      var result = await request(local,base + "/start",{method:"POST",body:JSON.stringify(body)},false);
      if (current(host,at) && issuedSerial === serial) {
        if (result.network !== "TESTNET" || !result.id || result.max_entries !== body.max_entries) throw new Error("Activación no verificable");
        unknown = false; records = [result]; render({network:"TESTNET",authorization:false,items:records,control_revision:[0,0],history_pending:true});
        notice(body.max_entries > 1 ? "Bot activado · límites vigentes" : "Ciclo activado · sin rearme automático");
      }
    } catch (error) {
      if (current(host,at) && issuedSerial === serial) {
        if (error.message === "Mínimos de GRVT o protección incompatibles; revisa los topes de entrada y salida") {
          unknown = false; notice(error.message,true);
        } else notice("Activación sin confirmar · consulta o detén el ciclo",true);
      }
    }
    finally {
      body.api_key = body.dedicated_private_key = body.expected_signer = "";
      if (current(host,at)) { busy = false; guards(); await refresh(false); }
    }
  }
  async function stop(run) {
    if (!api || !api.canSend()) return;
    var host = root, at = epoch, local = api, body = {confirmation:"DETENER_CICLO_AUTOMATICO_GRVT_TESTNET"};
    var path = base + (run ? "/" + encodeURIComponent(run) + "/stop" : "/stop");
    if (!run) body.sub_account_id = sub();
    serial += 1; revision = null;
    try {
      var result = await request(local,path,{method:"POST",body:JSON.stringify(body)},false);
      if (current(host,at)) {
        if (result.network !== "TESTNET") throw new Error("Parada no verificable");
        unknown = false; notice("Parada solicitada · protecciones conservadas");
      }
    } catch (_) { if (current(host,at)) notice("Parada sin confirmar · consulta el estado",true); }
    finally { if (current(host,at)) { guards(); await refresh(false); } }
  }
  async function rearm(run) {
    if (!api || !api.canSend() || busy || !revision || !stopState) return;
    var host = root, at = epoch, local = api, body = {confirmation:"REARMAR_GRVT_TESTNET_TRAS_CIERRE",
      permission_revision:revision.slice(),stop_revision:stopState.revision};
    busy = true; serial += 1; revision = null; var stamp = serial; guards();
    try {
      var result = await request(local,base + "/" + encodeURIComponent(run) + "/rearm",{method:"POST",body:JSON.stringify(body)},false);
      if (current(host,at) && stamp === serial) {
        if (result.network !== "TESTNET" || result.stopped !== false || result.authorization !== false) throw new Error("Rearme no verificable");
        notice("Subcuenta rearmada · requiere nueva activación");
      }
    } catch (_) { if (current(host,at) && stamp === serial) notice("Rearme sin confirmar · consulta el registro",true); }
    finally { if (current(host,at)) { busy = false; guards(); await refresh(false); } }
  }
  function schedule() {
    var at = epoch;
    timer = setTimeout(async function () {
      timer = null;
      if (!root || epoch !== at) return;
      if (!root.isConnected) { clear(); return; }
      if (root.open && !busy && !chartLoading) await refresh(false);
      if (root && epoch === at) schedule();
    },1500);
  }
  function mount(host, services) {
    clear(); root = host; api = services;
    host.innerHTML = '<summary>Bot · TESTNET</summary><div class="real-mexc-head"><strong>Señal → entrada → salida</strong><span class="statusbadge off" data-bot-mode>1 CICLO</span></div>' +
      '<div class="real-form"><label class="field">Subcuenta<input name="sub_account_id" inputmode="numeric" pattern="[0-9]+" maxlength="30" autocomplete="off"></label>' +
      '<div class="row"><button type="button" class="ghost" data-bot-refresh>Consultar</button><button type="button" class="ghost" data-bot-stop-all>Detener</button></div></div>' +
      '<small class="muted" data-bot-subaccount-state></small><div data-bot-records class="grvt-oco-groups"></div><div class="row"><button type="button" class="ghost" data-bot-prev>Más recientes</button><button type="button" class="ghost" data-bot-next disabled>Anteriores</button></div>' +
      '<div data-bot-fill-panel class="real-bot-chart-panel" hidden></div>' +
      '<form data-bot-form autocomplete="off"><fieldset data-bot-fields class="grvt-oco-controls"><div class="real-form">' +
      '<label class="field" data-help="Espera una nueva entrada de esta sesión. Las señales anteriores a la activación se descartan.">Sesión PAPER<select name="session_id" required><option value="">Consultar sesiones</option></select></label>' +
      '<label class="field" data-help="Nocional máximo por entrada, hasta 1.000 USDT. Las tres órdenes deben cumplir los mínimos del mercado; el capital PAPER es independiente.">Entrada · USDT<input name="entry_cap" value="50" inputmode="decimal" required></label>' +
      '<label class="field" data-help="Tope nocional de cada salida, hasta 10.000 USDT. No aumenta el presupuesto de entrada.">Tope salidas · USDT<input name="exit_cap" value="100" inputmode="decimal" required></label>' +
      '<label class="field">API key TESTNET<input name="api_key" type="password" autocomplete="off" spellcheck="false" maxlength="512" required></label>' +
      '<label class="field" data-help="Clave dedicada de firma TESTNET. No se guarda ni se reutiliza al reiniciar.">Firma TESTNET<input name="dedicated_private_key" type="password" autocomplete="off" spellcheck="false" required></label>' +
      '<label class="field">Firmante<input name="expected_signer" autocomplete="off" spellcheck="false" required></label></div>' +
      '<details><summary>Límites</summary><div class="real-form"><label class="field">Entradas máximas<input name="max_entries" type="number" value="1" min="1" max="20" required></label>' +
      '<label class="field" data-bot-aggregate hidden data-help="Reserva el tope de cada intento de entrada; los beneficios no amplían el presupuesto.">Presupuesto · USDT<input name="total_entry_cap" value="150" inputmode="decimal" disabled></label>' +
      '<label class="field" data-bot-aggregate hidden data-help="Detiene nuevas entradas al alcanzar esta pérdida neta realizada. Se comprueba después de conciliar cada ciclo; no garantiza un límite de pérdida durante la posición.">Pérdida neta · USDT<input name="loss_cap" value="10" inputmode="decimal" disabled></label>' +
      '<label class="field" data-help="Plazo total para aceptar entradas desde esta activación. No se renueva entre ciclos.">Vigencia entradas · s<input name="wait_seconds" type="number" value="900" min="30" max="86400" required></label>' +
      '<label class="field">Seguimiento · s<input name="exit_seconds" type="number" value="900" min="30" max="86280" required></label>' +
      '<label class="field">Intentos cierre<input name="max_attempts" type="number" value="2" min="1" max="3" required></label>' +
      '<label class="field">Spread · pb<input name="max_spread_bps" type="number" value="25" min="0" max="100" required></label>' +
      '<label class="field">Deslizamiento · pb<input name="max_slippage_bps" type="number" value="25" min="0" max="100" required></label></div></details>' +
      '<label class="grvt-bot-confirm"><input type="checkbox" required><span data-bot-confirm-text>Autorizar 1 entrada y sus salidas · TESTNET</span></label>' +
      '<button type="submit" data-bot-start disabled>Activar ciclo · TESTNET</button></fieldset></form><div data-bot-notice class="state muted" role="status">Sin permiso activo</div>';
    host.querySelector('[data-bot-form]').addEventListener("submit",start);
    host.querySelector('[name="max_entries"]').addEventListener("input",aggregateFields);
    host.querySelector('[data-bot-refresh]').addEventListener("click",function () { refresh(true); });
    host.querySelector('[data-bot-stop-all]').addEventListener("click",function () { stop(null); });
    host.querySelector('[data-bot-records]').addEventListener("click",function (event) {
      var button = event.target.closest('[data-bot-stop],[data-bot-rearm],[data-bot-fill-run]');
      if (!button) return;
      if (button.hasAttribute('data-bot-fill-run')) {
        openFillChart(button.dataset.botFillRun);
        host.querySelector('[data-bot-fill-panel]').scrollIntoView({block:"nearest"});
      }
      else if (button.hasAttribute('data-bot-rearm')) rearm(button.dataset.botRearm);
      else stop(button.dataset.botStop);
    });
    host.querySelector('[name="sub_account_id"]').addEventListener("input",function () {
      closeFillChart();
      serial += 1; revision = null; records = []; rendered = null; stopState = null; offset = 0; host.querySelector('[data-bot-records]').replaceChildren(); guards();
    });
    host.querySelector('[data-bot-prev]').addEventListener("click",function () { closeFillChart(); offset = Math.max(0,offset-20); serial += 1; refresh(false); });
    host.querySelector('[data-bot-next]').addEventListener("click",function () { closeFillChart(); offset += 20; serial += 1; refresh(false); });
    host.addEventListener("toggle",function () { if (host.open) refresh(true); else closeFillChart(); });
    aggregateFields(); guards(); schedule();
  }
  function setSubaccount(value) { if (root && !busy && !unknown && !sub()) root.querySelector('[name="sub_account_id"]').value = value; }
  window.OrbisGrvtBot = {mount:mount,clear:clear,setSubaccount:setSubaccount};
}());
