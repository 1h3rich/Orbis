/* GRVT historical inputs. No orders, workers or simulated P/L. */
(function () {
  "use strict";
  function esc(value) { return String(value == null ? "" : value).replace(/[&<>"']/g, function (c) { return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]; }); }
  function utc(seconds) { return new Date(seconds * 1000).toISOString().slice(0,16).replace("T"," ") + " UTC"; }
  function observedUtc(ns) { return new Date(Number(BigInt(ns)/1000000n)).toISOString().slice(0,23).replace("T"," ")+" UTC"; }
  function download(bundle, filename) {
    var link=document.createElement("a"),url=URL.createObjectURL(new Blob([JSON.stringify(bundle,null,2)],{type:"application/json"}));
    link.href=url;link.download=filename;link.click();
    setTimeout(function(){URL.revokeObjectURL(url);},1000);
  }
  function mount(root) {
    var alive = true, serial = 0, selected = null, controller = new AbortController();
    var rulesSerial=0,rulesItems=[],rulesNext=null,rulesSymbol=null;
    root.innerHTML = '<div class="lab-grid"><form class="lab-form" id="perp-data-form">' +
      '<label class="field">Mercado<select name="symbol"><option>BTC/USDT:PERP</option><option>ETH/USDT:PERP</option><option>SOL/USDT:PERP</option></select></label>' +
      '<label class="field">Intervalo<select name="timeframe"><option>1h</option><option>4h</option><option>1d</option><option>15m</option><option>5m</option><option>1m</option></select></label>' +
      '<label class="field">Inicio UTC<input name="start" type="datetime-local" required></label>' +
      '<label class="field">Fin UTC<input name="end" type="datetime-local" required></label>' +
      '<button type="submit" data-help="Descarga TRADE, MARK, financiación y una captura actual de reglas. Comprueba toda la cobertura antes de guardar; máximo 5.000 velas y 366 días.">Descargar datos</button></form>' +
      '<section><label class="field">Archivo conservado<select id="perp-data-selection"><option value="">Selecciona archivo</option></select></label>' +
      '<div id="perp-data-report" class="state" role="status">Sin archivo seleccionado.</div>' +
      '<button id="perp-data-export" type="button" class="ghost" disabled data-help="Exporta los datos exactos y sus huellas. No contiene claves, órdenes ni resultados de un backtest.">Exportar JSON</button></section></div>' +
      '<p id="perp-data-status" class="muted" role="status"></p>' +
      '<span class="statusbadge" data-help="Las reglas descargadas son actuales; no prueban su vigencia histórica. Las pruebas derivadas emplean supuestos experimentales.">Datos · reglas actuales</span>' +
      '<details id="perp-rules-panel"><summary>Historial de reglas GRVT</summary><div class="lab-toolbar">' +
      '<button id="perp-rules-capture" type="button" class="ghost">Capturar reglas</button>' +
      '<button id="perp-rules-export" type="button" class="ghost" disabled>Exportar capturas</button></div>' +
      '<p id="perp-rules-state" class="muted" role="status"></p><div id="perp-rules-list"></div>' +
      '<button id="perp-rules-more" type="button" class="ghost" hidden>Más capturas</button></details>';
    var form = root.querySelector("form"), choice = root.querySelector("#perp-data-selection");
    var status = root.querySelector("#perp-data-status"), report = root.querySelector("#perp-data-report");
    var exportButton = root.querySelector("#perp-data-export"), submit = form.querySelector('button[type="submit"]');
    var rulesPanel=root.querySelector("#perp-rules-panel"),rulesList=root.querySelector("#perp-rules-list");
    var rulesState=root.querySelector("#perp-rules-state"),rulesCapture=root.querySelector("#perp-rules-capture");
    var rulesExport=root.querySelector("#perp-rules-export"),rulesMore=root.querySelector("#perp-rules-more");
    var end = Math.floor(Date.now() / 3600000) * 3600;
    form.elements.start.value = new Date((end - 48 * 3600) * 1000).toISOString().slice(0,16);
    form.elements.end.value = new Date(end * 1000).toISOString().slice(0,16);
    function current(ticket) { return alive && root.isConnected && ticket === serial; }
    function clear() { selected = null; exportButton.disabled = true; report.textContent = "Sin archivo seleccionado."; }
    function errorMessage(error) { return error.status === 423 ? "LAB bloqueado · desbloquea en Ajustes." : error.message; }
    async function api(path, options, family) {
      var response = await fetch("/lab/perp/" + (family || "datasets") + path, Object.assign({cache:"no-store",signal:controller.signal,
        headers:{"Content-Type":"application/json"}}, options || {}));
      var body = await response.json();
      if (!response.ok) { var error = new Error(typeof body.detail === "string" ? body.detail : "No se pudieron verificar los datos."); error.status = response.status; throw error; }
      return body;
    }
    function rulesCurrent(ticket,symbol) {
      return alive&&root.isConnected&&rulesPanel.open&&rulesSerial===ticket&&form.elements.symbol.value===symbol;
    }
    function renderRule(item) {
      var data=item.observation,tiers=data.rules.risk_brackets,spec=data.specification;
      var changed=item.same_instant?"Fecha ambigua":item.changes===null?"Primera captura":
        item.changes.length?item.changes.map(function(key){return key==="rules"?"Margen":"Instrumento";}).join(" + ")+" · cambio":"Sin cambios";
      return '<details class="state"><summary>'+esc(observedUtc(item.observed_at_ns)+" · "+changed)+'</summary>'+
        '<p>'+esc('Tick '+spec.tick_size+' · Tamaño mínimo '+spec.min_size+' · Máximo '+data.rules.max_position_size)+'</p>'+
        '<ul>'+tiers.map(function(tier){
          var range=tier.notional_floor+'–'+(tier.notional_cap||'∞')+' USDT';
          return '<li>'+esc(range+' · Máx. '+tier.max_leverage+'× · Inicial '+tier.initial_margin_rate+' · Mantenimiento '+tier.maintenance_margin_rate)+'</li>';
        }).join('')+'</ul></details>';
    }
    async function loadRules(action) {
      if(!rulesPanel.open)return;
      var symbol=form.elements.symbol.value,ticket=++rulesSerial,cursor=action==="more"?rulesNext:null;
      if(action!=="more"||rulesSymbol!==symbol){rulesItems=[];rulesNext=null;rulesList.replaceChildren();}
      rulesCapture.disabled=true;rulesExport.disabled=true;rulesMore.disabled=true;
      rulesState.textContent=action==="capture"?"Consultando GRVT…":"Cargando capturas…";
      try {
        if(action==="capture")await api("/capture",{method:"POST",body:JSON.stringify({symbol:symbol})},"rules");
        if(!rulesCurrent(ticket,symbol))return;
        var query=new URLSearchParams({symbol:symbol});
        if(cursor){query.set("before_ns",cursor.ns);query.set("before_hash",cursor.hash);}
        var page=await api("?"+query.toString(),null,"rules");
        if(!rulesCurrent(ticket,symbol))return;
        rulesSymbol=symbol;rulesNext=page.next_before;rulesItems=rulesItems.concat(page.items);
        rulesList.insertAdjacentHTML("beforeend",page.items.map(renderRule).join(""));
        rulesState.textContent=symbol+" · "+(rulesItems.length?"Capturas fechadas":"Sin capturas");
        rulesExport.disabled=!rulesItems.length;
      } catch(error) {
        if(rulesCurrent(ticket,symbol))rulesState.textContent=errorMessage(error);
      } finally {
        if(rulesCurrent(ticket,symbol)){rulesCapture.disabled=false;rulesMore.disabled=false;rulesMore.hidden=!rulesNext;}
      }
    }
    rulesPanel.addEventListener("toggle",function(){if(rulesPanel.open)loadRules("list");else rulesSerial+=1;});
    form.elements.symbol.addEventListener("change",function(){loadRules("list");});
    rulesCapture.addEventListener("click",function(){loadRules("capture");});
    rulesMore.addEventListener("click",function(){loadRules("more");});
    rulesExport.addEventListener("click",function(){
      if(rulesItems.length&&rulesSymbol===form.elements.symbol.value)
        download({symbol:rulesSymbol,scope:"OBSERVED_AT_CAPTURE_ONLY",items:rulesItems,next_before:rulesNext},
          "orbis-reglas-"+rulesSymbol.split("/")[0]+".json");
    });
    function show(item) {
      selected = item; exportButton.disabled = false;
      report.innerHTML = '<strong>' + esc(item.symbol) + ' · ' + esc(item.timeframe) + '</strong>' +
        '<p>' + esc(utc(item.start)) + ' → ' + esc(utc(item.end)) + '</p>' +
        '<dl><dt>TRADE</dt><dd>' + esc(item.trade_count) + ' velas</dd><dt>MARK</dt><dd>' + esc(item.mark_count) + ' velas</dd>' +
        '<dt>Funding</dt><dd>' + esc(item.funding_count) + ' liquidaciones + ancla</dd>' +
        '<dt data-help="Reglas observadas al descargar. La fecha es la de captura.">Reglas</dt><dd>Captura actual'+
        (item.rules_captured_ns?" · "+esc(observedUtc(item.rules_captured_ns)):"")+'</dd>' +
        '<dt>Huella</dt><dd data-help="' + esc(item.hash) + '">' + esc(item.hash.slice(0,16)) + '…</dd></dl>';
    }
    async function load(wanted) {
      var ticket = ++serial; clear(); status.textContent = "Cargando archivo…";
      try {
        var rows = await api(""); if (!current(ticket)) return;
        choice.innerHTML = '<option value="">Selecciona archivo</option>' + rows.map(function (row) {
          return '<option value="' + esc(row.id) + '">' + esc(row.symbol + " · " + row.timeframe + " · " + utc(row.start)) + '</option>';
        }).join("");
        choice.value = wanted || "";
        var item = rows.find(function (row) { return row.id === choice.value; });
        if (item) show(item);
        status.textContent = rows.length ? rows.length + " archivo(s) verificados." : "Sin datos perpetuos guardados.";
      } catch (error) { if (current(ticket)) { clear(); status.textContent = errorMessage(error); } }
    }
    choice.addEventListener("change", async function () {
      var id = choice.value, ticket = ++serial; clear(); status.textContent = "";
      if (!id) return;
      try { var item = await api("/" + encodeURIComponent(id)); if (current(ticket) && choice.value === id) show(item); }
      catch (error) { if (current(ticket)) { clear(); status.textContent = errorMessage(error); } }
    });
    form.addEventListener("submit", async function (event) {
      event.preventDefault(); var ticket = ++serial; clear(); submit.disabled = true;
      status.textContent = "Descargando y verificando…";
      try {
        var start = Date.parse(form.elements.start.value + "Z") / 1000;
        var stop = Date.parse(form.elements.end.value + "Z") / 1000;
        if (!Number.isSafeInteger(start) || !Number.isSafeInteger(stop)) throw new Error("Fechas UTC inválidas.");
        var item = await api("/download",{method:"POST",body:JSON.stringify({symbol:form.elements.symbol.value,
          timeframe:form.elements.timeframe.value,start:start,end:stop})});
        if (current(ticket)) { await load(item.id); loadRules("list"); }
      } catch (error) { if (current(ticket)) { clear(); status.textContent = errorMessage(error); } }
      finally { if (alive && root.isConnected) submit.disabled = false; }
    });
    exportButton.addEventListener("click", async function () {
      if (!selected) return;
      var id = selected.id, ticket = ++serial; exportButton.disabled = true;
      try {
        var bundle = await api("/" + encodeURIComponent(id) + "/export");
        if (!current(ticket) || !selected || selected.id !== id) return;
        download(bundle,"orbis-perp-"+id+".json");
      } catch (error) { if (current(ticket)) { clear(); status.textContent = errorMessage(error); } }
      finally { if (current(ticket)) exportButton.disabled = !selected; }
    });
    root.addEventListener("orbis-perp-refresh", function () { load(choice.value); });
    return {load:load,dispose:function () { alive=false;serial+=1;controller.abort(); }};
  }
  window.OrbisPerpDatasets = {mount:mount};
})();
