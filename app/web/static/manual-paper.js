/* Manual PAPER creation, unlock and verified offline protection. Secrets stay in RAM. */
(function () {
  "use strict";
  var previous=null, storageKey="orbis.manual.paper.protection.job";
  function saved(){try{return sessionStorage.getItem(storageKey);}catch(_){return null;}}
  function remember(id){try{if(id)sessionStorage.setItem(storageKey,id);else sessionStorage.removeItem(storageKey);}catch(_){}}
  function mount(root,options){
    if(previous)previous.dispose();options=options||{};
    var alive=true, epoch=0, busy=null, reading=false, state=null, control=null, waiting=saved(), timer=null, controllers=new Set(), actionError=null, protectionError=null;
    root.innerHTML='<div id="cfg-legacy-status" class="state" role="status">Comprobando PAPER…</div>'+
      '<form id="cfg-legacy-create-form" class="row" hidden><label class="field">Nueva contraseña PAPER<input name="passphrase" type="password" minlength="12" maxlength="1024" autocomplete="new-password" required></label>'+
      '<label class="field">Repetir contraseña<input name="repeated" type="password" minlength="12" maxlength="1024" autocomplete="new-password" required></label><button type="submit" class="ghost">Crear PAPER cifrado</button></form>'+
      '<form id="cfg-legacy-unlock-form" class="row" hidden><label class="field" data-help="Desbloqueo local de 15 minutos; la clave sólo vive en memoria.">Contraseña PAPER<input name="passphrase" type="password" minlength="12" maxlength="1024" autocomplete="off" required></label><button type="submit" class="ghost">Desbloquear</button></form>'+
      '<button id="cfg-legacy-lock" type="button" class="ghost" hidden data-help="Cancela un desbloqueo pendiente y bloquea nuevas operaciones. Las solicitudes ya iniciadas pueden terminar.">Bloquear PAPER</button>'+
      '<form id="cfg-legacy-protect-form" hidden><div class="row"><label class="field">Contraseña nueva<input name="passphrase" type="password" minlength="12" maxlength="1024" autocomplete="new-password" required></label>'+
      '<label class="field">Repetir contraseña<input name="repeated" type="password" minlength="12" maxlength="1024" autocomplete="new-password" required></label>'+
      '<button type="submit" class="ghost" data-help="Pausa ORBIS, verifica una copia cifrada y conserva operaciones, saldos y preferencias. PAPER vuelve bloqueado, en el mismo puerto. Guarda la contraseña.">Proteger PAPER</button></div></form>'+
      '<div id="cfg-legacy-protect-state" class="state muted" role="status"></div><div id="cfg-legacy-protect-result"></div>'+
      '<p id="cfg-legacy-migration" class="muted" hidden>Para cifrar PAPER activo, cierra ORBIS y usa <code>.venv/bin/python -m scripts.encrypt_legacy_paper --source /ruta/orbis.db</code>. Abre el lanzador actualizado para protegerlo desde Ajustes.</p>'+
      '<div id="cfg-legacy-copies" class="state blocked" role="status" hidden></div>'+
      '<form id="cfg-legacy-backup-form" class="row" hidden><label class="field">Clave de copia<input name="passphrase" type="password" minlength="12" maxlength="1024" autocomplete="new-password" required></label>'+
      '<label class="field">Repetir clave<input name="repeat" type="password" minlength="12" maxlength="1024" autocomplete="new-password" required></label>'+
      '<label class="field" style="display:flex;align-items:center;gap:8px;min-height:42px"><input name="acknowledge" type="checkbox" style="width:16px;height:16px;min-height:16px;padding:0;margin:0;flex:0 0 16px;accent-color:var(--menta)" required><span id="cfg-legacy-backup-ack-text">Solo copia: PAPER activo no cambia</span></label>'+
      '<button type="submit" class="ghost">Crear copia cifrada</button></form><div id="cfg-legacy-backup-state" class="state muted" role="status" hidden></div>';
    function el(name){return root.querySelector('#cfg-legacy-'+name);}
    function valid(ticket){return alive&&root.isConnected&&ticket===epoch;}
    function words(node,text){node.textContent=text;}
    function protectNotice(text){words(el('protect-state'),text||'');el('protect-state').hidden=!text;}
    function clearSecrets(){root.querySelectorAll('input[type=password]').forEach(function(input){input.value='';});}
    async function request(path,options){
      var controller=new AbortController();controllers.add(controller);
      var timeout=setTimeout(function(){controller.abort();},6000);
      try{
        var response=await fetch('/paper/legacy'+path,Object.assign({cache:'no-store',credentials:'same-origin',signal:controller.signal,
          headers:{'Content-Type':'application/json'}},options||{}));
        var data=await response.json();
        if(!response.ok){var error=new Error(typeof data.detail==='string'?data.detail:'PAPER no disponible');error.status=response.status;throw error;}
        return data;
      }finally{clearTimeout(timeout);controllers.delete(controller);}
    }
    function guard(){
      var format=state&&state.format, encrypted=format==='encrypted_or_invalid';
      var frozen=!!waiting||!!(state&&(state.maintenance||state.creating));
      el('create-form').hidden=!(state&&state.gui_access&&format==='missing');
      el('unlock-form').hidden=!encrypted||!!state.unlocked;
      el('lock').hidden=!(encrypted&&state.unlocked||state&&state.gui_access&&(busy==='UNLOCKING'||busy==='CREATING'||state.creating));
      el('lock').disabled=!!waiting||busy==='LOCKING'||!!(state&&state.maintenance);
      el('protect-form').hidden=format!=='plain'||!(control&&control.supported);
      el('migration').hidden=format!=='plain'||!!(control&&control.supported);
      el('backup-form').hidden=!(format==='plain'||encrypted&&state.unlocked)||!!(state&&state.requires_review);
      words(el('backup-ack-text'),encrypted?'Solo copia: PAPER activo permanece cifrado':'Solo copia: PAPER activo sigue sin cifrar');
      root.querySelectorAll('form input,form button').forEach(function(input){input.disabled=!!busy||frozen;});
      el('protect-form').querySelector('button').disabled=!!busy||frozen||!control||!control.ready;
    }
    function showState(){
      if(!state)return;
      var text=state.creating?'Creando PAPER cifrado…':state.requires_review?'PAPER cambió · revisar archivo':
        state.format==='plain'?'PAPER sin cifrar':state.format==='encrypted_or_invalid'?
        (state.unlocked?'PAPER cifrado · '+Math.ceil((state.remaining_seconds||0)/60)+' min restantes':'PAPER cifrado · bloqueado'):
        state.format==='memory'?'PAPER temporal · sin archivo':'PAPER no inicializado';
      if(!busy)words(el('status'),text+(actionError?' · '+actionError:''));
      if(options.onStatus)options.onStatus(state);
      var plain=state.plaintext_sibling_copies||[],unknown=state.unverified_sibling_copies||[];
      el('copies').hidden=!plain.length&&!unknown.length&&state.sibling_scan_complete!==false;
      if(!el('copies').hidden)words(el('copies'),plain.length+' copia(s) SQLite en claro junto a PAPER'+
        (plain.length?' · '+plain.slice(0,8).join(', '):'')+(unknown.length?' · otras copias sin verificar':''));
    }
    function showJob(job){
      if(!job)return;
      var labels={QUEUED:'Preparando pausa…',RUNNING:'Protegiendo PAPER…',SUCCEEDED:'PAPER protegido · copia verificada',
        FAILED:'No se completó la protección',ENCRYPTED_REVIEW_REQUIRED:'Formato cifrado · revisar copia',REOPEN_REVIEW_REQUIRED:'Revisar reapertura de PAPER'};
      var result=el('protect-result');result.replaceChildren();
      var label=document.createElement('strong');label.textContent=labels[job.state]||'Trabajo en revisión';result.append(label);
      if(job.backup){var details=document.createElement('details'),summary=document.createElement('summary'),path=document.createElement('p');
        summary.textContent='Copia';path.textContent=job.backup;details.append(summary,path);result.append(details);}
      if(job.error){var error=document.createElement('p');error.textContent=job.error;result.append(error);}
    }
    function schedule(delay){clearTimeout(timer);if(alive&&root.isConnected)timer=setTimeout(load,delay||1000);}
    async function load(){
      if(reading||busy&&!waiting){schedule();return;}
      reading=true;var ticket=epoch;
      try{
        var next=await request('/status');if(!valid(ticket))return;
        if(!next||typeof next.format!=='string')throw new Error('Estado PAPER no verificable');
        state=next;showState();
        try{control=await request('/protection');}catch(error){if(error.status===404)control=null;else throw error;}
        if(!valid(ticket))return;
        protectNotice(waiting?'Comprobando el mismo trabajo…':protectionError||
          (state.format==='plain'&&control&&control.supported?(control.reason||''):''));
        var job=control&&control.latest_job;
        if(waiting){
          try{job=await request('/protection/jobs/'+encodeURIComponent(waiting));}
          catch(error){if(error.status===404){waiting=null;remember(null);protectionError='Trabajo no disponible · revisa el estado actual';protectNotice(protectionError);}else throw error;}
          if(!valid(ticket))return;
        }
        showJob(job);
        if(waiting&&job&&!['QUEUED','RUNNING'].includes(job.state)){
          waiting=null;remember(null);guard();
          if(options.onComplete){options.onComplete();return;}
        }
        guard();
      }catch(error){if(valid(ticket)){if(waiting)protectNotice('Servicio en pausa · comprobando el resultado…');else words(el('status'),error.message);guard();}}
      finally{reading=false;}
      if(waiting||state&&state.creating)schedule();
      else if(root.closest('details')&&root.closest('details').open)schedule(5000);
    }
    async function act(name,path,payload,form){
      var ticket=++epoch;busy=name;actionError=null;guard();clearSecrets();words(el('status'),name==='LOCKING'?'Bloqueando PAPER…':name==='CREATING'?'Creando PAPER cifrado…':'Desbloqueando PAPER…');
      try{
        var next=await request(path,{method:'POST',body:JSON.stringify(payload)});payload=null;
        if(!valid(ticket))return;state=next;
      }catch(error){payload=null;if(valid(ticket)){actionError=error.message;words(el('status'),error.message);}}
      finally{if(valid(ticket)){busy=null;guard();schedule(150);}}
    }
    el('create-form').addEventListener('submit',function(event){event.preventDefault();if(busy||waiting)return;
      var form=this,key=form.elements.passphrase.value,repeated=form.elements.repeated.value;
      if(key!==repeated){clearSecrets();actionError='Las contraseñas no coinciden';words(el('status'),actionError);return;}
      act('CREATING','/create',{passphrase:key,repeated:repeated,confirmation:'CREAR PAPER CIFRADO'},form);
    });
    el('unlock-form').addEventListener('submit',function(event){event.preventDefault();if(busy||waiting)return;
      act('UNLOCKING','/unlock',{passphrase:this.elements.passphrase.value},this);
    });
    el('lock').onclick=function(){if(waiting||busy==='LOCKING')return;act('LOCKING','/lock',{},null);};
    el('backup-form').addEventListener('submit',async function(event){
      event.preventDefault();if(busy||waiting)return;var form=this,box=el('backup-state');box.hidden=false;
      if(form.elements.passphrase.value!==form.elements.repeat.value){clearSecrets();words(box,'Las claves no coinciden');return;}
      var body=JSON.stringify({passphrase:form.elements.passphrase.value,confirmation:'COPIA PAPER'}),ticket=++epoch;
      clearSecrets();form.elements.acknowledge.checked=false;busy='BACKUP';guard();words(box,'Creando copia cifrada…');
      try{var result=await request('/backup/encrypted',{method:'POST',body:body});body=null;if(!valid(ticket))return;
        words(box,'Copia verificada: '+result.path+'. PAPER activo '+(result.active_database_encrypted?'permanece cifrado':'sigue sin cifrar')+'.');
      }catch(error){body=null;if(valid(ticket))words(box,error.message);}
      finally{if(valid(ticket)){busy=null;guard();schedule(150);}}
    });
    el('protect-form').addEventListener('submit',async function(event){
      event.preventDefault();if(busy||waiting||!control||!control.ready)return;
      var key=this.elements.passphrase.value,repeated=this.elements.repeated.value;
      if(key!==repeated){clearSecrets();protectionError='Las contraseñas no coinciden';protectNotice(protectionError);return;}
      var id=crypto.randomUUID(),body=JSON.stringify({id:id,passphrase:key,repeated:repeated,confirmation:'CIFRAR PAPER'});
      key=null;repeated=null;clearSecrets();waiting=id;remember(id);busy='PROTECTING';var ticket=++epoch;guard();
      protectionError=null;protectNotice('Preparando protección…');
      try{var job=await request('/protection/jobs',{method:'POST',body:body});body=null;if(valid(ticket))showJob(job);}
      catch(error){body=null;if(!valid(ticket))return;if(error.status&&error.status<500){waiting=null;remember(null);protectionError=error.message;protectNotice(protectionError);}}
      finally{if(valid(ticket)){busy=null;guard();schedule();}}
    });
    var observer=new MutationObserver(function(){if(!root.isConnected)widget.dispose();});
    var widget={load:load,dispose:function(){if(!alive)return;alive=false;epoch++;clearTimeout(timer);observer.disconnect();
      controllers.forEach(function(controller){controller.abort();});controllers.clear();clearSecrets();if(previous===widget)previous=null;}};
    observer.observe(document.body,{childList:true,subtree:true});previous=widget;guard();load();return widget;
  }
  window.OrbisManualPaper={mount:mount};
})();
