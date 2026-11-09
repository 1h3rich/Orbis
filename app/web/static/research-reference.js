/* Referencia de investigación reproducible; no concede permisos de ejecución. */
(function () {
  "use strict";

  function mount(host, options) {
    var button = document.createElement("button");
    button.type = "button";
    button.className = "ghost research-reference";
    button.textContent = "Referencia";
    button.setAttribute("data-help", "Exporta la versión, los datos y las huellas de esta validación en JSON. Repite la comprobación antes de descargar.");
    host.appendChild(button);
    var enabled = false, pending = false, generation = 0;
    function current() { return host.isConnected && options.isCurrent(); }
    function paint() { button.disabled = !enabled || pending; }
    button.addEventListener("click", function () {
      if (!enabled || pending || !current()) return;
      var request = ++generation;
      pending = true; paint();
      fetch(options.url, {cache:"no-store", credentials:"same-origin", headers:{Accept:"application/json"}})
        .then(function (response) {
          return response.json().then(function (body) {
            if (!response.ok) throw new Error(typeof body.detail === "string" ? body.detail : "No se pudo comprobar la referencia.");
            return body;
          });
        }).then(function (reference) {
          if (request !== generation || !enabled || !current()) return;
          if (reference.schema !== "orbis.research.reference.v1" || reference.execution_authority !== "NONE") {
            throw new Error("Formato de referencia no compatible.");
          }
          var blob = new Blob([JSON.stringify(reference, null, 2) + "\n"], {type:"application/json"});
          var url = URL.createObjectURL(blob), link = document.createElement("a");
          try {
            link.href = url; link.download = options.fileName; link.hidden = true;
            host.appendChild(link); link.click();
          } finally {
            link.remove(); setTimeout(function () { URL.revokeObjectURL(url); }, 0);
          }
        }).catch(function (error) {
          if (request !== generation || !current()) return;
          enabled = false;
          options.onError(error);
        }).finally(function () {
          if (request !== generation) return;
          pending = false; paint();
        });
    });
    paint();
    return {setEnabled:function (value) {
      enabled = value === true;
      if (!enabled) { generation++; pending = false; }
      paint();
    }};
  }
  window.OrbisResearchReference = {mount:mount};
})();
