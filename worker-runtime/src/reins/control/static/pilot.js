/* Real, authenticated operator actions. Rendering and previews never dispatch paid work. */
let pilotData, invoiceRows = [], invoiceReady = false;
const reviewDrafts = new Map();
const readable = s => String(s).replaceAll("_", " ");
function formValues(form) { return Object.fromEntries(new FormData(form)); }
function downloadJSON(name, data) {
  const url = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2)], {type: "application/json"}));
  const a = el("a"); a.href = url; a.download = name + ".json"; a.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
}
function button(label, handler) {
  const b = el("button", label); b.type = "button";
  b.onclick = async () => { b.disabled = true; try { await handler(); await refreshPilot(); await refresh(); }
    catch (err) { $("#message").textContent = err.message; } finally { b.disabled = false; } };
  return b;
}
for (const b of document.querySelectorAll("[data-page]")) b.onclick = () => {
  for (const panel of document.querySelectorAll("[data-panel]")) panel.hidden = panel.dataset.panel !== b.dataset.page;
  for (const tab of document.querySelectorAll("[data-page]")) {
    if (tab === b) tab.setAttribute("aria-current", "page"); else tab.removeAttribute("aria-current");
  }
};
function policyBody() {
  const f = formValues($("#policy-form"));
  return {policy_id: f.policy_id, workflow: {budget: f.budget, wrapup_reserve: f.wrapup_reserve,
    mode: "enforce", max_iterations: Number(f.max_iterations), repeat_limit: Number(f.repeat_limit),
    failure_limit: Number(f.failure_limit), allowed_tools: f.allowed_tools.split(",").map(s => s.trim()).filter(Boolean)}, progress: {window: Number(f.window), action: f.action}};
}
$("#policy-form").onsubmit = async e => {
  e.preventDefault(); try { await api("pilot/policies/save", policyBody()); $("#policy-message").textContent = "Policy saved. Existing runs keep their original version."; await refreshPilot(); }
  catch (err) { $("#policy-message").textContent = err.message; }
};
$("#policy-preview").onsubmit = async e => {
  e.preventDefault(); try {
    const data = await api("pilot/policies/preview", {...policyBody(), ...formValues(e.target)});
    $("#preview-result").textContent = data.historical_matches.length + " tasks match this progress rule. " + data.note;
  } catch (err) { $("#preview-result").textContent = err.message; }
};
$("#workflow-form").onsubmit = async e => {
  e.preventDefault(); try { await api("workflows", formValues(e.target)); $("#workflow-message").textContent = "Task provisioned. No paid work has started."; await refresh(); }
  catch (err) { $("#workflow-message").textContent = err.message; }
};
async function refreshPilot() {
  if (!credential) return;
  const data = await api("pilot/status", {}); pilotData = data;
  $("#pilot-summary").replaceChildren(el("h2", data.issues.filter(i => i.state === "open").length + " issues need attention"),
    el("p", data.reviews.filter(i => i.state === "pending").length + " results await review · " + data.policies.length + " saved policy versions"));
  $("#policy-list").replaceChildren(table(["Version", "Task budget", "Progress window", "Action"], data.policies.map(p => [p.id, money(p.body.workflow.budget), p.body.progress.window, readable(p.body.progress.action)])));
  const issues = $("#pilot-issues"); issues.replaceChildren();
  for (const issue of data.issues.filter(i => i.state === "open")) {
    const card = el("article"); card.append(el("h3", readable(issue.kind)), el("p", "Task: " + issue.task + " · Workflow: " + issue.workflow));
    const evidence = issue.body.evidence;
    if (evidence.attempts_without_progress) card.append(el("p", evidence.attempts_without_progress + " settled attempts without new checked results. Missing: " + evidence.missing_results.join(", ")));
    else card.append(el("p", evidence.reason ? readable(evidence.reason) : "Inspect execution history and confirm the next safe action."));
    const control = data.controls.find(c => c.task === issue.task);
    card.append(el("p", "Control: " + (control ? readable(control.state) : "Advice only") + " · " + issue.body.count + " recorded occurrences"));
    const actions = el("div"); actions.className = "actions";
    for (const [label, action] of [["Pause task", "pause"], ["Stop further work", "wrapup"], ["Resume task", "resume"], ["Mark reviewed", "resolve"]]) {
      if (control?.state === "wrapup" && action !== "resolve") continue;
      if (action === "resume" && !control) continue;
      actions.append(button(label, async () => {
        const reason = prompt(label + ": explain your decision. This affects task " + issue.task + " and its descendants.");
        if (reason) await api("pilot/issues/action", {issue_id: issue.id, version: issue.version, action_id: crypto.randomUUID(), action, reason});
      }));
    }
    actions.append(button("Create regression case", async () => {
      const form = $("#failure-capture"); form.elements.issue_id.value = issue.id;
      form.elements.review_id.value = crypto.randomUUID();
      const existing = data.reviews.find(r => r.workflow === issue.workflow && issue.task === issue.workflow);
      form.elements.input.value = existing ? JSON.stringify(existing.body.submitted.input, null, 2) : "{}";
      form.elements.output.value = existing ? JSON.stringify(existing.body.submitted.output, null, 2) : "{}";
      form.elements.source_version.value = existing?.body.submitted.source_version || "";
      $("#failure-details").open = true; $("#failure-details").scrollIntoView({behavior: "smooth"});
    }));
    card.append(actions); issues.append(card);
  }
  if (!issues.children.length) issues.append(el("p", "No open issues. Review task history in Runs."));
  const reviews = $("#pilot-reviews");
  // Polling must not erase an operator's in-progress review.
  if (!reviews.contains(document.activeElement) && reviews.dataset.snapshot !== JSON.stringify(data.reviews)) {
    reviews.dataset.snapshot = JSON.stringify(data.reviews); reviews.replaceChildren();
    for (const review of data.reviews) {
      const card = el("article"); card.append(el("h3", review.id), el("p", "Workflow: " + review.workflow + " · " + readable(review.state)));
      const source = el("pre", JSON.stringify(review.body.submitted, null, 2)); source.className = "evidence";
      const detail = el("details"); detail.append(el("summary", "Inspect input, output and source version"), source); card.append(detail);
      if (review.state === "pending") {
        const expected = el("textarea"); expected.rows = 4; expected.placeholder = 'Confirmed expected fields, e.g. {"license":"MIT"}'; expected.setAttribute("aria-label", "Expected result for " + review.id);
        const rubric = el("input"); rubric.placeholder = "Rubric version"; rubric.setAttribute("aria-label", "Rubric version");
        const reviewer = el("input"); reviewer.placeholder = "Reviewer label"; reviewer.setAttribute("aria-label", "Reviewer label");
        const draft = reviewDrafts.get(review.id);
        if (draft) { expected.value = draft.expected; rubric.value = draft.rubric; reviewer.value = draft.reviewer; }
        for (const field of [expected, rubric, reviewer]) field.addEventListener("input", () => {
          reviewDrafts.set(review.id, {expected: expected.value, rubric: rubric.value, reviewer: reviewer.value});
        });
        card.append(expected, rubric, reviewer);
        const actions = el("div"); actions.className = "actions";
        for (const decision of ["approve", "reject"]) actions.append(button(decision === "approve" ? "Confirm expected result" : "Exclude from test set", async () => {
          const reason = prompt("Review reason:"); if (!reason) return;
          await api("pilot/reviews/decide", {review_id: review.id, version: review.version, decision,
            expected: decision === "approve" ? JSON.parse(expected.value) : {}, rubric_version: rubric.value, reviewer: reviewer.value, reason});
          reviewDrafts.delete(review.id);
        }));
        card.append(actions);
      }
      reviews.append(card);
    }
  }
  $("#pilot-costs").replaceChildren(table(["Request / workflow", "Recorded", "Reserved", "Source", "Price version"], data.costs.map(c => [c.request_id + " / " + c.workflow_id, money(c.actual), c.actual === null ? money(c.reserved) : "Settled", readable(c.source), c.price_version || "Not recorded"])));
  $("#invoice-history").replaceChildren(table(["Invoice / line", "Revision", "Amount", "Request", "Matched"], data.bills.map(b => [b.invoice_id + " / " + b.line_id, b.revision, money(b.amount), b.request_id, b.matched ? "Yes" : "No"])));
}
$("#suite-export").onsubmit = async e => {
  e.preventDefault(); try {
    const f = formValues(e.target), body = {suite_id: f.suite_id, review_ids: f.review_ids.split(",").map(s => s.trim()).filter(Boolean)};
    downloadJSON("reins-reviewed-suite", await api("pilot/suites/export", body));
  } catch (err) { $("#message").textContent = err.message; }
};
function parseCSV(text) {
  const rows = []; let row = [], cell = "", quoted = false;
  for (let i=0; i<text.length; i++) {
    const c = text[i];
    if (c === '"') { if (quoted && text[i+1] === '"') { cell += '"'; i++; } else quoted = !quoted; }
    else if (c === ',' && !quoted) { row.push(cell); cell = ""; }
    else if ((c === '\n' || c === '\r') && !quoted) { if (c === '\r' && text[i+1] === '\n') i++; row.push(cell); if (row.some(v => v)) rows.push(row); row = []; cell = ""; }
    else cell += c;
  }
  if (quoted) throw Error("Unclosed quoted CSV field");
  row.push(cell); if (row.some(v => v)) rows.push(row);
  const headers = rows.shift()?.map(h => h.replace(/^\uFEFF/, "").trim());
  if (!headers || new Set(headers).size !== headers.length) throw Error("Missing or duplicate CSV headers");
  return rows.map(r => { if (r.length !== headers.length) throw Error("CSV row width differs from header"); const x = Object.fromEntries(headers.map((h,i) => [h,r[i]])); x.revision = Number(x.revision || 1); return x; });
}
$("#invoice-file").onchange = () => { invoiceReady = false; invoiceRows = []; $("#invoice-import").disabled = true; };
$("#invoice-preview").onclick = async () => {
  invoiceReady = false; $("#invoice-import").disabled = true;
  try {
    const file = $("#invoice-file").files[0]; if (!file || file.size > 900000) throw Error("Choose a CSV smaller than 900 KB");
    invoiceRows = parseCSV(await file.text());
    const r = await api("pilot/invoices/preview", {rows: invoiceRows});
    $("#invoice-results").replaceChildren(table(["Row", "Match", "Difference", "Status"], r.rows.map(x => [x.row, x.matched ? "Matched" : "Unmatched", x.difference === null ? "Not available" : money(x.difference), x.error || (x.duplicate ? "Already imported" : "Ready")])));
    invoiceReady = r.valid; $("#invoice-import").disabled = !r.valid;
  } catch (err) { $("#invoice-results").textContent = err.message; }
};
$("#invoice-import").onclick = async () => {
  if (!invoiceReady) return; $("#invoice-import").disabled = true;
  let done = 0;
  try { for (const row of invoiceRows) { await api("reconcile", row); done++; } invoiceReady = false; $("#invoice-results").textContent = done + " invoice rows imported. Revisions retained."; await refreshPilot(); await refresh(); }
  catch (err) { invoiceReady = false; $("#invoice-results").textContent = done + " rows imported. Preview again to safely resume. " + err.message; }
};
$("#economic-form").onsubmit = async e => {
  e.preventDefault(); try { await api("economics", formValues(e.target)); $("#economics-message").textContent = "Entry recorded."; await refresh(); }
  catch (err) { $("#economics-message").textContent = err.message; }
};
