'use strict';
const el=id=>document.getElementById(id);
let token=document.querySelector('meta[name=request-token]').content;
let apps=[],profiles=[],sets=[],fixtures=[],steps=[],rid=null,current=null,developer=false,poll=null,copyInputId=null;

async function api(path,data,method){
  const opt={};
  if(data!==undefined||method){
    opt.method=method||'POST';
    opt.headers={'Content-Type':'application/json','X-LocalMedBot-Token':token};
    if(data!==undefined)opt.body=JSON.stringify(data);
  }
  const r=await fetch(path,opt); const v=await r.json();
  if(!r.ok){const e=new Error(v.error?.code||'request_failed');e.payload=v;throw e}
  return v;
}
function errorMessage(e){return e.payload?.error?.detail?`${e.message}: ${e.payload.error.detail}`:e.message}
function guard(fn){return async(...args)=>{el('error').hidden=true;try{await fn(...args)}catch(e){el('error').hidden=false;el('error').textContent=errorMessage(e)}}}
function workflow(){return el('workflow').value}
function selectedProfile(){return profiles.find(x=>x.id===el('profile').value)}
function parseJson(id,empty=null){const text=el(id).value.trim();if(!text)return empty;return JSON.parse(text)}
function setText(id,value){el(id).textContent=value==null?'':String(value)}

async function loadProfiles(preserve=true){
  const previous=preserve?el('profile').value:'';
  profiles=await api('/api/model-profiles?workflow_id='+encodeURIComponent(workflow()));
  el('profile').replaceChildren(...profiles.map(x=>new Option(x.name,x.id)));
  const desired=profiles.find(x=>x.id===previous)||profiles.find(x=>x.selected)||profiles.find(x=>x.executor==='recorded')||profiles[0];
  if(desired)el('profile').value=desired.id;
  showProfile();
}
function showProfile(){
  const p=selectedProfile(); if(!p){el('provider-config').hidden=true;setText('destination','Select a model profile.');return}
  el('base-url').value=p.base_url||''; el('model').value=p.model||'';
  const d=p.destination||{}; const nonLocal=['non_local','unknown'].includes(d.classification);
  el('destination').className='destination '+(nonLocal?'non-local':'');
  el('destination').textContent=nonLocal?`⚠ Non-local/unknown execution (${d.host||p.executor}): submitted text may leave your local domain. You are responsible for not sending patient data outside that domain.`:`Local/local-network execution: ${d.host||p.executor}. Local storage is not encrypted.`;
  el('provider-config').hidden=['recorded','self'].includes(p.executor);
  if(p.executor==='recorded')el('input-mode').value='demo'; else if(el('input-mode').value==='demo')el('input-mode').value='free_text';
  showInputs();
}
async function saveProfile(){
  const p=selectedProfile(); if(!p)throw new Error('profile_required');
  const overlay={}; if(el('base-url').value.trim())overlay.base_url=el('base-url').value.trim(); if(el('model').value.trim())overlay.model=el('model').value.trim();
  await api('/api/model-profiles/configure',{profile_id:p.id,overlay,credential:el('credential').value||undefined});
  el('credential').value=''; await loadProfiles(false); el('profile').value=p.id; showProfile();
}
async function verify(){const p=selectedProfile();if(!p)throw new Error('profile_required');setText('verify-result',JSON.stringify(await api('/api/providers/verify',{profile_id:p.id,workflow_id:workflow()}),null,2))}

async function loadSets(){
  sets=await api('/api/guideline-sets'); const prev=el('guideline-set').value;
  el('guideline-set').replaceChildren(...sets.map(x=>new Option(x.name,x.id)));
  if(sets.some(x=>x.id===prev))el('guideline-set').value=prev; showVersions();
}
function showVersions(){
  const s=sets.find(x=>x.id===el('guideline-set').value); if(!s){el('guideline-version').replaceChildren();return}
  const opts=[new Option(`Current default · ${s.default_release}`,'default'),...(s.releases||[]).map(x=>new Option(x,x))];
  if(developer&&s.devel_available)opts.push(new Option('Development','devel'));
  el('guideline-version').replaceChildren(...opts);
}
function showInputs(){
  const mode=el('input-mode').value, wf=workflow();
  el('demo-fields').hidden=mode!=='demo'; el('copied-fields').hidden=mode!=='copied';
  el('letter-fields').hidden=mode!=='free_text'||wf!=='clinical_letter';
  el('qa-fields').hidden=(mode!=='free_text'&&mode!=='copied')||wf!=='guideline_qa';
  el('advanced-fields').hidden=!(developer&&mode==='advanced');
}
async function chooseWorkflow(){
  const app=apps.find(x=>x.id===workflow());
  el('example').replaceChildren(...(app?.examples||[]).map(x=>new Option(x,x)));
  await loadProfiles(false); if(workflow()==='guideline_qa')await loadSets();
  showInputs(); if(developer){await loadSteps();await loadFixtures()}
}
async function start(){
  const p=selectedProfile(); if(!p)throw new Error('profile_required');
  const mode=el('input-mode').value; let input={}; let guideline_selection;
  if(mode==='free_text'&&workflow()==='clinical_letter')input={notes:el('notes').value,purpose:el('purpose').value};
  else if(mode==='free_text'&&workflow()==='guideline_qa')input={question:el('question').value};
  else if(mode==='advanced')input=parseJson('advanced-input',{});
  if((mode==='free_text'||mode==='advanced'||mode==='copied')&&workflow()==='guideline_qa')guideline_selection={set_id:el('guideline-set').value,selector:el('guideline-version').value};
  const d={workflow_id:workflow(),profile_id:p.id,input_mode:mode,input,profile_overrides:{}};
  if(guideline_selection)d.guideline_selection=guideline_selection;
  if(mode==='demo')d.example=el('example').value;
  if(mode==='copied')d.copy_input_id=copyInputId;
  const r=await api('/api/runs',d); rid=r.id; copyInputId=null; await refresh();
}

async function listRuns(){
  const rows=await api('/api/runs'); el('runs').replaceChildren();
  for(const row of rows){const b=document.createElement('button');b.className='run';b.textContent=`${row.application} · ${row.status}`;b.onclick=guard(async()=>{rid=row.id;await refresh()});el('runs').append(b)}
}
function renderFacts(data){
  el('facts-panel').replaceChildren(); const facts=data.artifacts?.facts?.payload?.facts||[]; if(!facts.length)return;
  const h=document.createElement('h3');h.textContent='Extracted facts';el('facts-panel').append(h);
  for(const f of facts){const d=document.createElement('div');d.className='fact';const cb=document.createElement('input');cb.type='checkbox';cb.className='inline';cb.dataset.fact=f.id;cb.checked=(data.run.omission_policy?.omitted_fact_ids||[]).includes(f.id);const label=document.createElement('label');label.append(cb,document.createTextNode(` Omit ${f.id} from this communication`));const p=document.createElement('div');p.textContent=f.text;const reason=document.createElement('input');reason.placeholder='Reason required if omitted';reason.dataset.reason=f.id;reason.value=data.run.omission_policy?.reasons?.[f.id]||'';d.append(label,p,reason);el('facts-panel').append(d)}
}
function renderConflicts(out){
  el('conflicts-panel').replaceChildren(); const ids=out?.payload?.conflict_ids||[];
  for(const id of ids){const d=document.createElement('div');d.className='conflict';const cb=document.createElement('input');cb.type='checkbox';cb.className='inline';cb.dataset.conflict=id;const label=document.createElement('label');label.append(cb,document.createTextNode(` Acknowledge displayed conflict ${id}`));d.append(label);el('conflicts-panel').append(d)}
}
function renderTrace(data){
  const trace={step_attempts:data.step_attempts||[],model_calls:data.model_calls||[],tool_calls:data.tool_calls||[]};
  setText('developer-trace',JSON.stringify(trace,null,2));
}
async function refresh(){
  if(!rid)return; const data=await api('/api/runs/'+rid); current=data; const r=data.run;
  el('status').textContent=r.status;el('status').dataset.state=r.status;el('result-title').textContent=r.snapshot?.manifest?.name||'Legacy run';
  setText('provenance',`Executor: ${r.profile?.executor||'legacy'} · Data origin: ${r.data_origin||'unknown'} · Run: ${r.id}`);
  setText('outcomes',`Checks: ${JSON.stringify(r.check_outcomes||{})} · Evidence: ${r.evidence_outcome||'not applicable'} · Approval: ${r.approval?'approved':r.review_disposition||'not approved'}`);
  el('steps').replaceChildren(); for(const n of r.snapshot?.workflow?.nodes||[]){const s=document.createElement('span');s.className='step';s.dataset.state=r.nodes?.[n.id];s.textContent=`${n.id} · ${r.nodes?.[n.id]}`;el('steps').append(s)}
  const out=data.artifacts?.[r.snapshot?.workflow?.output]; el('output').textContent=out?.payload?.text||''; el('output').dataset.revision=out?.revision||'';
  el('citations').replaceChildren(); for(const [i,c] of (out?.payload?.citations||[]).entries()){const b=document.createElement('button');b.className='citation';b.dataset.testid='citation';b.textContent=`[${i+1}] ${c.source} · ${c.locator}`;b.onclick=guard(async()=>{const src=await api(`/api/runs/${rid}/source/${c.corpus_id}/${encodeURIComponent(c.evidence_id)}`);setText('source-content',JSON.stringify(src,null,2));el('source-panel').hidden=false;el('source-panel').open=true});el('citations').append(b)}
  renderFacts(data);renderConflicts(out);
  const reviewable=['waiting_review','completed','rejected'].includes(r.status)||(r.status==='blocked'&&r.block?.human_revisable);el('review').hidden=!reviewable;
  let targets=(r.status==='blocked'?r.block?.allowed_revision_targets:null)||(r.snapshot?.workflow?.revision_targets)||[r.snapshot?.workflow?.revision_target];targets=(targets||[]).filter(Boolean);el('review-target').replaceChildren(...targets.map(x=>new Option(x,x)));
  el('approve').disabled=!['waiting_review','completed'].includes(r.status);el('reject').disabled=!['waiting_review','completed'].includes(r.status);
  el('resume').hidden=!['pending','failed'].includes(r.status);el('delete-run').hidden=['running','waiting_model'].includes(r.status);el('copy-legacy').hidden=!r.legacy;
  setText('artifacts',JSON.stringify(data.artifacts,null,2));setText('audit',JSON.stringify(data.events,null,2));
  el('developer-trace-panel').hidden=!developer;if(developer)renderTrace(data);
  el('self-panel').hidden=!(developer&&r.status==='waiting_model'&&data.handoff);if(!el('self-panel').hidden)setText('handoff',JSON.stringify(data.handoff,null,2));
  clearTimeout(poll);if(['pending','running'].includes(r.status))poll=setTimeout(()=>refresh().catch(e=>{el('error').textContent=errorMessage(e);el('error').hidden=false}),500);await listRuns();
}
async function review(decision){
  const r=current.run,out=current.artifacts?.[r.snapshot?.workflow?.output];
  const payload={review_request_id:crypto.randomUUID(),revision:out?.revision??null,actor:el('actor').value,decision,comments:el('comments').value};
  if(decision==='approve'){
    payload.acknowledged_omission_ids=[...document.querySelectorAll('[data-fact]:checked')].map(x=>x.dataset.fact);
    payload.acknowledged_conflict_ids=[...document.querySelectorAll('[data-conflict]:checked')].map(x=>x.dataset.conflict);
  } else if(decision==='revise'){
    payload.target=el('review-target').value;if(r.status==='blocked'){payload.expected_block={code:r.block.code,node_id:r.block.node_id,attempt:r.block.attempt};payload.revision=out?.revision??null}
    if(r.snapshot?.manifest?.id==='clinical_letter'&&payload.target==='draft')payload.omissions=[...document.querySelectorAll('[data-fact]:checked')].map(x=>({fact_id:x.dataset.fact,reason:document.querySelector(`[data-reason="${x.dataset.fact}"]`).value,extraction_revision:r.active?.facts}));
  }
  await api(`/api/runs/${rid}/review`,payload);await refresh();
}

async function setDeveloper(){
  const r=await api('/api/developer-mode',{enabled:el('developer').checked});developer=r.developer_enabled;
  document.querySelectorAll('.developer-only').forEach(x=>{if(x.id!=='self-panel'&&x.id!=='advanced-fields')x.hidden=!developer});
  el('advanced-option').hidden=!developer;if(!developer&&el('input-mode').value==='advanced')el('input-mode').value='free_text';
  if(workflow()==='guideline_qa')await loadSets();if(developer){await loadSteps();await loadFixtures()}showInputs();if(rid)await refresh();
}
async function loadSteps(){
  if(!developer)return;steps=await api('/api/dev/workflows/'+workflow()+'/steps');const prev=el('step').value;el('step').replaceChildren(...steps.map(x=>new Option(x.id,x.id)));if(steps.some(x=>x.id===prev))el('step').value=prev;await loadFixtures();
}
async function loadFixtures(){
  if(!developer)return;fixtures=await api('/api/dev/fixtures');const wf=workflow(),node=el('step').value;const matching=fixtures.filter(x=>x.workflow_id===wf&&(!node||x.node_id===node));
  el('fixture-existing').replaceChildren(new Option('Select saved fixture',''),...matching.map(x=>new Option(`${x.id} v${x.version} · ${x.kind}`,`${x.id}|${x.version}`)));
}
function schemaTemplate(schema){
  if(!schema||typeof schema!=='object')return null;if(schema.default!==undefined)return schema.default;
  if(schema.const!==undefined)return schema.const;if(schema.enum?.length)return schema.enum[0];
  if(schema.type==='object'||schema.properties){const o={};for(const k of schema.required||[])o[k]=schemaTemplate(schema.properties?.[k]||{});return o}
  if(schema.type==='array')return [];if(schema.type==='string')return '';if(schema.type==='integer'||schema.type==='number')return schema.minimum??0;if(schema.type==='boolean')return false;return null;
}
function baseFixture(resolved){return {fixture_schema_version:1,id:el('fixture-id').value,version:Number(el('fixture-version').value),workflow_id:workflow(),node_id:el('step').value,step_contract_version:1,resolved_inputs:resolved,evidence_snapshots:[],context:{feedback:null,review_decisions:[]},provenance:{source_run_id:null,source_node_attempt:null},data_suitability:'synthetic',origins:{task_input:'synthetic',evidence:{},revision_feedback:{}}}}
async function newFixture(){const s=steps.find(x=>x.id===el('step').value);if(!s)throw new Error('step_required');el('fixture').value=JSON.stringify(baseFixture(schemaTemplate(s.input_schema)||{}),null,2);el('fixture-source').value='interactive';setText('fixture-result','Created schema-shaped fixture. Complete required clinical fields before running.')}
async function chooseSavedFixture(){const value=el('fixture-existing').value;if(!value)return;const [id,v]=value.split('|');const row=fixtures.find(x=>x.id===id&&String(x.version)===v);if(!row)throw new Error('fixture_not_found');el('fixture').value=JSON.stringify(row.document,null,2);el('fixture-id').value=row.id;el('fixture-version').value=row.version;el('fixture-source').value='saved'}
async function captureStep(){if(!rid||!current)throw new Error('run_required');const node=el('step').value,attempt=current.run.attempts?.[node];if(!attempt)throw new Error('step_attempt_not_found');const doc=await api('/api/dev/fixtures/capture',{run_id:rid,node_id:node,attempt});doc.id=el('fixture-id').value;doc.version=Number(el('fixture-version').value);el('fixture').value=JSON.stringify(doc,null,2);el('fixture-source').value='prior';setText('fixture-result',`Captured ${node} attempt ${attempt} from ${rid}`)}
async function saveFixture(){const doc=parseJson('fixture');doc.id=el('fixture-id').value;doc.version=Number(el('fixture-version').value);const r=await api('/api/dev/fixtures',{fixture:doc});el('fixture').value=JSON.stringify(doc,null,2);setText('fixture-result','Saved '+r.path);await loadFixtures()}
async function deleteFixture(){const id=el('fixture-id').value,v=Number(el('fixture-version').value);await api(`/api/dev/fixtures/${encodeURIComponent(id)}?version=${v}`,undefined,'DELETE');setText('fixture-result',`Deleted scratch ${id} v${v}`);await loadFixtures()}
async function promoteFixture(){const id=el('fixture-id').value,v=Number(el('fixture-version').value);const r=await api(`/api/dev/fixtures/${encodeURIComponent(id)}/promote`,{version:v,new_id:el('promote-id').value,new_version:Number(el('promote-version').value),suitability:el('fixture-suitability').value,acknowledge_reviewed:el('promote-ack').checked,actor:el('promote-actor').value});setText('fixture-result','Promoted '+r.path);await loadFixtures()}
async function runStep(){const fixture=parseJson('fixture');const payload={workflow_id:workflow(),node_id:el('step').value,profile_id:el('profile').value,fixture,configuration_source:el('configuration-source').value};const tape=parseJson('tape',null);if(tape)payload.tape=tape;const r=await api('/api/dev/step-runs',payload);rid=r.id;await refresh()}
async function submitSelf(){const h=current.handoff||JSON.parse(el('handoff').textContent);await api(`/api/dev/runs/${rid}/handoff`,{contract_version:h.contract_version,request_id:h.request_id,content:el('self-response').value});el('self-response').value='';await refresh()}
async function importGuideline(){
  if(workflow()!=='guideline_qa')throw new Error('guideline_workflow_required');const file=el('guideline-file').files[0];if(!file)throw new Error('source_file_required');if(file.size>1024*1024)throw new Error('file_too_large');const ext=file.name.split('.').pop().toLowerCase();if(!['json','md','txt'].includes(ext))throw new Error('source_format');const content=await file.text();const sources=ext==='json'?JSON.parse(content):[{id:'uploaded',title:file.name,format:ext==='md'?'markdown':'text',content}];const result=await api(`/api/dev/guideline-sets/${encodeURIComponent(el('guideline-set').value)}/import`,{sources,profile_id:el('profile').value});setText('guideline-import-result',JSON.stringify(result,null,2));await loadSets();if(result.run_id){rid=result.run_id;await refresh()}
}
async function copyLegacy(){const r=await api(`/api/runs/${rid}/copy-input`,{});copyInputId=r.copy_input_id;el('workflow').value=r.workflow_id;await chooseWorkflow();el('profile').prepend(new Option('Select fresh model profile',''));el('profile').value='';showProfile();if(r.workflow_id==='guideline_qa'){el('guideline-set').prepend(new Option('Select guideline set',''));el('guideline-set').value='';el('guideline-version').replaceChildren(new Option('Select guideline version',''))}if(![...el('input-mode').options].some(x=>x.value==='copied'))el('input-mode').append(new Option('Copied legacy input','copied'));el('input-mode').value='copied';setText('copied-preview',JSON.stringify(r.input,null,2));showInputs();window.scrollTo({top:0,behavior:'smooth'})}

el('workflow').onchange=guard(chooseWorkflow);el('profile').onchange=showProfile;el('input-mode').onchange=showInputs;el('guideline-set').onchange=showVersions;el('step').onchange=guard(loadFixtures);el('fixture-existing').onchange=guard(chooseSavedFixture);
el('save-profile').onclick=guard(saveProfile);el('verify').onclick=guard(verify);el('start').onclick=guard(start);el('developer').onchange=guard(setDeveloper);el('new-fixture').onclick=guard(newFixture);el('run-step').onclick=guard(runStep);el('capture-step').onclick=guard(captureStep);el('save-fixture').onclick=guard(saveFixture);el('delete-fixture').onclick=guard(deleteFixture);el('promote-fixture').onclick=guard(promoteFixture);el('guideline-import').onclick=guard(importGuideline);el('submit-self').onclick=guard(submitSelf);el('copy-legacy').onclick=guard(copyLegacy);
el('resume').onclick=guard(async()=>{await api(`/api/runs/${rid}/resume`,{});await refresh()});el('delete-run').onclick=guard(async()=>{await api(`/api/runs/${rid}`,undefined,'DELETE');rid=null;current=null;el('output').textContent='';await listRuns()});for(const d of ['approve','revise','reject'])el(d).onclick=guard(()=>review(d));

(async()=>{const ss=await api('/api/session');token=ss.csrf;developer=ss.developer_enabled;el('developer').checked=developer;el('advanced-option').hidden=!developer;document.querySelectorAll('.developer-only').forEach(x=>{if(x.id!=='self-panel'&&x.id!=='advanced-fields')x.hidden=!developer});apps=await api('/api/applications');el('workflow').replaceChildren(...apps.map(x=>new Option(x.name,x.id)));await chooseWorkflow();await listRuns()})().catch(e=>{el('error').hidden=false;el('error').textContent=errorMessage(e)});
