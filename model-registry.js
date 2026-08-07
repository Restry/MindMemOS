/* Endpoint registry + cached global model catalog. No remote /models call occurs during page load. */
const MODEL_KINDS=['llm','embedding','rerank'];
let MODELS_LOADED=0,MODELS_TESTED=false,ENDPOINTS=[],CACHED_MODELS=[],CURRENT_MODELS={};
const MODEL_SELECTION=Object.fromEntries(MODEL_KINDS.map(k=>[k,null]));

function modelPrefix(kind){
  const value=(CURRENT_MODELS[kind]||{}).model||'';
  return value.includes('/')?value.split('/',1)[0]:'';
}
function modelItems(kind){
  const prefix=modelPrefix(kind);
  return CACHED_MODELS.map(item=>({...item,model:prefix&&!item.id.startsWith(prefix+'/')?prefix+'/'+item.id:item.id}));
}
function modelPayload(){
  const models={};
  MODEL_KINDS.forEach(k=>{const s=MODEL_SELECTION[k];models[k]=s?{model:s.model,model_id:s.id,endpoint_id:s.endpoint_id}:{model:$('#model-'+k+'-name').value.trim()}});
  return {models};
}
function invalidateModelTest(){
  MODELS_TESTED=false;$('#model-save').disabled=true;$('#model-global-state').textContent='模型选择已变化，请重新测试';
  MODEL_KINDS.forEach(k=>{const e=$('#model-'+k+'-test');e.textContent='尚未测试';e.style.color='var(--dim)'});
}
function closeModelMenu(kind){$('#model-'+kind+'-options').classList.remove('open');$('#model-'+kind+'-name').setAttribute('aria-expanded','false')}
function closeAllModelMenus(except=''){MODEL_KINDS.forEach(k=>{if(k!==except)closeModelMenu(k)})}
function renderModelMenu(kind,query=''){
  const needle=query.trim().toLocaleLowerCase();
  const items=modelItems(kind).filter(x=>!needle||x.model.toLocaleLowerCase().includes(needle)||x.endpoint_name.toLocaleLowerCase().includes(needle));
  $('#model-'+kind+'-options').innerHTML=items.length?items.map(x=>`<button type="button" class="model-option" role="option" data-endpoint="${esc(x.endpoint_id)}" data-id="${esc(x.id)}" data-model="${esc(x.model)}"><b>${esc(x.model)}</b><small>${esc(x.endpoint_name)}</small></button>`).join(''):'<div class="model-option-empty">缓存中没有匹配模型</div>';
}
function openModelMenu(kind,all=false){
  closeAllModelMenus(kind);const input=$('#model-'+kind+'-name'),menu=$('#model-'+kind+'-options');
  renderModelMenu(kind,all?'':input.value);menu.classList.add('open');input.setAttribute('aria-expanded','true');
}
function chooseModelOption(kind,option){
  MODEL_SELECTION[kind]={endpoint_id:option.dataset.endpoint,id:option.dataset.id,model:option.dataset.model};
  $('#model-'+kind+'-name').value=option.dataset.model;
  const ep=ENDPOINTS.find(x=>x.id===option.dataset.endpoint);
  $('#model-'+kind+'-source').textContent='来源：'+(ep?ep.name:option.dataset.endpoint);$('#model-'+kind+'-source').style.color='#34d399';
  closeModelMenu(kind);invalidateModelTest();
}
function endpointTime(value){if(!value)return '尚未同步';try{return fmtBeijing(value)}catch(_){return value}}
function renderEndpoints(){
  $('#endpoint-cache-summary').textContent=`${ENDPOINTS.length} 个 Endpoint · ${CACHED_MODELS.length} 条缓存模型`;
  $('#endpoint-list').innerHTML=ENDPOINTS.length?ENDPOINTS.map(ep=>`<div class="endpoint-item" data-id="${esc(ep.id)}"><strong>${esc(ep.name)}</strong><div class="endpoint-url" title="${esc(ep.endpoint)}">${esc(ep.endpoint)}</div><div class="endpoint-meta">${ep.key_configured?'KEY 已配置':'KEY 未配置'} · ${ep.model_count} 个模型<br>${esc(endpointTime(ep.fetched_at))}</div><div class="endpoint-item-actions"><button data-action="refresh">刷新缓存</button><button data-action="edit">编辑</button><button data-action="delete">删除</button></div></div>`).join(''):'<div class="empty">还没有 Endpoint，请先添加</div>';
}
function showEndpointEditor(ep=null){
  $('#endpoint-edit-id').value=ep?.id||'';$('#endpoint-edit-name').value=ep?.name||'';$('#endpoint-edit-url').value=ep?.endpoint||'';$('#endpoint-edit-key').value='';
  $('#endpoint-editor-state').textContent=ep?'Key 留空保留当前值；保存会更新缓存':'保存时会抓取并缓存 /v1/models';$('#endpoint-editor').style.display='grid';$('#endpoint-edit-name').focus();
}
async function endpointAction(path,body={},button=null){
  if(button)button.disabled=true;
  try{const r=await fetch('/api/model-endpoints/'+path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}),d=await r.json();if(!r.ok||!d.ok)throw new Error(d.error||'操作失败');ENDPOINTS=d.endpoints||[];CACHED_MODELS=d.catalog||[];renderEndpoints();return d}
  finally{if(button)button.disabled=false}
}
async function saveEndpoint(){
  const btn=$('#endpoint-save');btn.disabled=true;$('#endpoint-editor-state').textContent='正在验证 Endpoint 并同步模型…';
  try{await endpointAction('save',{id:$('#endpoint-edit-id').value,name:$('#endpoint-edit-name').value.trim(),endpoint:$('#endpoint-edit-url').value.trim(),api_key:$('#endpoint-edit-key').value.trim()});$('#endpoint-editor').style.display='none';await loadModels()}
  catch(e){$('#endpoint-editor-state').textContent='保存失败：'+e.message}finally{btn.disabled=false}
}
async function refreshEndpoints(id='',button=null){
  $('#model-global-state').textContent='正在刷新 Endpoint 缓存…';
  try{const d=await endpointAction('refresh',id?{id}:{},button),failed=(d.results||[]).filter(x=>!x.ok);$('#model-global-state').textContent=failed.length?`${failed.length} 个 Endpoint 刷新失败，旧缓存已保留`:'缓存刷新完成';await loadModels()}
  catch(e){$('#model-global-state').textContent='刷新失败：'+e.message}
}
function bindCurrentSelections(){
  MODEL_KINDS.forEach(k=>{const current=CURRENT_MODELS[k]||{},raw=(current.model||'').includes('/')?current.model.split('/').slice(1).join('/'):(current.model||'');const hit=modelItems(k).find(x=>x.endpoint_id===current.endpoint_id&&x.id===raw);MODEL_SELECTION[k]=hit||null;$('#model-'+k+'-name').value=current.model||'';const ep=ENDPOINTS.find(x=>x.id===current.endpoint_id);$('#model-'+k+'-source').textContent=hit?'来源：'+(ep?.name||current.endpoint_id):'当前模型尚未出现在缓存，请刷新 Endpoint';$('#model-'+k+'-source').style.color=hit?'#34d399':'#fbbf24'});
}
async function loadModels(){
  MODELS_LOADED=1;MODELS_TESTED=false;$('#model-save').disabled=true;$('#model-global-state').textContent='正在读取本地缓存…';
  try{const [er,mr]=await Promise.all([fetch('/api/model-endpoints',{cache:'no-store'}),fetch('/api/models',{cache:'no-store'})]),e=await er.json(),m=await mr.json();if(!er.ok||!e.ok)throw new Error(e.error||'Endpoint 缓存读取失败');if(!mr.ok||!m.ok)throw new Error(m.error||'模型配置读取失败');ENDPOINTS=e.endpoints||[];CACHED_MODELS=e.catalog||[];CURRENT_MODELS=m.models||{};renderEndpoints();bindCurrentSelections();$('#model-config-path').textContent='运行时配置 · Endpoint 缓存文件权限 0600';$('#model-global-state').textContent=`只读本地缓存：${ENDPOINTS.length} 个 Endpoint / ${CACHED_MODELS.length} 条模型记录`}
  catch(e){MODELS_LOADED=0;$('#model-global-state').textContent='读取失败：'+e.message}
}
function requireModelSelections(){for(const k of MODEL_KINDS)if(!MODEL_SELECTION[k])throw new Error(`${k} 必须从缓存目录中选择模型`)}
async function testModels(){
  const btn=$('#model-test');btn.disabled=true;btn.textContent='测试中…';$('#model-save').disabled=true;
  try{requireModelSelections();const r=await fetch('/api/models/test',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(modelPayload())}),d=await r.json();if(!r.ok||d.error)throw new Error(d.error||'测试失败');MODEL_KINDS.forEach(k=>{const x=d.results?.[k]||{ok:false,message:'没有测试结果'},el=$('#model-'+k+'-test');el.textContent=(x.ok?'✓ ':'✕ ')+x.message;el.style.color=x.ok?'#34d399':'#f87171'});MODELS_TESTED=!!d.ok;$('#model-save').disabled=!MODELS_TESTED;$('#model-global-state').textContent=d.ok?'全部连接通过，可以保存':'存在失败项'}
  catch(e){MODELS_TESTED=false;$('#model-global-state').textContent='测试失败：'+e.message}finally{btn.disabled=false;btn.textContent='测试全部（不保存）'}
}
async function saveModels(){
  if(!MODELS_TESTED)return;const btn=$('#model-save');btn.disabled=true;btn.textContent='保存并重载中…';
  try{requireModelSelections();const r=await fetch('/api/models',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(modelPayload())}),d=await r.json();if(!r.ok||!d.ok)throw new Error(d.error||'保存失败');$('#model-global-state').textContent=(d.reload_message||'保存完成')+' · 备份 '+(d.backup||'已创建');setTimeout(loadModels,1800)}
  catch(e){$('#model-global-state').textContent='保存失败：'+e.message;btn.disabled=false}finally{btn.textContent='保存并重载服务'}
}
MODEL_KINDS.forEach(k=>{
  const input=$('#model-'+k+'-name'),menu=$('#model-'+k+'-options'),toggle=$('#model-'+k+'-toggle');
  input.addEventListener('input',()=>{MODEL_SELECTION[k]=null;$('#model-'+k+'-source').textContent='输入搜索后请从下拉选择完整项';invalidateModelTest();openModelMenu(k)});input.addEventListener('focus',()=>openModelMenu(k));
  input.addEventListener('keydown',e=>{if(e.key==='ArrowDown'){e.preventDefault();openModelMenu(k,true)}if(e.key==='Escape')closeModelMenu(k);if(e.key==='Enter'&&menu.classList.contains('open')){const first=menu.querySelector('.model-option');if(first){e.preventDefault();chooseModelOption(k,first)}}});
  toggle.onclick=e=>{e.stopPropagation();menu.classList.contains('open')?closeModelMenu(k):openModelMenu(k,true)};menu.onmousedown=e=>{const option=e.target.closest('.model-option');if(option){e.preventDefault();chooseModelOption(k,option)}};
});
document.addEventListener('mousedown',e=>{if(!e.target.closest('.model-combobox'))closeAllModelMenus()});
$('#endpoint-add').onclick=()=>showEndpointEditor();$('#endpoint-cancel').onclick=()=>{$('#endpoint-editor').style.display='none'};$('#endpoint-save').onclick=saveEndpoint;$('#endpoint-refresh-all').onclick=e=>refreshEndpoints('',e.currentTarget);
$('#endpoint-list').onclick=async e=>{const button=e.target.closest('button'),item=e.target.closest('.endpoint-item');if(!button||!item)return;const ep=ENDPOINTS.find(x=>x.id===item.dataset.id);if(button.dataset.action==='edit')showEndpointEditor(ep);if(button.dataset.action==='refresh')refreshEndpoints(ep.id,button);if(button.dataset.action==='delete'&&confirm(`删除 Endpoint「${ep.name}」？`)){try{await endpointAction('delete',{id:ep.id},button);await loadModels()}catch(err){alert(err.message)}}};
$('#model-test').onclick=testModels;$('#model-save').onclick=saveModels;$('#model-reload').onclick=loadModels;
