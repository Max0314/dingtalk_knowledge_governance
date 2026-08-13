/* Relative URLs keep the app working both at / (tunnel/dev) and behind the
   /knowledge_governance/ prefix, where nginx strips the prefix. */
const api=(url,options={})=>fetch(url.replace(/^\//,''),{headers:{'Content-Type':'application/json'},...options}).then(async r=>{const data=await r.json().catch(()=>({}));
  if(r.status===401){renderLogin();throw new Error(data.detail?.message||'请先登录')}
  if(!r.ok)throw new Error(data.detail?.message||data.detail||'请求失败');return data});
const app=document.querySelector('#app');
const state={view:'overview',coverageNames:{},depth:0};
const nf=v=>Number(v||0).toLocaleString('zh-CN');
const fmt=v=>v||'—';
const scoreClass=s=>s>=80?'good':s>=60?'warn':'bad';
const verdictText=v=>({pass:'通过',manual_review:'待人工审核',return:'退回'})[v]||v;
const COLOR={routine:'#2563eb',bulk:'#f59e0b'};
function toast(msg){const x=document.querySelector('#toast');x.textContent=msg;x.classList.add('show');setTimeout(()=>x.classList.remove('show'),2800)}

/* ---- charts ---- */
const charts=new Map();
window.addEventListener('resize',()=>charts.forEach(c=>c.resize()));
function disposeCharts(){charts.forEach(c=>c.dispose());charts.clear()}
function renderChart(id,option){const el=document.getElementById(id);if(!el)return;if(!window.echarts){el.outerHTML='<div class="chart-fallback">图表组件未加载（/vendor/echarts.min.js）。数据见下方表格。</div>';return}
  const old=charts.get(id);if(old)old.dispose();
  const c=echarts.init(el);charts.set(id,c);c.setOption(option);requestAnimationFrame(()=>{if(!c.isDisposed())c.resize()})}
function stackedOption(rows,{zoom=false}={}){return{
  tooltip:{trigger:'axis',axisPointer:{type:'shadow'},formatter:ps=>{const m=ps[0].axisValue;const r=ps.find(p=>p.seriesName==='日常新增')?.value||0;const b=ps.find(p=>p.seriesName==='批量导入')?.value||0;return `<b>${m}</b><br/>全量新增：<b>${nf(r+b)}</b><br/>日常新增：${nf(r)}<br/>批量导入：${nf(b)}`}},
  legend:{data:['日常新增','批量导入'],top:0,itemWidth:14,itemHeight:9,textStyle:{fontSize:12,color:'#6b7280'}},
  grid:{left:8,right:8,top:34,bottom:zoom?46:8,containLabel:true},
  xAxis:{type:'category',triggerEvent:true,data:rows.map(r=>r.month),axisTick:{show:false},axisLine:{lineStyle:{color:'#e5e7eb'}},axisLabel:{color:'#6b7280',fontSize:11}},
  yAxis:{type:'value',splitLine:{lineStyle:{color:'#f3f4f6'}},axisLabel:{color:'#6b7280',fontSize:11}},
  dataZoom:zoom?[{type:'slider',height:18,bottom:6,brushSelect:false}]:undefined,
  series:[
    {name:'日常新增',type:'bar',stack:'total',barMaxWidth:28,data:rows.map(r=>r.routine),itemStyle:{color:COLOR.routine}},
    {name:'批量导入',type:'bar',stack:'total',barMaxWidth:28,data:rows.map(r=>r.bulk_import),itemStyle:{color:COLOR.bulk,borderRadius:[3,3,0,0]}},
  ]}}
/* 图表下钻交互（参考 ai_code_review_web）：点击柱体下钻；点击 x 轴标签区下钻
   对应类目；点击图表空白处回退一级。 */
function bindChartDrill(id,{keys,labels,onDrill,onBack}){const c=charts.get(id);if(!c)return;
  c.on('click',p=>{if(!onDrill)return;
    if(p.componentType==='xAxis'){const i=labels.indexOf(String(p.value??p.name??''));if(i>=0&&keys[i]!=null)onDrill(keys[i]);return}
    if(p.componentType==='series'&&keys[p.dataIndex]!=null)onDrill(keys[p.dataIndex])});
  const zr=c.getZr&&c.getZr();if(!zr||!zr.on)return;
  zr.on('click',p=>{if(p.target)return;
    let rect=null;try{rect=c.getModel().getComponent('grid',0)?.coordinateSystem?.getRect?.()||null}catch(e){rect=null}
    if(rect&&onDrill){const y=p.offsetY;
      if(y>=rect.y+rect.height&&y<=rect.y+rect.height+42){
        let v=null;try{v=c.convertFromPixel({xAxisIndex:0},[p.offsetX,y])}catch(e){v=null}
        const i=Math.round(Array.isArray(v)?v[0]:v);
        if(Number.isFinite(i)&&i>=0&&i<keys.length){onDrill(keys[i]);return}}}
    if(onBack)onBack()})}

/* ---- scaffolding ---- */
function shell(title,sub,content,actions=''){disposeCharts();app.innerHTML=`<section class="page-head"><div><h1>${title}</h1><p class="sub">${sub}</p></div><div class="page-actions">${actions}</div></section>${content}`}
function statCard(label,value,note){return `<article class="card"><div class="metric-label">${label}</div><div class="metric-value">${value}</div><div class="metric-note">${note||''}</div></article>`}
function statusChip(item){if(item.status==='scanned')return '<span class="chip green">已扫描</span>';if(item.status==='empty')return '<span class="chip gray">空库</span>';if(item.status==='excluded')return `<span class="chip amber" title="${item.excluded_reason}">已排除</span>`;return '<span class="chip">未知</span>'}
function bindDocRows(){document.querySelectorAll('[data-doc]').forEach(x=>x.onclick=()=>goDoc(x.dataset.doc))}

/* ---- views ---- */
async function overview(){const d=await api('/api/v1/dashboard/overview');
  const org=d.org_context||{};
  document.querySelector('#scopePill').textContent=`已登记 ${d.metrics.workspace_count} 库`;
  shell('知识库总览','公司知识库全景：规模、增量与文档质量。月份按钉钉 createTime（Asia/Shanghai）归属。',`
  ${org.note?`<div class="banner">${org.note}</div>`:''}
  <div class="grid metrics">
    <div id="go-ws-card" style="cursor:pointer" title="点击进入知识库管理">${statCard('知识库数量',d.metrics.workspace_count,'服务身份已加入并登记 · 点击查看清单')}</div>
    ${statCard('文件总量（去重）',nf(d.metrics.total_files),'主基线 + 实时增量')}
    ${statCard('本月文件增量',nf(d.metrics.month_increment),'按 createTime 归属当月')}
    ${statCard('本月文档平均分',d.metrics.month_average_score??'—',d.metrics.month_average_score==null?'本月暂无评审':`历史均值 ${d.metrics.average_ai_score??'—'}`)}
  </div>
  <section class="card section-gap"><div class="card-head"><h2 id="trendTitle">月度新增趋势（近 14 个月）</h2><span class="hint" id="trendHint">点击柱状图下钻到当月每日 · 点击空白处返回</span></div><div id="trendChart" class="chart"></div><div class="legend-note"><span><span class="dot" style="background:${COLOR.routine}"></span>日常新增</span><span><span class="dot" style="background:${COLOR.bulk}"></span>批量导入（系统迁移/同步，同样计入总量）</span><button class="secondary" id="go-increments" style="margin-left:auto">查看完整报表</button></div></section>
  <section class="card section-gap"><div class="card-head"><h2>各年增量总数</h2><span class="hint">点击年份进入数据看板查看月度明细</span></div>
    <div class="table-wrap"><table class="data-table"><thead><tr><th>年份</th><th class="num">全量新增</th><th class="num">日常新增</th><th class="num">批量导入</th></tr></thead><tbody>
    ${Object.keys(d.yearly||{}).sort().reverse().map(y=>`<tr class="rowlink" data-goyear="${y}"><td><b>${y} 年</b> <small style="color:#9ca3af">›</small></td><td class="num"><b>${nf(d.yearly[y].total)}</b></td><td class="num">${nf(d.yearly[y].routine)}</td><td class="num">${d.yearly[y].bulk_import?nf(d.yearly[y].bulk_import):'—'}</td></tr>`).join('')}
    </tbody></table></div></section>`);
  state.ovMonthly=d.monthly;state.ovDay='';
  renderOverviewTrend();
  document.querySelector('#go-ws-card').onclick=()=>navigate('workspaces');
  document.querySelector('#go-increments').onclick=()=>navigate('increments');
  document.querySelectorAll('[data-goyear]').forEach(r=>r.onclick=()=>{state.incPreset={year:r.dataset.goyear,month:'',orgDept:'',orgGroup:'',orgPerson:null};navigate('increments')})}

async function renderOverviewTrend(){const title=document.querySelector('#trendTitle'),hint=document.querySelector('#trendHint');if(!title)return;
  if(!state.ovDay){
    title.textContent='月度新增趋势（近 14 个月）';hint.textContent='点击柱状图下钻到当月每日 · 点击空白处返回';
    const rows=state.ovMonthly||[];
    renderChart('trendChart',stackedOption(rows));
    bindChartDrill('trendChart',{keys:rows.map(r=>r.month),labels:rows.map(r=>r.month),
      onDrill:m=>{state.ovDay=m;renderOverviewTrend()},onBack:null});
    return}
  try{
    const tree=await api('/api/v1/metrics/increments/tree?'+new URLSearchParams({month:state.ovDay}));
    if(!document.querySelector('#trendTitle'))return;
    const rows=tree.rows.slice().sort((a,b)=>a.key<b.key?-1:1).map(r=>({month:r.key.slice(8)+'日',total:r.total,bulk_import:r.bulk,routine:r.routine}));
    title.textContent=`${state.ovDay} 每日新增`;hint.textContent='点击空白处返回月度视图';
    renderChart('trendChart',stackedOption(rows,{zoom:rows.length>18}));
    bindChartDrill('trendChart',{keys:rows.map(r=>r.month),labels:rows.map(r=>r.month),
      onDrill:null,onBack:()=>{state.ovDay='';renderOverviewTrend()}});
  }catch(e){toast(e.message);state.ovDay='';renderOverviewTrend()}}

async function increments(){
  const st=state.inc=state.inc||{year:'',month:'',fDept:'',fGroup:'',fPerson:'',orgDept:'',orgGroup:'',orgPerson:null,orgQ:'',excl:true};
  if(state.incPreset){Object.assign(st,state.incPreset);state.incPreset=null}
  const [d,meta]=await Promise.all([
    api('/api/v1/metrics/monthly-increments'),
    api('/api/v1/metrics/uploaders/months').catch(()=>null)]);
  st.latestYear=meta&&meta.months&&meta.months.length?meta.months[meta.months.length-1].month.slice(0,4):String(new Date().getFullYear());
  const sum=k=>d.rows.reduce((a,r)=>a+r[k],0);
  shell('数据看板',d.metric_note,`
  <div class="grid metrics">
    ${statCard('全量新增',nf(sum('total')),'全部观测月份合计')}
    ${statCard('其中批量导入',nf(sum('bulk_import')),d.bulk_day_rule)}
    ${statCard('其中日常新增',nf(sum('routine')),'全量 − 批量导入')}
    ${statCard('文件总保有量',nf(d.total_files),meta?`${meta.workspace_count} 库 · ${meta.uploader_count} 位上传人`:'主基线 + 实时增量')}
  </div>
  <section class="card section-gap" id="treeCard"><div class="empty">正在加载增量构成…</div></section>
  <section class="section-gap"><div class="grid two-cols">
    <section class="card" id="orgCard"><div class="empty">正在加载部门分布…</div></section>
    <section class="card person-panel" id="personPanel"><h2>分布明细</h2><div class="empty">点击左侧部门、业务组或成员，查看对应的上传分布。</div></section>
  </div></section>`,
  `<button class="secondary" id="csvBtn">导出 CSV</button>`);
  document.querySelector('#csvBtn').onclick=()=>{const t=st._tree;if(!t)return;const lvlName={year:'年份',month:'月份',day:'日期'}[t.level];
    const lines=[`${lvlName},全量新增,批量导入,日常新增`].concat(t.rows.map(r=>`${r.key},${r.total},${r.bulk},${r.routine}`));
    const blob=new Blob(['﻿'+lines.join('\n')],{type:'text/csv;charset=utf-8'});const a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download=`知识库增量构成-${t.level==='year'?'按年':t.level==='month'?(st.year||st.month.slice(0,4)):st.month}.csv`;a.click();URL.revokeObjectURL(a.href)};
  await Promise.all([renderTreeSection(),renderOrgSection()])}

function refreshIncSections(){return Promise.all([renderTreeSection(),renderOrgSection()])}

async function renderTreeSection(){const st=state.inc,card=document.querySelector('#treeCard');if(!card)return;
  const treeParams=new URLSearchParams({year:st.month?'':st.year,month:st.month,department:st.fDept,biz_group:st.fGroup,person:st.fPerson});
  const tree=await api('/api/v1/metrics/increments/tree?'+treeParams);st._tree=tree;
  if(!document.querySelector('#treeCard'))return;
  const lvl=tree.level,lvlName={year:'年份',month:'月份',day:'日期'}[lvl];
  const crumbs=`<div class="crumbs"><button class="crumb ${lvl==='year'?'current':''}" data-tcrumb="root">全部年份</button>${(st.year||st.month)?`<span class="sep">›</span><button class="crumb ${lvl==='month'?'current':''}" data-tcrumb="year">${(st.month?st.month.slice(0,4):st.year)} 年</button>`:''}${st.month?`<span class="sep">›</span><button class="crumb current" data-tcrumb="month">${st.month.slice(5)} 月</button>`:''}</div>`;
  const treeRows=tree.rows.map(r=>{const label=lvl==='year'?r.key+' 年':lvl==='month'?r.key.slice(5)+' 月':r.key.slice(8)+' 日';
    return `<tr class="${lvl!=='day'?'rowlink':''}" ${lvl!=='day'?`data-tdrill="${r.key}"`:''}><td><b>${label}</b>${lvl!=='day'?' <small style="color:#9ca3af">›</small>':''}</td><td class="num"><b>${nf(r.total)}</b></td><td class="num">${r.bulk?nf(r.bulk):'—'}</td><td class="num">${nf(r.routine)}</td></tr>`}).join('')||'<tr><td colspan="4"><div class="empty">该范围内无数据</div></td></tr>';
  const sorted=tree.rows.slice().sort((a,b)=>a.key<b.key?-1:1);
  const chartRows=sorted.map(r=>({month:lvl==='year'?r.key+'年':lvl==='month'?r.key.slice(5)+'月':r.key.slice(8)+'日',total:r.total,bulk_import:r.bulk,routine:r.routine}));
  card.innerHTML=`<div class="card-head"><h2>增量构成</h2><span class="hint">点击柱状图或表格行下钻：年 → 月 → 日 · 点击图表空白处回退</span></div>
    <div class="controls" style="flex-wrap:wrap;gap:8px;margin-bottom:8px">${crumbs}<span style="flex:1"></span>
      <input class="input" id="tf-dept" list="dl-dept" placeholder="部门" value="${st.fDept}" style="width:130px">
      <input class="input" id="tf-group" list="dl-group" placeholder="业务组" value="${st.fGroup}" style="width:115px">
      <input class="input" id="tf-person" list="dl-person" placeholder="成员" value="${st.fPerson}" style="width:105px">
      <datalist id="dl-dept"></datalist><datalist id="dl-group"></datalist><datalist id="dl-person"></datalist>
      <button class="primary" id="tf-go">筛选</button>${(st.fDept||st.fGroup||st.fPerson)?'<button class="secondary" id="tf-clear">清除</button>':''}
    </div>
    ${tree.filter_label?`<p class="hint" style="margin:0 0 8px">当前筛选：${tree.filter_label}</p>`:''}
    <div id="incChart" class="chart"></div>
    <div class="table-wrap" style="margin-top:8px"><table class="data-table"><thead><tr><th>${lvlName}</th><th class="num">全量新增</th><th class="num">批量导入</th><th class="num">日常新增</th></tr></thead><tbody>${treeRows}</tbody></table></div>`;
  renderChart('incChart',stackedOption(chartRows,{zoom:chartRows.length>18}));
  bindChartDrill('incChart',{keys:sorted.map(r=>r.key),labels:chartRows.map(r=>r.month),
    onDrill:lvl==='day'?null:k=>{if(lvl==='year'){st.year=k;st.month=''}else{st.month=k}refreshIncSections()},
    onBack:lvl==='year'?null:()=>{if(lvl==='day'){st.year=st.month.slice(0,4);st.month=''}else{st.year='';st.month=''}refreshIncSections()}});
  card.querySelectorAll('[data-tdrill]').forEach(r=>r.onclick=()=>{const k=r.dataset.tdrill;if(lvl==='year'){st.year=k;st.month=''}else if(lvl==='month'){st.month=k}refreshIncSections()});
  card.querySelectorAll('[data-tcrumb]').forEach(b=>b.onclick=()=>{const c=b.dataset.tcrumb;if(c==='root'){st.year='';st.month=''}else if(c==='year'){if(st.month)st.year=st.month.slice(0,4);st.month=''}refreshIncSections()});
  card.querySelector('#tf-go').onclick=()=>{st.fDept=card.querySelector('#tf-dept').value.trim();st.fGroup=card.querySelector('#tf-group').value.trim();st.fPerson=card.querySelector('#tf-person').value.trim();renderTreeSection()};
  card.querySelectorAll('#tf-dept,#tf-group,#tf-person').forEach(i=>{
    i.onkeydown=e=>{if(e.key==='Enter')card.querySelector('#tf-go').click()};
    i.oninput=()=>fillIncDatalists()});
  fillIncDatalists();
  const tfc=card.querySelector('#tf-clear');if(tfc)tfc.onclick=()=>{st.fDept='';st.fGroup='';st.fPerson='';renderTreeSection()}}

/* 筛选下拉选项：部门/业务组/成员来自 /metrics/org 的嵌套结果，业务组、成员
   随已输入的上级联动收窄。datalist = 原生"下拉+可搜索"。 */
function fillIncDatalists(){const st=state.inc,org=st&&st._org;if(!org)return;
  const dd=document.querySelector('#dl-dept');if(!dd)return;
  const deptVal=(document.querySelector('#tf-dept')||{value:''}).value.trim();
  const groupVal=(document.querySelector('#tf-group')||{value:''}).value.trim();
  const groups=[],people=[];
  org.filter(x=>!deptVal||(x.department_name||'').includes(deptVal)).forEach(x=>(x.groups||[]).forEach(g=>{
    if(g.biz_group_name)groups.push(g.biz_group_name);
    if(!groupVal||(g.biz_group_name||'').includes(groupVal))(g.people||[]).forEach(pp=>{if(pp.name)people.push(pp.name)})}));
  const fill=(sel,arr)=>{const el=document.querySelector(sel);if(el)el.innerHTML=[...new Set(arr)].slice(0,400).map(v=>`<option value="${v}">`).join('')};
  fill('#dl-dept',org.map(x=>x.department_name).filter(Boolean));fill('#dl-group',groups);fill('#dl-person',people)}

async function renderOrgSection(){const st=state.inc,card=document.querySelector('#orgCard');if(!card)return;
  const orgYear=st.month?'':(st.year||st.latestYear);
  const org=await api('/api/v1/metrics/org?'+new URLSearchParams({year:orgYear,month:st.month||''})).catch(()=>({items:[]}));
  if(!document.querySelector('#orgCard'))return;
  st._org=org.items||[];fillIncDatalists();
  const periodLabel=st.month||((st.year||st.latestYear)+' 全年');
  const orgLevel=st.orgDept?(st.orgGroup?'person':'group'):'dept';
  const visibleOrg=(org.items||[]).filter(x=>!st.excl||!['系统/机器人','未映射'].includes(x.department_name));
  let orgRows=visibleOrg,currentDept=null,currentGroup=null;
  if(st.orgDept){currentDept=(org.items||[]).find(x=>x.department_name===st.orgDept);
    if(currentDept){orgRows=currentDept.groups;if(st.orgGroup){currentGroup=currentDept.groups.find(g=>g.biz_group_name===st.orgGroup);if(currentGroup)orgRows=currentGroup.people}}}
  if(st.orgQ){const q=st.orgQ;orgRows=orgRows.filter(x=>((x.department_name||x.biz_group_name||x.name||'')+'').includes(q))}
  const orgCrumbs=`<div class="crumbs"><button class="crumb ${orgLevel==='dept'?'current':''}" data-ocrumb="root">全部部门</button>${st.orgDept?`<span class="sep">›</span><button class="crumb ${orgLevel==='group'?'current':''}" data-ocrumb="dept">${st.orgDept}</button>`:''}${st.orgGroup?`<span class="sep">›</span><button class="crumb current" data-ocrumb="group">${st.orgGroup}</button>`:''}</div>`;
  const orgTable=orgLevel==='dept'
    ?`<table class="data-table"><thead><tr><th>部门</th><th class="num">保有量</th><th class="num">${periodLabel}增量</th><th class="num hide-sm">上传人数</th></tr></thead><tbody>${orgRows.map(x=>`<tr class="rowlink" data-odept="${x.department_name}"><td><b>${x.department_name}</b>${x.is_robot?'<span class="row-tag">系统</span>':''}</td><td class="num">${nf(x.stock)}</td><td class="num ${x.delta?'delta-pos':'delta-zero'}">${x.delta?'+'+nf(x.delta):'0'}</td><td class="num hide-sm">${x.uploaders}</td></tr>`).join('')||'<tr><td colspan="4"><div class="empty">无匹配</div></td></tr>'}</tbody></table>`
    :orgLevel==='group'
    ?`<table class="data-table"><thead><tr><th>业务组</th><th class="num">保有量</th><th class="num">${periodLabel}增量</th><th class="num hide-sm">上传人数</th></tr></thead><tbody>${orgRows.map(g=>`<tr class="rowlink" data-ogroup="${g.biz_group_name}"><td><b>${g.biz_group_name}</b></td><td class="num">${nf(g.stock)}</td><td class="num ${g.delta?'delta-pos':'delta-zero'}">${g.delta?'+'+nf(g.delta):'0'}</td><td class="num hide-sm">${g.uploaders}</td></tr>`).join('')||'<tr><td colspan="4"><div class="empty">无匹配</div></td></tr>'}</tbody></table>`
    :`<table class="data-table"><thead><tr><th>成员</th><th class="num">保有量</th><th class="num">${periodLabel}增量</th></tr></thead><tbody>${orgRows.map(pp=>`<tr class="rowlink" data-operson="${pp.user_id}"><td><b>${pp.name}</b>${pp.matched?'':'<span class="row-tag">未映射</span>'}</td><td class="num">${nf(pp.stock)}</td><td class="num ${pp.delta?'delta-pos':'delta-zero'}">${pp.delta?'+'+nf(pp.delta):'0'}</td></tr>`).join('')||'<tr><td colspan="3"><div class="empty">无匹配</div></td></tr>'}</tbody></table>`;
  card.innerHTML=`<div class="card-head">${orgCrumbs}<span style="flex:1"></span>
      <input class="input" id="org-q" placeholder="搜索${orgLevel==='dept'?'部门':orgLevel==='group'?'业务组':'成员'}" value="${st.orgQ}" style="width:140px">
      <label class="chip" style="cursor:pointer;margin-left:6px"><input type="checkbox" id="org-excl" ${st.excl?'checked':''} style="margin-right:4px">排除系统/未映射</label></div>
      <p class="hint" style="margin:4px 0 8px">${periodLabel}口径 · 点击行下钻：部门 → 业务组 → 成员</p>
      <div class="table-wrap" style="max-height:430px;overflow-y:auto">${orgTable}</div>`;
  card.querySelector('#org-q').onkeydown=e=>{if(e.key==='Enter'){st.orgQ=e.target.value.trim();renderOrgSection()}};
  card.querySelector('#org-excl').onchange=e=>{st.excl=e.target.checked;renderOrgSection()};
  card.querySelectorAll('[data-ocrumb]').forEach(b=>b.onclick=()=>{const c=b.dataset.ocrumb;if(c==='root'){st.orgDept='';st.orgGroup=''}else if(c==='dept'){st.orgGroup=''}st.orgPerson=null;st.orgQ='';renderOrgSection()});
  card.querySelectorAll('[data-odept]').forEach(r=>r.onclick=()=>{st.orgDept=r.dataset.odept;st.orgGroup='';st.orgPerson=null;st.orgQ='';renderOrgSection();loadOrgEntity('department',st.orgDept,orgYear,st.month)});
  card.querySelectorAll('[data-ogroup]').forEach(r=>r.onclick=()=>{st.orgGroup=r.dataset.ogroup;st.orgPerson=null;st.orgQ='';renderOrgSection();loadOrgEntity('biz_group',st.orgGroup,orgYear,st.month)});
  card.querySelectorAll('[data-operson]').forEach(r=>r.onclick=()=>{st.orgPerson=r.dataset.operson;loadPerson(st.orgPerson,orgYear,st.month)})}

async function loadOrgEntity(kind,name,year,month){const panel=document.querySelector('#personPanel');if(!panel||!name)return;
  const params=new URLSearchParams({year:month?'':year,month:month||''});params.set(kind,name);
  const tree=await api('/api/v1/metrics/increments/tree?'+params).catch(()=>null);
  if(!document.querySelector('#personPanel'))return;
  if(!tree){panel.innerHTML='<h2>分布明细</h2><div class="empty">加载失败</div>';return}
  const label=month||(year+' 全年');const unit=tree.level==='day'?'日':'月';
  const series=tree.rows.slice().sort((a,b)=>a.key<b.key?-1:1).map(r=>({k:tree.level==='day'?r.key.slice(8)+'日':r.key.slice(5)+'月',v:r.total,b:r.bulk}));
  const total=tree.rows.reduce((a,r)=>a+r.total,0),bulk=tree.rows.reduce((a,r)=>a+r.bulk,0);
  panel.innerHTML=`<div class="card-head" style="margin-bottom:6px"><h2 style="margin:0">${name}</h2><span class="chip blue">${kind==='department'?'部门':'业务组'}</span></div>
  <p class="hint">${label}上传 <b>${nf(total)}</b> 篇 · 其中批量 ${nf(bulk)} · 日常 ${nf(total-bulk)}${tree.filter_label?` · ${tree.filter_label}`:''}</p>
  <div id="entityChart" class="chart" style="height:190px"></div>
  <p class="hint" style="margin-top:8px">按${unit}分布；点左侧成员行可看个人明细。</p>`;
  renderChart('entityChart',{tooltip:{trigger:'axis'},grid:{left:6,right:6,top:10,bottom:4,containLabel:true},
    xAxis:{type:'category',data:series.map(x=>x.k),axisTick:{show:false},axisLabel:{color:'#6b7280',fontSize:10,interval:series.length>15?'auto':0}},
    yAxis:{type:'value',splitLine:{lineStyle:{color:'#f3f4f6'}},axisLabel:{color:'#6b7280',fontSize:10}},
    series:[{type:'bar',stack:'t',barMaxWidth:16,data:series.map(x=>x.v-x.b),itemStyle:{color:'#2563eb'}},{type:'bar',stack:'t',barMaxWidth:16,data:series.map(x=>x.b),itemStyle:{color:'#f59e0b',borderRadius:[3,3,0,0]}}]})}

async function loadPerson(personId,year,month){const panel=document.querySelector('#personPanel');if(!panel||!personId)return;
  const d=await api('/api/v1/metrics/uploaders/'+personId+'/breakdown?'+new URLSearchParams({year:month?'':year,month:month||''}));
  if(!document.querySelector('#personPanel'))return;
  const label=month||(year+' 全年');
  const series=month?d.days.map(x=>({k:x.day.slice(8)+'日',v:x.count})):d.months.filter(m=>m.month.startsWith(year)).map(x=>({k:x.month.slice(5)+'月',v:x.count}));
  panel.innerHTML=`<div class="card-head" style="margin-bottom:6px"><h2 style="margin:0">${d.name||d.user_id}</h2><span class="chip ${d.matched?'blue':'amber'}">${d.department_name}${d.biz_group_name&&d.biz_group_name!==d.department_name?' · '+d.biz_group_name:''}</span></div>
  <p class="hint">${label}上传 <b>${nf(d.period_total)}</b> 篇 · 历史累计 ${nf(d.all_total)} 篇</p>
  <div id="personChart" class="chart" style="height:190px"></div>
  <p class="hint" style="margin-top:8px">知识库分布（${label}）：</p>
  <div>${d.workspaces.map(w=>`<span class="ws-chip">${w.name} <b>${nf(w.files)}</b></span>`).join('')||'<span class="hint">该期间无上传</span>'}</div>`;
  renderChart('personChart',{tooltip:{trigger:'axis'},grid:{left:6,right:6,top:10,bottom:4,containLabel:true},
    xAxis:{type:'category',data:series.map(x=>x.k),axisTick:{show:false},axisLabel:{color:'#6b7280',fontSize:10,interval:series.length>15?'auto':0}},
    yAxis:{type:'value',splitLine:{lineStyle:{color:'#f3f4f6'}},axisLabel:{color:'#6b7280',fontSize:10}},
    series:[{type:'bar',barMaxWidth:16,data:series.map(x=>x.v),itemStyle:{color:'#2563eb',borderRadius:[3,3,0,0]}}]})}

/* ---- unified document list (baseline snapshot + live increments) ---- */
function fileTable(d){if(!d.items.length)return '<div class="empty">未找到匹配文档。</div>';
  return `<div class="table-wrap"><table class="data-table"><thead><tr><th>文档名称 / 节点</th><th>知识库</th><th>上传人 / 部门</th><th class="num">AI评审</th><th>入库时间</th><th></th></tr></thead><tbody>${d.items.map(f=>{
    const ws=state.coverageNames[f.workspace_id]||f.workspace_id;
    return `<tr ${f.has_detail?`class="rowlink" data-doc="${f.node_id}"`:''}><td><b>${f.name}</b><br><small>${f.node_id}</small></td><td><small>${ws}</small></td><td>${fmt(f.uploader_name)}${f.department_name?`<br><small>${f.department_name}</small>`:''}</td><td class="num">${f.ai_score!=null?`<span class="score ${scoreClass(f.ai_score)}">${f.ai_score}</span>`:'—'}${f.verdict?`<br><span class="badge ${f.verdict}">${verdictText(f.verdict)}</span>`:''}</td><td>${fmt((f.created_at||'').slice(0,10))}</td><td>${f.url?`<a class="link-btn" href="${f.url}" target="_blank" rel="noopener" onclick="event.stopPropagation()">打开 ↗</a>`:''}${f.has_detail?' <small style="color:#9ca3af">›</small>':''}</td></tr>`}).join('')}</tbody></table></div>
  <div class="controls section-gap"><span class="hint">共 ${nf(d.total)} 个 · 第 ${Math.floor(d.offset/d.limit)+1} / ${Math.max(1,Math.ceil(d.total/d.limit))} 页</span><button class="secondary" id="bl-prev" ${d.offset<=0?'disabled':''}>上一页</button><button class="secondary" id="bl-next" ${d.offset+d.limit>=d.total?'disabled':''}>下一页</button></div>`}
function renderFilesBox(d){const box=document.querySelector('#bl-results');if(!box)return;box.innerHTML=fileTable(d);
  const t=document.querySelector('#fl-total');if(t)t.textContent=nf(d.total);
  bindDocRows();
  const p=state.bl,prev=document.querySelector('#bl-prev'),next=document.querySelector('#bl-next');
  if(prev)prev.onclick=()=>{p.offset=Math.max(0,p.offset-50);blSearch()};
  if(next)next.onclick=()=>{p.offset+=50;blSearch()}}
async function blSearch(){const p=state.bl;const qs=new URLSearchParams({query:p.query||'',workspace_id:p.ws||'',folder:p.folder||'',department:p.dept||'',uploader:p.up||'',offset:p.offset,limit:50});
  const d=await api('/api/v1/files?'+qs);state._filesLast=d;renderFilesBox(d)}

async function documents(){
  state.bl={query:'',ws:'',dept:'',up:'',offset:0};
  shell('文档列表','基线快照与实时增量合并去重后的全部文档，默认展示最新入库；可按知识库、归属部门、上传人、文件名过滤，有评审的行可点击查看详情。',`
  <section class="card"><div class="card-head"><h2>全部文档（<span id="fl-total">…</span>）</h2><div class="controls" style="flex-wrap:wrap;gap:8px">
    <select class="input" id="bl-ws"><option value="">全部知识库</option></select>
    <select class="input" id="fl-dept" title="知识库归属部门"><option value="">全部部门</option></select>
    <input class="input" id="fl-up" placeholder="上传人" style="width:100px">
    <input class="input" id="bl-q" placeholder="按文件名搜索">
    <button class="secondary" id="bl-btn">查询</button></div></div>
  <div id="bl-results"><div class="empty">正在加载最新文档…</div></div></section>`);
  const applyFilters=()=>{state.bl.query=document.querySelector('#bl-q').value;state.bl.ws=document.querySelector('#bl-ws').value;state.bl.dept=document.querySelector('#fl-dept').value;state.bl.up=document.querySelector('#fl-up').value.trim();state.bl.offset=0;blSearch()};
  document.querySelector('#bl-btn').onclick=applyFilters;
  document.querySelectorAll('#bl-q,#fl-up').forEach(i=>i.onkeydown=e=>{if(e.key==='Enter')applyFilters()});
  document.querySelectorAll('#bl-ws,#fl-dept').forEach(s=>s.onchange=applyFilters);
  blSearch().catch(e=>{const box=document.querySelector('#bl-results');if(box)box.innerHTML=`<div class="empty">${e.message}</div>`});
  loadWorkspaceOptions('#bl-ws');
  loadDeptOptions('#fl-dept')}

async function loadWorkspaceOptions(sel){try{
  if(!Object.keys(state.coverageNames).length){const c=await api('/api/v1/metrics/coverage');c.items.forEach(i=>state.coverageNames[i.workspace_id]=i.name)}
  const el=document.querySelector(sel);if(!el)return;const cur=el.value;
  el.innerHTML='<option value="">全部知识库</option>'+Object.entries(state.coverageNames).map(([k,v])=>`<option value="${k}">${v}</option>`).join('');
  el.value=cur;
  if(state._filesLast&&document.querySelector('#bl-results table'))renderFilesBox(state._filesLast)
}catch(e){}}
async function loadDeptOptions(sel){try{
  state.deptOptions=state.deptOptions||(await api('/api/v1/filters/departments')).items;
  const el=document.querySelector(sel);if(!el)return;const cur=el.value;
  el.innerHTML='<option value="">全部部门</option>'+state.deptOptions.map(x=>`<option value="${x.name}">${x.name}（${x.count} 库）</option>`).join('');
  el.value=cur
}catch(e){}}

async function reviews(state_={verdict:'',query:'',dept:'',up:'',offset:0}){const qs=new URLSearchParams({verdict:state_.verdict,query:state_.query,department:state_.dept||'',uploader:state_.up||'',offset:state_.offset,limit:50});
  const d=await api('/api/v1/reviews?'+qs);
  shell('评审记录','全部 AI 评审实例；实例不可变，重评产生新记录。分数为建议，最终结论由审核员保存。',`
  <section class="card"><div class="card-head"><div class="controls" style="flex-wrap:wrap;gap:8px">
    <select class="input" id="rv-verdict"><option value="">全部结论</option><option value="pass" ${state_.verdict==='pass'?'selected':''}>通过</option><option value="manual_review" ${state_.verdict==='manual_review'?'selected':''}>待人工审核</option><option value="return" ${state_.verdict==='return'?'selected':''}>退回</option></select>
    <select class="input" id="rv-dept" title="知识库归属部门"><option value="">全部部门</option>${state_.dept?`<option value="${state_.dept}" selected>${state_.dept}</option>`:''}</select>
    <input class="input" id="rv-up" placeholder="上传人" value="${state_.up||''}" style="width:100px">
    <input class="input" id="rv-q" placeholder="按文档名搜索" value="${state_.query}"><button class="secondary" id="rv-btn">查询</button></div>
    <span class="hint">共 ${nf(d.total)} 条</span></div>
  ${d.items.length?`<div class="table-wrap"><table class="data-table"><thead><tr><th>文档</th><th>上传人 / 部门</th><th class="num">AI评分</th><th>结论</th><th>范围</th><th>触发</th><th>时间</th></tr></thead><tbody>
    ${d.items.map(x=>`<tr class="rowlink" data-doc="${x.node_id}"><td><b>${x.document_name}</b><br><small>${x.review_instance_id.slice(0,8)}</small></td><td>${fmt(x.uploader_name)}<br><small>${fmt(x.department_name)}</small></td><td class="num score ${x.ai_score>0?scoreClass(x.ai_score):''}">${x.ai_score>0?x.ai_score:'—'}</td><td><span class="badge ${x.verdict}">${verdictText(x.verdict)}</span></td><td>${x.review_scope==='full_content'?'完整正文':'元数据'}</td><td>${x.trigger}</td><td><small>${(x.created_at||'').replace('T',' ').slice(0,16)}</small></td></tr>`).join('')}
  </tbody></table></div>
  <div class="controls section-gap"><span class="hint">第 ${Math.floor(d.offset/50)+1} / ${Math.max(1,Math.ceil(d.total/50))} 页</span><button class="secondary" id="rv-prev" ${d.offset<=0?'disabled':''}>上一页</button><button class="secondary" id="rv-next" ${d.offset+50>=d.total?'disabled':''}>下一页</button></div>`
  :'<div class="empty">暂无评审记录。评审在增量同步发现新文档、或文档详情页手动触发后产生。</div>'}
  </section>`);
  bindDocRows();
  loadDeptOptions('#rv-dept').then(()=>{const el=document.querySelector('#rv-dept');if(el)el.value=state_.dept||''});
  document.querySelector('#rv-btn').onclick=()=>reviews({verdict:document.querySelector('#rv-verdict').value,query:document.querySelector('#rv-q').value,dept:document.querySelector('#rv-dept').value,up:document.querySelector('#rv-up').value.trim(),offset:0});
  document.querySelectorAll('#rv-verdict,#rv-dept').forEach(s=>s.onchange=()=>document.querySelector('#rv-btn').click());
  document.querySelector('#rv-up').onkeydown=e=>{if(e.key==='Enter')document.querySelector('#rv-btn').click()};
  const pv=document.querySelector('#rv-prev'),nx=document.querySelector('#rv-next');
  if(pv)pv.onclick=()=>reviews({...state_,offset:Math.max(0,state_.offset-50)});
  if(nx)nx.onclick=()=>reviews({...state_,offset:state_.offset+50})}

async function documentDetail(id){const d=await api('/api/v1/documents/'+id);const r=d.latest_review;
  const allDims=r?r.dimensions:{};const model=allDims.model;const ruleDims=Object.entries(allDims).filter(([k])=>k!=='model').map(([,v])=>v);
  const dualBanner=model?`<section class="card section-gap"><div class="card-head"><h2>综合评分构成</h2><span class="chip">文体：${model.genre||'未判定'}</span></div>
    <div class="detail-meta" style="font-size:15px;flex-wrap:wrap;gap:18px">
      <span>规则合规分 <b>${model.rule_score}</b> × ${Math.round((model.rule_weight??0.4)*100)}%</span>
      <span>＋</span>
      <span>模型内容分 <b>${model.model_score}</b> × ${Math.round((1-(model.rule_weight??0.4))*100)}%</span>
      <span>＝</span>
      <span>综合 <b class="score ${scoreClass(r.ai_score)}">${r.ai_score}</b></span>
    </div>
    ${model.model_dimensions&&Object.keys(model.model_dimensions).length?`<div class="controls" style="flex-wrap:wrap;gap:8px;margin-top:10px">${Object.entries(model.model_dimensions).map(([k,v])=>`<span class="chip">${k} ${v}</span>`).join('')}</div>`:''}
    ${model.findings&&model.findings.length?`<ul style="margin-top:10px">${model.findings.map(f=>`<li>${f.message}</li>`).join('')}</ul>`:''}
  </section>`:'';
  shell('评审详情','AI 分数为建议；最终结论由知识库审核员保存。',`
  <section class="card"><div class="detail-header"><div class="document-icon">▤</div><div><h2 style="margin:0">${d.name}</h2><p class="sub">节点 ${d.node_id} · 知识库 ${state.coverageNames[d.workspace_id]||d.workspace_id}</p><div class="detail-meta"><span>上传人：${fmt(d.uploader_name)}</span><span>入库时间：${fmt(d.source_created_at)}</span><span>归属：${fmt(d.department_name)} / ${fmt(d.biz_group_name)}</span><span>重评次数：${d.rerun_count}</span></div></div><div class="big-score">${r&&r.ai_score>0?r.ai_score:'—'}<span>/100</span><br><small>${r?verdictText(r.verdict):'未评审'}</small></div></div></section>
  ${dualBanner}
  <section class="grid deductions section-gap">${ruleDims.map(x=>`<article class="card deduction${x.advisory?' muted':''}"><div class="field-label">${x.label}${x.advisory?' <span class="chip amber">仅提示不计分</span>':''}</div><div class="number">${x.advisory?'<small>该文体不适用</small>':`-${x.deduction}<small> / ${x.cap}</small>`}</div><ul>${x.findings.length?x.findings.map(f=>`<li>${f.message}</li>`).join(''):'<li>未发现扣分项</li>'}</ul></article>`).join('')||'<div class="card muted">暂无评审维度数据</div>'}</section>
  <section class="card section-gap"><h2>评审记录</h2>${d.reviews.length?`<div class="table-wrap"><table class="data-table"><thead><tr><th>实例 ID</th><th class="num">分数</th><th>范围</th><th>触发</th><th>规则版本</th><th>时间</th></tr></thead><tbody>${d.reviews.map(x=>`<tr><td><small>${x.review_instance_id}</small></td><td class="num score ${scoreClass(x.ai_score)}">${x.ai_score}</td><td>${x.review_scope==='full_content'?'完整正文':`元数据合规${x.content_note?`<br><small style="color:#9ca3af" title="正文不可用原因">${x.content_note}</small>`:''}`}</td><td>${x.trigger}</td><td>${x.rule_version}${x.rule_config_ref&&x.rule_config_ref!=='builtin'?`<br><small style="color:#9ca3af">${x.rule_config_ref}</small>`:''}</td><td>${(x.created_at||'').replace('T',' ').slice(0,19)}</td></tr>`).join('')}</tbody></table></div>`:'<p class="hint">暂无记录</p>'}
  <div class="section-gap"><button class="primary" id="rerun">重新评审</button><button class="secondary" id="back" style="margin-left:8px">返回</button></div></section>`);
  document.querySelector('#back').onclick=()=>goBack('documents');
  document.querySelector('#rerun').onclick=async()=>{const j=await api('/api/v1/documents/'+id+'/reviews',{method:'POST',body:JSON.stringify({trigger:'manual_rerun'})});toast('已提交评审任务 '+j.job_id.slice(0,8))}}

async function workspaces(){
  state.wsReg=state.wsReg||{query:'',level:'',department:'',creator:'',admin:'',offset:0};const p=state.wsReg;
  const qs=new URLSearchParams({query:p.query,level:p.level==='其他'?'':p.level,department:p.department,creator:p.creator,admin:p.admin,offset:p.offset,limit:50});
  if(p.level==='其他')qs.set('level','其他');
  const [reg,cov]=await Promise.all([api('/api/v1/workspaces?'+qs),api('/api/v1/metrics/coverage').catch(()=>null)]);
  if(cov)cov.items.forEach(i=>state.coverageNames[i.workspace_id]=i.name);
  const s=cov?cov.summary:{visible_workspaces:reg.total,scanned:'—',empty:'—',excluded:'—'},org=(cov&&cov.org_context)||{};
  const facet=Object.fromEntries((reg.levels||[]).map(l=>[l.level,l.count]));
  const tabs=[['','全部'],['C','C-公司级'],['D','D-部门级'],['P','P-项目级'],['I','I-个人级'],['其他','其他']];
  shell('知识库管理','公司知识库注册表：等级分类、搜索、筛选与分页；点击行查看月度分布与治理配置。',`
  <div class="grid metrics">
    ${statCard('注册知识库',reg.total_all??reg.total,`全公司约 ${org.org_total_knowledge_bases||'—'} 库`)}
    ${statCard('已扫描',s.scanned,'基线或实时同步有数据')}
    ${statCard('空库',s.empty,'探测确认无文件')}
    ${statCard('已排除',s.excluded,'不计入指标')}
  </div>
  <section class="card section-gap"><div class="card-head"><h2>知识库清单（${nf(reg.total)}）</h2><span class="hint">${org.note||''}</span></div>
  <div class="controls" style="flex-wrap:wrap;gap:8px;margin-bottom:8px">
    ${tabs.map(([v,l])=>`<button class="${p.level===v?'primary':'secondary'}" data-level="${v}">${l}${v&&facet[v]!==undefined?` (${facet[v]})`:''}</button>`).join('')}
  </div>
  <div class="controls" style="flex-wrap:wrap;gap:8px;margin-bottom:8px">
    <input class="input" id="wsq" placeholder="按名称搜索" value="${p.query}" style="flex:2;min-width:150px">
    <input class="input" id="wsdept" placeholder="部门" value="${p.department}" style="flex:1;min-width:100px">
    <input class="input" id="wsadmin" placeholder="管理员" value="${p.admin}" style="flex:1;min-width:100px">
    <button class="primary" id="wsgo">查询</button><button class="secondary" id="wsreset">重置</button>
  </div>
  <div class="table-wrap"><table class="data-table"><thead><tr><th>知识库</th><th>等级</th><th class="num">文档数</th><th>创建人</th><th>管理员</th><th>归属部门</th></tr></thead><tbody>
    ${reg.items.map(i=>`<tr class="rowlink" data-ws="${i.workspace_id}"><td><b>${i.name}</b><br><small>${i.workspace_id}</small></td><td><span class="chip">${i.level_label}</span></td><td class="num">${i.document_count?nf(i.document_count):'—'}</td><td>${i.creator||'—'}</td><td>${(i.administrators||[]).slice(0,3).join('、')||'—'}${(i.administrators||[]).length>3?` 等${i.administrators.length}人`:''}</td><td>${i.department_name||'—'}</td></tr>`).join('')||'<tr><td colspan="6"><div class="empty">无匹配结果</div></td></tr>'}
  </tbody></table></div>
  <div class="controls" style="justify-content:space-between;margin-top:8px"><span class="hint">第 ${reg.total?p.offset+1:0}-${Math.min(p.offset+50,reg.total)} 条 / 共 ${reg.total} 条</span><span><button class="secondary" id="wsprev" ${p.offset<=0?'disabled':''}>上一页</button><button class="secondary" id="wsnext" ${p.offset+50>=reg.total?'disabled':''} style="margin-left:8px">下一页</button></span></div>
  </section>
  `);
  document.querySelectorAll('[data-level]').forEach(x=>x.onclick=()=>{p.level=x.dataset.level;p.offset=0;workspaces()});
  document.querySelector('#wsgo').onclick=()=>{p.query=document.querySelector('#wsq').value.trim();p.department=document.querySelector('#wsdept').value.trim();p.creator='';p.admin=document.querySelector('#wsadmin').value.trim();p.offset=0;workspaces()};
  document.querySelector('#wsq').onkeydown=e=>{if(e.key==='Enter')document.querySelector('#wsgo').click()};
  document.querySelector('#wsreset').onclick=()=>{state.wsReg={query:'',level:'',department:'',creator:'',admin:'',offset:0};workspaces()};
  document.querySelector('#wsprev').onclick=()=>{p.offset=Math.max(0,p.offset-50);workspaces()};
  document.querySelector('#wsnext').onclick=()=>{p.offset=p.offset+50;workspaces()};
  document.querySelectorAll('[data-ws]').forEach(x=>x.onclick=()=>goWs(x.dataset.ws))}

async function workspaceDetail(id){const[m,g,fd]=await Promise.all([api('/api/v1/metrics/workspaces/'+id+'/months'),api('/api/v1/workspaces/'+id).catch(()=>null),api('/api/v1/baseline/workspaces/'+id+'/folders?limit=100').catch(()=>({items:[],total_folders:0,note:''}))]);
  if(!Object.keys(state.coverageNames).length){try{const c=await api('/api/v1/metrics/coverage');c.items.forEach(i=>state.coverageNames[i.workspace_id]=i.name)}catch(e){}}
  const name=state.coverageNames[id]||id;
  shell('知识库详情',name,`
  <section class="card"><div class="card-head"><h2>月度入库分布（基线 + 增量，共 ${nf(m.total_files)} 个文件）</h2><button class="secondary" id="back">返回</button></div><div id="wsChart" class="chart"></div></section>
  ${fd.items.length?`<section class="card section-gap"><div class="card-head"><h2>目录分布（共 ${nf(fd.total_folders)} 个目录，按文件数 Top ${fd.items.length}）</h2><span class="hint">${fd.note}</span></div><div class="grid two-cols"><div class="table-wrap" style="max-height:340px;overflow-y:auto"><table class="data-table"><thead><tr><th>目录</th><th class="num">文件数</th><th>时间跨度</th></tr></thead><tbody>
    ${fd.items.map(f=>`<tr class="rowlink" data-folder="${f.parent_node_id}"><td><b>${f.folder_name||(f.parent_node_id==='(根目录)'?'（根目录）':'（未记录名称）')}</b><br><small>${f.parent_node_id}</small></td><td class="num">${nf(f.file_count)}</td><td><small>${f.earliest} ~ ${f.latest}</small></td></tr>`).join('')}
  </tbody></table></div><div><div class="controls"><input class="input" id="ws-q" placeholder="在本库内按文件名搜索" style="flex:1"><button class="secondary" id="ws-q-btn">搜索</button></div><div id="bl-results" class="section-gap"><div class="empty">点击左侧目录或输入关键字查看文件。</div></div></div></div></section>`:''}
  ${g?`<section class="grid two-cols section-gap"><form class="card" id="governance-form"><h2>治理归属与角色</h2><div class="form-grid" style="margin-top:12px">
    <label class="form-field"><span class="field-label">归属部门 ID</span><input class="input" name="owner_department_id" value="${g.owner_department_id||''}"></label>
    <label class="form-field"><span class="field-label">归属部门名称</span><input class="input" name="owner_department_name" value="${g.owner_department_name||''}"></label>
    <label class="form-field"><span class="field-label">业务组名称</span><input class="input" name="owner_biz_group_name" value="${g.owner_biz_group_name||''}"></label>
    <label class="form-field"><span class="field-label">管理员（employeeKey，逗号分隔）</span><input class="input" name="administrators" value="${(g.administrators||[]).join(',')}"></label>
    <label class="form-field full"><span class="field-label">审核员（employeeKey，逗号分隔）</span><input class="input" name="reviewers" value="${(g.reviewers||[]).join(',')}"></label></div>
    <p class="hint">员工归属以 bi_center 契约为准；仅 matched 且 includeInOfficialStats 的身份进入正式统计。</p><button class="primary">保存治理配置</button></form>
  <section class="card"><h2>基本信息</h2><p class="hint">知识库 ID：${id}</p><p class="hint">创建：${fmt((g.source_created_at||'').slice(0,10))} · 最近更新：${fmt((g.source_updated_at||'').slice(0,10))}</p>${g.url?`<p class="hint"><a href="${g.url}" target="_blank" rel="noopener">在钉钉中打开 ↗</a></p>`:''}<p class="hint">实时同步文档数：${nf(g.document_count)}</p></section></section>`:''}`);
  renderChart('wsChart',stackedOption(m.months.map(x=>({month:x.month,routine:x.count,bulk_import:0}))));
  state.bl={query:'',ws:id,offset:0};
  document.querySelectorAll('[data-folder]').forEach(x=>x.onclick=()=>{state.bl={query:'',ws:id,offset:0,folder:x.dataset.folder};blSearch()});
  const wsBtn=document.querySelector('#ws-q-btn');
  if(wsBtn)wsBtn.onclick=()=>{state.bl={query:document.querySelector('#ws-q').value,ws:id,offset:0};blSearch()};
  const wsQ=document.querySelector('#ws-q');
  if(wsQ)wsQ.onkeydown=e=>{if(e.key==='Enter')wsBtn.click()};
  document.querySelector('#back').onclick=()=>goBack('workspaces');
  const form=document.querySelector('#governance-form');
  if(form)form.onsubmit=async e=>{e.preventDefault();const f=new FormData(e.currentTarget),arr=k=>String(f.get(k)||'').split(',').map(x=>x.trim()).filter(Boolean);
    await api('/api/v1/workspaces/'+id+'/governance',{method:'PATCH',body:JSON.stringify({owner_department_id:f.get('owner_department_id'),owner_department_name:f.get('owner_department_name'),owner_biz_group_name:f.get('owner_biz_group_name'),administrators:arr('administrators'),reviewers:arr('reviewers')})});toast('治理配置已保存')}}

const MODEL_PRESETS=['qwen3.7-plus','qwen3.7-max','qwen3.6-plus','qwen3.6-flash','deepseek-v4-pro','deepseek-v4-flash','deepseek-v3.2','kimi-k2.7-code','kimi-k2.6','kimi-k2.5','glm-5.2','glm-5.1','glm-5','MiniMax-M2.5'];
async function models(editing){const d=await api('/api/v1/model-configs');
  const cur=editing?d.items.find(x=>x.id===editing):null;
  const f=cur||{name:'',provider:'openai_compatible',base_url:'',model_name:'',temperature:null,thinking_mode:'',timeout_seconds:60,enabled:false,version:'v1',has_key:false,api_key_masked:''};
  shell('模型配置',`评分规则 ${d.rule_version} · ${d.api_key_policy}`,`
  <div class="grid two-cols">
  <form class="card" id="model-form"><div class="card-head"><h2>${cur?'编辑：'+cur.name:'新建模型配置'}</h2>${cur?'<button type="button" class="secondary" id="cancel-edit">取消编辑</button>':''}</div>
    <div class="form-grid">
    <label class="form-field"><span class="field-label">配置名称</span><input class="input" name="name" required value="${f.name}" ${cur?'readonly':''} placeholder="knowledge-review-prod"></label>
    <label class="form-field"><span class="field-label">服务提供商</span><input class="input" name="provider" value="${f.provider}" list="providers"><datalist id="providers"><option value="openai_compatible"><option value="tokenplan"><option value="dashscope"></datalist></label>
    <label class="form-field full"><span class="field-label">API 基础地址（OpenAI 兼容）</span><input class="input" name="base_url" value="${f.base_url}" placeholder="https://token-plan.cn-beijing.maas.aliyuncs.com/compatible-mode/v1"></label>
    <label class="form-field"><span class="field-label">模型名称</span><input class="input" name="model_name" value="${f.model_name}" list="models"><datalist id="models">${MODEL_PRESETS.map(m=>`<option value="${m}">`).join('')}</datalist></label>
    <label class="form-field"><span class="field-label">API Key ${f.has_key?`<span class="mini-note">已保存 ${f.api_key_masked}，留空沿用</span>`:''}</span><input class="input" name="api_key" type="password" autocomplete="new-password" placeholder="${f.has_key?'留空保持不变':'sk-...'}"></label>
    <label class="form-field"><span class="field-label">温度（留空用模型默认）</span><input class="input" name="temperature" type="number" min="0" max="2" step="0.1" value="${f.temperature??''}"></label>
    <label class="form-field"><span class="field-label">思考模式</span><select class="input" name="thinking_mode"><option value="" ${!f.thinking_mode?'selected':''}>模型默认</option><option value="on" ${f.thinking_mode==='on'?'selected':''}>开启</option><option value="off" ${f.thinking_mode==='off'?'selected':''}>关闭</option></select></label>
    <label class="form-field"><span class="field-label">超时（秒）</span><input class="input" name="timeout_seconds" type="number" min="1" max="120" value="${f.timeout_seconds}"></label>
    <label class="form-field"><span class="field-label">版本标记</span><input class="input" name="version" value="${f.version}"></label>
    <label class="form-field"><span class="field-label">启用</span><select class="input" name="enabled"><option value="false" ${!f.enabled?'selected':''}>否（用规则引擎）</option><option value="true" ${f.enabled?'selected':''}>是（唯一启用）</option></select></label>
    </div>
    <p class="hint">评审正文仅在任务执行期间存在于内存/tmpfs，评完即释放；Key 只回显掩码。</p>
    <button class="primary">${cur?'保存修改':'创建配置'}</button></form>
  <section class="card"><h2>已保存配置</h2>${d.items.length?`<div class="table-wrap"><table class="data-table"><thead><tr><th>名称 / 模型</th><th class="hide-sm">参数</th><th>状态</th><th></th></tr></thead><tbody>
    ${d.items.map(x=>`<tr><td><b>${x.name}</b><br><small>${x.model_name||'—'} · ${x.base_url.replace('https://','').slice(0,28)}…</small></td><td class="hide-sm"><small>温度 ${x.temperature??'默认'} · 思考 ${x.thinking_mode==='on'?'开':x.thinking_mode==='off'?'关':'默认'} · Key ${x.has_key?x.api_key_masked:'ENV'}</small></td><td>${x.enabled?'<span class="badge pass">启用中</span>':'<span class="badge">停用</span>'}</td>
    <td><div class="controls"><button class="secondary" data-edit="${x.id}">编辑</button><button class="secondary" data-check="${x.id}">测试</button><button class="secondary" data-history="${x.id}">历史</button></div></td></tr>`).join('')}
  </tbody></table></div><div id="history-box" class="section-gap"></div>`:'<div class="empty">尚未配置模型，评分使用可审计的 V1.1 规则引擎。</div>'}</section></div>`);
  const form=document.querySelector('#model-form');
  form.onsubmit=async e=>{e.preventDefault();const fd=new FormData(form);
    const body={name:fd.get('name'),provider:fd.get('provider'),base_url:fd.get('base_url'),model_name:fd.get('model_name'),
      api_key:fd.get('api_key')||'',api_key_env_name:'KG_MODEL_API_KEY',
      temperature:fd.get('temperature')===''?null:Number(fd.get('temperature')),
      thinking_mode:fd.get('thinking_mode'),timeout_seconds:Number(fd.get('timeout_seconds')),
      enabled:fd.get('enabled')==='true',version:fd.get('version')};
    try{if(cur){await api('/api/v1/model-configs/'+cur.id,{method:'PUT',body:JSON.stringify(body)})}else{await api('/api/v1/model-configs',{method:'POST',body:JSON.stringify(body)})}
      toast('已保存');models()}catch(err){toast(err.message)}};
  const cancel=document.querySelector('#cancel-edit');if(cancel)cancel.onclick=()=>models();
  document.querySelectorAll('[data-edit]').forEach(b=>b.onclick=()=>models(Number(b.dataset.edit)));
  document.querySelectorAll('[data-check]').forEach(b=>b.onclick=async()=>{b.disabled=true;try{const r=await api('/api/v1/model-configs/'+b.dataset.check+'/connection-check',{method:'POST'});toast(r.status+'：'+r.message)}catch(e){toast(e.message)}b.disabled=false});
  document.querySelectorAll('[data-history]').forEach(b=>b.onclick=async()=>{const id=b.dataset.history;const h=await api('/api/v1/model-configs/'+id+'/history');
    document.querySelector('#history-box').innerHTML=`<h2>历史版本（配置 #${id}）</h2>${h.items.length?`<div class="table-wrap"><table class="data-table"><thead><tr><th>时间</th><th>动作</th><th>模型 / 参数</th><th></th></tr></thead><tbody>
      ${h.items.map(it=>`<tr><td><small>${(it.saved_at||'').replace('T',' ').slice(0,16)}</small></td><td><span class="badge">${{create:'创建',update:'修改前留档',rollback:'回滚后'}[it.action]||it.action}</span></td><td><small>${it.model_name} · 温度${it.temperature??'默认'} · 思考${it.thinking_mode||'默认'} · ${it.version} · Key ${it.api_key_masked||'ENV'}</small></td><td><button class="secondary" data-rb="${it.id}" data-cfg="${id}">回滚到此</button></td></tr>`).join('')}
    </tbody></table></div>`:'<p class="hint">暂无历史。</p>'}`;
    document.querySelectorAll('[data-rb]').forEach(r=>r.onclick=async()=>{if(!confirm('回滚到该历史版本？当前状态会先留档。'))return;
      await api('/api/v1/model-configs/'+r.dataset.cfg+'/rollback/'+r.dataset.rb,{method:'POST'});toast('已回滚');models()})})}

async function diagnostics(){const[d,n]=await Promise.all([api('/api/v1/diagnostics/connectivity'),api('/api/v1/notifications?limit=10').catch(()=>null)]);
  shell('连接诊断',d.body_storage,`
  <section class="card"><h2>外部接口状态</h2>${d.items.map(x=>`<div class="diagnostic"><span class="health ${x.status}"></span><div><b>${x.name}</b><br><small>${x.status}</small></div><span class="message">${x.message}</span></div>`).join('')}<div class="section-gap"><button class="primary" id="refresh">刷新诊断</button></div></section>
  ${n?`<section class="card section-gap"><div class="card-head"><h2>评审结果推送（机器人）</h2><span class="chip ${n.notify_enabled?'green':'gray'}">${n.notify_enabled?'已启用':'未启用（KG_NOTIFY_ENABLED）'}</span></div>
    <p class="hint">robotCode：${n.robot_code} · 不合格评审自动入队，worker 逐条发送并留痕。</p>
    <div class="controls"><input class="input" id="test-uid" placeholder="接收人 userId（如 01115324500438248944）" style="min-width:280px"><button class="secondary" id="test-send">发送测试消息</button></div>
    ${n.items.length?`<div class="table-wrap section-gap"><table class="data-table"><thead><tr><th>时间</th><th>标题</th><th>接收人</th><th>状态</th><th>错误码</th></tr></thead><tbody>${n.items.map(x=>`<tr><td><small>${(x.created_at||'').replace('T',' ').slice(0,19)}</small></td><td>${x.title||'—'}</td><td><small>${x.target_user_id||'—'}</small></td><td><span class="badge ${x.status==='sent'?'pass':x.status==='failed'?'return':''}">${x.status}</span></td><td><small>${x.error_code||'—'}</small></td></tr>`).join('')}</tbody></table></div>`:'<p class="hint section-gap">暂无推送记录。</p>'}</section>`:''}
  <section class="card section-gap"><div class="card-head"><h2>手动增量同步</h2></div>
    <p class="hint">调用钉钉接口全量拉取已登记知识库的文档元数据，需要企业应用 Wiki 读取权限。日常增量由 watcher 自动轮询发现，正常情况无需手动执行。</p>
    <button class="secondary" id="manual-sync">执行增量同步</button></section>
  <section class="card section-gap"><h2>上线前检查</h2>
    <p class="hint">1. 钉钉企业应用开通知识库读取与机器人发送权限，并发布版本；operatorId 使用数字员工 UnionID。</p>
    <p class="hint">2. 数字员工需为目标知识库成员；缺失的库在「知识库管理」的待授权清单中。</p>
    <p class="hint">3. bi_center 只读 Token 用于员工/部门归属映射（employeeKey=UnionID）。</p>
    <p class="hint">4. 仅在合规确认后配置正文临时获取网关与模型正文传输策略。</p></section>`);
  document.querySelector('#refresh').onclick=diagnostics;
  document.querySelector('#manual-sync').onclick=async()=>{if(!confirm('确认触发一次增量同步？日常增量已由 watcher 自动处理。'))return;
    toast('同步已提交…');try{const r=await api('/api/v1/sync-runs',{method:'POST'});toast(r.status==='succeeded'?'增量同步完成':'同步未成功：'+(r.error_code||'见上方接口状态'))}catch(e){toast(e.message)}};
  const send=document.querySelector('#test-send');
  if(send)send.onclick=async()=>{const uid=document.querySelector('#test-uid').value.trim();if(!uid)return toast('请填写 userId');
    try{await api('/api/v1/notifications/test',{method:'POST',body:JSON.stringify({user_id:uid})});toast('已发送，请在钉钉查收')}catch(e){toast(e.message)}}}

/* ---- 评分规则配置：全局默认 + 部门覆盖，参数级编辑 ---- */
function rcDimsHtml(cat,cfg,canEdit){const dis=canEdit?'':'disabled';
  return cat.map(dm=>{const dc=(cfg.dimensions||{})[dm.key]||{cap:dm.cap,rules:{}};
    return `<section class="card section-gap"><div class="card-head"><h2>${dm.label}</h2><span class="controls" style="gap:8px"><label class="rc-par">维度扣分上限<input class="input rc-num" type="number" min="0" max="100" data-cap="${dm.key}" value="${dc.cap}" ${dis}></label>${dm.document_only?'<span class="chip gray" title="表格类文件（sheet）不参与该维度评分">表格类不适用</span>':''}</span></div>
    <div class="table-wrap"><table class="data-table"><thead><tr><th style="width:52px">启用</th><th>规则</th><th class="num" style="width:92px">扣分</th><th class="num" style="width:92px">规则上限</th><th>检测参数</th></tr></thead><tbody>
    ${dm.rules.map(r=>{const rc=dc.rules[r.key]||{enabled:true,points:r.points,max:r.max,params:{}};
      return `<tr class="${rc.enabled?'':'rc-off'}"><td><input type="checkbox" data-rule="${dm.key}|${r.key}|enabled" ${rc.enabled?'checked':''} ${dis}></td>
      <td><b>${r.key}</b> ${r.label}${r.count_type?'<br><small style="color:#9ca3af">按出现次数累计扣分</small>':''}</td>
      <td class="num"><input class="input rc-num" type="number" min="0" max="100" data-rule="${dm.key}|${r.key}|points" value="${rc.points}" ${dis}></td>
      <td class="num">${r.count_type?`<input class="input rc-num" type="number" min="0" max="100" data-rule="${dm.key}|${r.key}|max" value="${rc.max??r.max}" ${dis}>`:'—'}</td>
      <td>${r.params.length?r.params.map(p=>`<label class="rc-par">${p.label}<input class="input rc-num" type="number" min="${p.min}" max="${p.max}" data-par="${dm.key}|${r.key}|${p.key}" value="${(rc.params||{})[p.key]??p.default}" ${dis}></label>`).join(' '):'—'}</td></tr>`}).join('')}
    </tbody></table></div></section>`}).join('')}

async function rules(){const d=await api('/api/v1/scoring-rules');
  const st=state.rc=state.rc||{dept:''};
  if(st.dept&&!d.departments.find(x=>x.department_name===st.dept))st.dept='';
  const perm=d.permissions,cur=st.dept?d.departments.find(x=>x.department_name===st.dept):d.global;
  const canEdit=perm.is_admin||(!!st.dept&&perm.editable_departments.includes(st.dept));
  const cfg=cur.config,dis=canEdit?'':'disabled';
  const chips=[`<button class="${st.dept?'secondary':'primary'}" data-rcscope="">全局默认</button>`]
    .concat(d.departments.map(x=>`<button class="${st.dept===x.department_name?'primary':'secondary'}" data-rcscope="${x.department_name}">${x.department_name}</button>`)).join('');
  const addBox=perm.is_admin?`<input class="input" id="rc-newdept" list="rc-cands" placeholder="部门名称（bi_center 口径）" style="width:190px"><datalist id="rc-cands">${d.department_candidates.filter(n=>!d.departments.find(x=>x.department_name===n)).map(n=>`<option value="${n}">`).join('')}</datalist><button class="secondary" id="rc-adddept">＋ 新建部门规则</button>`:'';
  const metaLine=cur.config_id?`当前版本 v${cur.version} · 最近由 ${cur.updated_by||'—'} 于 ${(cur.updated_at||'').replace('T',' ').slice(0,16)} 修改`:'尚未保存过：当前生效的是内置 V1.1 默认参数';
  const editorsBox=st.dept?`<div class="controls" style="flex-wrap:wrap;gap:8px;margin-top:10px"><span class="field-label">规则维护人</span>${perm.is_admin?`<input class="input" id="rc-editors" style="flex:1;min-width:260px" placeholder="unionId:姓名, unionId:姓名（逗号分隔）" value="${(cur.editors||[]).map(e=>e.union_id+(e.name?':'+e.name:'')).join(', ')}"><button class="secondary" id="rc-editors-save">保存维护人</button>`:`<span class="hint">${(cur.editors||[]).map(e=>e.name||e.union_id).join('、')||'未指定（仅全局管理员可修改本部门规则）'}</span>`}</div>`:'';
  shell('评分规则配置',d.match_note,`
  <section class="card"><div class="controls" style="flex-wrap:wrap;gap:8px">${chips}<span style="flex:1"></span>${addBox}</div></section>
  <section class="card section-gap"><div class="card-head"><h2>${st.dept?st.dept+' · 部门规则':'全局默认规则'}</h2><div class="controls" style="flex-wrap:wrap;gap:8px">
    ${canEdit?`<button class="secondary" id="rc-fill-default">填入内置默认</button>${st.dept?'<button class="secondary" id="rc-fill-global">填入全局当前值</button>':''}`:''}
    ${cur.config_id?'<button class="secondary" id="rc-history-btn">历史</button>':''}
    ${st.dept&&perm.is_admin?'<button class="secondary" id="rc-delete">删除部门配置</button>':''}
    ${canEdit?'<button class="primary" id="rc-save">保存</button>':'<span class="chip gray">只读</span>'}
  </div></div>
  <p class="hint">${metaLine}${st.dept?'':' · 未建独立规则的部门与未映射人员按此配置评分'}</p>
  <div class="controls" style="flex-wrap:wrap;gap:14px;margin-top:6px">
    <label class="rc-par">通过线（分数 ≥ 为通过）<input class="input rc-num" type="number" min="0" max="100" id="rc-pass" value="${cfg.pass_score}" ${dis}></label>
    <label class="rc-par">退回线（低于则退回，两线之间待人工审核）<input class="input rc-num" type="number" min="0" max="100" id="rc-return" value="${cfg.return_score}" ${dis}></label>
    <label class="rc-par">规则分权重%（与模型内容分合成）<input class="input rc-num" type="number" min="0" max="100" id="rc-weight" placeholder="默认 ${Math.round(d.settings_rule_weight*100)}" value="${cfg.rule_weight==null?'':Math.round(cfg.rule_weight*100)}" ${dis}></label>
  </div>${editorsBox}
  <div id="rc-history" class="section-gap"></div></section>
  <div id="rc-dims">${rcDimsHtml(d.catalog,cfg,canEdit)}</div>`);
  const bindOff=()=>document.querySelectorAll('#rc-dims input[type=checkbox][data-rule]').forEach(cb=>cb.onchange=()=>cb.closest('tr').classList.toggle('rc-off',!cb.checked));
  bindOff();
  document.querySelectorAll('[data-rcscope]').forEach(b=>b.onclick=()=>{st.dept=b.dataset.rcscope;rules()});
  const add=document.querySelector('#rc-adddept');
  if(add)add.onclick=async()=>{const name=document.querySelector('#rc-newdept').value.trim();if(!name)return toast('请填写部门名称');
    try{await api('/api/v1/scoring-rules/departments',{method:'POST',body:JSON.stringify({department_name:name})});st.dept=name;toast('已创建：初始值为全局当前参数');rules()}catch(e){toast(e.message)}};
  function rcCollect(){const wv=document.querySelector('#rc-weight').value.trim();
    const out={pass_score:+document.querySelector('#rc-pass').value,return_score:+document.querySelector('#rc-return').value,rule_weight:wv===''?null:+wv/100,dimensions:{}};
    document.querySelectorAll('[data-cap]').forEach(i=>out.dimensions[i.dataset.cap]={cap:+i.value,rules:{}});
    document.querySelectorAll('[data-rule]').forEach(i=>{const[dk,rk,f]=i.dataset.rule.split('|');const dim=out.dimensions[dk];if(!dim)return;const r=dim.rules[rk]=dim.rules[rk]||{};if(f==='enabled')r.enabled=i.checked;else r[f]=+i.value});
    document.querySelectorAll('[data-par]').forEach(i=>{const[dk,rk,pk]=i.dataset.par.split('|');const r=(out.dimensions[dk]||{rules:{}}).rules[rk];if(r)(r.params=r.params||{})[pk]=+i.value});
    return out}
  const save=document.querySelector('#rc-save');
  if(save)save.onclick=async()=>{try{const url=st.dept?'/api/v1/scoring-rules/departments/'+encodeURIComponent(st.dept):'/api/v1/scoring-rules/global';
    await api(url,{method:'PUT',body:JSON.stringify({config:rcCollect()})});toast('已保存，之后的评审按新参数执行');rules()}catch(e){toast(e.message)}};
  const fillFrom=g=>{document.querySelector('#rc-dims').innerHTML=rcDimsHtml(d.catalog,g,canEdit);bindOff();
    document.querySelector('#rc-pass').value=g.pass_score;document.querySelector('#rc-return').value=g.return_score;
    document.querySelector('#rc-weight').value=g.rule_weight==null?'':Math.round(g.rule_weight*100);toast('已填入，保存后生效')};
  const fd=document.querySelector('#rc-fill-default');if(fd)fd.onclick=()=>fillFrom(d.defaults);
  const fg=document.querySelector('#rc-fill-global');if(fg)fg.onclick=()=>fillFrom(d.global.config);
  const del=document.querySelector('#rc-delete');
  if(del)del.onclick=async()=>{if(!confirm(`删除「${st.dept}」的独立规则？该部门将回落使用全局默认。`))return;
    try{await api('/api/v1/scoring-rules/departments/'+encodeURIComponent(st.dept),{method:'DELETE'});toast('已删除');st.dept='';rules()}catch(e){toast(e.message)}};
  const ed=document.querySelector('#rc-editors-save');
  if(ed)ed.onclick=async()=>{const editors=document.querySelector('#rc-editors').value.split(/[,，;；]+/).map(s=>s.trim()).filter(Boolean).map(s=>{const[i,n]=s.split(/[:：]/);return{union_id:(i||'').trim(),name:(n||'').trim()}});
    try{await api('/api/v1/scoring-rules/departments/'+encodeURIComponent(st.dept)+'/editors',{method:'PUT',body:JSON.stringify({editors})});toast('维护人已更新');rules()}catch(e){toast(e.message)}};
  const hb=document.querySelector('#rc-history-btn');
  if(hb)hb.onclick=async()=>{const h=await api('/api/v1/scoring-rules/'+cur.config_id+'/history');
    document.querySelector('#rc-history').innerHTML=`<div class="table-wrap"><table class="data-table"><thead><tr><th>时间</th><th>动作</th><th>操作人</th><th class="num">版本</th><th></th></tr></thead><tbody>
    ${h.items.map(it=>`<tr><td><small>${(it.saved_at||'').replace('T',' ').slice(0,16)}</small></td><td><span class="badge">${{create:'创建',update:'修改前留档',rollback:'回滚后',delete:'删除前留档'}[it.action]||it.action}</span></td><td>${it.saved_by||'—'}</td><td class="num">v${it.version??'—'}</td><td>${canEdit?`<button class="secondary" data-rcrb="${it.id}">回滚到此</button>`:''}</td></tr>`).join('')||'<tr><td colspan="5"><div class="empty">暂无历史</div></td></tr>'}
    </tbody></table></div>`;
    document.querySelectorAll('[data-rcrb]').forEach(r=>r.onclick=async()=>{if(!confirm('回滚到该历史版本的规则参数？当前状态会先留档。'))return;
      try{await api('/api/v1/scoring-rules/'+cur.config_id+'/rollback/'+r.dataset.rcrb,{method:'POST'});toast('已回滚');rules()}catch(e){toast(e.message)}})}}

/* ---- auth gate ---- */
function renderLogin(){disposeCharts();document.body.classList.remove('sidebar-open');
  app.innerHTML=`<div class="login-wrap"><section class="card login-card"><div class="login-brand"><span class="brand-mark">知</span><div><b>钉钉知识库治理</b><br><small>入库可追溯 · 质量可评审 · 增量可统计</small></div></div>
  <p class="hint">使用钉钉账号登录后访问治理数据。</p>
  <button class="primary" id="ding-login" style="width:100%">钉钉扫码登录</button>
  <p class="hint" id="login-err"></p></section></div>`;
  document.querySelector('#ding-login').onclick=async()=>{try{
    const r=await fetch('api/auth/login-url?return_url='+encodeURIComponent(location.href)).then(x=>x.json());
    if(!r.login_url)throw new Error(r.detail?.message||'登录服务不可用');
    location.href=r.login_url}catch(e){document.querySelector('#login-err').textContent=e.message}}}
function renderUser(u){const box=document.querySelector('#userBox');if(!box)return;
  if(!u||!state.authEnabled){box.innerHTML='<span class="avatar">管</span>';return}
  box.innerHTML=`<span class="avatar" title="${u.union_id}">${(u.name||'员')[0]}</span><span class="user-name">${u.name||''}</span><button class="secondary" id="logout" style="padding:5px 10px;font-size:12px">退出</button>`;
  document.querySelector('#logout').onclick=async()=>{await fetch('api/auth/logout',{method:'POST'});location.reload()}}

/* ---- hash router：#/视图、#/doc/:id、#/ws/:id。浏览器前进/后退可用，
   详情页"返回"走 history.back() 回到真实来源页。 ---- */
const views={overview,increments,documents,reviews,rules,workspaces,models,diagnostics};
function routeOf(hash){const h=(hash||'').replace(/^#\/?/,'');const i=h.indexOf('/');
  const head=i<0?h:h.slice(0,i),rest=i<0?'':decodeURIComponent(h.slice(i+1));
  if(head==='doc'&&rest)return{view:'documents',run:()=>documentDetail(rest)};
  if(head==='ws'&&rest)return{view:'workspaces',run:()=>workspaceDetail(rest)};
  if(views[head])return{view:head,run:views[head]};
  return{view:'overview',run:overview}}
function render(){const r=routeOf(location.hash);state.view=r.view;document.body.classList.remove('sidebar-open');
  document.querySelectorAll('.nav').forEach(b=>b.classList.toggle('active',b.dataset.view===r.view));
  r.run().catch(e=>{if(app.querySelector('.login-card'))return;disposeCharts();app.innerHTML=`<section class="card"><h2>加载失败</h2><p class="hint">${e.message}</p><button class="secondary" onclick="location.reload()">重试</button></section>`})}
function navigate(view){const h='#/'+view;if(location.hash===h){render()}else{location.hash=h}}
const goDoc=id=>{location.hash='#/doc/'+encodeURIComponent(id)};
const goWs=id=>{location.hash='#/ws/'+encodeURIComponent(id)};
function goBack(fallback){if(state.depth>0){history.back()}else{navigate(fallback)}}
window.addEventListener('hashchange',()=>{state.depth++;render()});
document.querySelectorAll('.nav').forEach(b=>b.onclick=()=>navigate(b.dataset.view));
document.querySelector('#menuBtn').onclick=()=>document.body.classList.toggle('sidebar-open');
document.querySelector('#backdrop').onclick=()=>document.body.classList.remove('sidebar-open');
(async()=>{let authed=true;
  try{
    const r=await fetch('api/auth/me');const d=await r.json().catch(()=>({}));
    if(r.status===401){state.authEnabled=true;renderLogin();authed=false}
    else{state.authEnabled=!!d.auth_enabled;renderUser(d.user)}
  }catch(e){}
  if(!authed)return;
  if(!location.hash)history.replaceState(null,'','#/overview');
  render()})();
