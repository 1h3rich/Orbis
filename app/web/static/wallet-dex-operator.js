/* Generated operator preparation. Passwords/files stay out of browser storage. */
(function(){
  'use strict';
  var root=null,api=null,epoch=0,identity='',seenProvider=null,busy=false,draft=null,needsLoad=true,pendingId=null;
  var message='Crea o restaura tu operadora.';
  function snapshot(){return api&&api.snapshot();}
  function key(s){return s&&s.accountId?[s.accountId,s.generation,s.wallet,s.chain,s.verified].join(':'):'';}
  function ready(s){return s&&s.accountId&&s.wallet&&s.provider&&s.chain==='0x38'&&s.verified;}
  function current(host,issued,s){return root===host&&root.isConnected&&epoch===issued&&key(snapshot())===key(s)&&snapshot().provider===s.provider;}
  function field(name){return root.querySelector('[name="operator_'+name+'"]');}
  function path(s){return '/accounts/'+encodeURIComponent(s.accountId)+'/dex-operator';}
  function post(s,action,body){return api.request(path(s)+'/'+action,{method:'POST',body:JSON.stringify(Object.assign({address:s.wallet},body))});}
  function exists(){return !!(draft&&draft.exists);}
  function notify(){api.onBusyChange();paint();}
  function apply(value){draft=value&&value.exists?value:null;if(draft&&draft.backup_verified)api.useOperator(draft.operator_address);}
  function paint(){
    if(!root||!root.isConnected)return;
    var valid=ready(snapshot()),locked=busy||api.blocked()||!valid,hasFile=field('backup_file').files.length===1;
    root.querySelectorAll('input,button,select').forEach(function(el){el.disabled=locked;});
    field('minutes').disabled=locked||exists()||!!pendingId;
    root.querySelector('[data-operator-create]').disabled=locked||exists();
    root.querySelector('[data-operator-discard]').disabled=locked||!exists();
    root.querySelector('[data-operator-export]').disabled=locked||!exists()||field('export_password').value.length<12;
    root.querySelector('[data-operator-verify]').disabled=locked||!exists()||!hasFile||field('import_password').value.length<12;
    root.querySelector('[data-operator-restore]').disabled=locked||exists()||!hasFile||field('import_password').value.length<12;
    root.querySelector('[data-operator-address]').textContent=exists()?draft.operator_address:'';
    root.querySelector('[data-operator-status]').textContent=message;
    root.querySelector('[data-operator-copy-status]').textContent=exists()?(draft.backup_verified?'Copia comprobada':'Copia pendiente')+' · Memoria hasta '+new Date(draft.expires_at).toLocaleTimeString():'';
  }
  async function run(task){
    var s=snapshot();if(busy||api.blocked()||!ready(s))return;
    var host=root,issued=epoch;busy=true;notify();
    function valid(){return current(host,issued,s);}
    try{await task(s,valid);}
    catch(e){if(valid())message=e.message&&!/fetch|network/i.test(e.message)?e.message:'Respuesta sin confirmar · Actualiza la operadora.';}
    finally{if(valid()){busy=false;notify();}}
  }
  function load(){
    if(busy||api.blocked()||!ready(snapshot()))return;
    needsLoad=false;
    return run(async function(s,valid){
      var result=await post(s,'status',{});if(!valid())return;
      apply(result);pendingId=null;message=exists()?(draft.backup_verified?'Copia comprobada · Revisa política y gas.':'Descarga y comprueba tu copia cifrada.'):'Crea o restaura tu operadora.';
    });
  }
  function create(){return run(async function(s,valid){
    var identifier=pendingId||(pendingId=api.uuid());message='Creando operadora…';paint();
    try{
      var result=await post(s,'draft',{request_id:identifier,ttl_seconds:Number(field('minutes').value)*60});if(!valid())return;
      apply(result);pendingId=null;message='Operadora creada · Descarga y comprueba su copia.';
    }catch(e){if(valid()&&e.status>=400&&e.status<500)pendingId=null;throw e;}
  });}
  function exportCopy(){return run(async function(s,valid){
    if(!exists())return;
    var id=draft.id,password=field('export_password').value;field('export_password').value='';
    message='Cifrando copia…';paint();
    try{
      var result=await post(s,'backup',{draft_id:id,password:password});if(!valid())return;
      var url=URL.createObjectURL(new Blob([JSON.stringify(result,null,2)],{type:'application/json'}));
      var link=document.createElement('a');link.href=url;link.download='orbis-operadora-'+id.slice(0,8)+'.json';root.append(link);link.click();link.remove();
      setTimeout(function(){URL.revokeObjectURL(url);},5000);
      message='Copia descargada · Selecciónala para comprobarla.';
    }finally{password='';}
  });}
  function importCopy(restore){return run(async function(s,valid){
    var file=field('backup_file').files[0],password=field('import_password').value;field('import_password').value='';
    if(!file||file.size>8192)throw Error('El archivo de copia debe ocupar como máximo 8 KB.');
    message=restore?'Restaurando copia…':'Comprobando copia…';paint();
    try{
      var content=await file.text();if(!valid())return;
      var body={password:password,backup_json:content};
      if(restore){body.request_id=pendingId||(pendingId=api.uuid());body.ttl_seconds=Number(field('minutes').value)*60;}
      else{if(!exists())throw Error('Selecciona primero una operadora.');body.draft_id=draft.id;}
      var result=await post(s,restore?'restore':'verify-backup',body);if(!valid())return;
      apply(result);pendingId=null;field('backup_file').value='';
      message=restore?'Copia restaurada · Revisa política y gas.':'Copia comprobada · Revisa política y gas.';
    }catch(e){if(valid()&&e.status>=400&&e.status<500)pendingId=null;throw e;}
    finally{password='';}
  });}
  function discard(){
    if(!exists()||!confirm('¿Retirar la preparación local de esta operadora? Conserva su copia cifrada para recuperarla. No envía STOP ni mueve fondos.'))return;
    return run(async function(s,valid){
      var result=await post(s,'discard',{draft_id:draft.id,confirmation:'RETIRAR_PREPARACION_OPERADORA_DEX'});if(!valid())return;
      draft=null;pendingId=null;field('backup_file').value='';message='Preparación retirada.';
    });
  }
  function clear(){
    ++epoch;identity='';seenProvider=null;busy=false;draft=null;pendingId=null;needsLoad=true;message='Crea o restaura tu operadora.';
    if(root&&root.isConnected)root.querySelectorAll('form').forEach(function(form){form.reset();});paint();
  }
  function update(){
    if(!root||!root.isConnected)return;
    var s=snapshot(),next=key(s);
    if(identity!==next||seenProvider!==s.provider){clear();identity=next;seenProvider=s.provider;}
    paint();
    if(root.querySelector('details').open&&needsLoad&&!busy&&!api.blocked()&&ready(s))queueMicrotask(load);
  }
  function mount(element,client){
    root=element;api=client;
    root.innerHTML='<details><summary data-help="Genera una cuenta dedicada para el ejecutor. Su clave vive en memoria; la copia cifrada permite recuperarla. Configuración, gas y activación siguen siendo pasos separados.">Operadora · Preparar</summary>'+
      '<div class="row"><label class="field" data-help="Tiempo máximo de la clave en memoria. La política y el permiso del bot pueden caducar antes.">Vigencia<select name="operator_minutes"><option value="15">15 min</option><option value="60">1 hora</option><option value="360">6 horas</option><option value="1440">24 horas</option></select></label><button type="button" class="ghost" data-operator-create>Nueva operadora</button><button type="button" class="ghost" data-operator-refresh>Actualizar operadora</button><button type="button" class="ghost" data-operator-discard>Retirar preparación</button></div>'+
      '<code data-operator-address></code><p data-operator-copy-status class="muted"></p>'+
      '<form class="row"><label class="field">Contraseña de copia<input name="operator_export_password" type="password" autocomplete="new-password" minlength="12" maxlength="256"></label><button type="button" class="ghost" data-operator-export>Descargar copia</button></form>'+
      '<form class="row"><label class="field">Copia cifrada<input name="operator_backup_file" type="file" accept=".json,application/json"></label><label class="field">Contraseña<input name="operator_import_password" type="password" autocomplete="current-password" minlength="12" maxlength="256"></label>'+
      '<button type="button" class="ghost" data-operator-verify>Comprobar copia</button><button type="button" class="ghost" data-operator-restore>Restaurar copia</button></form><p data-operator-status class="state muted" role="status"></p></details>';
    root.querySelectorAll('form').forEach(function(form){form.addEventListener('submit',function(e){e.preventDefault();});});
    root.addEventListener('input',paint);root.addEventListener('change',paint);
    root.querySelector('[data-operator-create]').addEventListener('click',create);
    root.querySelector('[data-operator-refresh]').addEventListener('click',load);
    root.querySelector('[data-operator-export]').addEventListener('click',exportCopy);
    root.querySelector('[data-operator-verify]').addEventListener('click',function(){importCopy(false);});
    root.querySelector('[data-operator-restore]').addEventListener('click',function(){importCopy(true);});
    root.querySelector('[data-operator-discard]').addEventListener('click',discard);
    root.querySelector('details').addEventListener('toggle',function(){if(this.open)load();});
    clear();identity=key(snapshot());seenProvider=snapshot().provider;paint();
  }
  window.OrbisDexOperatorDraft={mount:mount,update:update,clear:clear,isBusy:function(){return busy;},verifiedDraft:function(){
    return draft&&draft.backup_verified&&Date.now()<Date.parse(draft.expires_at)?{id:draft.id,operator_address:draft.operator_address}:null;
  }};
})();
