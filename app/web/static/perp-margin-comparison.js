/* Margin and leverage scenarios for one archived LAB run. */
(function () {
  "use strict";

  const reasons = {
    SKIPPED_SIZE_OR_MARGIN: "Tamaño o margen",
    SKIPPED_PRICE_BELOW_TICK: "Precio bajo el tick",
    ORDER_REJECTED: "Orden rechazada",
    ENTRY_PRE_TRADE_CANCELLED: "Cancelada antes del fill",
    LEVERAGE_TIER: "Límite de apalancamiento del tramo"
  };

  function mount(panel, api, utc, format) {
    panel.innerHTML = '<summary data-help="Repite el mismo tramo con margen aislado o cruzado y distintos apalancamientos.">Margen y apalancamiento</summary>' +
      '<form class="row"><label class="field" data-help="Hasta tres valores separados por espacios. Admite coma decimal.">Apalancamiento ×' +
      '<input name="leverages" value="1 2 5" maxlength="200" required></label><label class="field">Margen<select name="margin">' +
      '<option value="BOTH">Aislado y cruzado</option><option value="ISOLATED">Aislado</option><option value="CROSS">Cruzado</option></select></label>' +
      '<button type="submit" disabled>Comparar margen</button><button type="button" class="ghost" disabled>Exportar JSON</button></form>' +
      '<p class="muted" role="status"></p><div class="tablewrap"><table aria-label="Comparación de margen y apalancamiento" hidden>' +
      '<thead><tr><th scope="col">Escenario</th><th scope="col">Estado</th><th scope="col">Capital al corte · USDT</th>' +
      '<th scope="col">Drawdown %</th><th scope="col">Operaciones</th><th scope="col">Entradas bloqueadas</th>' +
      '<th scope="col">Liquidación · MARK</th></tr></thead><tbody></tbody></table></div>';
    const form = panel.querySelector("form");
    const submit = form.querySelector("[type=submit]");
    const exportButton = form.querySelector("[type=button]");
    const status = panel.querySelector("[role=status]");
    const table = panel.querySelector("table");
    const body = table.querySelector("tbody");
    let selected = null;
    let pending = null;
    let report = null;

    function controls() {
      submit.disabled = !selected || !!pending;
      exportButton.disabled = !report || !!pending;
    }
    function reset() {
      pending?.abort();
      pending = null;
      report = null;
      status.textContent = "";
      body.replaceChildren();
      table.hidden = true;
      controls();
    }
    function addScenario(scenario) {
      const row = body.insertRow();
      const heading = document.createElement("th");
      const metrics = scenario.metrics;
      const liquidation = scenario.liquidation;
      const profile = (scenario.margin_mode === "ISOLATED" ? "Aislado" : "Cruzado") + " · " + scenario.leverage + "×";
      heading.scope = "row";
      heading.textContent = scenario.base ? "Base · " + profile : profile;
      row.append(heading);
      const restrictions = scenario.entry_restrictions.map(item => {
        const reason = item.reason && (reasons[item.reason] || item.reason);
        return (reason || reasons[item.action]) + " ×" + item.count;
      });
      const state = scenario.complete ? (metrics.trades ? "Completa" : "Sin operaciones") :
        (metrics.open_position ? "Interrumpida · posición abierta" : "Interrumpida");
      const values = [
        [state, scenario.halt_reason],
        [format(metrics.final_equity), metrics.final_equity],
        [format(metrics.max_drawdown_pct), metrics.max_drawdown_pct],
        [metrics.trades, null],
        [restrictions.length ? restrictions.join(" · ") : "—", null],
        liquidation ? [format(liquidation.mark_price) + " · " + utc(liquidation.time_ns) + " UTC",
          liquidation.mark_price + " · " + liquidation.time_ns + " ns · " + liquidation.cause] : ["—", null]
      ];
      for (const [label, exact] of values) {
        const cell = row.insertCell();
        cell.textContent = label;
        if (exact != null) {
          cell.title = exact;
          cell.dataset.exact = exact;
        }
      }
    }
    async function compare(download) {
      if (!selected || pending || (download && !report)) return;
      if (download && !window.confirm("¿Descargar la comparación de margen en un JSON sin cifrar?")) return;
      const leverages = form.elements.leverages.value.trim().split(/\s+/).map(value => value.replace(",", "."));
      if (leverages.length > 3 || !leverages.every(value => /^\d+(\.\d+)?$/.test(value))) {
        status.textContent = "Introduce entre uno y tres apalancamientos separados por espacios.";
        return;
      }
      const mode = form.elements.margin.value;
      const marginModes = mode === "BOTH" ? ["ISOLATED", "CROSS"] : [mode];
      const {id, hash} = selected;
      const expected = download ? report.hash : null;
      reset();
      const request = pending = new AbortController();
      const active = () => pending === request && panel.isConnected && panel.open;
      controls();
      status.textContent = "Reproduciendo escenarios…";
      try {
        const value = await api("/perp/runs/" + encodeURIComponent(id) + "/compare-margin", {
          method: "POST", signal: request.signal,
          body: JSON.stringify({run_hash: hash, leverages, margin_modes: marginModes, expected_hash: expected})
        });
        if (!active()) return;
        if (value.schema !== "orbis.lab.perp.margin-comparison.v1" ||
            value.original_run.id !== id || value.original_run.hash !== hash ||
            (expected && value.hash !== expected)) throw new Error("La comparación cambió. Vuelve a calcularla.");
        value.scenarios.forEach(addScenario);
        report = value;
        table.hidden = false;
        status.textContent = value.scenarios.length + " escenarios · mismo periodo · experimental";
        if (download) {
          const url = URL.createObjectURL(new Blob([JSON.stringify(value, null, 2)], {type:"application/json"}));
          const link = document.createElement("a");
          link.href = url;
          link.download = "orbis-perp-margen-" + value.hash.slice(0, 12) + ".json";
          link.click();
          setTimeout(() => URL.revokeObjectURL(url), 1000);
        }
      } catch (error) {
        if (active()) status.textContent = error.message;
      } finally {
        if (pending === request) { pending = null; controls(); }
      }
    }

    form.addEventListener("submit", event => { event.preventDefault(); compare(false); });
    form.addEventListener("input", reset);
    exportButton.addEventListener("click", () => compare(true));
    panel.addEventListener("toggle", () => { if (!panel.open) reset(); });
    return {setRun(run) {
      selected = run;
      panel.open = false;
      form.reset();
      reset();
    }};
  }

  window.OrbisPerpMarginComparison = {mount};
})();
