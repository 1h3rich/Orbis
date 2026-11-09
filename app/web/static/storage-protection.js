/* A password stays in the form/request only. Persist just an opaque job ID. */
(function () {
  "use strict";
  var previous = null, key = "orbis.storage.protection.job";
  function esc(value) { return String(value == null ? "" : value).replace(/[&<>"']/g, function(c) {return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c];}); }
  function stored() { try {return sessionStorage.getItem(key);} catch(e) {return null;} }
  function remember(id) { try {if(id)sessionStorage.setItem(key,id);else sessionStorage.removeItem(key);}catch(e){} }
  function mount(root, options) {
    if (previous) previous.dispose();
    options = options || {};
    var alive=true, timer=null, generation=0, controllers=new Set(), state=null, waiting=stored(), storesKey=null, observer=null;
    root.innerHTML='<form id="storage-protection-form"><fieldset><legend>Almacenes</legend><div id="storage-protection-stores" class="row"></div></fieldset>'+
      '<div class="row"><label class="field">Contraseña nueva<input name="passphrase" type="password" minlength="12" maxlength="1024" autocomplete="new-password" required></label>'+
      '<label class="field">Repetir contraseña<input name="repeated" type="password" minlength="12" maxlength="1024" autocomplete="new-password" required></label>'+
      '<button type="submit" disabled data-help="Pausa el servicio, verifica copias cifradas y vuelve a abrirlo. Guarda esta contraseña: los almacenes protegidos quedarán bloqueados. No cierra posiciones ni mueve fondos.">Proteger</button></div></form>'+
      '<p id="storage-protection-status" class="state" role="status">Comprobando…</p><div id="storage-protection-result"></div>'+
      '<small data-help="Este flujo protege LAB, DCA y los PAPER de estrategias. La bóveda REAL y PAPER manual tienen sus propios controles.">LAB · DCA · Spot PAPER · GRVT PAPER</small>';
    var form=root.querySelector('form'), list=root.querySelector('#storage-protection-stores');
    var status=root.querySelector('#storage-protection-status'), result=root.querySelector('#storage-protection-result');
    var button=form.querySelector('button'), inputs=form.querySelectorAll('input[type=password]');
    function current(id) {return alive && root.isConnected && id===generation;}
    function update() {
      var picked=list.querySelectorAll('input:checked').length;
      button.disabled=!state || !state.ready || !!waiting || !picked;
      inputs.forEach(function(input){input.disabled=!!waiting || !!(state && state.busy);});
    }
    async function api(path,options) {
      var controller=new AbortController();controllers.add(controller);
      var timeout=setTimeout(function(){controller.abort();},6000);
      try {
        var response=await fetch('/system/storage-protection'+path,Object.assign({cache:'no-store',signal:controller.signal,
          headers:{'Content-Type':'application/json'}},options||{}));
        var body=await response.json();
        if(!response.ok){var error=new Error(typeof body.detail==='string'?body.detail:'No se pudo comprobar el trabajo.');error.status=response.status;throw error;}
        return body;
      } finally {clearTimeout(timeout);controllers.delete(controller);}
    }
    var names={ENCRYPTED:'Cifrado · copia guardada',FAILED:'No completado',NOT_ATTEMPTED:'Sin iniciar',ENCRYPTED_REVIEW_REQUIRED:'Cifrado · revisar copia'};
    function showJob(job) {
      if(!job) {result.textContent='';return;}
      var labels={QUEUED:'Preparando pausa…',RUNNING:'Protegiendo datos…',SUCCEEDED:'Protección completada',PARTIAL:'Protección parcial',FAILED:'No se completó la protección'};
      result.innerHTML='<strong>'+esc(labels[job.state]||job.state)+'</strong><ul>'+(job.results||[]).map(function(row){
        return '<li><span>'+esc(row.target)+'</span> · <span data-help="'+esc(row.backup||row.error||'')+'">'+esc(names[row.state]||row.state)+'</span></li>';
      }).join('')+'</ul>'+(job.error?'<p class="muted">'+esc(job.error)+'</p>':'');
    }
    function schedule(delay) {clearTimeout(timer);if(alive&&root.isConnected)timer=setTimeout(load,delay||1500);}
    async function load() {
      var ticket=++generation;
      try {
        var response=await api('');if(!current(ticket))return;
        state=response;
        var nextStoresKey=JSON.stringify(response.stores);
        if(nextStoresKey!==storesKey) {
        var chosen=Array.from(list.querySelectorAll('input:checked')).map(function(input){return input.value;});
        var first=!list.children.length;
        list.innerHTML=response.stores.map(function(store){
          return '<label><input type="checkbox" value="'+esc(store.target)+'" '+(!store.eligible?'disabled ':'')+
            (store.eligible&&(first||chosen.indexOf(store.target)>=0)?'checked ':'')+'>'+esc(store.label)+
            ' · '+esc(store.format==='plain'?'sin cifrar':store.format==='encrypted_or_invalid'?'cifrado':store.format==='missing'?'sin datos':'revisar')+'</label>';
        }).join('');
        list.querySelectorAll('input').forEach(function(input){input.addEventListener('change',update);});
        storesKey=nextStoresKey;
        }
        status.textContent=!response.supported?'Disponible al abrir ORBIS con su lanzador.':response.busy?'Pausando el servicio…':response.reason||'Elige los datos que deseas proteger.';
        var job=response.latest_job;
        if(waiting) {
          try {job=await api('/jobs/'+encodeURIComponent(waiting));if(!current(ticket))return;}
          catch(error) {if(error.status===404){waiting=null;remember(null);status.textContent='Trabajo no disponible. Revisa el estado actual de los almacenes.';}else throw error;}
        }
        showJob(job);
        if(waiting&&job&&['SUCCEEDED','PARTIAL','FAILED'].indexOf(job.state)>=0) {
          waiting=null;remember(null);update();
          if(options.onComplete){options.onComplete();return;}
        }
        update();
      } catch(error) {
        if(!current(ticket))return;
        status.textContent=waiting?'El servicio está en pausa. Comprobando el resultado…':error.message;
      }
      if(waiting||state&&state.busy)schedule();
      else if(root.closest("details") && root.closest("details").open)schedule(5000);
    }
    form.addEventListener('submit',async function(event){
      event.preventDefault();if(!state||!state.ready||waiting)return;
      var fields=new FormData(form), password=fields.get('passphrase'), repeated=fields.get('repeated');
      if(password!==repeated){status.textContent='Las contraseñas no coinciden.';inputs.forEach(function(input){input.value='';});return;}
      var targets=Array.from(list.querySelectorAll('input:checked')).map(function(input){return input.value;});
      var id=crypto.randomUUID(), body=JSON.stringify({id:id,targets:targets,passphrase:password,repeated:repeated,confirmation:'PROTEGER DATOS'});
      password=null;repeated=null;fields=null;inputs.forEach(function(input){input.value='';});
      waiting=id;remember(id);var ticket=++generation;update();status.textContent='Preparando protección…';
      try {
        var job=await api('/jobs',{method:'POST',body:body});body=null;if(!current(ticket))return;showJob(job);
      } catch(error) {
        body=null;if(!current(ticket))return;
        if(error.status&&error.status<500){waiting=null;remember(null);status.textContent=error.message;update();return;}
        status.textContent='Respuesta pendiente. Comprobando el mismo trabajo…';
      }
      schedule();
    });
    var widget={load:load,dispose:function(){if(!alive)return;alive=false;generation+=1;clearTimeout(timer);if(observer)observer.disconnect();controllers.forEach(function(c){c.abort();});controllers.clear();inputs.forEach(function(i){i.value='';});if(previous===widget)previous=null;}};
    observer=new MutationObserver(function(){if(!root.isConnected)widget.dispose();});
    observer.observe(document.body,{childList:true,subtree:true});
    previous=widget;return widget;
  }
  window.OrbisStorageProtection={mount:mount};
})();
