const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm');
const source=fs.readFileSync(require('node:path').join(__dirname,'../static/app.js'),'utf8');
function helpers(){const context=vm.createContext({URL,location:{href:'https://example.test/knowledge_governance/'},state:{coverageNames:{}}});
  vm.runInContext(source.slice(source.indexOf('const nf='),source.indexOf('const COLOR=')),context);return context}
test('HTML text and quoted attributes escape metadata, preserving the original text',()=>{
  const context=helpers();context.payload='<img src=x onerror="alert(1)"> & \' /';
  const value=vm.runInContext('esc(payload)',context);
  assert.equal(value,'&lt;img src=x onerror=&quot;alert(1)&quot;&gt; &amp; &#39; /');
});
test('external document links reject active schemes and URL credentials',()=>{
  const context=helpers();
  for(const url of ['javascript:alert(1)','data:text/html,<script>alert(1)</script>','https://user:password@example.test']){
    context.url=url;assert.equal(vm.runInContext('safeLink(url)',context),'#')}
  context.url='https://example.test/document?a=1&b=2';
  assert.equal(vm.runInContext('safeLink(url)',context),'https://example.test/document?a=1&amp;b=2');
});
test('unified document table renders malicious metadata as text, not HTML or a live script link',()=>{
  const context=helpers();
  vm.runInContext(source.slice(source.indexOf('function fileTable('),source.indexOf('function renderFilesBox(')),context);
  context.data={items:[{node_id:'"><img src=x onerror=alert(1)>',name:'<svg onload=alert(1)>',
    uploader_name:'<script>alert(1)</script>',department_name:'"><iframe src=x>',verdict:'" onclick="alert(1)',
    url:'javascript:alert(1)',has_detail:true,created_at:'2026-10-10'}],total:1,offset:0,limit:50};
  const html=vm.runInContext('fileTable(data)',context);
  assert.ok(!/<(?:img|svg|iframe|script)\b/.test(html));assert.ok(!html.includes('href="javascript:'));
  assert.ok(html.includes('&lt;svg'));assert.ok(html.includes('href="#"'));
});
test('review details escape model-generated findings and document metadata',async()=>{
  const context=helpers();let output='';
  Object.assign(context,{navAbort:null,ensureCurrentNav:()=>{},api:async()=>({name:'<img src=x onerror=alert(1)>',node_id:'synthetic',workspace_id:'synthetic',
    reviews:[],latest_review:{ai_score:80,genre:'<svg onload=alert(1)>',tier:'A',verdict:'pass',dimensions:{
      synthetic:{label:'<script>bad</script>',deduction:1,cap:10,findings:[{message:'<iframe src=x>'}]},
      model:{genre:'<img src=x>',rule_score:90,model_score:80,findings:[{message:'<script>bad</script>'}],model_dimensions:{'<img src=x>':'<svg>'}}}}}),
    shell:(title,sub,body)=>{output=body},document:{querySelector:()=>({})}});
  vm.runInContext(source.slice(source.indexOf('async function documentDetail('),source.indexOf('async function workspaces(')),context);
  await vm.runInContext("documentDetail('synthetic')",context);
  assert.ok(!/<(?:img|svg|iframe|script)\b/.test(output));assert.ok(output.includes('&lt;iframe'));
});
