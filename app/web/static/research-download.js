/* Verified local JSONL research downloads. */
(function () {
  "use strict";
  async function save(result, expected, stillCurrent) {
    if (!stillCurrent()) return false;
    if (!result || result.schema !== expected.schema || result.revision !== expected.revision ||
        result.record_count !== expected.count || !/^[0-9a-f]{64}$/.test(result.sha256 || "") ||
        typeof result.jsonl !== "string" || !result.jsonl.endsWith("\n") ||
        result.jsonl.split("\n").length !== result.record_count + 1) throw new Error("Archivo de fills no verificable");
    var bytes = new TextEncoder().encode(result.jsonl);
    if (bytes.length > (expected.maxBytes || 4*1024*1024)) throw new Error("Archivo de fills demasiado grande");
    var digest = await crypto.subtle.digest("SHA-256",bytes);
    if (!stillCurrent()) return false;
    var sha = Array.from(new Uint8Array(digest),function (value) { return value.toString(16).padStart(2,"0"); }).join("");
    if (sha !== result.sha256) throw new Error("La huella del archivo no coincide");
    var url = URL.createObjectURL(new Blob([bytes],{type:"application/x-ndjson"}));
    var link = document.createElement("a");
    link.href = url; link.download = expected.filename+"-"+sha.slice(0,12)+".jsonl";
    document.body.append(link); link.click(); link.remove();
    setTimeout(function () { URL.revokeObjectURL(url); },1000);
    return true;
  }
  window.OrbisResearchDownload = {save:save};
})();
