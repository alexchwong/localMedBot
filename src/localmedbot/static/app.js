'use strict';
const el=id=>document.getElementById(id);
const token=document.querySelector('meta[name=request-token]').content;
let apps=[],rid=null,current=null,poll=null;
async function api(path,data){
 const r=await fetch(path,data===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json','X-LocalMedBot-Token':token},body:JSON.stringify(data)});
 const value=await r.json();if(!r.ok)throw new Error(value.error?.code||'request_failed');return value;
}
function report(e){el('error').hidden=false;el('error').textContent=e.message;}
function guard(fn){return async()=>{el('error').hidden=true;try{await fn();}catch(e){report(e);}};}
async function loadExample(){el('input').value=JSON.stringify(await api(`/api/examples/${el('application').value}/${el('example').value}`),null,2);}
async function chooseApp(){const app=apps.find(a=>a.id===el('application').value);el('example').replaceChildren(...app.examples.map(x=>new Option(x,x)));el('ingest').disabled=!app.corpus;await loadExample();}
async function listRuns(){const rows=await api('/api/runs');el('runs').replaceChildren();for(const row of rows){const b=document.createElement('button');b.className='run';b.textContent=`${row.application} · ${row.status}`;b.onclick=guard(async()=>{rid=row.id;await refresh();});el('runs').append(b);}}
async function refresh(){
 if(!rid)return;const data=await api('/api/runs/'+rid);current=data;
 const r=data.run;el('status').textContent=r.status;el('status').dataset.state=r.status;el('run-id').textContent=r.id;
 el('result-title').textContent=r.snapshot.manifest.name;el('steps').replaceChildren();
 for(const node of r.snapshot.workflow.nodes){const name=node.id,state=r.nodes[name];const s=document.createElement('span');s.className='step';s.dataset.state=state;s.textContent=`${name} · ${state}`;el('steps').append(s);}
 const out=data.artifacts[r.snapshot.workflow.output];el('empty').hidden=Boolean(out);el('output').textContent=out?.payload.text||'';el('output').dataset.revision=out?.revision||'';
 el('citations').replaceChildren();for(const [i,c] of (out?.payload.citations||[]).entries()){const b=document.createElement('button');b.className='citation';b.dataset.testid='citation';b.textContent=`[${i+1}] ${c.source} · ${c.locator}`;b.onclick=guard(async()=>{const source=await api(`/api/runs/${rid}/source/${c.corpus_id}/${encodeURIComponent(c.evidence_id)}`);el('source-content').textContent=JSON.stringify(source,null,2);el('source-panel').hidden=false;el('source-panel').open=true;});el('citations').append(b);}
 el('review').hidden=!out||!['waiting_review','completed'].includes(r.status);el('approve').disabled=r.status==='completed';el('resume').hidden=!['pending','running','failed'].includes(r.status);
 el('artifacts').textContent=JSON.stringify(data.artifacts,null,2);el('audit').textContent=JSON.stringify(data.events,null,2);
 if(r.error){el('error').hidden=false;el('error').textContent=r.error.code;}
 clearTimeout(poll);if(['pending','running'].includes(r.status))poll=setTimeout(()=>refresh().catch(report),500);await listRuns();
}
async function review(decision){const out=current.artifacts[current.run.snapshot.workflow.output];await api(`/api/runs/${rid}/review`,{revision:out.revision,actor:el('actor').value,decision,comments:el('comments').value});await refresh();}
el('application').onchange=guard(chooseApp);el('example').onchange=guard(loadExample);
el('start').onclick=guard(async()=>{el('start').disabled=true;try{const result=await api('/api/runs',{application:el('application').value,example:el('example').value,input:JSON.parse(el('input').value)});rid=result.id;el('source-panel').hidden=true;await refresh();}finally{el('start').disabled=false;}});
for(const action of ['approve','revise','reject'])el(action).onclick=guard(()=>review(action));
el('resume').onclick=guard(async()=>{await api(`/api/runs/${rid}/resume`,{});await refresh();});
el('ingest').onclick=guard(async()=>{const file=el('source-file').files[0];let sources;
 if(file){if(file.size>1024*1024)throw new Error('file_too_large');const ext=file.name.split('.').pop().toLowerCase();if(!['json','md','txt'].includes(ext))throw new Error('source_format');const content=await file.text();sources=ext==='json'?JSON.parse(content):[{id:'uploaded',title:file.name,format:ext==='md'?'markdown':'text',content}];}
 const result=await api(`/api/ingest/${el('application').value}`,sources?{sources}:{});el('import-result').textContent='Imported revision '+result.corpus_id;});
(async()=>{apps=await api('/api/applications');el('application').replaceChildren(...apps.map(a=>new Option(a.name,a.id)));await chooseApp();await listRuns();})().catch(report);
