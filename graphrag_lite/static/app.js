const $ = id => document.getElementById(id);
function elt(tag, text, cls) { const n=document.createElement(tag); if(text!==undefined)n.textContent=text; if(cls)n.className=cls; return n; }
async function api(path, data) { const r=await fetch(path,{method:data?'POST':'GET',headers:{'Content-Type':'application/json'},body:data?JSON.stringify(data):undefined}); const out=await r.json(); if(!r.ok)throw Error(out.error||'Request failed'); return out; }
async function refresh() { const s=await api('/api/stats'); $('stats').textContent=`${s.documents} 文档 · ${s.entities} 实体 · ${s.assertions} 断言`; const rows=await api('/api/resolutions'); const box=$('resolutions'); box.replaceChildren(); if(!rows.length)box.textContent='暂无待处理项'; rows.forEach(row=>{const div=elt('div',undefined,'resolution');div.append(elt('strong',`${row.surface} ↔ ${row.candidate_name}`),elt('p',`类型 ${row.type} · 相似度 ${row.similarity.toFixed(2)}`)); for(const [action,label,cls] of [['merge','合并',''],['distinct','保留为不同实体','secondary']]){const b=elt('button',label,cls);b.onclick=async()=>{await api('/api/resolve',{id:row.id,action});await refresh()};div.append(b)}box.append(div)}); }
function render(result){$('results').hidden=false;$('route').textContent={lexical:'文本检索',graph:'图谱检索',hybrid:'融合检索'}[result.route];$('reason').textContent=result.route_reason;$('entities').textContent=result.entities.join('、')||'未命中';$('hopIndicator').textContent=result.multi_hop?'检测到多跳意图':'常规查询';$('method').textContent=result.answer_method.toUpperCase();const answerBox=elt('div',undefined,'answer-text');const re=/\[([a-f0-9]{16}:\d+)\]/g;let start=0,match;while((match=re.exec(result.answer))){answerBox.append(document.createTextNode(result.answer.slice(start,match.index)));const link=elt('a',match[0]);link.href='#source-'+match[1];answerBox.append(link);start=re.lastIndex}answerBox.append(document.createTextNode(result.answer.slice(start)));$('answer').replaceChildren(answerBox);const sources=$('sources');sources.replaceChildren();result.sources.forEach(s=>{const box=elt('div',undefined,'source');box.id='source-'+s.id;box.append(elt('strong',`${s.title} [${s.id}]`),elt('p',s.text),elt('small',`文本位次 ${s.ranks.text||'—'} · 图谱位次 ${s.ranks.graph||'—'} · RRF ${s.rrf} · 重排 ${s.rerank}`));sources.append(box)});const paths=$('paths');paths.replaceChildren();if(!result.paths.length)paths.textContent='没有可展示的关系路径。';result.paths.forEach(p=>{const box=elt('div',undefined,'path');box.append(elt('div',`${p.hops} 跳 · 分数 ${p.score}`,'path-title'));p.assertions.forEach(a=>{box.append(elt('p',`${a.subject} — ${a.predicate} → ${a.object}`),elt('small',`“${a.quote}” [${a.chunk_id}]`))});paths.append(box)});$('results').scrollIntoView({behavior:'smooth',block:'start'});}
$('askForm').onsubmit=async e=>{e.preventDefault();$('error').textContent='';try{const r=await api('/api/ask',{question:$('question').value,mode:$('mode').value,max_hops:Number($('hops').value)});render(r)}catch(err){$('error').textContent=err.message}};
$('ingestForm').onsubmit=async e=>{e.preventDefault();$('ingestResult').textContent='处理中…';try{const facts=$('facts').value.trim();const data={title:$('title').value,text:$('text').value};if(facts)data.facts=JSON.parse(facts);const r=await api('/api/ingest',data);$('ingestResult').textContent=JSON.stringify(r,null,2);documentOffset=0;await Promise.all([refresh(),loadGraph(),loadDocuments(),loadConflicts()]);await showDocument(r.document_id);if(r.status==='pending')$('conflictPanel').scrollIntoView({behavior:'smooth'})}catch(err){$('ingestResult').textContent=err.message}};
refresh().catch(err=>$('error').textContent=err.message);

const svgNS='http://www.w3.org/2000/svg';
const graphState={nodes:[],edges:[],positions:new Map(),dragging:null,view:{x:0,y:0,w:900,h:540}};
let documentOffset=0,documentTotal=0,selectedDocument=null;
function svg(tag,attrs={}){const n=document.createElementNS(svgNS,tag);for(const [k,v] of Object.entries(attrs))n.setAttribute(k,v);return n}
function graphColor(type){const colors=['#d46e4c','#408b83','#6486ab','#a47db0','#c6a14a'];let hash=0;for(const ch of type)hash=(hash*31+ch.charCodeAt(0))>>>0;return colors[hash%colors.length]}
function viewDocumentButton(id,label='查看来源文档 ↗'){const b=elt('button',label,'small-button');b.onclick=()=>showDocument(id);return b}
function showGraphEdge(edge){const names=new Map(graphState.nodes.map(n=>[n.id,n.name]));const box=$('graphDetail');box.replaceChildren(elt('small',edge.status==='contested'?'争议断言':'断言'),elt('h3',`${names.get(edge.subject_id)} — ${edge.predicate} → ${names.get(edge.object_id)}`),elt('blockquote',edge.quote),elt('p',`状态 ${versionLabel(edge.status)} · 置信度 ${edge.confidence} · 证据块 ${edge.chunk_id}`),viewDocumentButton(edge.document_id))}
function showGraphNode(node){const box=$('graphDetail');box.replaceChildren(elt('small','ENTITY'),elt('h3',node.name),elt('p',`类型：${node.type}`));const names=new Map(graphState.nodes.map(n=>[n.id,n.name]));const edges=graphState.edges.filter(e=>e.subject_id===node.id||e.object_id===node.id);box.append(elt('h4',`可见关系 · ${edges.length}`));for(const edge of edges.slice(0,30)){const item=elt('div',undefined,'detail-edge');item.append(elt('strong',`${names.get(edge.subject_id)} — ${edge.predicate} → ${names.get(edge.object_id)}`),elt('p',`“${edge.quote}”`),viewDocumentButton(edge.document_id,edge.document_title+' ↗'));box.append(item)}}
function graphPoint(event){const p=$('graphSvg').createSVGPoint();p.x=event.clientX;p.y=event.clientY;return p.matrixTransform($('graphSvg').getScreenCTM().inverse())}
function drawGraph(){const canvas=$('graphSvg');canvas.replaceChildren();const v=graphState.view;canvas.setAttribute('viewBox',`${v.x} ${v.y} ${v.w} ${v.h}`);const edgeLayer=svg('g'),nodeLayer=svg('g');for(const edge of graphState.edges){const a=graphState.positions.get(edge.subject_id),b=graphState.positions.get(edge.object_id);if(!a||!b)continue;const line=svg('line',{x1:a.x,y1:a.y,x2:b.x,y2:b.y,class:'graph-edge'+(edge.status==='contested'?' contested':'')});line.addEventListener('click',()=>showGraphEdge(edge));edgeLayer.append(line);if(graphState.edges.length<=35){const label=svg('text',{x:(a.x+b.x)/2,y:(a.y+b.y)/2-5,class:'graph-label'});label.textContent=edge.predicate;label.addEventListener('click',()=>showGraphEdge(edge));edgeLayer.append(label)}}for(const node of graphState.nodes){const p=graphState.positions.get(node.id);const group=svg('g',{class:'graph-node',transform:`translate(${p.x} ${p.y})`});group.append(svg('circle',{r:17,fill:graphColor(node.type)}));const label=svg('text',{y:34,'text-anchor':'middle'});label.textContent=node.name.length>12?node.name.slice(0,12)+'…':node.name;group.append(label);group.addEventListener('pointerdown',event=>{showGraphNode(node);graphState.dragging=node.id;canvas.setPointerCapture(event.pointerId)});nodeLayer.append(group)}canvas.append(edgeLayer,nodeLayer)}
function layoutGraph(){graphState.positions=new Map();graphState.nodes.forEach((node,i)=>{const angle=i*2.3999632297,radius=Math.min(235,45*Math.sqrt(i+1));graphState.positions.set(node.id,{x:450+radius*Math.cos(angle),y:270+radius*Math.sin(angle)})});for(let step=0;step<90&&graphState.nodes.length<=150;step++){const delta=new Map(graphState.nodes.map(n=>[n.id,{x:0,y:0}]));for(let i=0;i<graphState.nodes.length;i++)for(let j=i+1;j<graphState.nodes.length;j++){const a=graphState.positions.get(graphState.nodes[i].id),b=graphState.positions.get(graphState.nodes[j].id),dx=a.x-b.x,dy=a.y-b.y,d=Math.max(1,Math.hypot(dx,dy)),f=Math.min(12,2500/d**2);delta.get(graphState.nodes[i].id).x+=dx/d*f;delta.get(graphState.nodes[i].id).y+=dy/d*f;delta.get(graphState.nodes[j].id).x-=dx/d*f;delta.get(graphState.nodes[j].id).y-=dy/d*f}for(const e of graphState.edges){const a=graphState.positions.get(e.subject_id),b=graphState.positions.get(e.object_id);if(!a||!b)continue;const dx=b.x-a.x,dy=b.y-a.y,d=Math.max(1,Math.hypot(dx,dy)),f=(d-115)*.018;delta.get(e.subject_id).x+=dx/d*f;delta.get(e.subject_id).y+=dy/d*f;delta.get(e.object_id).x-=dx/d*f;delta.get(e.object_id).y-=dy/d*f}for(const n of graphState.nodes){const p=graphState.positions.get(n.id),m=delta.get(n.id);p.x=Math.max(35,Math.min(865,p.x+m.x*.6));p.y=Math.max(35,Math.min(505,p.y+m.y*.6))}}const points=[...graphState.positions.values()];if(points.length){const minX=Math.min(...points.map(p=>p.x))-75,maxX=Math.max(...points.map(p=>p.x))+75,minY=Math.min(...points.map(p=>p.y))-75,maxY=Math.max(...points.map(p=>p.y))+75;graphState.view={x:minX,y:minY,w:maxX-minX,h:maxY-minY}}else graphState.view={x:0,y:0,w:900,h:540}}
async function loadGraph(){const q=$('graphSearch').value.trim(),data=await api('/api/graph'+(q?'?q='+encodeURIComponent(q):''));graphState.nodes=data.nodes;graphState.edges=data.edges;layoutGraph();drawGraph();$('graphEmpty').hidden=!!data.nodes.length;$('graphCount').textContent=`${data.nodes.length} 实体 · ${data.edges.length} 断言`;$('graphNotice').textContent=data.truncated?`仅显示前 ${data.limit} 条断言，请输入实体或关系筛选。`:'拖动节点调整布局；滚轮缩放；点击节点或关系查看证据。';const box=$('graphDetail'),names=new Map(data.nodes.map(n=>[n.id,n.name]));box.replaceChildren(elt('h3','关系列表'));if(!data.edges.length)box.append(elt('p','没有可显示的关系断言。'));for(const edge of data.edges){const row=elt('button',`${edge.status==='contested'?'⚠ 争议 · ':''}${names.get(edge.subject_id)} — ${edge.predicate} → ${names.get(edge.object_id)}`,'relation-row');row.onclick=()=>showGraphEdge(edge);box.append(row)}}
$('graphSvg').addEventListener('pointermove',e=>{if(!graphState.dragging)return;const p=graphPoint(e);graphState.positions.set(graphState.dragging,{x:p.x,y:p.y});drawGraph()});
$('graphSvg').addEventListener('pointerup',()=>graphState.dragging=null);
$('graphSvg').addEventListener('pointercancel',()=>graphState.dragging=null);
$('graphSvg').addEventListener('wheel',e=>{e.preventDefault();const v=graphState.view,f=e.deltaY>0?1.12:.89,w=Math.max(100,Math.min(1600,v.w*f)),h=w*v.h/v.w;v.x+=(v.w-w)/2;v.y+=(v.h-h)/2;v.w=w;v.h=h;drawGraph()},{passive:false});
$('graphSearchButton').onclick=()=>loadGraph().catch(e=>$('error').textContent=e.message);
$('graphSearch').onkeydown=e=>{if(e.key==='Enter')$('graphSearchButton').click()};
$('graphReset').onclick=()=>{$('graphSearch').value='';$('graphSearchButton').click()};

async function loadDocuments(){const params=new URLSearchParams({q:$('documentSearch').value.trim(),limit:25,offset:documentOffset}),data=await api('/api/documents?'+params);documentTotal=data.total;const list=$('documentList');list.replaceChildren();if(!data.items.length)list.append(elt('p','没有匹配的文档。','empty-state'));for(const doc of data.items){const row=elt('button',undefined,'document-row'+(doc.id===selectedDocument?' selected':''));row.append(elt('strong',doc.title),elt('small',`${doc.chunk_count} 分块 · ${doc.assertion_count} 断言 · ${doc.created_at}`),elt('p',doc.preview));row.onclick=()=>showDocument(doc.id);list.append(row)}$('documentCount').textContent=`${data.total} 份文档`;$('documentPage').textContent=data.total?`${data.offset+1}–${Math.min(data.offset+data.limit,data.total)} / ${data.total}`:'0 / 0';$('documentPrev').disabled=documentOffset===0;$('documentNext').disabled=documentOffset+25>=documentTotal}
async function showDocument(id){try{const doc=await api('/api/documents/'+encodeURIComponent(id));selectedDocument=id;const box=$('documentDetail');box.replaceChildren(elt('small',`DOCUMENT · ${doc.id}`),elt('h3',doc.title),elt('p',`入库时间：${doc.created_at} · ${doc.chunks.length} 分块 · ${doc.assertions.length} 断言`,'hint'),elt('h4','完整正文'),elt('div',doc.body,'document-body'),elt('h4','抽取的关系'));if(!doc.assertions.length)box.append(elt('p','该文档没有已入库的关系断言。','hint'));for(const fact of doc.assertions){const item=elt('div',undefined,'document-fact');item.append(elt('strong',`${fact.subject} — ${fact.predicate} → ${fact.object}`),elt('p',`“${fact.quote}”`),elt('small',`证据块 ${fact.chunk_id} · 置信度 ${fact.confidence}`));box.append(item)}box.append(elt('h4','文本分块'));for(const chunk of doc.chunks){const item=elt('div',undefined,'document-chunk');item.append(elt('small',`${chunk.id} · 第 ${chunk.ordinal+1} 块`),elt('p',chunk.body));box.append(item)}$('library').scrollIntoView({behavior:'smooth',block:'start'})}catch(e){$('error').textContent=e.message}}
$('documentSearchButton').onclick=()=>{documentOffset=0;loadDocuments().catch(e=>$('error').textContent=e.message)};
$('documentSearch').onkeydown=e=>{if(e.key==='Enter')$('documentSearchButton').click()};
$('documentPrev').onclick=()=>{documentOffset=Math.max(0,documentOffset-25);loadDocuments()};
$('documentNext').onclick=()=>{documentOffset+=25;loadDocuments()};
Promise.all([loadGraph(),loadDocuments()]).catch(e=>$('error').textContent=e.message);

let editSeriesId=null,editBaseVersion=null;
function versionLabel(status){return {active:'生效',superseded:'历史',pending:'待审核',rejected:'已驳回',accepted:'已采纳',contested:'有争议'}[status]||status}
const showDocumentBase=showDocument;
showDocument=async function(id){
  await showDocumentBase(id);
  try{
    const doc=await api('/api/documents/'+encodeURIComponent(id));
    if(!doc.series_id)return;
    const box=$('documentDetail');
    box.insertBefore(elt('p',`版本 v${doc.version} · ${versionLabel(doc.status)}`,'version-badge'),box.querySelector('h4'));
    if(doc.status==='active'){
      const edit=elt('button','以此版本编辑 ↗','small-button');
      edit.onclick=()=>{
        editSeriesId=doc.series_id;editBaseVersion=doc.version;
        $('updateTarget').textContent=`正在编辑「${doc.title}」v${doc.version}；提交时将校验版本，避免覆盖别人的更新。`;
        $('updateTitle').value=doc.title;$('updateText').value=doc.body;
        $('updateFacts').value=JSON.stringify(doc.assertions.map(f=>({subject:f.subject,subject_type:f.subject_type,predicate:f.predicate,object:f.object,object_type:f.object_type,quote:f.quote,confidence:f.confidence})),null,2);
        $('updateNote').value='';$('updatePanel').scrollIntoView({behavior:'smooth'});
      };
      box.insertBefore(edit,box.querySelector('h4'));
    }
    const versions=await api('/api/series/'+encodeURIComponent(doc.series_id)+'/versions');
    const history=elt('div',undefined,'version-history');history.append(elt('h4','版本历史'));
    for(const item of versions){
      const row=elt('div',undefined,'version-row');
      row.append(elt('strong',`v${item.version} · ${versionLabel(item.status)}`),elt('p',item.note||'无变更说明'));
      const view=elt('button','查看快照','small-button secondary');view.onclick=()=>showDocument(item.document_id);row.append(view);
      if(item.version>1){const compare=elt('button','与上一版对比','small-button secondary');compare.onclick=async()=>{const params=new URLSearchParams({from:item.version-1,to:item.version});const diff=await api(`/api/series/${encodeURIComponent(doc.series_id)}/diff?${params}`);history.querySelector('.diff-view')?.remove();const panel=elt('div',undefined,'diff-view');panel.append(elt('h4',`v${diff.from} → v${diff.to}`),elt('pre',diff.text_diff||'正文无变化'),elt('p','新增关系：'+(diff.added_facts.join('；')||'无')),elt('p','移除关系：'+(diff.removed_facts.join('；')||'无')));history.append(panel)};row.append(compare)}
      history.append(row);
    }
    box.append(history);
  }catch(error){$('error').textContent=error.message}
};

$('updateForm').onsubmit=async event=>{
  event.preventDefault();const output=$('updateResult');output.textContent='提交中…';
  if(!editSeriesId){output.textContent='请先从文档库打开当前版本，并点击“以此版本编辑”。';return}
  try{
    const data={base_version:editBaseVersion,title:$('updateTitle').value,text:$('updateText').value,note:$('updateNote').value};
    if($('updateFacts').value.trim())data.facts=JSON.parse($('updateFacts').value);
    const result=await api(`/api/series/${encodeURIComponent(editSeriesId)}/versions`,data);
    output.textContent=`已创建 v${result.version}，状态：${versionLabel(result.status)}。${result.status==='pending'?'请在冲突审核区处理。':''}`;
    await Promise.all([refresh(),loadGraph(),loadDocuments(),loadConflicts()]);
    await showDocument(result.document_id);
    if(result.status==='pending')$('conflictPanel').scrollIntoView({behavior:'smooth'});
    else{editSeriesId=null;editBaseVersion=null;$('updateTarget').textContent=`已提交 v${result.version}。如需继续编辑，请重新打开当前版本。`}
  }catch(error){output.textContent=error.message+'。请重新打开当前版本核对变更后再提交。'}
};

async function loadConflicts(){
  const proposals=await api('/api/conflicts'),box=$('conflicts');box.replaceChildren();
  $('conflictCount').textContent=`${proposals.length} 待审核`;
  if(!proposals.length){box.textContent='暂无待审核版本。';return}
  for(const proposal of proposals){
    const card=elt('div',undefined,'conflict-card');
    card.append(elt('h3',`${proposal.title} · v${proposal.version} 待审核`),
      elt('p',`基于 v${proposal.base_version} · ${proposal.note||'无变更说明'}`));
    for(const item of proposal.conflicts){
      const pair=elt('div',undefined,'conflict-pair');
      pair.append(elt('strong',`${item.subject} — ${item.predicate}`),
        elt('p',`当前：${item.old_object} · “${item.old_quote}”`),
        elt('p',`新版本：${item.new_object} · “${item.new_quote}”`));card.append(pair);
    }
    for(const [action,label,description] of [
      ['replace','采用新版','旧冲突断言退出当前检索，新版本生效'],
      ['coexist','并列保留','两条断言都保留并标记争议'],
      ['reject','驳回新版','保留当前版本，新版本仅留在历史中']
    ]){
      const button=elt('button',label,'small-button'+(action==='reject'?' secondary':''));button.title=description;
      button.onclick=async()=>{try{const result=await api('/api/conflicts/resolve',{series_id:proposal.series_id,version:proposal.version,action});await Promise.all([refresh(),loadGraph(),loadDocuments(),loadConflicts()]);$('documentDetail').textContent=result.status==='pending'?'冲突证据已变化，请重新审核待处理项。':'审核完成，请从文档库重新选择文档。'}catch(error){$('error').textContent=error.message}};
      card.append(button);
    }
    box.append(card);
  }
}
loadConflicts().catch(error=>$('error').textContent=error.message);
