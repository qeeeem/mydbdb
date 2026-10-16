const test=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const vm=require('node:vm');
const source=fs.readFileSync(path.join(__dirname,'..','add.js'),'utf8');

function element(){
  let content='';
  return {value:'',style:{},disabled:false,children:[],textContent:'',listeners:{},options:new Map(),
    classList:{add(){},remove(){}},
    get innerHTML(){return content;},set innerHTML(value){content=value;this.children=[];this.textContent='';},
    appendChild(node){this.children.push(node);},focus(){},
    addEventListener(name,fn){this.listeners[name]=fn;},
    querySelector(name){if(!this.options.has(name))this.options.set(name,element());return this.options.get(name);},
  };
}
function panel({connected=true,mark=null,searchItems=null}={}){
  const nodes=new Map(),calls=[],state={connected,mark,error:null,searchItems:searchItems||[{uuid:'movie123',title:'查理 <script>alert(1)</script>',year:'2024',category:'movie',poster:null}]};
  let reloads=0;
  const document={
    getElementById(id){if(!nodes.has(id))nodes.set(id,element());return nodes.get(id);},
    createElement:element,addEventListener(){},
    querySelectorAll(selector){return selector.startsWith('#add-bg')?[...nodes.values()]:[];},
  };
  const context=vm.createContext({document,setTimeout,clearTimeout,loadData:async()=>{reloads++;return true;},
    fetch:async(url,options={})=>{
      calls.push({url,options});
      if(state.handle)return state.handle(url,options);
      let data;
      if(url==='/api/session')data={csrf:'csrf-test',connected:state.connected,username:'test-user'};
      else if(url==='/api/connect'){state.connected=true;data={ok:true,username:'test-user'};}
      else if(url.startsWith('/api/search'))data=state.searchItems;
      else if(url.startsWith('/api/mark'))data={mark:state.mark};
      else if(url==='/api/add')data=state.error?{ok:false,error:state.error}:{ok:true,synced:true};
      else throw Error('unexpected URL '+url);
      return {ok:!(url==='/api/add'&&state.error),status:state.errorStatus||200,json:async()=>data};
    },
  });
  vm.runInContext(source,context);
  const run=code=>vm.runInContext(code,context);
  return {nodes,calls,state,run,document,reloads:()=>reloads,
    async choose(){await run('openAddPanel()');await run("doSearch('查理')");await run('selectItem(0)');},
  };
}

test('existing add panel searches locally and renders titles as text',async()=>{
  const p=panel();await p.run('openAddPanel()');await p.run("doSearch('查理')");
  assert.ok(p.calls.some(c=>c.url.startsWith('/api/search?')));
  const button=p.nodes.get('add-results').children[0];
  assert.equal(button.children[0].children[0].textContent,'查理 <script>alert(1)</script>');
  assert.equal(button.children[0].children[0].innerHTML,'');
});

test('new mark defaults private, submission syncs once and refreshes the page',async()=>{
  const p=panel();await p.choose();
  assert.equal(p.nodes.get('add-visibility').value,'2');
  p.nodes.get('add-rating').value='8';p.nodes.get('add-comment').value='短评';
  await p.run('submitAdd()');await p.run('submitAdd()');
  const posts=p.calls.filter(c=>c.url==='/api/add');assert.equal(posts.length,1);
  assert.deepEqual(JSON.parse(posts[0].options.body),{uuid:'movie123',status:'complete',rating:8,comment:'短评',visibility:2});
  assert.equal(posts[0].options.headers['X-Mydbdb-CSRF'],'csrf-test');
  assert.equal(p.reloads(),1);assert.equal(p.nodes.get('add-msg').textContent,'已保存并同步 NeoDB');
});

test('existing mark prepopulates all editable fields, including public visibility',async()=>{
  const p=panel({mark:{shelf_type:'wishlist',rating_grade:6,comment_text:'已有短评',visibility:0}});await p.choose();
  assert.equal(p.nodes.get('add-status').value,'wishlist');
  assert.equal(p.nodes.get('add-rating').value,6);
  assert.equal(p.nodes.get('add-comment').value,'已有短评');
  assert.equal(p.nodes.get('add-visibility').value,'0');
  assert.equal(p.nodes.get('add-submit').textContent,'更新并同步 NeoDB');
});

test('failed sync keeps input and does not report success or refresh',async()=>{
  const p=panel();await p.choose();p.nodes.get('add-comment').value='不要丢失';p.state.error='授权已失效';
  await p.run('submitAdd()');
  assert.equal(p.nodes.get('add-comment').value,'不要丢失');
  assert.equal(p.nodes.get('add-msg').textContent,'授权已失效');
  assert.equal(p.nodes.get('add-submit').disabled,false);assert.equal(p.reloads(),0);
});

test('missing authorization blocks saving and can connect in the same panel',async()=>{
  const p=panel({connected:false});await p.choose();await p.run('submitAdd()');
  assert.equal(p.nodes.get('add-auth').style.display,'block');
  assert.equal(p.nodes.get('add-submit').disabled,true);
  assert.equal(p.calls.filter(c=>c.url==='/api/add').length,0);
  p.document.getElementById('add-token').value='test-token';await p.run('connectNeoDB()');
  assert.equal(p.nodes.get('add-token').value,'');
  assert.equal(p.nodes.get('add-auth').style.display,'none');
  assert.equal(p.nodes.get('add-submit').disabled,false);
});

test('invalid rating never submits',async()=>{
  const p=panel();await p.choose();p.nodes.get('add-rating').value='7.5';await p.run('submitAdd()');
  assert.equal(p.calls.filter(c=>c.url==='/api/add').length,0);
  assert.match(p.nodes.get('add-msg').textContent,/整数/);
});

test('cancelled admin verification does not claim the local server is missing',async()=>{
  const p=panel();
  p.state.handle=async()=>({ok:false,status:401,json:async()=>{throw new SyntaxError('HTML response');}});
  await p.run('openAddPanel()');
  assert.equal(p.nodes.get('add-msg').textContent,'添加条目需要管理员验证，请重新打开添加窗口');
  assert.equal(p.nodes.get('add-submit').disabled,true);
  assert.equal(p.calls.filter(c=>c.url==='/api/add').length,0);
});

test('expired authorization exposes reconnect and retains the unsaved draft',async()=>{
  const p=panel();await p.choose();p.nodes.get('add-comment').value='待保存短评';
  p.state.error='授权已失效';p.state.errorStatus=401;
  await p.run('submitAdd()');
  assert.equal(p.nodes.get('add-auth').style.display,'block');
  assert.equal(p.nodes.get('add-submit').disabled,true);
  p.state.error=null;p.state.errorStatus=200;
  p.document.getElementById('add-token').value='new-test-token';await p.run('connectNeoDB()');
  assert.equal(p.nodes.get('add-comment').value,'待保存短评');
  assert.equal(p.nodes.get('add-submit').disabled,false);
});

test('editing fields after success enables an intentional update',async()=>{
  const p=panel();await p.choose();await p.run('submitAdd()');
  assert.equal(p.nodes.get('add-submit').disabled,true);
  p.nodes.get('add-comment').value='新短评';p.nodes.get('add-form').listeners.input();
  assert.equal(p.nodes.get('add-submit').disabled,false);
});

test('slow stale search cannot replace newer search results',async()=>{
  const p=panel();let release;
  p.state.handle=async(url)=>{
    if(url.includes('q=old'))return new Promise(resolve=>{release=()=>resolve({ok:true,json:async()=>[{title:'old'}]});});
    return {ok:true,json:async()=>[{title:'new'}]};
  };
  const pending=p.run("doSearch('old')");await p.run("doSearch('new')");release();await pending;
  assert.equal(p.nodes.get('add-results')._items[0].title,'new');
});

test('pending submission ignores duplicate clicks and cannot close the panel',async()=>{
  const p=panel();await p.choose();let release;
  p.state.handle=async()=>new Promise(resolve=>{release=()=>resolve({ok:true,json:async()=>({ok:true,synced:true})});});
  const pending=p.run('submitAdd()');await p.run('submitAdd()');p.run('closeAddPanel()');
  assert.equal(p.calls.filter(c=>c.url==='/api/add').length,1);
  assert.equal(p.nodes.get('add-bg').style.display,'flex');
  release();await pending;
});
