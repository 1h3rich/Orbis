/* Local encrypted archive of opt-in LAB interpretations. */
(function () {
  "use strict";

  function mount(panel, api, download) {
    var filter = panel.querySelector("select");
    var status = panel.querySelector("[data-ai-archive-status]");
    var list = panel.querySelector("tbody");
    var detail = panel.querySelector("[data-ai-archive-detail]");
    var previous = panel.querySelector("[data-ai-archive-prev]");
    var next = panel.querySelector("[data-ai-archive-next]");
    var exportSelection = panel.querySelector("[data-ai-archive-jsonl]");
    var selected = new Map(), maxSelection = 50, exportPending = null;
    var offset = 0, limit = 25, pending = null, disposed = false;

    function paintSelection() {
      exportSelection.disabled = !!exportPending || !selected.size;
      exportSelection.textContent = "Exportar JSONL" + (selected.size ? " · " + selected.size : "");
      list.querySelectorAll("input[type=checkbox]").forEach(function (input) {
        input.disabled = !!exportPending || (!input.checked && selected.size >= maxSelection);
      });
    }

    function clearSelection() {
      if (exportPending) exportPending.abort();
      exportPending = null;
      selected.clear();
      paintSelection();
    }

    function reset() {
      if (pending) pending.abort();
      pending = null;
      list.replaceChildren();
      detail.replaceChildren();
      detail.hidden = true;
      status.textContent = "";
      previous.disabled = next.disabled = true;
    }

    function startRequest() {
      if (pending) pending.abort();
      pending = new AbortController();
      return pending;
    }

    function current(controller) {
      return panel.open && panel.isConnected && !disposed && pending === controller;
    }

    function node(parent, tag, label) {
      var element = document.createElement(tag);
      element.textContent = label;
      parent.append(element);
      return element;
    }

    function failure(error) {
      if (error.status === 423) {
        reset();
        clearSelection();
      }
      status.textContent = error.message;
    }

    function load() {
      reset();
      var controller = startRequest();
      var params = new URLSearchParams({offset: offset, limit: limit});
      if (filter.value) params.set("kind", filter.value);
      status.textContent = "Consultando archivo…";
      api("/ai-archive?" + params, {cache:"no-store", signal:controller.signal}).then(function (page) {
        if (!current(controller)) return;
        page.items.forEach(function (item) {
          var row = list.insertRow();
          var selection = node(row.insertCell(), "input", "");
          selection.type = "checkbox";
          selection.setAttribute("aria-label", "Incluir " + item.question);
          selection.checked = selected.has(item.id) && selected.get(item.id) === item.hash;
          selection.addEventListener("change", function () {
            if (selection.checked) selected.set(item.id, item.hash);
            else selected.delete(item.id);
            paintSelection();
          });
          var button = node(row.insertCell(), "button", item.question);
          button.type = "button";
          button.className = "ghost lab-ai-archive-question";
          button.addEventListener("click", function () { inspect(item.id); });
          row.insertCell().textContent = item.comparison ? "Comparación" : item.kind === "research" ? "Estructuras" : "Backtest";
          row.insertCell().textContent = [item.symbol, item.timeframe].filter(Boolean).join(" · ") || "—";
          var date = new Date(item.created_at);
          row.insertCell().textContent = Number.isFinite(date.getTime()) ?
            date.toISOString().slice(0, 16).replace("T", " ") : "—";
          row.insertCell().textContent = item.model;
        });
        status.textContent = page.items.length ?
          String(offset + 1) + "–" + String(offset + page.items.length) : "Sin análisis en esta página.";
        previous.disabled = offset === 0;
        next.disabled = !page.has_more;
        paintSelection();
      }).catch(function (error) { if (current(controller)) failure(error); });
    }

    function inspect(id) {
      var controller = startRequest();
      detail.replaceChildren();
      detail.hidden = false;
      node(detail, "p", "Verificando huella…");
      var path = "/ai-archive/" + encodeURIComponent(id);
      api(path, {cache:"no-store", signal:controller.signal}).then(function (record) {
        if (!current(controller)) return;
        detail.replaceChildren();
        var heading = node(detail, "h4", record.question);
        heading.tabIndex = -1;
        var answer = node(detail, "p", record.answer);
        answer.className = "lab-ai-result";
        var runLabel = record.run_id.slice(0, 8);
        if (record.evidence.schema === "orbis.lab.ai-backtest-comparison.v1") {
          runLabel = record.evidence.runs.map(function (run) { return run.run_id.slice(0, 8); }).join(" ↔ ");
        }
        var meta = node(detail, "p", record.model + " · " + runLabel + " · " + record.hash.slice(0, 12));
        meta.className = "muted";
        var evidence = node(detail, "details", "");
        node(evidence, "summary", "Evidencia guardada");
        node(evidence, "pre", JSON.stringify(record.evidence, null, 2)).className = "lab-events";
        var actions = node(detail, "div", "");
        actions.className = "row";
        var exportButton = node(actions, "button", "Exportar JSON");
        var remove = node(actions, "button", "Borrar");
        [exportButton, remove].forEach(function (button) { button.type = "button"; button.className = "ghost"; });
        exportButton.addEventListener("click", function () {
          if (!current(controller) || !window.confirm("¿Exportar este análisis como JSON sin cifrar?")) return;
          exportButton.disabled = true;
          api(path, {cache:"no-store", signal:controller.signal}).then(function (fresh) {
            if (!current(controller)) return;
            if (fresh.id !== record.id || fresh.hash !== record.hash) throw new Error("El análisis cambió.");
            download({schema:"orbis.lab.ai-archive.v1", environment:"LAB", theoretical_only:true,
              kind:fresh.kind, run_id:fresh.run_id, records:[fresh]}, "orbis-ia-" + fresh.id + ".json");
          }).catch(function (error) {
            if (current(controller)) failure(error);
          }).finally(function () { if (current(controller)) exportButton.disabled = false; });
        });
        remove.addEventListener("click", function () {
          if (!current(controller) || !window.confirm("¿Borrar este análisis de LAB? Las copias anteriores pueden conservarlo.")) return;
          remove.disabled = true;
          api(path, {method:"DELETE", cache:"no-store", signal:controller.signal,
            body:JSON.stringify({confirmation:"BORRAR ANÁLISIS", expected_hash:record.hash})}).then(function () {
            if (current(controller)) {
              selected.delete(id);
              paintSelection();
              load();
            }
          }).catch(function (error) {
            if (current(controller)) { remove.disabled = false; failure(error); }
          });
        });
        heading.focus();
      }).catch(function (error) {
        if (current(controller)) {
          detail.hidden = true; detail.replaceChildren(); failure(error);
        }
      });
    }

    function exportSelected() {
      if (!selected.size || exportPending || !window.confirm(
        "¿Exportar " + selected.size + " análisis como JSONL sin cifrar? Las respuestas proceden de un modelo.")) return;
      var records = Array.from(selected, function (entry) { return {id:entry[0], expected_hash:entry[1]}; });
      var controller = new AbortController();
      exportPending = controller;
      paintSelection();
      function currentExport() {
        return panel.open && panel.isConnected && !disposed && exportPending === controller;
      }
      api("/ai-archive/export-jsonl", {method:"POST", cache:"no-store", signal:controller.signal,
        body:JSON.stringify({records:records, confirmation:"EXPORTAR JSONL EN CLARO"})}).then(function (bundle) {
        if (!currentExport()) return;
        if (bundle.record_count !== records.length || !/^[0-9a-f]{64}$/.test(bundle.sha256) ||
            typeof bundle.jsonl !== "string" || !bundle.jsonl.endsWith("\n")) throw new Error("Exportación incompleta.");
        download(bundle.jsonl, "orbis-ia-" + bundle.sha256.slice(0, 12) + ".jsonl", "application/x-ndjson");
        status.textContent = bundle.record_count + " análisis exportados.";
      }).catch(function (error) {
        if (currentExport()) failure(error);
      }).finally(function () {
        if (currentExport()) {
          exportPending = null;
          paintSelection();
        }
      });
    }

    panel.addEventListener("toggle", function () {
      if (disposed) return;
      if (panel.open) load();
      else {
        reset();
        clearSelection();
      }
    });
    filter.addEventListener("change", function () { offset = 0; clearSelection(); load(); });
    exportSelection.addEventListener("click", exportSelected);
    previous.addEventListener("click", function () { offset = Math.max(0, offset - limit); load(); });
    next.addEventListener("click", function () { offset += limit; load(); });
    panel.querySelector("[data-ai-archive-refresh]").addEventListener("click", load);
    return {dispose:function () { disposed = true; reset(); clearSelection(); }};
  }

  window.OrbisLabAiArchive = {mount:mount};
})();
