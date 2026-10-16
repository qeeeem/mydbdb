async function dataRequest(path,payload){
  const options=payload===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json','X-Mydbdb-CSRF':dataState?.csrf||''},body:JSON.stringify(payload)};
  let response;
  try{response=await fetch(path,options);}
  catch{throw new Error('无法连接网站服务，请检查网络后重试');}
  let result;
  try{result=await response.json();}
  catch{throw new Error(response.status===401||response.status===403?'已拒绝':'服务返回异常，请稍后重试');}
  if(!response.ok)throw new Error(response.status===401||response.status===403?'已拒绝':result.error||'请求失败');
  return result;
}

// Collection import. The reverse proxy authenticates every /api/ request.
let dataState=null, dataBusy=false, dataOpen=false, dataPoll=null, zipLibrary=null;
function dataMessage(text,error=false){
  const el=document.getElementById('data-msg');el.textContent=text;el.style.color=error?'#e88a67':'var(--text2)';
}
function dataControls(){
  const job=dataState?.job, active=['uploading','running','ready'].includes(job?.state);
  ['data-import','data-file','data-choose'].forEach(id=>{document.getElementById(id).disabled=dataBusy||active||!dataState;});
  document.getElementById('data-commit').disabled=dataBusy||job?.state!=='ready';
  document.getElementById('data-cancel').hidden=!active;
  document.getElementById('data-cancel').disabled=dataBusy;
}
function renderDataState(state){
  dataState=state;document.getElementById('data-import-area').hidden=false;
  document.getElementById('data-count').textContent=`本站收藏：${state.count} 条`;
  const job=state.job;
  document.getElementById('data-preview').hidden=job?.state!=='ready';
  if(job?.state==='ready'){
    const p=job.preview;
    document.getElementById('data-preview-count').textContent=`更新为 ${p.total} 条：新增 ${p.added}，修改 ${p.updated}，移除 ${p.removed}`;
  }
  if(job)dataMessage(job.message,job.state==='error');
  dataControls();
}
async function refreshDataStatus(){
  clearTimeout(dataPoll);
  const state=await dataRequest('/api/data');
  renderDataState(state);
  if(dataOpen&&['running','uploading'].includes(state.job?.state))dataPoll=setTimeout(()=>refreshDataStatus().catch(e=>dataMessage(e.message,true)),1200);
  return state;
}
async function openDataPanel(){
  dataOpen=true;dataState=null;document.getElementById('data-import-area').hidden=true;
  document.getElementById('data-preview').hidden=true;document.getElementById('data-bg').style.display='flex';dataMessage('正在读取数据…');
  dataBusy=true;dataControls();
  try{const state=await refreshDataStatus();if(!state.job)dataMessage('');}
  catch(e){dataMessage(e.message,true);}
  finally{dataBusy=false;dataControls();}
}
function closeDataPanel(){
  if(dataBusy)return;
  dataOpen=false;clearTimeout(dataPoll);document.getElementById('data-bg').style.display='none';
}
function loadZipLibrary(){
  if(!zipLibrary)zipLibrary=new Promise((resolve,reject)=>{
    const script=document.createElement('script');script.src='/api/import/library.js';
    script.onload=()=>resolve(globalThis.fflate);
    script.onerror=()=>{zipLibrary=null;reject(new Error('ZIP 组件加载失败，请重新打开数据管理'));};
    document.head.appendChild(script);
  });
  return zipLibrary;
}
function collectionZip(bytes,zip){
  const names=new Set(),wanted=new Set(['catalog.ndjson','journal.ndjson']);let expanded=0;
  const files=zip.unzipSync(bytes,{filter:entry=>{
    if(!wanted.has(entry.name))return false;
    if(names.has(entry.name))throw new Error('ZIP 中的收藏文件重复');
    names.add(entry.name);expanded+=entry.originalSize;
    if(!Number.isSafeInteger(expanded)||expanded>96*1024*1024)throw new Error('解压后的收藏数据超过 96 MiB');
    return true;
  }});
  if(names.size!==2)throw new Error('请选择包含 catalog.ndjson 和 journal.ndjson 的 NeoDB 导出包');
  // Never decompress or transmit actor.ndjson, attachments or account keys.
  return zip.zipSync(files,{level:1});
}
async function importDataZip(){
  if(dataBusy)return;
  const files=document.getElementById('data-file').files;
  if(!files?.length){dataMessage('未选择 ZIP，当前数据保持不变');return;}
  if(files.length!==1){dataMessage('请只选择一个 ZIP',true);return;}
  if(files[0].size>64*1024*1024){dataMessage('ZIP 超过 64 MiB',true);return;}
  dataBusy=true;dataControls();dataMessage('正在读取 ZIP…');
  let id=null;
  try{
    if(!dataState)await refreshDataStatus();
    const bytes=new Uint8Array(await files[0].arrayBuffer());
    const hash=Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',bytes)),v=>v.toString(16).padStart(2,'0')).join('');
    const result=await dataRequest('/api/import/start',{sha256:hash});
    if(result.unchanged){dataMessage(result.message);return;}
    id=result.id;
    const packed=collectionZip(bytes,await loadZipLibrary());
    for(let offset=0;offset<packed.length;offset+=32768){
      const chunk=packed.subarray(offset,offset+32768);
      await dataRequest('/api/import/chunk',{id,offset,data:btoa(String.fromCharCode(...chunk))});
      dataMessage(`正在上传收藏数据：${Math.min(100,Math.round((offset+chunk.length)/packed.length*100))}%`);
    }
    await dataRequest('/api/import/finish',{id});await refreshDataStatus();
  }catch(e){
    if(id){try{await dataRequest('/api/data/cancel',{id});}catch{ /* Reopen to cancel a surviving upload. */ }}
    dataMessage(e.message,true);
  }finally{dataBusy=false;dataControls();}
}
async function cancelData(){
  if(dataBusy||!dataState?.job)return;
  dataBusy=true;dataControls();clearTimeout(dataPoll);
  try{await dataRequest('/api/data/cancel',{id:dataState.job.id});await refreshDataStatus();dataMessage('已取消，当前数据保持不变');}
  catch(e){dataMessage(e.message,true);}
  finally{dataBusy=false;dataControls();}
}
async function commitData(){
  if(dataBusy||dataState?.job?.state!=='ready')return;
  dataBusy=true;dataControls();let saved=false;
  try{
    const result=await dataRequest('/api/data/commit',{id:dataState.job.id});saved=true;
    await refreshDataStatus();
    if(await loadData()===false)throw new Error('页面刷新失败');
    document.getElementById('data-file').value='';document.getElementById('data-filename').textContent='未选择文件';dataMessage(result.message);
  }catch(e){dataMessage(saved?'收藏已保存，请刷新页面查看':e.message,true);}
  finally{dataBusy=false;dataControls();}
}
document.getElementById('data-bg').addEventListener('click',e=>{if(e.target===document.getElementById('data-bg'))closeDataPanel();});
document.addEventListener('keydown',e=>{if(e.key==='Escape'&&dataOpen)closeDataPanel();});
document.getElementById('data-file').addEventListener('change',e=>{
  document.getElementById('data-filename').textContent=e.target.files[0]?'已选择 ZIP':'未选择文件';
});
