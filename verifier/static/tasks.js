/* 任务列表、新建/演示任务、需求配置与接口清单（含 OpenAPI 导入） */

async function loadTasks() {
  const data = await api("/api/tasks");
  state.tasks = data.tasks;
  renderTasks();
}

function renderTasks() {
  const box = document.getElementById("task-list");
  if (!state.tasks.length) { box.innerHTML = '<p class="muted">还没有任务。</p>'; return; }
  box.innerHTML = state.tasks
    .map((t) => `
      <div class="task-item ${state.task && state.task.task.id === t.id ? "active" : ""}" onclick="selectTask(${t.id})">
        <div class="t-name">${esc(t.name)}</div>
        <div class="t-meta">#${t.id} · 规则版本 ${t.rule_versions || 0} 个 · 修改后 ${esc(t.candidate_url)}</div>
      </div>`)
    .join("");
}

function toggleNewTask() {
  document.getElementById("new-task-panel").classList.toggle("hidden");
}

async function submitNewTask() {
  const name = document.getElementById("nt-name").value.trim();
  const baseline_url = document.getElementById("nt-baseline").value.trim();
  const candidate_url = document.getElementById("nt-candidate").value.trim();
  const requirement = document.getElementById("nt-requirement").value;
  if (!name) return showToast("请填写任务名称", "error");
  try {
    const data = await api("/api/tasks", { method: "POST", body: { name, baseline_url, candidate_url, requirement } });
    showToast("任务已创建", "success");
    toggleNewTask();
    await loadTasks();
    await selectTask(data.id);
  } catch (e) { showToast(e.message, "error"); }
}

async function createDemoTask(dead) {
  try {
    const data = await api("/api/demo/create-task?dead_candidate=" + (dead ? "true" : "false"), { method: "POST" });
    showToast("演示任务已创建（规则 v1 已确认，共 " + data.checks + " 条检查）", "success");
    await loadTasks();
    await selectTask(data.task_id);
    switchTab("runs");
  } catch (e) { showToast(e.message, "error"); }
}

async function selectTask(id) {
  if (state.pollTimer) { clearTimeout(state.pollTimer); state.pollTimer = null; }
  state.runId = null; state.run = null;
  const data = await api(`/api/tasks/${id}`);
  state.task = data;
  state.versions = data.rule_versions;
  state.runs = data.runs;
  const latest = state.versions[0];
  state.versionId = latest ? latest.id : null;
  state.version = null;
  state.checks = [];
  document.getElementById("empty-state").classList.add("hidden");
  document.getElementById("task-view").classList.remove("hidden");
  document.getElementById("tv-name").textContent = data.task.name;
  document.getElementById("tv-meta").innerHTML =
    `#${data.task.id} · 修改前 <code>${esc(data.task.baseline_url)}</code> → 修改后 <code>${esc(data.task.candidate_url)}</code>`;
  await loadChecksForVersion(state.versionId);
  renderTasks();
  switchTab(state.versions.length ? "rules" : "requirement");
}

function switchTab(tab) {
  state.tab = tab;
  for (const t of ["requirement", "endpoints", "rules", "runs"]) {
    document.getElementById("tab-" + t).classList.toggle("hidden", t !== tab);
  }
  document.querySelectorAll(".tab").forEach((el) => el.classList.toggle("active", el.dataset.tab === tab));
  renderTab();
}

function renderTab() {
  if (!state.task) return;
  if (state.tab === "requirement") renderRequirementTab();
  else if (state.tab === "endpoints") renderEndpointsTab();
  else if (state.tab === "rules") renderRulesTab();
  else if (state.tab === "runs") renderRunsTab();
}

function renderRequirementTab() {
  const t = state.task.task;
  document.getElementById("tab-requirement").innerHTML = `
    <div class="panel">
      <h2>需求与配置</h2>
      <div class="form-grid">
        <label>任务名称<input id="t-name" value="${esc(t.name)}"></label>
        <label>修改前地址<input id="t-baseline" value="${esc(t.baseline_url)}"></label>
        <label>修改后地址<input id="t-candidate" value="${esc(t.candidate_url)}"></label>
      </div>
      <label>需求描述（验收规则的依据）<textarea id="t-requirement" rows="6">${esc(t.requirement)}</textarea></label>
      <div class="check-inline"><input type="checkbox" id="t-allow-remote" ${t.allow_remote ? "checked" : ""}> <label for="t-allow-remote" style="margin:0">允许远程环境（默认只允许 127.0.0.1/localhost）</label></div>
      <div class="check-inline"><input type="checkbox" id="t-allow-writes" ${t.allow_remote_writes ? "checked" : ""}> <label for="t-allow-writes" style="margin:0">允许向远程环境写入数据（危险，POST/PUT/PATCH/DELETE）</label></div>
      <div class="form-actions"><button class="btn primary" onclick="saveTask()">保存配置</button></div>
    </div>`;
}

async function saveTask() {
  const body = {
    name: document.getElementById("t-name").value.trim(),
    requirement: document.getElementById("t-requirement").value,
    baseline_url: document.getElementById("t-baseline").value.trim(),
    candidate_url: document.getElementById("t-candidate").value.trim(),
    allow_remote: document.getElementById("t-allow-remote").checked,
    allow_remote_writes: document.getElementById("t-allow-writes").checked,
  };
  try {
    await api(`/api/tasks/${state.task.task.id}`, { method: "PATCH", body });
    showToast("配置已保存", "success");
    await selectTask(state.task.task.id);
  } catch (e) { showToast(e.message, "error"); }
}

function renderEndpointsTab() {
  const eps = state.task.task.endpoints || [];
  document.getElementById("tab-endpoints").innerHTML = `
    <div class="panel">
      <h2>接口清单（${eps.length} 个）</h2>
      ${eps.length ? `<table><thead><tr><th>方法</th><th>路径</th><th>说明</th></tr></thead><tbody>
        ${eps.map((e) => `<tr><td><code>${esc(e.method)}</code></td><td><code>${esc(e.path)}</code></td><td>${esc(e.summary)}</td></tr>`).join("")}
      </tbody></table>` : '<p class="muted">还没有接口。可从 OpenAPI 导入或手动添加。</p>'}
    </div>
    <div class="panel">
      <h2>导入 OpenAPI</h2>
      <label>OpenAPI 文件地址（如 http://127.0.0.1:8101/openapi.json）</label>
      <input id="ep-spec-url" placeholder="https://.../openapi.json">
      <label style="margin-top:8px">或直接粘贴 OpenAPI JSON 内容</label>
      <textarea id="ep-spec-json" rows="6" placeholder='{"openapi": "3.0.0", "paths": { ... }}'></textarea>
      <div class="form-actions"><button class="btn primary" onclick="importOpenapi()">导入</button></div>
    </div>
    <div class="panel">
      <h2>手动添加接口</h2>
      <div class="form-grid" style="grid-template-columns: 120px 1fr 1fr;">
        <label>方法<select id="ep-method"><option>GET</option><option>POST</option><option>PUT</option><option>PATCH</option><option>DELETE</option></select></label>
        <label>路径<input id="ep-path" placeholder="/api/orders"></label>
        <label>说明<input id="ep-summary" placeholder="订单列表"></label>
      </div>
      <div class="form-actions"><button class="btn" onclick="addEndpoint()">添加</button></div>
    </div>`;
}

async function importOpenapi() {
  const url = document.getElementById("ep-spec-url").value.trim();
  const raw = document.getElementById("ep-spec-json").value.trim();
  const body = {};
  if (raw) {
    try { body.spec = JSON.parse(raw); } catch (e) { return showToast("粘贴的 JSON 无法解析：" + e.message, "error"); }
  } else if (url) { body.spec_url = url; }
  else return showToast("请填写地址或粘贴 JSON", "error");
  try {
    const data = await api(`/api/tasks/${state.task.task.id}/endpoints/import`, { method: "POST", body });
    state.task.task.endpoints = data.endpoints;
    showToast(`成功导入 ${data.endpoints.length} 个接口`, "success");
    renderEndpointsTab();
  } catch (e) { showToast(e.message, "error"); }
}

async function addEndpoint() {
  const body = {
    method: document.getElementById("ep-method").value,
    path: document.getElementById("ep-path").value.trim(),
    summary: document.getElementById("ep-summary").value.trim(),
  };
  try {
    const data = await api(`/api/tasks/${state.task.task.id}/endpoints`, { method: "POST", body });
    state.task.task.endpoints = data.endpoints;
    showToast("接口已添加", "success");
    renderEndpointsTab();
  } catch (e) { showToast(e.message, "error"); }
}
