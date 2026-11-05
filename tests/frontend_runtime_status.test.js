const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const source=fs.readFileSync(require('node:path').join(__dirname,'../static/app.js'),'utf8');
const helpers=source.slice(source.indexOf('function beijingTime'),source.indexOf('/* ---- charts ----'));

test('header and pause notice load independently of overview and can retry',async()=>{
  const pill={},notice={hidden:true},requests=[];let fail=true;
  const context=vm.createContext({AbortController,setTimeout,clearTimeout,Intl,Date,Number,
    state:{authReady:true},nf:String,renderLogin(){throw new Error('unexpected login')},
    document:{querySelector:s=>s==='#scopePill'?pill:notice},
    fetch:async url=>{requests.push(url);return {status:fail?503:200,ok:!fail,json:async()=>({registered_workspaces:150,
      audit_enabled:true,review_enabled:false,notify_enabled:false,scan:{enabled:false,total:149,succeeded:107,failed:42,remaining:42}})}}});
  vm.runInContext(helpers,context);
  await vm.runInContext('refreshRuntimeStatus()',context);
  assert.match(pill.textContent,/暂不可用/);
  fail=false;await pill.onclick();
  assert.equal(pill.textContent,'已登记 150 库');
  assert.match(pill.title,/成功 107.*未完成 42/);
  assert.match(notice.textContent,/AI 自动评审已暂停/);assert.match(notice.textContent,/通知已暂停/);
  assert.equal(notice.hidden,false);
  assert.deepEqual(requests,['api/v1/runtime/status','api/v1/runtime/status']);
});

test('historical errors show Beijing time and do not assert current permission failure',()=>{
  const context=vm.createContext({Intl,Date,Number});vm.runInContext(helpers,context);
  assert.match(vm.runInContext("beijingTime('2026-11-05T04:04:17')",context),/12:04:17/);
  assert.match(vm.runInContext("beijingTime('2026-11-05T04:04:17Z')",context),/12:04:17/);
  assert.match(vm.runInContext("beijingTime('2026-11-05T12:04:17+08:00')",context),/12:04:17/);
  assert.match(vm.runInContext("scanErrorText('dingtalk_permission_denied:403')",context),/不能据此判断当前权限/);
});
