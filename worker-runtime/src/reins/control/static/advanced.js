'use strict';
// Progressive controls share the existing API, escaping, dialogs and draft handling.
const runSelection=new Set();
let bulkCases=[],automationSnapshot={rules:[]};
const adviceCache=new Map();
function automationRows(){return table(['Rule','Deployment','State','Reviewed outcomes',''],automationSnapshot.rules.map(r=>[esc(r.id),esc(r.body.deployment),status(r.state),esc(r.body.observed_outcomes||0),['waiting','monitoring'].includes(r.state)?button('Disable','automation-disable',r.id):'—']));}
const priorDraw=draw;
draw=function(){priorDraw();
 if(route==='runs'&&!selected){

  // Task buttons identify rows without relying on column order or displayed labels.
  for(const b of document.querySelectorAll('#content button[data-act="run"]')){
   const tr=b.closest('tr');if(!tr||tr.querySelector('[data-run-pick]'))continue;
   const box=document.createElement('input');box.type='checkbox';box.dataset.runPick=b.dataset.id;
   box.checked=runSelection.has(b.dataset.id);box.setAttribute('aria-label','Select task '+b.dataset.id);
   b.parentNode.prepend(box);
  }
  const bar=document.createElement('div');bar.className='actions tabs';
  bar.innerHTML=button('Select this page','pick-page')+button('Clear selection','unpick')+button('Create review batch','batch-reviews')+`<small id="run-pick-count">${runSelection.size} selected</small>`;
  $('#content').prepend(bar);
 }
 if(route==='runs'&&selected&&detail){
  $('#content').insertAdjacentHTML('beforeend',panel('Should this task continue?',
   form('economic-advice',field('success_value','Value of a successful remaining task (USD)','1','number','min="0" step="0.01"'),'Estimate from history')+
   `<div id="economic-result">${adviceCache.get(detail.workflow_id)||''}</div>`,'Uses confirmed comparable outcomes; unknown results are excluded.')+
   (detail.state==='completed'?panel('Confirm business outcome',form('business-outcome',select('accepted','Did this task meet its business requirements?',[['true','Accepted'],['false','Not accepted']])+field('reason','Outcome evidence','','text','required'),'Record outcome')):''));
 }
 if(route==='experiments'&&!selected){
  $('#content').insertAdjacentHTML('beforeend',panel('Automatic adoption and quality rollback',
   button('Enable an automation rule','automation-new')+' '+button('Refresh rules','automation-refresh')+`<div id="automation-list">${automationRows()}</div>`,
   'Explicitly authorize a completed experiment to replace its matching baseline. Running tasks keep their configuration.'));
 }
 restoreDrafts();
};
const priorAction=action;
action=async function(act,id){
 if(act==='pick-page'){for(const w of snapshot.q.workflows)runSelection.add(w.id);draw();return;}
 if(act==='unpick'){runSelection.clear();draw();return;}
 if(act==='batch-reviews'){
  if(!runSelection.size||runSelection.size>50)throw Error('Select between 1 and 50 tasks.');
  bulkCases=await Promise.all([...runSelection].map(workflow_id=>api('console/detail',{workflow_id})));
  const fields=bulkCases.map((d,i)=>{
   const saved=snapshot.catalog.runs.find(r=>r.spec.run_id===d.workflow_id)?.spec;
   const captured=snapshot.pilot.reviews.find(r=>r.workflow===d.workflow_id)?.body.submitted;
   const input=captured?.input||saved?.input||{};
   return `<fieldset class="wide"><legend>${esc(d.workflow_id)}</legend>`+field('source_'+i,'Source version','recorded-v1','text','required')+
    area('input_'+i,'Original Agent input',input,2)+area('output_'+i,'Result to review',captured?.output||d.requests.at(-1)?.output||{},2)+'</fieldset>';
  }).join('');
  openDialog('Create pending review cases',form('batch-reviews',`<input type="hidden" name="batch_id" value="${uuid()}">`+fields,'Create review batch')+
   '<p class="small">Confirm original inputs. Missing inputs are never invented. Every case still requires review before entering a test set.</p>');return;
 }
 if(act==='automation-refresh'){
  automationSnapshot=await api('console/automation/list');
  $('#automation-list').innerHTML=automationRows();return;
 }
 if(act==='automation-disable')return confirmAction('Disable automation','The currently deployed version stays active.',f=>api('console/automation/disable',{id,reason:f.reason}));
 if(act==='automation-new'){
  if(!snapshot.catalog.deployments.length)throw Error('Adopt a baseline deployment first.');
  openDialog('Authorize automatic adoption',form('automation-rule',field('id','Rule name',uuid())+
   select('deployment','Existing deployment',snapshot.catalog.deployments.map(d=>[d.id,d.id+' · version '+d.version]))+
   select('experiment','Comparison experiment',snapshot.catalog.experiments.map(e=>e.id))+
   field('min_savings','Minimum savings (%)',10,'number','min="1" max="100"')+
   field('min_samples','Confirmed outcomes before rollback check',30,'number','min="30"')+
   field('rollback_below','Roll back below acceptance (%)',95,'number','min="0" max="100"')+
   field('reason','Authorization note','','text','required'),'Enable rule')+
   '<p class="small">The experiment needs at least 30 cases. Its baseline must match the deployment. A newer manual configuration supersedes the rule.</p>');return;
 }
 await priorAction(act,id);
 if(act==='new-experiment'&&$('#experiment')){
  const setup=$('#experiment [name=setup]');setup.add(new Option('Visual configuration builder','visual'));setup.value='visual';
  const variants=$('#experiment [name=variants]');variants.disabled=true;variants.closest('label').hidden=true;
  const editor=document.createElement('div');editor.id='variant-editor';editor.className='wide';
  editor.innerHTML=['baseline','candidate'].map(arm=>`<fieldset data-config-arm="${arm}"><legend>${human(arm)} configuration</legend><div class="config-pairs"></div>${button('Add setting','config-add',arm)}</fieldset>`).join('');
  variants.closest('label').after(editor);addPair('baseline','model','flash');addPair('candidate','model','lite');
 }
 if(act==='config-add')addPair(id);
 if(act==='config-remove')document.getElementById(id)?.remove();
};
function addPair(arm,key='',value=''){
 const node=document.createElement('div');node.id='pair-'+uuid();node.className='config-pair form-grid';
 node.innerHTML=field('setting-key','Setting name',key)+select('setting-type','Type',['text','number','boolean','json'])+field('setting-value','Value',value)+button('Remove setting','config-remove',node.id);
 document.querySelector(`[data-config-arm="${arm}"] .config-pairs`).append(node);
}
const priorVariants=experimentVariants;
experimentVariants=function(v){if(v.setup!=='visual')return priorVariants(v);
 return ['baseline','candidate'].map(arm=>{
  const config=Object.create(null);
  for(const row of document.querySelectorAll(`[data-config-arm="${arm}"] .config-pair`)){
   const key=row.querySelector('[name=setting-key]').value.trim(),type=row.querySelector('[name=setting-type]').value,raw=row.querySelector('[name=setting-value]').value;
   if(!key||Object.hasOwn(config,key))throw Error('Each setting needs a unique name.');
   let value=raw;if(type==='number'){value=Number(raw);if(!raw.trim()||!Number.isFinite(value))throw Error(key+': enter a finite number.');}
   if(type==='boolean'){if(!['true','false'].includes(raw))throw Error(key+': enter true or false.');value=raw==='true';}
   if(type==='json'){try{value=JSON.parse(raw);}catch{throw Error(key+': enter valid JSON.');}}
   config[key]=value;
  }
  return {id:arm==='baseline'?v.baseline:(v.baseline==='candidate'?'alternative':'candidate'),policy:v.policy,config};
 });
};
document.addEventListener('change',e=>{
 if(e.target.dataset.runPick){e.target.checked?runSelection.add(e.target.dataset.runPick):runSelection.delete(e.target.dataset.runPick);$('#run-pick-count').textContent=runSelection.size+' selected';}
 if(e.target.name==='setup'&&$('#variant-editor')){
  const visual=e.target.value==='visual';$('#variant-editor').hidden=!visual;
  const a=$('#experiment [name=variants]');a.disabled=visual||e.target.value==='gemini';a.closest('label').hidden=a.disabled;
 }
});
document.addEventListener('submit',async e=>{
 const f=e.target,id=f.getAttribute('id');if(!['batch-reviews','economic-advice','business-outcome','automation-rule'].includes(id))return;
 e.preventDefault();e.stopImmediatePropagation();if(f.dataset.submitting)return;f.dataset.submitting='true';
 const b=f.querySelector('.form-footer button');b.disabled=true;f.querySelector('.inline-error')?.remove();
 const v=Object.fromEntries(new FormData(f));
 try{
  if(id==='batch-reviews'){
   await api('console/reviews/batch',{batch_id:v.batch_id,items:bulkCases.map((d,i)=>({workflow_id:d.workflow_id,source_version:v['source_'+i],input:parseObject(v['input_'+i],'Original input'),output:parseObject(v['output_'+i],'Result')}))});
   runSelection.clear();$('#dialog').close();approvalTab='quality';navigate('approvals');notify('Review cases created. Confirm their expected results before making a test set.');
  }
  if(id==='economic-advice'){
   const result=await api('console/economics/decision',{workflow_id:detail.workflow_id,success_value:v.success_value});
   $('#economic-result').innerHTML=`<h3>${esc(human(result.action))}</h3><p>${esc(human(result.reason))} · ${result.samples} comparable outcomes</p>`+
    (result.success_probability===null?'':`<p>Historical acceptance: ${pct(result.success_probability)} (${pct(result.probability_low)}–${pct(result.probability_high)}). Remaining recorded API cost P90: ${cash(result.remaining_p90)}.</p>`)+
    '<p class="small">A historical estimate, not a guarantee or causal prediction. No task was stopped.</p>';adviceCache.set(detail.workflow_id,$('#economic-result').innerHTML);
  }
  if(id==='business-outcome'){await api('console/outcomes/confirm',{workflow_id:detail.workflow_id,accepted:v.accepted==='true',reason:v.reason});notify('Business outcome recorded.');}
  if(id==='automation-rule'){
   const d=snapshot.catalog.deployments.find(d=>d.id===v.deployment);
   await api('console/automation/save',{...v,expected_version:d.version,min_savings:Number(v.min_savings)/100,min_samples:Number(v.min_samples),rollback_below:Number(v.rollback_below)/100});
   $('#dialog').close();notify('Automation authorized.');await action('automation-refresh');
  }
 }catch(err){const p=document.createElement('p');p.className='inline-error form-note';p.setAttribute('role','alert');p.textContent=err.message;f.append(p);}
 finally{delete f.dataset.submitting;b.disabled=false;}
},true);
