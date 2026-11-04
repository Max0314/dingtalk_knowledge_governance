const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const source=fs.readFileSync(require('node:path').join(__dirname,'../static/app.js'),'utf8');

test('navigation cancellation during JSON body reading remains AbortError',async()=>{
  const parent=new AbortController();let bodyStarted;
  const started=new Promise(resolve=>bodyStarted=resolve);
  const context=vm.createContext({AbortController,setTimeout,clearTimeout,navAbort:parent,
    aborted:e=>e.name==='AbortError',renderLogin:()=>{},fetch:async(url,options)=>({status:200,ok:true,
      json:()=>new Promise((resolve,reject)=>{bodyStarted();options.signal.addEventListener('abort',()=>{
        const e=new Error('synthetic cancellation');e.name='AbortError';reject(e)
      })})})});
  vm.runInContext(source.slice(source.indexOf('const api='),source.indexOf('// Each region')),context);
  const result=vm.runInContext("api('/api/v1/synthetic')",context);
  await started;parent.abort();await assert.rejects(result,e=>e.name==='AbortError');
});

test('downloaded chart script without an engine rejects and can be retried',async()=>{
  const scripts=[];
  const context=vm.createContext({setTimeout,clearTimeout,window:{},document:{createElement:()=>({remove(){}}),head:{append:s=>scripts.push(s)}}});
  vm.runInContext(source.slice(source.indexOf('let chartScript='),source.indexOf('const charts=new Map')),context);
  const first=vm.runInContext('ensureCharts()',context);scripts[0].onload();
  await assert.rejects(first,/初始化失败/);
  const next=vm.runInContext('ensureCharts()',context);assert.equal(scripts.length,2);
  context.window.echarts={init(){}};scripts[1].onload();await next;
});

test('failed chart instance creation cannot loop forever while binding drill handlers',async()=>{
  let loads=0;
  const context=vm.createContext({charts:new Map(),document:{getElementById:()=>({isConnected:true})},ensureCharts:()=>{loads++;return Promise.resolve()}});
  vm.runInContext(source.slice(source.indexOf('function bindChartDrill'),source.indexOf('/* ---- scaffolding')),context);
  vm.runInContext("bindChartDrill('synthetic',{keys:[],labels:[]})",context);
  await new Promise(resolve=>setImmediate(resolve));assert.equal(loads,1);
});

test('parallel diagnostic probes retain both results and independent busy buttons',async()=>{
  const pending={},buttons={};
  for(const target of ['dingtalk','bi_center']){
    const fields={};const row={querySelector:k=>fields[k]||(fields[k]={textContent:'',className:''})};
    buttons[target]={dataset:{probe:target},disabled:false,closest:()=>row,row}
  }
  const boxes=new Map(['connectivity','usage','sync','notifications','performance'].map(name=>['diagnostic-'+name,{
    innerHTML:'',querySelectorAll:()=>name==='connectivity'?Object.values(buttons):[]} ]));
  const payloads={connectivity:{items:[{target:'dingtalk',status:'configured'},{target:'bi_center',status:'configured'}]},
    'api-usage':{limits:{},switches:{},items:[]},'sync-runs':{items:[]},notifications:{items:[]},performance:{items:[],refreshes:[]}};
  const context=vm.createContext({navAbort:{},shell(){},esc:String,nf:String,aborted:()=>false,ensureCurrentNav(){},toast(){},
    document:{getElementById:id=>boxes.get(id)},api:async(url,options)=>{
      if(options?.method==='POST')return new Promise(resolve=>pending[url.split('/').pop()]=resolve);
      return payloads[url.split('/').pop().split('?')[0]]
    }});
  vm.runInContext(source.slice(source.indexOf('function diagnosticItems'),source.indexOf('/* ---- 评分规则配置')),context);
  await vm.runInContext('diagnostics()',context);
  const a=buttons.dingtalk.onclick(),b=buttons.bi_center.onclick();
  pending.dingtalk({status:'healthy',message:'first result'});await a;
  assert.equal(buttons.dingtalk.row.querySelector('.message').textContent,'first result');
  assert.equal(buttons.bi_center.disabled,true);
  pending.bi_center({status:'failed',message:'second result'});await b;
  assert.equal(buttons.bi_center.row.querySelector('.message').textContent,'second result');
});
