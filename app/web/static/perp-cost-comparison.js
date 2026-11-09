/* Fee and slippage sensitivity for a replayed LAB run. */
(function () {
  "use strict";

  const metrics = [
    ["Operaciones", "trades"],
    ["Capital final · USDT", "final_equity"],
    ["Neto · USDT", "net_equity_change"],
    ["Rentabilidad %", "return_pct"],
    ["Comisiones · USDT", "fees_paid"],
    ["Financiación · USDT", "funding_paid"],
    ["Drawdown máx. %", "max_drawdown_pct"],
    ["Salidas parciales", "partial_exits"],
    ["Liquidación", "liquidated"]
  ];

  function mount(panel, api, toPercent, toFraction, format) {
    const form = panel.querySelector("form");
    const submit = form.querySelector("[type=submit]");
    const exportButton = form.querySelector("[data-cost-export]");
    const status = panel.querySelector("[role=status]");
    const table = panel.querySelector("table");
    const body = table.querySelector("tbody");
    const costs = ["fee_rate", "slippage_rate"];
    let selected = null;
    let pending = null;
    let report = null;

    function displayRate(value) {
      // Stored nonnegative rates can include Decimal signed zero.
      return toPercent(value.replace(/^-/,""));
    }

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

    function addRow(label, values, formatValue = String) {
      const row = body.insertRow();
      const heading = document.createElement("th");
      heading.scope = "row";
      heading.textContent = label;
      row.append(heading);
      for (const value of values) {
        const cell = row.insertCell();
        cell.textContent = value == null ? "—" : formatValue(value);
        if (value != null) {
          cell.title = String(value);
          cell.dataset.exact = String(value);
        }
      }
    }

    function state(outcome) {
      if (outcome.status !== "COMPLETE") return "Interrumpida";
      if (outcome.metrics.open_position) return "Posición abierta";
      return "Completa";
    }

    function show(value) {
      report = value;
      const {base, alternative, deltas} = value;
      body.replaceChildren();
      addRow("Estado", [state(base), state(alternative), null]);
      addRow("Comisión por ejecución %", [base.costs.fee_rate, alternative.costs.fee_rate, null], displayRate);
      addRow("Deslizamiento %", [base.costs.slippage_rate, alternative.costs.slippage_rate, null], displayRate);
      for (const [label, key] of metrics) {
        addRow(label, [base.metrics[key], alternative.metrics[key], deltas?.[key]], function (metric) {
          if (typeof metric === "boolean") return metric ? "Sí" : "No";
          return typeof metric === "number" ? String(metric) : format(metric);
        });
      }
      status.textContent = deltas ? "Mismo periodo · escenario experimental" : "Diferencias ocultas · tramo incompleto";
      table.hidden = false;
    }

    async function calculate(download) {
      if (!selected || pending || (download && !report)) return;
      if (download && !window.confirm("El JSON descargado queda sin cifrar. ¿Exportar la comparación?")) return;
      const [fee, slip] = costs.map(key => toFraction(form.elements[key].value));
      if (fee === null || slip === null) {
        status.textContent = "Introduce dos porcentajes válidos.";
        return;
      }
      const {id, hash} = selected.run;
      const expected = download ? report.hash : null;
      reset();
      const request = pending = new AbortController();
      const active = () => pending === request && panel.isConnected && panel.open;
      controls();
      status.textContent = "Reproduciendo escenarios…";
      try {
        const value = await api("/perp/runs/" + encodeURIComponent(id) + "/compare-costs", {
          method: "POST", signal: request.signal,
          body: JSON.stringify({run_hash: hash, fee_rate: fee, slippage_rate: slip, expected_hash: expected})
        });
        if (!active()) return;
        if (value.schema !== "orbis.lab.perp.cost-comparison.v1" ||
            value.original_run.id !== id || value.original_run.hash !== hash ||
            (expected && value.hash !== expected)) throw new Error("La comparación cambió. Vuelve a calcularla.");
        show(value);
        if (download) {
          const url = URL.createObjectURL(new Blob([JSON.stringify(value, null, 2)], {type: "application/json"}));
          const link = document.createElement("a");
          link.href = url;
          link.download = "orbis-perp-costes-" + value.hash.slice(0, 12) + ".json";
          link.click();
          setTimeout(() => URL.revokeObjectURL(url), 1000);
        }
      } catch (error) {
        if (active()) status.textContent = error.message;
      } finally {
        if (pending === request) {
          pending = null;
          controls();
        }
      }
    }

    form.addEventListener("submit", event => { event.preventDefault(); calculate(false); });
    form.addEventListener("input", reset);
    exportButton.addEventListener("click", () => calculate(true));
    panel.addEventListener("toggle", () => { if (!panel.open) reset(); });

    return {setRun(bundle) {
      selected = bundle;
      panel.open = false;
      for (const key of costs) form.elements[key].value = bundle ? displayRate(bundle.strategy.definition[key]) : "";
      reset();
    }};
  }

  window.OrbisPerpCostComparison = {mount};
})();
