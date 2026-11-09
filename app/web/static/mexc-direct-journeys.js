/* Direct credits already recorded in the selected REAL book. */
(function () {
  "use strict";

  function mount(host, options) {
    host.innerHTML = '<summary data-help="Aportaciones USDT registradas desde MEXC View. Consulta local.">Ingresos directos MEXC</summary>' +
      '<div class="row"><label class="field">Ingreso registrado<select data-direct-journey></select></label>' +
      '<button class="ghost" type="button" data-direct-prev>Anterior</button>' +
      '<button class="ghost" type="button" data-direct-next>Siguiente</button>' +
      '<button class="ghost" type="button" data-direct-refresh>Actualizar</button></div>' +
      '<div class="state muted" data-direct-result role="status"></div>';
    const find = key => host.querySelector("[data-direct-" + key + "]");
    const selection = find("journey"), result = find("result");
    const path = "/accounts/" + encodeURIComponent(options.accountId) + "/mexc-external-deposits/journeys";
    const size = 20;
    let offset = 0, hasMore = false, generation = 0;

    function current(ticket) {
      return ticket === generation && host.open && host.isConnected && options.current();
    }

    function controls(busy) {
      selection.disabled = busy || selection.options.length < 2;
      find("prev").disabled = busy || offset === 0;
      find("next").disabled = busy || !hasMore;
      find("refresh").disabled = busy;
    }

    async function load(next) {
      const ticket = ++generation;
      result.textContent = "Cargando ingresos…";
      controls(true);
      try {
        const page = await options.request(path + "?limit=" + size + "&offset=" + next, null, true);
        if (!current(ticket)) return;
        offset = next;
        hasMore = page.has_more;
        selection.replaceChildren(new Option("Selecciona un ingreso", ""));
        page.items.forEach(row => {
          const stamp = row.booked_at.slice(0, 19).replace("T", " ");
          selection.add(new Option(stamp + " UTC · " + row.credits + (row.credits === 1 ? " abono" : " abonos"), row.id));
        });
        result.textContent = page.items.length ? "" : "Sin ingresos directos registrados.";
      } catch (error) {
        if (current(ticket)) {
          selection.replaceChildren();
          offset = 0;
          hasMore = false;
          result.textContent = error.message;
        }
      } finally {
        if (current(ticket)) controls(false);
      }
    }

    selection.addEventListener("change", async function () {
      const ticket = ++generation, id = selection.value;
      result.replaceChildren();
      if (!id) return;
      result.textContent = "Verificando recorrido…";
      try {
        const report = await options.request(path + "/" + encodeURIComponent(id), null, true);
        if (current(ticket) && selection.value === id) {
          window.OrbisMexcJourneyReport.render(result, report, options.hidden(), step => {
            if (current(ticket) && selection.value === id) options.openStep(step);
          });
        }
      } catch (error) {
        if (current(ticket)) result.textContent = error.message;
      }
    });

    host.addEventListener("toggle", function () {
      if (host.open) load(0);
      else {
        generation += 1;
        selection.replaceChildren();
        result.replaceChildren();
      }
    });
    find("prev").onclick = () => load(Math.max(0, offset - size));
    find("next").onclick = () => load(offset + size);
    find("refresh").onclick = () => load(0);
    controls(false);
  }

  window.OrbisMexcDirectJourneys = {mount};
})();
