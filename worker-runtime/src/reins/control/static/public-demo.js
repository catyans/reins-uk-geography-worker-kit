'use strict';
// Public visitors can inspect the full console and run bounded example tasks.
// The backend independently enforces the route allowlist and quota.
const demoActions=new Set([
  'open-workspace','nav','run','step','experiment','sort','page','group',
  'clear-filters','approval-tab','review-tab','experiment-json','print',
  'runs-csv','attribution-csv','company-select','company-history',
  'company-replay','brief-download','workload-download','company-new',
  'company-report','company-ask','company-ask-submit',
  'company-examples','workloads-new','workload-kind','workload-example',
  'workloads-examples','copy-trace',
]);
const demoForms=new Set([
  'company-submit','workload-submit','workloads-examples','multi-submit',
  'portfolio-submit',
]);
function lockPublicControls(scope){
  for(const button of scope.querySelectorAll('[data-act]')){
    if(!demoActions.has(button.dataset.act))button.hidden=true;
  }
  for(const form of scope.querySelectorAll('form[id]')){
    if(demoForms.has(form.id)){
      const budget=form.querySelector('[name="budget"]');
      if(budget)budget.max='1.5';
      continue;
    }
    for(const field of form.querySelectorAll('input,select,textarea,button'))field.disabled=true;
    if(!form.querySelector('.public-demo-note')){
      const note=document.createElement('p');note.className='public-demo-note';
      note.textContent='Configuration changes are available in a private workspace.';
      form.prepend(note);
    }
  }
}
const demoDraw=draw;
draw=function(){demoDraw();lockPublicControls($('#content'));lockPublicControls($('#page-actions'));};
const demoOpenDialog=openDialog;
openDialog=function(...args){demoOpenDialog(...args);lockPublicControls($('#dialog-content'));};
const demoAction=action;
action=async function(act,id){
  if(!demoActions.has(act))throw Error('This public workspace cannot change control settings.');
  return demoAction(act,id);
};
document.addEventListener('submit',e=>{
  if(e.target.method==='dialog'||demoForms.has(e.target.id))return;
  e.preventDefault();e.stopImmediatePropagation();
},true);
$('#mode-label').textContent='Public demo · task spend capped';
$('#connection-state').textContent='Live task execution';
const demoRefresh=refresh;
refresh=async function(...args){
  await demoRefresh(...args);
  if(snapshot){
    $('#connection-state').textContent='Online task service';
    $('#sync-status').textContent='Live results · updates every 5s';
    $('#mode-label').textContent='Public demo';
  }
};
