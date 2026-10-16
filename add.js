// Existing add-entry panel; all requests use the local page's origin.
let addCat='movie', addSelected=null, addSearchTimer=null;
let addSession=null, addBusy=false, addReady=false, addSaved=false;
let addSearchVersion=0, addSelectionVersion=0;

function addMessage(text, error=false){
  const msg=document.getElementById('add-msg');
  msg.textContent=text;
  msg.style.color=error?'#e88a67':'var(--text2)';
}

async function addRequest(path, payload){
  const options=payload===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json','X-Mydbdb-CSRF':addSession?.csrf||''},body:JSON.stringify(payload)};
  let response;
  try { response=await fetch(path, options); }
  catch(e){ throw new Error('无法连接本地服务，请运行 python server.py 后打开服务地址'); }
  let data;
  try { data=await response.json(); }
  catch(e){ throw new Error('当前页面未连接添加服务，请用 python server.py 启动后打开服务地址'); }
  if(!response.ok){
    if(response.status===401){
      if(addSession)addSession.connected=false;
      document.getElementById('add-auth').style.display='block';
      document.getElementById('add-account').textContent='';
      updateAddControls();
    }
    throw new Error(data.error||'请求失败，请重试');
  }
  return data;
}

function updateAddControls(){
  document.querySelectorAll('#add-bg input, #add-bg textarea, #add-bg select, #add-bg button').forEach(el=>{el.disabled=addBusy;});
  document.getElementById('add-submit').disabled=addBusy||!addSession?.connected||!addReady||addSaved;
}

async function openAddPanel(){
  document.getElementById('add-bg').style.display='flex';
  document.getElementById('add-search').focus();
  updateAddControls();
  try{
    addSession=await addRequest('/api/session');
    document.getElementById('add-auth').style.display=addSession.connected?'none':'block';
    document.getElementById('add-account').textContent=addSession.connected?`已连接 ${addSession.username}`:'';
    if(addSession.error) addMessage(addSession.error,true);
  }catch(e){ addSession=null; addMessage(e.message,true); }
  updateAddControls();
}

async function connectNeoDB(){
  if(addBusy)return;
  const input=document.getElementById('add-token');
  const token=input.value.trim();
  if(!token){addMessage('请粘贴 NeoDB 访问令牌',true);return;}
  addBusy=true; updateAddControls(); addMessage('正在连接 NeoDB…');
  try{
    if(!addSession) addSession=await addRequest('/api/session');
    const result=await addRequest('/api/connect',{token});
    addSession.connected=true; addSession.username=result.username;
    input.value='';
    document.getElementById('add-auth').style.display='none';
    document.getElementById('add-account').textContent=`已连接 ${result.username}`;
    addMessage('已连接 NeoDB');
    if(addSelected&&!addReady) await loadSelectedMark();
  }catch(e){addMessage(e.message,true);}
  finally{addBusy=false;updateAddControls();}
}

function closeAddPanel(){
  if(addBusy)return;
  document.getElementById('add-bg').style.display='none';
  document.getElementById('add-token').value='';
  resetAddPanel();
}

function resetAddPanel(){
  clearTimeout(addSearchTimer); addSearchVersion++; addSelectionVersion++;
  document.getElementById('add-search').value='';
  const results=document.getElementById('add-results');
  results.style.display='none'; results.innerHTML=''; results._items=[];
  document.getElementById('add-selected').style.display='none';
  document.getElementById('add-form').style.display='none';
  document.getElementById('add-rating').value='';
  document.getElementById('add-comment').value='';
  document.getElementById('add-visibility').value='2';
  addSelected=null;addReady=false;addSaved=false;addMessage('');
  updateAddControls();
}

document.querySelectorAll('.add-cat-btn').forEach(btn=>{
  btn.onclick=()=>{
    if(addBusy)return;
    document.querySelectorAll('.add-cat-btn').forEach(b=>b.classList.remove('active'));
    btn.classList.add('active'); addCat=btn.dataset.c; resetAddPanel();
  };
});

document.getElementById('add-search').addEventListener('input',e=>{
  clearTimeout(addSearchTimer);
  const version=++addSearchVersion, q=e.target.value.trim();
  const results=document.getElementById('add-results');
  results._items=[]; results.innerHTML='';
  if(!q){results.style.display='none';return;}
  addSearchTimer=setTimeout(()=>doSearch(q,version),400);
});

async function doSearch(q,version=++addSearchVersion){
  const res=document.getElementById('add-results'), category=addCat;
  res.textContent='搜索中…'; res.style.display='flex'; res._items=[];
  try{
    const items=await addRequest(`/api/search?q=${encodeURIComponent(q)}&cat=${category}`);
    if(version!==addSearchVersion||category!==addCat)return;
    res.innerHTML=''; res._items=items;
    if(!items.length){res.textContent='没有找到条目';return;}
    items.forEach((item,i)=>{
      const button=document.createElement('button');
      button.type='button'; button.className='add-result-item'; button.onclick=()=>selectItem(i);
      if(item.poster){
        const img=document.createElement('img'); img.src=item.poster; img.alt=''; button.appendChild(img);
      }
      const info=document.createElement('div');
      const title=document.createElement('div'); title.textContent=item.title; info.appendChild(title);
      if(item.year){const year=document.createElement('div');year.className='add-result-year';year.textContent=item.year;info.appendChild(year);}
      button.appendChild(info);res.appendChild(button);
    });
  }catch(e){if(version===addSearchVersion)res.textContent=e.message;}
}

async function selectItem(i){
  if(addBusy)return;
  const item=document.getElementById('add-results')._items[i];
  if(!item)return;
  clearTimeout(addSearchTimer); addSearchVersion++;
  addSelected=item; addReady=false; addSaved=false;
  document.getElementById('add-results').style.display='none';
  document.getElementById('add-search').value='';
  document.getElementById('add-sel-img').src=item.poster||'';
  document.getElementById('add-sel-img').style.display=item.poster?'block':'none';
  document.getElementById('add-sel-title').textContent=item.title;
  document.getElementById('add-sel-year').textContent=item.year||'';
  document.getElementById('add-selected').style.display='flex';
  document.getElementById('add-form').style.display='none';
  if(!addSession?.connected){addMessage('请先连接 NeoDB，再确认添加',true);updateAddControls();return;}
  await loadSelectedMark();
}

async function loadSelectedMark(){
  const item=addSelected, version=++addSelectionVersion;
  addReady=false; updateAddControls(); addMessage('正在读取 NeoDB 收藏状态…');
  try{
    const result=await addRequest(`/api/mark?uuid=${encodeURIComponent(item.uuid)}`);
    if(version!==addSelectionVersion||item!==addSelected)return;
    if(result.item){
      Object.assign(item,result.item);
      document.getElementById('add-sel-title').textContent=item.title;
      document.getElementById('add-sel-year').textContent=item.year||'';
    }
    const mark=result.mark;
    const verbs={book:['读过','想读','在读'],game:['玩过','想玩','在玩'],music:['听过','想听','在听'],podcast:['听过','想听','在听']}[item.category]||['看过','想看','在看'];
    const status=document.getElementById('add-status');
    ['complete','wishlist','progress'].forEach((value,i)=>{status.querySelector(`option[value="${value}"]`).textContent=verbs[i];});
    status.value=mark?.shelf_type||'complete';
    document.getElementById('add-rating').value=mark?.rating_grade??'';
    document.getElementById('add-comment').value=mark?.comment_text||'';
    document.getElementById('add-visibility').value=String(mark?.visibility??2);
    document.getElementById('add-submit').textContent=mark?'更新并同步 NeoDB':'添加并同步 NeoDB';
    document.getElementById('add-form').style.display='block';
    addReady=true; addMessage('');
  }catch(e){if(version===addSelectionVersion)addMessage(e.message,true);}
  updateAddControls();
}

async function submitAdd(){
  if(addBusy||!addSelected||!addReady||!addSession?.connected||addSaved)return;
  const value=document.getElementById('add-rating').value;
  const rating=value===''?null:Number(value);
  if(rating!==null&&(!Number.isInteger(rating)||rating<1||rating>10)){addMessage('评分请输入 1–10 的整数，或留空',true);return;}
  const payload={uuid:addSelected.uuid,status:document.getElementById('add-status').value,rating,
    comment:document.getElementById('add-comment').value,visibility:Number(document.getElementById('add-visibility').value)};
  addBusy=true;updateAddControls();addMessage('正在同步 NeoDB…');
  let synced=false;
  try{
    const result=await addRequest('/api/add',payload);
    if(!result.ok||!result.synced)throw new Error(result.error||'未确认同步成功，请重试');
    synced=true; addSaved=true;
    const loaded=await loadData();
    if(loaded===false)throw new Error('页面数据读取失败');
    addMessage('已保存并同步 NeoDB');
  }catch(e){addMessage(synced?'已同步 NeoDB 并保存本地，请刷新页面查看':e.message,true);}
  finally{addBusy=false;updateAddControls();}
}

document.addEventListener('keydown',e=>{if(e.key==='Escape')closeAddPanel();});
document.getElementById('add-bg').addEventListener('click',e=>{if(e.target===document.getElementById('add-bg'))closeAddPanel();});
document.getElementById('add-form').addEventListener('input',()=>{
  if(!addBusy){addSaved=false;updateAddControls();}
});
