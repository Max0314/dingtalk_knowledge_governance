const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const source=fs.readFileSync(require('node:path').join(__dirname,'../static/app.js'),'utf8');
const section=source.slice(source.indexOf('const regionRequests='),source.indexOf("const app=document"));
test('same-page old response cannot replace the newest filter',async()=>{
  const pending=[];
  const context=vm.createContext({AbortController,navAbort:new AbortController(),aborted:e=>e.name==='AbortError',toast:()=>{},
    api:()=>new Promise(resolve=>pending.push(resolve))});
  vm.runInContext(section,context);
  const old=vm.runInContext("regionFetch('tree','old')",context);
  const recent=vm.runInContext("regionFetch('tree','new')",context);
  pending[1]({period:'new'});pending[0]({period:'old'});
  assert.equal(await old,null);assert.equal((await recent).period,'new');
});
test('navigation invalidates a same-page region even if fetch ignores abort',async()=>{
  let finish;
  const context=vm.createContext({AbortController,navAbort:new AbortController(),aborted:e=>false,toast:()=>{},
    api:()=>new Promise(resolve=>finish=resolve)});
  vm.runInContext(section,context);
  const response=vm.runInContext("regionFetch('files','old')",context);
  vm.runInContext('navAbort.abort();navAbort=new AbortController()',context);
  finish({period:'old'});assert.equal(await response,null);
});
