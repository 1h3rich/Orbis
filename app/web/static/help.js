/* Ayuda declarativa: data-help vive en el control al que explica.
 * Mantiene la interfaz compacta sin esconder avisos esenciales. */
(function () {
  "use strict";
  function decorate(element) {
    if (!element || !element.hasAttribute || !element.hasAttribute("data-help")) return;
    var detail = element.getAttribute("data-help");
    if (!detail) return;
    if (!element.hasAttribute("title")) element.setAttribute("title", detail);
    if (!element.hasAttribute("aria-description")) element.setAttribute("aria-description", detail);
  }
  function scan(node) {
    if (!node || node.nodeType !== 1) return;
    decorate(node);
    node.querySelectorAll("[data-help]").forEach(decorate);
  }
  scan(document.body);
  new MutationObserver(function (records) {
    records.forEach(function (record) {
      if (record.type === "attributes") decorate(record.target);
      else record.addedNodes.forEach(scan);
    });
  }).observe(document.body, {childList:true, subtree:true, attributes:true, attributeFilter:["data-help"]});
})();
