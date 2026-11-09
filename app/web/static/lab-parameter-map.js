/* Display of archived development results. Colour intensity is visual only. */
(function () {
  "use strict";

  function element(tag, label) {
    const node = document.createElement(tag);
    if (label !== undefined) node.textContent = label;
    return node;
  }

  function appendHeading(row, label, scope) {
    const heading = element("th", label);
    heading.scope = scope;
    row.append(heading);
  }

  function displayNet(raw, value) {
    if (!Number.isFinite(value)) return "—";
    const exact = window.OrbisUI.fmtDecimal(raw, 2, 2);
    const magnitude = exact ?? Math.abs(value).toLocaleString("es-ES", {
      minimumFractionDigits:2, maximumFractionDigits:2,
    });
    if (value === 0) return magnitude;
    if (magnitude === "0,00") return value < 0 ? "− <0,01" : "+ <0,01";
    return (value < 0 ? "−" : "+") + magnitude.replace(/^-/, "");
  }

  function render(host, candidates) {
    host.replaceChildren();
    if (!candidates.length) return;
    const rows = new Map();
    const targets = new Set();
    let max = 0;
    for (const candidate of candidates) {
      const value = Number(candidate.net);
      if (candidate.eligible && Number.isFinite(value)) max = Math.max(max, Math.abs(value));
      if (!rows.has(candidate.stop)) rows.set(candidate.stop, new Map());
      rows.get(candidate.stop).set(candidate.target, {...candidate, value});
      targets.add(candidate.target);
    }

    const table = element("table");
    table.append(element("caption", "Mapa de calor · Neto de desarrollo"));
    const header = table.createTHead().insertRow();
    appendHeading(header, "Stop ↓ / Objetivo →", "col");
    for (const target of targets) appendHeading(header, target + " %", "col");
    const body = table.createTBody();
    for (const [stop, scores] of rows) {
      const row = body.insertRow();
      appendHeading(row, stop + " %", "row");
      for (const target of targets) {
        const score = scores.get(target);
        const cell = row.insertCell();
        if (!score) {
          cell.textContent = "—";
          continue;
        }
        const {net, value, eligible, selected} = score;
        const tone = !eligible ? "excluded" : !Number.isFinite(value) ? "missing"
          : value > 0 ? "gain" : value < 0 ? "loss" : "zero";
        cell.dataset.tone = tone;
        if (max && eligible && Number.isFinite(value) && value !== 0) {
          cell.style.setProperty("--heat", (10 + 28 * (Math.abs(value) / max)) + "%");
        }
        cell.append(element("strong", displayNet(net, value)));
        cell.title = "Neto exacto · " + net;
        if (selected) {
          cell.dataset.selected = "";
          cell.append(element("small", "Elegida"));
        } else if (!eligible) {
          cell.append(element("small", "Excluida"));
        }
      }
    }
    const scroller = element("div");
    scroller.className = "lab-parameter-map-scroll";
    scroller.append(table);
    const legend = element("div");
    legend.className = "lab-parameter-map-legend";
    legend.dataset.help = "La intensidad usa el mayor neto absoluto elegible de este mapa. Los valores de la reserva se muestran aparte.";
    for (const [tone, label] of [["loss","Pérdida"], ["zero","Cero"], ["gain","Ganancia"], ["excluded","Excluida"]]) {
      const key = element("span", label);
      key.dataset.tone = tone;
      legend.append(key);
    }
    host.append(scroller, legend);
  }

  window.OrbisLabParameterMap = {render};
})();
