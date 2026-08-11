/* Relative URLs keep the app working both at / (tunnel/dev) and behind the
   /knowledge_governance/ prefix, where nginx strips the prefix. */
const api=(url,options={})=>fetch(url.replace(/^\//,''),{headers:{'Content-Type':'application/json'},...options}).then(async r=>{const data=await r.json().catch(()=>({}));
  if(r.status===401){renderLogin();throw new Error(data.detail?.message||'请先登录')}
  if(!r.ok)throw new Error(data.detail?.message||data.detail||'请求失败');return data});
const app=document.querySelector('#app');
const state={view:'overview',coverageNames:{}};
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
  const c=echarts.init(el);charts.set(id,c);c.setOption(option);requestAnimationFrame(()=>{if(!c.isDisposed())c.resize()})}
function stackedOption(rows,{zoom=false}={}){return{
  tooltip:{trigger:'axis',axisPointer:{type:'shadow'},formatter:ps=>{const m=ps[0].axisValue;const r=ps.find(p=>p.seriesName==='日常新增')?.value||0;const b=ps.find(p=>p.seriesName==='批量导入')?.value||0;return `<b>${m}</b><br/>全量新增：<b>${nf(r+b)}</b><br/>日常新增：${nf(r)}<br/>批量导入：${nf(b)}`}},
  legend:{data:['日常新增','批量导入'],top:0,itemWidth:14,itemHeight:9,textStyle:{fontSize:12,color:'#6b7280'}},
  grid:{left:8,right:8,top:34,bottom:zoom?46:8,containLabel:true},
  xAxis:{type:'category',data:rows.map(r=>r.month),axisTick:{show:false},axisLine:{lineStyle:{color:'#e5e7eb'}},axisLabel:{color:'#6b7280',fontSize:11}},
  yAxis:{type:'value',splitLine:{lineStyle:{color:'#f3f4f6'}},axisLabel:{color:'#6b7280',fontSize:11}},
  dataZoom:zoom?[{type:'slider',height:18,bottom:6,brushSelect:false}]:undefined,
  series:[
    {name:'日常新增',type:'bar',stack:'total',barMaxWidth:28,data:rows.map(r=>r.routine),itemStyle:{color:COLOR.routine}},
    {name:'批量导入',type:'bar',stack:'total',barMaxWidth:28,data:rows.map(r=>r.bulk_import),itemStyle:{color:COLOR.bulk,borderRadius:[3,3,0,0]}},
  ]}}

/* ---- scaffolding ---- */
function shell(title,sub,content,actions=''){disposeCharts();app.innerHTML=`<section class="page-head"><div><h1>${title}</h1><p class="sub">${sub}</p></div><div class="page-actions">${actions}</div></section>${content}`}
function statCard(label,value,note){return `<article class="card"><div class="metric-label">${label}</div><div class="metric-value">${value}</div><div class="metric-note">${note||''}</div></article>`}
function statusChip(item){if(item.status==='scanned')return '<span class="chip green">已扫描</span>';if(item.status==='empty')return '<span class="chip gray">空库</span>';if(item.status==='excluded')return `<span class="chip amber" title="${item.excluded_reason}">已排除</span>`;return '<span class="chip">未知</span>'}
function docTable(rows){if(!rows.length)return '<div class="empty">暂无已同步文档。增量同步依赖钉钉企业应用凭据（Wiki 读权限）；基线统计请见「增量报表」。</div>';
  return `<div class="table-wrap"><table class="data-table"><thead><tr><th>文档名称 / 节点</th><th>上传人</th><th>归属部门 / 业务组</th><th class="num">AI评审分</th><th class="num">重评</th><th>入库时间</th><th>状态</th></tr></thead><tbody>${rows.map(d=>{const r=d.latest_review||{};return `<tr class="rowlink" data-doc="${d.node_id}"><td><b>${d.name}</b><br><small>${d.node_id}</small></td><td>${fmt(d.uploader_name)}</td><td>${fmt(d.department_name)}<br><small>${fmt(d.biz_group_name)}</small></td><td class="num score ${scoreClass(r.ai_score||0)}">${r.ai_score??'—'}</td><td class="num">${d.rerun_count||0}</td><td>${fmt((d.source_created_at||'').slice(0,10))}</td><td>${r.verdict?`<span class="badge ${r.verdict}">${verdictText(r.verdict)}</span>`:'<span class="badge">未评审</span>'}</td></tr>`}).join('')}</tbody></table></div>`}
function bindDocRows(){document.querySelectorAll('[data-doc]').forEach(x=>x.onclick=()=>documentDetail(x.dataset.doc))}

/* ---- views ---- */
async function overview(){const d=await api('/api/v1/dashboard/overview');
  const cs=d.coverage_summary,org=d.org_context||{};
  document.querySelector('#scopePill').textContent=`覆盖 ${cs.scanned}/${cs.visible_workspaces}/${org.org_total_knowledge_bases||'—'} 库`;
  const years=Object.keys(d.yearly||{}).sort();
  const yearChips=years.map(y=>`<span class="chip blue">${y} 全量 ${nf(d.yearly[y].total)} · 日常 ${nf(d.yearly[y].routine)}</span>`).join('');
  shell('治理概览','基线快照与增量同步合并去重；月份按钉钉 createTime（Asia/Shanghai）归属。',`
  ${org.note?`<div class="banner">⚠ ${org.note}</div>`:''}
  <div class="grid metrics">
    ${statCard('文件总量（去重）',nf(d.metrics.total_files),'基线 + 增量同步合并')}
    ${statCard('本月新增',nf(d.metrics.month_increment),'按 createTime 归属当月')}
    ${statCard('可见知识库',d.metrics.workspace_count,`已扫描 ${cs.scanned} · 空库 ${cs.empty} · 排除 ${cs.excluded}`)}
    ${statCard('平均 AI 评审分',d.metrics.average_ai_score??'—',d.metrics.average_ai_score==null?'评审尚未开始':'全部评审实例均值')}
  </div>
  <section class="card section-gap"><div class="card-head"><h2>月度新增趋势（近 14 个月）</h2><div class="controls">${yearChips}</div></div><div id="trendChart" class="chart"></div><div class="legend-note"><span><span class="dot" style="background:${COLOR.routine}"></span>日常新增</span><span><span class="dot" style="background:${COLOR.bulk}"></span>批量导入（系统迁移/同步，同样计入总量）</span><button class="secondary" id="go-increments" style="margin-left:auto">查看完整报表</button></div></section>
  <div class="grid two-cols section-gap">
    <section class="card"><div class="card-head"><h2>最近发现文档</h2><button class="secondary" id="go-docs">全部文档</button></div>${docTable(d.latest_documents)}</section>
    <section class="card"><h2>口径与数据边界</h2>
      <p class="hint">• 下限口径：扫描前已删除的文件不可观测。</p>
      <p class="hint">• 覆盖范围：仅当前授权可见的知识库，不能表述为全公司。</p>
      <p class="hint">• createTime 为知识库入库时间，非原文件创建时间。</p>
      <p class="hint">• 批量导入不扣减：仅拆分构成，供分析口径使用。</p>
      <button class="secondary" id="go-diagnostics">查看连接诊断</button></section>
  </div>`);
  renderChart('trendChart',stackedOption(d.monthly));
  bindDocRows();
  document.querySelector('#go-docs').onclick=()=>navigate('documents');
  document.querySelector('#go-increments').onclick=()=>navigate('increments');
  document.querySelector('#go-diagnostics').onclick=()=>navigate('diagnostics')}

async function increments(year){const q=year?`?year=${year}`:'';const d=await api('/api/v1/metrics/monthly-increments'+q);
  if(!Object.keys(state.coverageNames).length){try{const c=await api('/api/v1/metrics/coverage');c.items.forEach(i=>state.coverageNames[i.workspace_id]=i.name)}catch(e){}}
  const years=Object.keys(d.yearly).sort();
  const sum=k=>d.rows.reduce((a,r)=>a+r[k],0);
  const opts=['<option value="">全部年份</option>'].concat(years.map(y=>`<option value="${y}" ${y===year?'selected':''}>${y} 年</option>`)).join('');
  shell('增量报表',d.metric_note,`
  <div class="grid metrics">
    ${statCard('全量新增',nf(sum('total')),year?`${year} 年合计`:'全部观测月份合计')}
    ${statCard('其中批量导入',nf(sum('bulk_import')),d.bulk_day_rule)}
    ${statCard('其中日常新增',nf(sum('routine')),'全量 − 批量导入')}
    ${statCard('基线快照',d.baseline.snapshot_id?d.baseline.snapshot_id.replace('wiki-baseline-',''):'—',`${nf(d.total_files)} 个去重文件节点`)}
  </div>
  <section class="card section-gap"><div class="card-head"><h2>月度构成</h2></div><div id="incChart" class="chart tall"></div></section>
  <section class="card section-gap"><div class="card-head"><h2>月度明细</h2><span class="hint">批量日：${d.bulk_day_rule}</span></div><div class="table-wrap"><table class="data-table"><thead><tr><th>月份</th><th class="num">全量新增</th><th class="num">批量导入</th><th class="num">日常新增</th><th>批量日</th></tr></thead><tbody>
    ${d.rows.map(r=>`<tr><td><b>${r.month}</b></td><td class="num"><b>${nf(r.total)}</b></td><td class="num">${r.bulk_import?nf(r.bulk_import):'—'}</td><td class="num">${nf(r.routine)}</td><td>${r.bulk_days.map(b=>`<span class="chip amber" title="${(b.workspace_ids||[]).map(w=>state.coverageNames[w]||w).join('、')}">${b.day.slice(8)}日 ${nf(b.files)}（${Math.round(b.share_of_month*100)}%）</span>`).join(' ')||'—'}</td></tr>`).join('')}
  </tbody></table></div></section>
  <section class="card section-gap"><h2>年度合计</h2><div class="table-wrap"><table class="data-table"><thead><tr><th>年份</th><th class="num">全量新增</th><th class="num">批量导入</th><th class="num">日常新增</th></tr></thead><tbody>
    ${years.map(y=>`<tr><td><b>${y}</b></td><td class="num"><b>${nf(d.yearly[y].total)}</b></td><td class="num">${nf(d.yearly[y].bulk_import)}</td><td class="num">${nf(d.yearly[y].routine)}</td></tr>`).join('')}
  </tbody></table></div>${d.caveats.map(c=>`<p class="hint">• ${c}</p>`).join('')}</section>`,
  `<select class="input" id="yearSel">${opts}</select><button class="secondary" id="csvBtn">导出 CSV</button>`);
  renderChart('incChart',stackedOption(d.rows,{zoom:d.rows.length>18}));
  document.querySelector('#yearSel').onchange=e=>increments(e.target.value);
  document.querySelector('#csvBtn').onclick=()=>{const lines=['月份,全量新增,批量导入,日常新增,批量日'].concat(d.rows.map(r=>`${r.month},${r.total},${r.bulk_import},${r.routine},"${r.bulk_days.map(b=>`${b.day}:${b.files}`).join(';')}"`));
    const blob=new Blob(['﻿'+lines.join('\n')],{type:'text/csv;charset=utf-8'});const a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download=`知识库月度增量${year?'-'+year:''}.csv`;a.click();URL.revokeObjectURL(a.href)}}

function blTable(d){if(!d.items.length)return '<div class="empty">未找到匹配文件。</div>';
  return `<div class="table-wrap"><table class="data-table"><thead><tr><th>文件名</th><th class="hide-sm">类型</th><th>入库时间</th><th></th></tr></thead><tbody>${d.items.map(f=>`<tr><td><b>${f.name}</b><br><small>${(f.created_at||'').slice(0,10)}</small></td><td class="hide-sm">${f.extension||'—'}</td><td>${(f.created_at||'').slice(0,10)}</td><td>${f.url?`<a class="link-btn" href="${f.url}" target="_blank" rel="noopener">打开 ↗</a>`:'—'}</td></tr>`).join('')}</tbody></table></div>
  <div class="controls section-gap"><span class="hint">共 ${nf(d.total)} 个 · 第 ${Math.floor(d.offset/d.limit)+1} / ${Math.max(1,Math.ceil(d.total/d.limit))} 页</span><button class="secondary" id="bl-prev" ${d.offset<=0?'disabled':''}>上一页</button><button class="secondary" id="bl-next" ${d.offset+d.limit>=d.total?'disabled':''}>下一页</button></div>`}
async function blSearch(){const p=state.bl;const qs=new URLSearchParams({query:p.query||'',workspace_id:p.ws||'',folder:p.folder||'',offset:p.offset,limit:50});
  const d=await api('/api/v1/baseline/files?'+qs);const box=document.querySelector('#bl-results');box.innerHTML=blTable(d);
  const prev=document.querySelector('#bl-prev'),next=document.querySelector('#bl-next');
  if(prev)prev.onclick=()=>{p.offset=Math.max(0,p.offset-50);blSearch()};
  if(next)next.onclick=()=>{p.offset+=50;blSearch()}}
async function uploaders(){const meta=await api('/api/v1/metrics/uploaders/months');
  if(!meta.months.length){shell('数据看板','按人 / 部门 / 业务组多维统计知识库上传。',`<section class="card"><div class="empty">带创建人的扫描尚未完成。</div></section>`);return}
  const st=state.dash=state.dash||{year:meta.months[meta.months.length-1].month.slice(0,4),month:'',excl:true,dept:'',group:'',person:null};
  const org=await api('/api/v1/metrics/org?'+new URLSearchParams({year:st.month?'':st.year,month:st.month}));
  const years=Object.keys(meta.yearly).sort();
  const monthsOfYear=meta.months.filter(m=>m.month.startsWith(st.year));
  const visibleOrg=org.items.filter(d=>!st.excl||!['系统/机器人','未映射'].includes(d.department_name));
  const periodDelta=org.items.reduce((a,d)=>a+d.delta,0);
  const periodHuman=org.items.filter(d=>!['系统/机器人','未映射'].includes(d.department_name)).reduce((a,d)=>a+d.delta,0);
  const periodLabel=st.month||st.year+' 全年';
  const yearPills=years.map(y=>`<button class="pill ${(st.month?st.month.startsWith(y):y===st.year)?'active':''}" data-year="${y}">${y}</button>`).join('');
  const monthOpts=['<option value="">全年</option>'].concat(monthsOfYear.map(m=>`<option value="${m.month}" ${m.month===st.month?'selected':''}>${m.month.slice(5)}月（${nf(m.total)}）</option>`)).join('');
  let level='dept',rows=visibleOrg,currentDept=null,currentGroup=null;
  if(st.dept){currentDept=org.items.find(d=>d.department_name===st.dept);if(currentDept){level='group';rows=currentDept.groups;
    if(st.group){currentGroup=currentDept.groups.find(g=>g.biz_group_name===st.group);if(currentGroup){level='person';rows=currentGroup.people}}}}
  const crumbs=`<div class="crumbs"><button class="crumb ${level==='dept'?'current':''}" data-crumb="root">全部部门</button>${st.dept?`<span class="sep">›</span><button class="crumb ${level==='group'?'current':''}" data-crumb="dept">${st.dept}</button>`:''}${st.group?`<span class="sep">›</span><button class="crumb current" data-crumb="group">${st.group}</button>`:''}</div>`;
  const orgTable=level==='dept'
    ?`<table class="data-table"><thead><tr><th>部门</th><th class="num">保有量</th><th class="num">${periodLabel}增量</th><th class="num hide-sm">上传人数</th></tr></thead><tbody>${rows.map(d=>`<tr class="rowlink" data-drill-dept="${d.department_name}"><td><b>${d.department_name}</b>${d.is_robot?'<span class="row-tag">系统</span>':''}</td><td class="num">${nf(d.stock)}</td><td class="num ${d.delta?'delta-pos':'delta-zero'}">${d.delta?'+'+nf(d.delta):'0'}</td><td class="num hide-sm">${d.uploaders}</td></tr>`).join('')}</tbody></table>`
    :level==='group'
    ?`<table class="data-table"><thead><tr><th>业务组</th><th class="num">保有量</th><th class="num">${periodLabel}增量</th><th class="num hide-sm">上传人数</th></tr></thead><tbody>${rows.map(g=>`<tr class="rowlink" data-drill-group="${g.biz_group_name}"><td><b>${g.biz_group_name}</b></td><td class="num">${nf(g.stock)}</td><td class="num ${g.delta?'delta-pos':'delta-zero'}">${g.delta?'+'+nf(g.delta):'0'}</td><td class="num hide-sm">${g.uploaders}</td></tr>`).join('')}</tbody></table>`
    :`<table class="data-table"><thead><tr><th>成员</th><th class="num">保有量</th><th class="num">${periodLabel}增量</th></tr></thead><tbody>${rows.map(pp=>`<tr class="rowlink" data-drill-person="${pp.user_id}"><td><b>${pp.name}</b>${pp.matched?'':'<span class="row-tag">未映射</span>'}</td><td class="num">${nf(pp.stock)}</td><td class="num ${pp.delta?'delta-pos':'delta-zero'}">${pp.delta?'+'+nf(pp.delta):'0'}</td></tr>`).join('')}</tbody></table>`;
  shell('数据看板',`快照 ${meta.snapshot_id} · 全量口径 · 保有量为累计，增量为所选期间`,`
  <div class="filter-card">${yearPills}<select class="input" id="db-month">${monthOpts}</select><label class="chip" style="cursor:pointer"><input type="checkbox" id="db-excl" ${st.excl?'checked':''} style="margin-right:4px">排除系统/未映射</label><span class="hint hide-sm" style="margin-left:auto">部门 → 业务组 → 成员逐级下钻；点趋势柱可切月</span></div>
  <div class="grid metrics">
    ${['2025','2026'].map(y=>`<article class="card kpi ${y==='2026'?'green':''}"><div class="metric-label">${y} 年全量增长</div><div class="metric-value">${nf(meta.yearly[y]||0)}</div><div class="metric-note">含系统导入</div></article>`).join('')}
    <article class="card kpi amber"><div class="metric-label">${periodLabel}新增</div><div class="metric-value">${nf(periodDelta)}</div><div class="metric-note">其中人工 ${nf(periodHuman)}</div></article>
    <article class="card kpi gray"><div class="metric-label">文件总保有量</div><div class="metric-value">${nf(meta.total_files)}</div><div class="metric-note">${meta.workspace_count} 库 · ${meta.uploader_count} 位上传人</div></article>
  </div>
  <section class="card section-gap"><div class="card-head"><h2>${st.year} 年月度上传趋势</h2><span class="hint">点击柱形筛选该月</span></div><div id="dbTrend" class="chart"></div></section>
  <div class="grid two-cols section-gap">
    <section class="card"><div class="card-head">${crumbs}</div><div class="table-wrap" style="max-height:430px;overflow-y:auto">${orgTable}</div></section>
    <section class="card person-panel" id="personPanel">${st.person?'<div class="empty">加载中…</div>':'<h2>成员明细</h2><div class="empty">在左侧下钻到成员并点击，查看其每日上传与知识库分布。</div>'}</section>
  </div>`);
  renderChart('dbTrend',{tooltip:{trigger:'axis',axisPointer:{type:'shadow'}},grid:{left:8,right:8,top:16,bottom:8,containLabel:true},
    xAxis:{type:'category',data:monthsOfYear.map(m=>m.month.slice(5)+'月'),axisTick:{show:false},axisLabel:{color:'#6b7280',fontSize:11}},
    yAxis:{type:'value',splitLine:{lineStyle:{color:'#f3f4f6'}},axisLabel:{color:'#6b7280',fontSize:11}},
    series:[{type:'bar',barMaxWidth:30,data:monthsOfYear.map(m=>({value:m.total,itemStyle:{color:m.month===st.month?'#1d4ed8':'#2563eb',borderRadius:[3,3,0,0]}})),cursor:'pointer'}]});
  const trendChart=charts.get('dbTrend');
  if(trendChart){trendChart.off('click');trendChart.on('click',pt=>{const m=monthsOfYear[pt.dataIndex]?.month;st.month=(st.month===m?'':m);st.person=null;uploaders()})}
  document.querySelectorAll('[data-year]').forEach(b=>b.onclick=()=>{st.year=b.dataset.year;st.month='';st.dept='';st.group='';st.person=null;uploaders()});
  document.querySelector('#db-month').onchange=e=>{st.month=e.target.value;st.person=null;uploaders()};
  document.querySelector('#db-excl').onchange=e=>{st.excl=e.target.checked;uploaders()};
  document.querySelectorAll('[data-crumb]').forEach(b=>b.onclick=()=>{const c=b.dataset.crumb;if(c==='root'){st.dept='';st.group=''}else if(c==='dept'){st.group=''}st.person=null;uploaders()});
  document.querySelectorAll('[data-drill-dept]').forEach(r=>r.onclick=()=>{st.dept=r.dataset.drillDept;st.group='';st.person=null;uploaders()});
  document.querySelectorAll('[data-drill-group]').forEach(r=>r.onclick=()=>{st.group=r.dataset.drillGroup;st.person=null;uploaders()});
  document.querySelectorAll('[data-drill-person]').forEach(r=>r.onclick=()=>{st.person=r.dataset.drillPerson;loadPerson(st)});
  if(st.person)loadPerson(st)}

async function loadPerson(st){const panel=document.querySelector('#personPanel');if(!panel)return;
  const d=await api('/api/v1/metrics/uploaders/'+st.person+'/breakdown?'+new URLSearchParams({year:st.month?'':st.year,month:st.month}));
  const label=st.month?st.month:st.year+' 全年';
  const series=st.month?d.days.map(x=>({k:x.day.slice(8)+'日',v:x.count})):d.months.filter(m=>m.month.startsWith(st.year)).map(x=>({k:x.month.slice(5)+'月',v:x.count}));
  panel.innerHTML=`<div class="card-head" style="margin-bottom:6px"><h2 style="margin:0">${d.name||d.user_id}</h2><span class="chip ${d.matched?'blue':'amber'}">${d.department_name}${d.biz_group_name&&d.biz_group_name!==d.department_name?' · '+d.biz_group_name:''}</span></div>
  <p class="hint">${label}上传 <b>${nf(d.period_total)}</b> 篇 · 历史累计 ${nf(d.all_total)} 篇</p>
  <div id="personChart" class="chart" style="height:190px"></div>
  <p class="hint" style="margin-top:8px">知识库分布（${label}）：</p>
  <div>${d.workspaces.map(w=>`<span class="ws-chip">${w.name} <b>${nf(w.files)}</b></span>`).join('')||'<span class="hint">该期间无上传</span>'}</div>`;
  const el=document.getElementById('personChart');
  if(el&&window.echarts){const c=echarts.init(el);charts.set('personChart',c);c.setOption({tooltip:{trigger:'axis'},grid:{left:6,right:6,top:10,bottom:4,containLabel:true},
    xAxis:{type:'category',data:series.map(x=>x.k),axisTick:{show:false},axisLabel:{color:'#6b7280',fontSize:10,interval:series.length>15?'auto':0}},
    yAxis:{type:'value',splitLine:{lineStyle:{color:'#f3f4f6'}},axisLabel:{color:'#6b7280',fontSize:10}},
    series:[{type:'bar',barMaxWidth:16,data:series.map(x=>x.v),itemStyle:{color:'#2563eb',borderRadius:[3,3,0,0]}}]});requestAnimationFrame(()=>{if(!c.isDisposed())c.resize()})}}

async function documents(){const d=await api('/api/v1/documents');
  if(!Object.keys(state.coverageNames).length){try{const c=await api('/api/v1/metrics/coverage');c.items.forEach(i=>state.coverageNames[i.workspace_id]=i.name)}catch(e){}}
  state.bl={query:'',ws:'',offset:0};
  const wsOpts=['<option value="">全部知识库</option>'].concat(Object.entries(state.coverageNames).map(([k,v])=>`<option value="${k}">${v}</option>`)).join('');
  shell('文档治理','基线文件可直接检索；实时同步文档在凭据开通后自动进入评审流程。',`
  <section class="card"><div class="card-head"><h2>基线文件检索（53,908 个，2026-08-05 快照）</h2><div class="controls"><select class="input" id="bl-ws">${wsOpts}</select><input class="input" id="bl-q" placeholder="按文件名搜索"><button class="secondary" id="bl-btn">查询</button></div></div><div id="bl-results"><div class="empty">输入关键字或选择知识库开始检索。</div></div></section>
  <section class="card section-gap"><div class="card-head"><div class="controls"><input class="input" id="search" placeholder="搜索已同步文档"><button class="secondary" id="search-btn">查询</button></div><button class="primary" id="sync" title="需要钉钉凭据权限开通后使用">执行增量同步</button></div><div id="document-table">${docTable(d.items)}</div></section>`);
  bindDocRows();
  document.querySelector('#bl-btn').onclick=()=>{state.bl.query=document.querySelector('#bl-q').value;state.bl.ws=document.querySelector('#bl-ws').value;state.bl.offset=0;blSearch()};
  document.querySelector('#bl-q').onkeydown=e=>{if(e.key==='Enter')document.querySelector('#bl-btn').click()};
  document.querySelector('#search-btn').onclick=async()=>{const q=document.querySelector('#search').value;const r=await api('/api/v1/documents?query='+encodeURIComponent(q));document.querySelector('#document-table').innerHTML=docTable(r.items);bindDocRows()};
  document.querySelector('#sync').onclick=async()=>{toast('同步已提交…');try{const r=await api('/api/v1/sync-runs',{method:'POST'});toast(r.status==='succeeded'?'增量同步完成':'同步未成功：'+(r.error_code||'请看连接诊断'));if(r.status==='succeeded')documents()}catch(e){toast(e.message)}}}

async function reviews(state_={verdict:'',query:'',offset:0}){const qs=new URLSearchParams({verdict:state_.verdict,query:state_.query,offset:state_.offset,limit:50});
  const d=await api('/api/v1/reviews?'+qs);
  shell('评审记录','全部 AI 评审实例；实例不可变，重评产生新记录。分数为建议，最终结论由审核员保存。',`
  <section class="card"><div class="card-head"><div class="controls">
    <select class="input" id="rv-verdict"><option value="">全部结论</option><option value="pass" ${state_.verdict==='pass'?'selected':''}>通过</option><option value="manual_review" ${state_.verdict==='manual_review'?'selected':''}>待人工审核</option><option value="return" ${state_.verdict==='return'?'selected':''}>退回</option></select>
    <input class="input" id="rv-q" placeholder="按文档名搜索" value="${state_.query}"><button class="secondary" id="rv-btn">查询</button></div>
    <span class="hint">共 ${nf(d.total)} 条</span></div>
  ${d.items.length?`<div class="table-wrap"><table class="data-table"><thead><tr><th>文档</th><th>上传人 / 部门</th><th class="num">AI评分</th><th>结论</th><th>范围</th><th>触发</th><th>时间</th></tr></thead><tbody>
    ${d.items.map(x=>`<tr class="rowlink" data-doc="${x.node_id}"><td><b>${x.document_name}</b><br><small>${x.review_instance_id.slice(0,8)}</small></td><td>${fmt(x.uploader_name)}<br><small>${fmt(x.department_name)}</small></td><td class="num score ${scoreClass(x.ai_score)}">${x.ai_score}</td><td><span class="badge ${x.verdict}">${verdictText(x.verdict)}</span></td><td>${x.review_scope==='full_content'?'完整正文':'元数据'}</td><td>${x.trigger}</td><td><small>${(x.created_at||'').replace('T',' ').slice(0,16)}</small></td></tr>`).join('')}
  </tbody></table></div>
  <div class="controls section-gap"><span class="hint">第 ${Math.floor(d.offset/50)+1} / ${Math.max(1,Math.ceil(d.total/50))} 页</span><button class="secondary" id="rv-prev" ${d.offset<=0?'disabled':''}>上一页</button><button class="secondary" id="rv-next" ${d.offset+50>=d.total?'disabled':''}>下一页</button></div>`
  :'<div class="empty">暂无评审记录。评审在增量同步发现新文档、或文档详情页手动触发后产生。</div>'}
  </section>`);
  bindDocRows();
  document.querySelector('#rv-btn').onclick=()=>reviews({verdict:document.querySelector('#rv-verdict').value,query:document.querySelector('#rv-q').value,offset:0});
  document.querySelector('#rv-verdict').onchange=()=>document.querySelector('#rv-btn').click();
  const pv=document.querySelector('#rv-prev'),nx=document.querySelector('#rv-next');
  if(pv)pv.onclick=()=>reviews({...state_,offset:Math.max(0,state_.offset-50)});
  if(nx)nx.onclick=()=>reviews({...state_,offset:state_.offset+50})}

async function documentDetail(id){const d=await api('/api/v1/documents/'+id);const r=d.latest_review;const dims=r?Object.values(r.dimensions):[];
  shell('评审详情','AI 分数为建议；最终结论由知识库审核员保存。',`
  <section class="card"><div class="detail-header"><div class="document-icon">▤</div><div><h2 style="margin:0">${d.name}</h2><p class="sub">节点 ${d.node_id} · 知识库 ${state.coverageNames[d.workspace_id]||d.workspace_id}</p><div class="detail-meta"><span>上传人：${fmt(d.uploader_name)}</span><span>入库时间：${fmt(d.source_created_at)}</span><span>归属：${fmt(d.department_name)} / ${fmt(d.biz_group_name)}</span><span>重评次数：${d.rerun_count}</span></div></div><div class="big-score">${r?.ai_score??'—'}<span>/100</span><br><small>${r?verdictText(r.verdict):'未评审'}</small></div></div></section>
  <section class="grid deductions section-gap">${dims.map(x=>`<article class="card deduction"><div class="field-label">${x.label}</div><div class="number">-${x.deduction}<small> / ${x.cap}</small></div><ul>${x.findings.length?x.findings.map(f=>`<li>${f.message}</li>`).join(''):'<li>未发现扣分项</li>'}</ul></article>`).join('')||'<div class="card muted">暂无评审维度数据</div>'}</section>
  <section class="card section-gap"><h2>评审记录</h2>${d.reviews.length?`<div class="table-wrap"><table class="data-table"><thead><tr><th>实例 ID</th><th class="num">分数</th><th>范围</th><th>触发</th><th>规则版本</th><th>时间</th></tr></thead><tbody>${d.reviews.map(x=>`<tr><td><small>${x.review_instance_id}</small></td><td class="num score ${scoreClass(x.ai_score)}">${x.ai_score}</td><td>${x.review_scope==='full_content'?'完整正文':'元数据合规'}</td><td>${x.trigger}</td><td>${x.rule_version}</td><td>${(x.created_at||'').replace('T',' ').slice(0,19)}</td></tr>`).join('')}</tbody></table></div>`:'<p class="hint">暂无记录</p>'}
  <div class="section-gap"><button class="primary" id="rerun">重新评审</button><button class="secondary" id="back" style="margin-left:8px">返回列表</button></div></section>`);
  document.querySelector('#back').onclick=()=>navigate('documents');
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
    <input class="input" id="wscreator" placeholder="创建人" value="${p.creator}" style="flex:1;min-width:100px">
    <input class="input" id="wsadmin" placeholder="管理员" value="${p.admin}" style="flex:1;min-width:100px">
    <button class="primary" id="wsgo">查询</button><button class="secondary" id="wsreset">重置</button>
  </div>
  <div class="table-wrap"><table class="data-table"><thead><tr><th>知识库</th><th>等级</th><th class="num">文档数</th><th>创建人</th><th>管理员</th><th>归属部门</th></tr></thead><tbody>
    ${reg.items.map(i=>`<tr class="rowlink" data-ws="${i.workspace_id}"><td><b>${i.name}</b><br><small>${i.workspace_id}</small></td><td><span class="chip">${i.level_label}</span></td><td class="num">${i.document_count?nf(i.document_count):'—'}</td><td>${i.creator||'—'}</td><td>${(i.administrators||[]).slice(0,3).join('、')||'—'}${(i.administrators||[]).length>3?` 等${i.administrators.length}人`:''}</td><td>${i.department_name||'—'}</td></tr>`).join('')||'<tr><td colspan="6"><div class="empty">无匹配结果</div></td></tr>'}
  </tbody></table></div>
  <div class="controls" style="justify-content:space-between;margin-top:8px"><span class="hint">第 ${reg.total?p.offset+1:0}-${Math.min(p.offset+50,reg.total)} 条 / 共 ${reg.total} 条</span><span><button class="secondary" id="wsprev" ${p.offset<=0?'disabled':''}>上一页</button><button class="secondary" id="wsnext" ${p.offset+50>=reg.total?'disabled':''} style="margin-left:8px">下一页</button></span></div>
  </section>
  ${cov&&cov.unreachable&&cov.unreachable.length?`<section class="card section-gap"><div class="card-head"><h2>当前授权不可达（按宜搭 2026-04-27 快照，Top ${cov.unreachable.length}）</h2><span class="chip amber">待授权</span></div><div class="table-wrap"><table class="data-table"><thead><tr><th>知识库</th><th class="num">文件数</th></tr></thead><tbody>${cov.unreachable.map(u=>`<tr><td>${u.name}</td><td class="num">${nf(u.files)}</td></tr>`).join('')}</tbody></table></div><p class="hint">这些知识库需要把服务身份加为成员后才能纳入统计与评审。</p></section>`:''}`);
  document.querySelectorAll('[data-level]').forEach(x=>x.onclick=()=>{p.level=x.dataset.level;p.offset=0;workspaces()});
  document.querySelector('#wsgo').onclick=()=>{p.query=document.querySelector('#wsq').value.trim();p.department=document.querySelector('#wsdept').value.trim();p.creator=document.querySelector('#wscreator').value.trim();p.admin=document.querySelector('#wsadmin').value.trim();p.offset=0;workspaces()};
  document.querySelector('#wsq').onkeydown=e=>{if(e.key==='Enter')document.querySelector('#wsgo').click()};
  document.querySelector('#wsreset').onclick=()=>{state.wsReg={query:'',level:'',department:'',creator:'',admin:'',offset:0};workspaces()};
  document.querySelector('#wsprev').onclick=()=>{p.offset=Math.max(0,p.offset-50);workspaces()};
  document.querySelector('#wsnext').onclick=()=>{p.offset=p.offset+50;workspaces()};
  document.querySelectorAll('[data-ws]').forEach(x=>x.onclick=()=>workspaceDetail(x.dataset.ws))}

async function workspaceDetail(id){const[m,g,fd]=await Promise.all([api('/api/v1/metrics/workspaces/'+id+'/months'),api('/api/v1/workspaces/'+id).catch(()=>null),api('/api/v1/baseline/workspaces/'+id+'/folders?limit=100').catch(()=>({items:[],total_folders:0,note:''}))]);
  const name=state.coverageNames[id]||id;
  shell('知识库详情',name,`
  <section class="card"><div class="card-head"><h2>月度入库分布（基线 + 增量，共 ${nf(m.total_files)} 个文件）</h2><button class="secondary" id="back">返回列表</button></div><div id="wsChart" class="chart"></div></section>
  ${fd.items.length?`<section class="card section-gap"><div class="card-head"><h2>目录分布（共 ${nf(fd.total_folders)} 个目录，按文件数 Top ${fd.items.length}）</h2><span class="hint">${fd.note}</span></div><div class="grid two-cols"><div class="table-wrap" style="max-height:340px;overflow-y:auto"><table class="data-table"><thead><tr><th>目录（节点 ID）</th><th class="num">文件数</th><th>时间跨度</th></tr></thead><tbody>
    ${fd.items.map(f=>`<tr class="rowlink" data-folder="${f.parent_node_id}"><td><small>${f.parent_node_id}</small></td><td class="num">${nf(f.file_count)}</td><td><small>${f.earliest} ~ ${f.latest}</small></td></tr>`).join('')}
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
  document.querySelector('#back').onclick=()=>navigate('workspaces');
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
  <section class="card section-gap"><h2>上线前检查</h2>
    <p class="hint">1. 钉钉企业应用开通知识库读取与机器人发送权限，并发布版本；operatorId 使用数字员工 UnionID。</p>
    <p class="hint">2. 数字员工需为目标知识库成员；缺失的库在「知识库管理」的待授权清单中。</p>
    <p class="hint">3. bi_center 只读 Token 用于员工/部门归属映射（employeeKey=UnionID）。</p>
    <p class="hint">4. 仅在合规确认后配置正文临时获取网关与模型正文传输策略。</p></section>`);
  document.querySelector('#refresh').onclick=diagnostics;
  const send=document.querySelector('#test-send');
  if(send)send.onclick=async()=>{const uid=document.querySelector('#test-uid').value.trim();if(!uid)return toast('请填写 userId');
    try{await api('/api/v1/notifications/test',{method:'POST',body:JSON.stringify({user_id:uid})});toast('已发送，请在钉钉查收')}catch(e){toast(e.message)}}}

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

/* ---- shell nav ---- */
const views={overview,increments,uploaders,documents,reviews,workspaces,models,diagnostics};
function navigate(view){state.view=view;document.body.classList.remove('sidebar-open');
  document.querySelectorAll('.nav').forEach(b=>b.classList.toggle('active',b.dataset.view===view));
  views[view]().catch(e=>{if(app.querySelector('.login-card'))return;disposeCharts();app.innerHTML=`<section class="card"><h2>加载失败</h2><p class="hint">${e.message}</p><button class="secondary" onclick="location.reload()">重试</button></section>`})}
document.querySelectorAll('.nav').forEach(b=>b.onclick=()=>navigate(b.dataset.view));
document.querySelector('#menuBtn').onclick=()=>document.body.classList.toggle('sidebar-open');
document.querySelector('#backdrop').onclick=()=>document.body.classList.remove('sidebar-open');
(async()=>{try{
  const r=await fetch('api/auth/me');const d=await r.json().catch(()=>({}));
  if(r.status===401){state.authEnabled=true;renderLogin();return}
  state.authEnabled=!!d.auth_enabled;renderUser(d.user);navigate('overview')
}catch(e){navigate('overview')}})();
