/* Authenticated operational views. Refreshing never starts provider calls. */
let operationsData, editingToolDigest = null, editingToolName = "";
let notificationsEnabled = false;
const notifiedVersions = new Map();
function selectWorkspace(name) { document.querySelector('[data-page="' + name + '"]').click(); }
$("#notify-enable").onclick = async () => {
  if (!("Notification" in window)) { $("#notify-status").textContent = "Browser notifications are unavailable. Use the attention inbox."; return; }
  try {
    notificationsEnabled = (await Notification.requestPermission()) === "granted";
    $("#notify-status").textContent = notificationsEnabled ? "Enabled while this connected tab is open. No email or external messages are sent." : "Permission not granted. Alerts remain in the inbox.";
    await refreshOperations();
  } catch (error) { $("#notify-status").textContent = error.message; }
};
async function refreshOperations() {
  if (!credential) return;
  const data = await api("ops/status", {}); operationsData = data;
  const inbox = $("#ops-alerts"); inbox.replaceChildren();
  const unseen = data.alerts.filter(a => !a.acknowledged);
  inbox.append(el("p", unseen.length + " unread · " + data.alerts.length + " open alerts"));
  for (const alert of data.alerts) {
    const card = el("article"); card.className = "alert-card " + alert.severity;
    card.append(el("strong", readable(alert.kind)), el("p", alert.task_id + " · " + alert.severity + " · " + (alert.acknowledged ? "Acknowledged" : "Needs attention")));
    const actions = el("div"); actions.className = "actions";
    actions.append(button("Inspect task", async () => { selection = alert.workflow_id; selectWorkspace("runs"); }));
    if (!alert.acknowledged) actions.append(button("Acknowledge alert", async () => {
      const reason = prompt("Acknowledgement note:");
      if (reason) await api("ops/alerts/acknowledge", {issue_id: alert.issue_id, version: alert.version, reason});
    }));
    card.append(actions); inbox.append(card);
    if (notificationsEnabled && !alert.acknowledged && notifiedVersions.get(alert.issue_id) !== alert.version) {
      // Generic content keeps private task names and inputs out of OS notifications.
      try { const note = new Notification("Reins needs attention", {body: "An execution alert needs review in your private workspace.", tag: "reins-" + alert.issue_id});
        note.onclick = () => { window.focus(); selectWorkspace("overview"); note.close(); };
        notifiedVersions.set(alert.issue_id, alert.version);
      } catch { notificationsEnabled = false; }
    }
  }
  const tasks = data.tasks.filter(t => !selection || t.workflow_id === selection);
  const tree = $("#ops-tasks"); tree.replaceChildren();
  if (!tasks.length) tree.append(el("p", "No recorded tasks yet."));
  for (const task of tasks) {
    const card = el("article"); card.className = "task-node";
    let depth = 0, parent = task.parent_task_id;
    const visited = new Set([task.task_id]);
    while (parent && !visited.has(parent) && depth < 6) { visited.add(parent); depth++; parent = tasks.find(t => t.task_id === parent)?.parent_task_id; }
    card.style.marginLeft = Math.min(depth, 3) * 12 + "px";
    const heading = el("div"); heading.className = "card-heading";
    heading.append(el("h3", task.task_id), el("span", readable(task.state))); card.append(heading);
    card.append(el("p", task.parent_task_id ? "Child of " + task.parent_task_id : "Workflow root"));
    if (task.control_owner) card.append(el("p", "Controlled by " + task.control_owner));
    card.append(el("p", "Recorded " + money(task.known_cost) + " · Reserved " + money(task.reserved_cost) + " · " + task.pending_calls + " unsettled calls"));
    if (task.checked !== null) card.append(el("p", task.stage + " · " + task.checked + " / " + task.required + " source checks"), meter(task.checked, task.required, "Checked results for " + task.task_id));
    card.append(el("p", "Last recorded: " + readable(task.last_event || "created") + " · " + new Date(task.last_activity * 1000).toLocaleString()));
    tree.append(card);
  }
  const ids = new Set(tasks.map(t => t.task_id));
  $("#ops-handoffs").replaceChildren(el("h3", "Recorded handoffs"), table(["From → To", "Goal", "Shared state", "Receiver"], data.handoffs.filter(h => ids.has(h.sender)).map(h => [h.sender + " → " + h.receiver, h.goal, h.state_name + " v" + h.version + " · " + h.state_status, h.received_at ? "Receipt recorded" : "Awaiting receipt"])));
  $("#ops-tool-history").replaceChildren(table(["Tool", "Version", "Access", "First saved"], data.tool_history.map(t => [t.name, t.digest.slice(0,12), t.policy.read_only ? "Read only" : "Writes data", new Date(t.created * 1000).toLocaleString()])));
  const tools = $("#ops-tools"); tools.replaceChildren();
  for (const tool of data.tools) {
    const card = el("article");
    card.append(el("h3", tool.name), el("p", (tool.policy.read_only ? "Read only" : "Writes data") + " · " + (tool.policy.approval_required || !tool.policy.read_only ? "Approval required" : "Allowed reads")));
    card.append(el("pre", JSON.stringify({fields: tool.policy.fields, allowed_values: tool.policy.allowed_values}, null, 2)));
    card.append(button("Edit " + tool.name, async () => {
      const form = $("#tool-policy"); editingToolName = tool.name; editingToolDigest = tool.digest;
      form.elements.name.value = tool.name; form.elements.access.value = tool.policy.read_only ? "read" : "write";
      form.elements.approval.value = tool.policy.approval_required ? "yes" : "no";
      form.elements.fields.value = JSON.stringify(tool.policy.fields, null, 2);
      form.elements.allowed_values.value = JSON.stringify(tool.policy.allowed_values, null, 2);
      $("#tool-editor").open = true;
    })); tools.append(card);
  }
}
$("#tool-policy").onsubmit = async event => {
  event.preventDefault(); const f = formValues(event.target);
  try {
    const body = {name: f.name, read_only: f.access === "read", approval_required: f.approval === "yes",
      fields: JSON.parse(f.fields), allowed_values: JSON.parse(f.allowed_values), expected_digest: f.name === editingToolName ? editingToolDigest : null};
    const result = await api("ops/tools/save", body);
    editingToolName = f.name; editingToolDigest = result.policy_digest;
    $("#tool-message").textContent = "Saved. New execution policies can pin this version. Existing pinned policies cannot silently adopt changed permissions.";
    await refreshOperations();
  } catch (error) { $("#tool-message").textContent = error.message; }
};
$("#failure-capture").onsubmit = async event => {
  event.preventDefault(); const f = formValues(event.target);
  try {
    await api("ops/failures/capture", {...f, input: JSON.parse(f.input), output: JSON.parse(f.output)});
    $("#failure-message").textContent = "Saved as pending review. Confirm an expected answer before exporting it.";
    await refreshPilot();
  } catch (error) { $("#failure-message").textContent = error.message; }
};
