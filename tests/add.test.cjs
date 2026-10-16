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
function dataPanel(){
  const nodes=new Map(),calls=[];let reloads=0;
  const document={getElementById(id){if(!nodes.has(id))nodes.set(id,element());return nodes.get(id);},addEventListener(){}};
  const p={nodes,calls,document,reloads:()=>reloads,state:{}};
  const context=vm.createContext({document,setTimeout,clearTimeout,Uint8Array,crypto:require('node:crypto').webcrypto,
    fflate:require('../vendor/fflate.js'),btoa:s=>Buffer.from(s,'binary').toString('base64'),
    loadData:async()=>{reloads++;return true;},fetch:async(url,options={})=>{calls.push({url,options});return p.state.handle(url,options);}});
  vm.runInContext(source,context);p.run=code=>vm.runInContext(code,context);
  p.info={csrf:'csrf-test',count:12,job:null};
  p.state.handle=async(url,options)=>{
    let data;
    if(url==='/api/data')data=p.info;
    else if(p.handler)data=await p.handler(url,options);
    else throw Error('unexpected URL '+url);
    return {ok:true,status:200,json:async()=>data};
  };
  return p;
}

test('data management loads without NeoDB and no ZIP means no writes',async()=>{
  const p=dataPanel();await p.run('openDataPanel()');await p.run('importDataZip()');
  assert.equal(p.calls.length,1);assert.equal(p.calls[0].url,'/api/data');
  assert.equal(p.nodes.get('data-import').disabled,false);
  assert.match(p.nodes.get('data-msg').textContent,/当前数据保持不变/);
  p.run('closeDataPanel()');
});

test('upload controls remain hidden until administrator validation succeeds',async()=>{
  const p=dataPanel();let release;
  p.state.handle=()=>new Promise(resolve=>{release=resolve;});
  const opening=p.run('openDataPanel()');
  assert.equal(p.nodes.get('data-import-area').hidden,true);
  assert.equal(p.nodes.get('data-choose').disabled,true);
  release({ok:false,status:401,json:async()=>{throw Error('authentication required');}});
  await opening;
  assert.equal(p.nodes.get('data-import-area').hidden,true);
  assert.equal(p.nodes.get('data-choose').disabled,true);
  assert.equal(p.nodes.get('data-msg').textContent,'已拒绝');
  p.run('closeDataPanel()');
});

test('selected archive name is not displayed because it may contain account identity',async()=>{
  const p=dataPanel();await p.run('openDataPanel()');
  p.nodes.get('data-file').listeners.change({target:{files:[{name:'private-account-export.zip'}]}});
  assert.equal(p.nodes.get('data-filename').textContent,'已选择 ZIP');
  p.run('closeDataPanel()');
});

test('browser extracts only catalog and journal, never account keys or attachments',()=>{
  const p=dataPanel(),zip=require('../vendor/fflate.js');
  const bytes=zip.zipSync({'catalog.ndjson':Buffer.from('catalog'),'journal.ndjson':Buffer.from('journal'),
    'actor.ndjson':Buffer.from('PRIVATE KEY MUST NOT LEAVE BROWSER'),'attachments/a':Buffer.from('image')});
  p.run(`globalThis.input=Uint8Array.from(${JSON.stringify(Array.from(bytes))})`);
  const clean=p.run('collectionZip(input,fflate)');
  assert.deepEqual(Object.keys(zip.unzipSync(clean)).sort(),['catalog.ndjson','journal.ndjson']);
});

test('unchanged original ZIP hash skips upload and never refreshes or commits',async()=>{
  const p=dataPanel();await p.run('openDataPanel()');
  p.nodes.get('data-file').files=[{size:3,arrayBuffer:async()=>Uint8Array.from([1,2,3]).buffer}];
  p.handler=async(url,options)=>{
    assert.equal(url,'/api/import/start');
    assert.equal(JSON.parse(options.body).sha256,require('node:crypto').createHash('sha256').update(Buffer.from([1,2,3])).digest('hex'));
    return {unchanged:true,message:'ZIP 哈希未变化，当前数据保持不变'};
  };
  await p.run('importDataZip()');
  assert.equal(p.calls.length,2);assert.equal(p.reloads(),0);assert.equal(p.nodes.get('data-import').disabled,false);
  p.run('closeDataPanel()');
});

test('ZIP upload stays below request limit and waits for explicit preview confirmation',async()=>{
  const p=dataPanel(),zip=require('../vendor/fflate.js');await p.run('openDataPanel()');
  const bytes=zip.zipSync({'catalog.ndjson':require('node:crypto').randomBytes(90000),'journal.ndjson':Buffer.from('journal'),'actor.ndjson':Buffer.from('secret')});
  p.nodes.get('data-file').files=[{size:bytes.length,arrayBuffer:async()=>bytes.buffer.slice(bytes.byteOffset,bytes.byteOffset+bytes.byteLength)}];
  p.run('zipLibrary=Promise.resolve(fflate)');const chunks=[];
  p.handler=async(url,options)=>{
    const body=JSON.parse(options.body);assert.ok(Buffer.byteLength(options.body)<65536);
    if(url==='/api/import/start')return {id:'upload'};
    if(url==='/api/import/chunk'){assert.equal(body.offset,chunks.reduce((n,b)=>n+b.length,0));chunks.push(Buffer.from(body.data,'base64'));return {};}
    if(url==='/api/import/finish'){p.info.job={id:'upload',state:'ready',preview:{total:1,added:1,removed:12,updated:0},message:'请核对'};return {};}
    throw Error('unexpected '+url);
  };
  await p.run('importDataZip()');
  assert.ok(chunks.length>1);assert.deepEqual(Object.keys(zip.unzipSync(Buffer.concat(chunks))).sort(),['catalog.ndjson','journal.ndjson']);
  assert.equal(p.nodes.get('data-preview').hidden,false);assert.match(p.nodes.get('data-preview-count').textContent,/移除 12/);
  assert.equal(p.reloads(),0);assert.equal(p.calls.filter(c=>c.url==='/api/data/commit').length,0);
  p.run('closeDataPanel()');
});

test('confirm publishes once and refreshes shared dashboard data',async()=>{
  const p=dataPanel();p.info.job={id:'ready',state:'ready',preview:{total:3,added:0,updated:1,removed:9},message:'请核对'};
  await p.run('openDataPanel()');
  p.handler=async(url,options)=>{
    assert.equal(url,'/api/data/commit');assert.equal(JSON.parse(options.body).id,'ready');
    p.info.job={id:'ready',state:'done',message:'已更新 3 条收藏'};
    return {ok:true,message:'已更新 3 条收藏'};
  };
  await p.run('commitData()');await p.run('commitData()');
  assert.equal(p.reloads(),1);assert.equal(p.calls.filter(c=>c.url==='/api/data/commit').length,1);
  assert.equal(p.nodes.get('data-preview').hidden,true);p.run('closeDataPanel()');
});
