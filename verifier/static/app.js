/* 改动验收器 前端逻辑（无构建步骤，原生 JS） */

const state = {
  tasks: [],
  task: null,
  versions: [],
  runs: [],
  tab: "requirement",
  versionId: null,
  version: null, // 完整版本 {id, version, status, note, checks}
  checks: [], // 可编辑副本
  dirty: false,
  runId: null,
  run: null,
  pollTimer: null,
  llm: { available: false, hint: "" },
};

const CATEGORY_LABELS = { existing_behavior: "保持原有行为", new_requirement: "新需求验收" };
const STATUS_LABELS = { passed: "通过", failed: "失败", unverifiable: "无法判断" };
const RUN_STATUS_LABELS = { running: "运行中", completed: "已完成", cancelled: "已取消", error: "执行出错" };

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function showToast(msg, type = "") {
  const el = document.getElementById("toast");
  el.className = "toast " + type;
  el.textContent = msg;
  clearTimeout(el._t);
  el._t = setTimeout(() => el.classList.add("hidden"), 6000);
}

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  let data = null;
  try { data = await res.json(); } catch (e) { /* 非 JSON 响应 */ }
  if (!res.ok) {
    let msg = `请求失败（${res.status}）`;
    const d = data && data.detail;
    if (typeof d === "string") msg = d;
    else if (d && d.message) msg = d.message + (d.errors ? "\n" + d.errors.slice(0, 8).join("\n") : "");
    throw new Error(msg);
  }
  return data;
}

/* ---------------------------------------------------------------- 初始化 */

async function init() {
  try {
    const llm = await api("/api/llm/status");
    state.llm = llm;
    const chip = document.getElementById("llm-status");
    chip.textContent = (llm.available ? "LLM 建议：已配置（" + llm.model + "）" : "LLM 建议：未配置（可手动编写规则）");
    chip.className = "llm-chip " + (llm.available ? "on" : "off");
  } catch (e) { /* 忽略 */ }
  await loadTasks();
}

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

/* ---------------------------------------------------------------- 新建 / 演示任务 */

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

/* ---------------------------------------------------------------- 任务详情 */

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

/* ---------------------------------------------------------------- 需求与配置 */

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

/* ---------------------------------------------------------------- 接口清单 */

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

/* ---------------------------------------------------------------- 验收规则 */

async function loadChecksForVersion(versionId) {
  if (!versionId) { state.version = null; state.checks = []; return; }
  const v = await api(`/api/rule-versions/${versionId}`);
  state.version = v;
  state.checks = JSON.parse(JSON.stringify(v.checks));
  state.dirty = false;
}

function versionOptions() {
  return state.versions
    .map((v) => `<option value="${v.id}" ${v.id === state.versionId ? "selected" : ""}>v${v.version} · ${v.status === "confirmed" ? "已确认" : "草稿"} · ${v.check_count} 条检查${v.note ? " · " + esc(v.note).slice(0, 30) : ""}</option>`)
    .join("");
}

function renderRulesTab() {
  const box = document.getElementById("tab-rules");
  if (!state.version) {
    box.innerHTML = `<div class="panel"><h2>验收规则</h2>
      <p class="muted">还没有规则版本。可以手动添加检查、加载演示规则，或（配置模型 Key 后）请求 AI 建议。</p>
      <div class="form-actions">
        <button class="btn primary" onclick="newEmptyChecks()">新建空白规则</button>
        <button class="btn" onclick="loadDemoChecks()">加载演示规则</button>
        <button class="btn" onclick="aiSuggest()">AI 建议（可选）</button>
      </div></div>`;
    return;
  }
  const v = state.version;
  const refOptions = state.checks.filter((c) => c.category === "new_requirement").map((c) => c.id);
  box.innerHTML = `
    <div class="panel">
      <div class="form-grid" style="grid-template-columns: 1fr 2fr;">
        <label>规则版本<select onchange="onVersionChange(this.value)">${versionOptions()}</select></label>
        <label>版本说明<input id="rv-note" value="${esc(v.note || "")}" placeholder="例：根据需求 v2 调整"></label>
      </div>
      <div class="muted">
        当前：v${v.version}（${v.status === "confirmed" ? "已确认" : "草稿，未确认"}）
        ${v.status !== "confirmed" ? "· 草稿版本可以运行，但结果仅供参考" : ""}
        ${state.dirty ? '· <b style="color:#8a5a10">有未保存的修改</b>' : ""}
      </div>
      <div class="form-actions">
        <button class="btn primary" onclick="saveVersion(true)">保存并确认为新版本</button>
        <button class="btn" onclick="saveVersion(false)">仅保存草稿</button>
        ${v.status !== "confirmed" ? '<button class="btn" onclick="confirmCurrentVersion()">确认当前版本</button>' : ""}
        <button class="btn" onclick="loadDemoChecks()">加载演示规则</button>
        <button class="btn" onclick="aiSuggest()">AI 建议（可选）</button>
        <button class="btn" onclick="addCheck()">＋ 添加检查</button>
      </div>
    </div>
    <div class="panel" id="checks-editor">
      ${state.checks.length ? "" : '<p class="muted">规则为空。点击上方「添加检查」或「加载演示规则」。</p>'}
      ${state.checks.map((c, i) => checkCard(c, i, refOptions)).join("")}
    </div>`;
}

function checkCard(c, i, refOptions) {
  const stepsJson = JSON.stringify(c.steps ?? [], null, 2);
  const refSel = `<select id="cc-ref-${i}"><option value="">（无）</option>${refOptions
    .filter((rid) => rid !== c.id)
    .map((rid) => `<option value="${esc(rid)}" ${c.expected_change_ref === rid ? "selected" : ""}>${esc(rid)}</option>`)
    .join("")}</select>`;
  return `
    <div class="check-card" data-index="${i}">
      <div class="cc-head">
        <label>名称<input id="cc-name-${i}" value="${esc(c.name || "")}"></label>
        <label>id<input id="cc-id-${i}" value="${esc(c.id || "")}"></label>
        <label>类型<select id="cc-category-${i}" onchange="markDirty()">
          <option value="existing_behavior" ${c.category !== "new_requirement" ? "selected" : ""}>保持原有行为</option>
          <option value="new_requirement" ${c.category === "new_requirement" ? "selected" : ""}>新需求验收</option>
        </select></label>
        <label>行为变化引用（旧行为被新需求替代时填）${refSel}</label>
      </div>
      <label>期望说明<input id="cc-expectation-${i}" value="${esc(c.expectation || "")}"></label>
      <label style="margin-top:6px" for="cc-steps-${i}">步骤（JSON，含 method/path/headers/json/extract/assert）</label>
      <textarea id="cc-steps-${i}" rows="${Math.min(18, Math.max(6, stepsJson.split("\n").length))}">${esc(stepsJson)}</textarea>
      ${c._suggestion_errors ? `<div class="err-line">AI 建议存在问题：${esc(c._suggestion_errors.join("；"))}</div>` : ""}
      <div class="cc-tools">
        <span class="muted">检查 ${i + 1} / ${state.checks.length}</span>
        <span>
          <button class="btn small" onclick="duplicateCheck(${i})">复制</button>
          <button class="btn small danger" onclick="removeCheck(${i})">删除</button>
        </span>
      </div>
    </div>`;
}

function markDirty() { state.dirty = true; }

function collectChecks() {
  const checks = [];
  const cards = document.querySelectorAll(".check-card");
  for (const card of cards) {
    const i = card.dataset.index;
    const id = document.getElementById(`cc-id-${i}`).value.trim();
    const name = document.getElementById(`cc-name-${i}`).value.trim();
    const category = document.getElementById(`cc-category-${i}`).value;
    const ref = document.getElementById(`cc-ref-${i}`).value;
    const expectation = document.getElementById(`cc-expectation-${i}`).value.trim();
    let steps;
    try {
      steps = JSON.parse(document.getElementById(`cc-steps-${i}`).value);
    } catch (e) {
      showToast(`检查 ${id || i} 的步骤 JSON 无法解析：${e.message}`, "error");
      return null;
    }
    const check = { id, name, category, expectation, steps };
    if (ref) check.expected_change_ref = ref;
    checks.push(check);
  }
  return checks;
}

function addCheck() {
  const checks = collectChecks();
  if (checks === null) return;
  checks.push({
    id: "new-check-" + (checks.length + 1),
    name: "新的检查",
    category: "existing_behavior",
    expectation: "",
    steps: [{ name: "步骤 1", method: "GET", path: "/", assert: [{ type: "status", op: "equals", value: 200 }] }],
  });
  state.checks = checks;
  state.dirty = true;
  renderRulesTab();
}

function duplicateCheck(i) {
  const checks = collectChecks();
  if (checks === null) return;
  const copy = JSON.parse(JSON.stringify(checks[i]));
  copy.id = copy.id + "-copy";
  copy.name = copy.name + "（副本）";
  checks.splice(i + 1, 0, copy);
  state.checks = checks;
  state.dirty = true;
  renderRulesTab();
}

function removeCheck(i) {
  const checks = collectChecks();
  if (checks === null) return;
  if (!confirm(`确定删除检查「${checks[i].name}」？保存为新版本后才会生效。`)) return;
  checks.splice(i, 1);
  state.checks = checks;
  state.dirty = true;
  renderRulesTab();
}

function newEmptyChecks() {
  state.version = { id: null, version: "-", status: "draft", note: "", checks: [] };
  state.versionId = null;
  state.checks = [];
  addCheck();
}

async function loadDemoChecks() {
  try {
    const demo = await api("/api/demo/config");
    state.checks = demo.checks;
    state.dirty = true;
    if (!state.version) state.version = { id: null, version: "-", status: "draft", note: "", checks: [] };
    renderRulesTab();
    showToast("已加载演示规则（" + demo.checks.length + " 条检查）。注意：演示规则针对示例服务，保存前请核对登录凭据。", "success");
  } catch (e) { showToast(e.message, "error"); }
}

async function aiSuggest() {
  if (!state.llm.available) {
    showToast("未配置模型 API Key（LLM_API_KEY / OPENAI_API_KEY）。\n核心流程不依赖模型：可手动编写规则或加载演示规则。", "error");
    return;
  }
  try {
    showToast("正在请求模型建议…");
    const data = await api(`/api/tasks/${state.task.task.id}/suggest-checks`, { method: "POST", body: {} });
    const current = collectChecks();
    if (current === null) return;
    state.checks = current.concat(data.checks);
    state.dirty = true;
    if (!state.version) state.version = { id: null, version: "-", status: "draft", note: "", checks: [] };
    renderRulesTab();
    showToast(`模型（${data.model}）提出 ${data.checks.length} 条建议，仅供参考。` + (data.warnings.length ? "\n" + data.warnings.join("\n") : ""), "success");
  } catch (e) { showToast(e.message, "error"); }
}

async function onVersionChange(versionId) {
  if (state.dirty && !confirm("当前有未保存的修改，切换版本会丢弃它们。继续？")) {
    return renderRulesTab();
  }
  state.versionId = Number(versionId);
  await loadChecksForVersion(state.versionId);
  renderRulesTab();
}

async function saveVersion(confirmVersion) {
  const checks = collectChecks();
  if (checks === null) return;
  const note = document.getElementById("rv-note") ? document.getElementById("rv-note").value.trim() : "";
  try {
    const data = await api(`/api/tasks/${state.task.task.id}/rule-versions`, {
      method: "POST",
      body: { checks, note, confirm: !!confirmVersion },
    });
    showToast(`已保存规则 v${data.version}（${data.status === "confirmed" ? "已确认" : "草稿"}）`, "success");
    await selectTask(state.task.task.id);
    switchTab("rules");
  } catch (e) { showToast(e.message, "error"); }
}

async function confirmCurrentVersion() {
  try {
    await api(`/api/rule-versions/${state.versionId}/confirm`, { method: "POST" });
    showToast("当前版本已确认", "success");
    await selectTask(state.task.task.id);
    switchTab("rules");
  } catch (e) { showToast(e.message, "error"); }
}

/* ---------------------------------------------------------------- 运行与报告 */

function renderRunsTab() {
  const taskId = state.task.task.id;
  const box = document.getElementById("tab-runs");
  const versionInfo = state.versionId ? `将使用规则 v${state.version ? state.version.version : "?"}（${state.version && state.version.status === "confirmed" ? "已确认" : "草稿"}）` : "还没有规则版本";
  box.innerHTML = `
    <div class="panel">
      <h2>运行验收</h2>
      <div class="muted">${versionInfo}。相同检查会分别在「修改前」「修改后」两个环境各跑一遍。</div>
      <div class="form-actions">
        <button class="btn primary" id="btn-run" onclick="startRun()">运行验收</button>
        <span id="run-spinner" class="muted"></span>
      </div>
    </div>
    <div class="panel">
      <h2>运行记录</h2>
      ${state.runs.length ? `<table><thead><tr><th>ID</th><th>状态</th><th>规则版本</th><th>开始时间</th><th>结束时间</th><th></th></tr></thead><tbody>
        ${state.runs.map((r) => `<tr>
          <td>#${r.id}</td>
          <td>${RUN_STATUS_LABELS[r.status] || r.status}</td>
          <td>v${versionNumberOf(r.rule_version_id)}</td>
          <td>${esc(r.started_at)}</td>
          <td>${esc(r.finished_at || "—")}</td>
          <td class="run-row-action">
            <button class="btn small" onclick="openRun(${r.id})">查看</button>
            <a class="btn small" href="/api/runs/${r.id}/report.md">MD</a>
            <a class="btn small" href="/api/runs/${r.id}/report.json">JSON</a>
          </td></tr>`).join("")}
      </tbody></table>` : '<p class="muted">还没有运行记录。</p>'}
    </div>
    <div id="run-detail"></div>`;
  if (state.runId) renderRunDetail();
}

function versionNumberOf(versionId) {
  const v = state.versions.find((x) => x.id === versionId);
  return v ? v.version : versionId;
}

async function startRun() {
  if (!state.versionId) return showToast("请先保存规则版本", "error");
  if (state.dirty) return showToast("当前有未保存的规则修改，请先保存为新版本再运行", "error");
  try {
    const data = await api(`/api/tasks/${state.task.task.id}/runs`, { method: "POST", body: { rule_version_id: state.versionId } });
    if (data.warning) showToast(data.warning, "error");
    state.runId = data.run_id;
    await refreshRuns();
    await openRun(data.run_id);
  } catch (e) { showToast(e.message, "error"); }
}

async function refreshRuns() {
  const data = await api(`/api/tasks/${state.task.task.id}/runs`);
  state.runs = data.runs;
}

async function openRun(runId) {
  state.runId = runId;
  const data = await api(`/api/runs/${runId}`);
  state.run = data;
  renderRunsTab();
  if (data.run.status === "running") {
    document.getElementById("run-spinner").textContent = "正在运行…（可点击取消）";
  }
  if (state.pollTimer) { clearTimeout(state.pollTimer); state.pollTimer = null; }
  if (data.run.status === "running") {
    state.pollTimer = setTimeout(async () => {
      await refreshRuns();
      await openRun(runId);
    }, 1200);
  }
}

async function cancelRun() {
  if (!state.runId) return;
  try {
    await api(`/api/runs/${state.runId}/cancel`, { method: "POST" });
    showToast("已请求取消，当前检查完成后停止", "success");
  } catch (e) { showToast(e.message, "error"); }
}

function renderRunDetail() {
  const box = document.getElementById("run-detail");
  const data = state.run;
  if (!data) { box.innerHTML = ""; return; }
  const run = data.run;
  const result = data.result;
  let html = `<div class="panel">
    <h2>运行 #${run.id} · ${RUN_STATUS_LABELS[run.status] || run.status} · 规则 v${data.rule_version.version}（${data.rule_version.status === "confirmed" ? "已确认" : "草稿"}）</h2>`;
  if (run.status === "error") html += `<p class="err-line">执行出错：${esc(run.error)}</p>`;
  if (run.status === "running") html += `<div class="form-actions"><button class="btn danger" onclick="cancelRun()">取消运行</button></div>`;
  if (result) {
    const s = result.summary;
    const cls = (result.summary.counts.regression ? "bad" : result.summary.counts.unverifiable || result.summary.counts.not_met ? "warn" : "ok");
    html += `<div class="headline ${cls}">${esc(s.headline)}</div>`;
    if (result.partial) html += `<div class="muted">运行中，以下为已完成部分。</div>`;
    for (const n of s.notes) html += `<div class="muted">· ${esc(n)}</div>`;
    html += `<div class="summary-chips">`;
    for (const code of result.classification_order) {
      if (s.counts[code]) html += `<div class="summary-chip"><b>${s.counts[code]}</b>${esc(result.classification_labels[code])}</div>`;
    }
    html += `</div>`;
    html += `<div class="muted">共 ${s.total_checks} 条检查，已执行 ${s.executed_checks} 条。导出：<a href="/api/runs/${run.id}/report.md">Markdown</a> · <a href="/api/runs/${run.id}/report.json">JSON</a></div>`;
    html += `<div class="muted" style="margin-top:6px">限制：只覆盖下列检查项；超时/连接失败归类为「无法判断」；证据已脱敏。</div>`;
    for (let i = 0; i < result.checks.length; i++) html += renderCheckResult(result.checks[i], i);
  } else {
    html += `<p class="muted">尚无结果。</p>`;
  }
  html += `</div>`;
  box.innerHTML = html;
}

function renderCheckResult(cmp, i) {
  const cl = cmp.classification;
  return `
    <div class="result-check">
      <div class="rc-head" onclick="toggleCheckBody(${i})">
        <span class="badge ${cl.code}">${esc(cl.label)}</span>
        <b>${esc(cmp.name)}</b>
        <span class="muted">${esc(CATEGORY_LABELS[cmp.category] || cmp.category)} · ${esc(cmp.diff_summary)}</span>
      </div>
      <div class="rc-body hidden" id="rc-body-${i}">
        <p>${esc(cl.reason)}</p>
        ${cmp.expectation ? `<p class="muted">期望：${esc(cmp.expectation)}</p>` : ""}
        ${cmp.expected_change_ref ? `<p class="muted">行为变化引用：${esc(cmp.expected_change_ref)}</p>` : ""}
        <div class="env-grid">
          ${renderEnv(cmp.baseline, "修改前")}
          ${renderEnv(cmp.candidate, "修改后")}
        </div>
      </div>
    </div>`;
}

function toggleCheckBody(i) {
  document.getElementById("rc-body-" + i).classList.toggle("hidden");
}

function renderEnv(env, label) {
  const cls = env.status === "passed" ? "ok" : env.status === "failed" ? "bad" : "unknown";
  let html = `<div class="env ${cls}"><h4>${label}：${STATUS_LABELS[env.status]}</h4><div class="muted">${esc(env.reason)}</div>`;
  for (const step of env.steps || []) {
    html += `<div class="step-block"><div class="req-line">${esc(step.request.method)} ${esc(step.request.path)}</div>`;
    if (step.request.headers && Object.keys(step.request.headers).length)
      html += `<div class="muted">请求头：${esc(JSON.stringify(step.request.headers))}</div>`;
    if (step.request.json !== undefined && step.request.json !== null)
      html += `<pre>${esc(JSON.stringify(step.request.json))}</pre>`;
    if (step.error) html += `<div class="err-line">⚠ ${esc(step.error.label || step.error.kind)}：${esc(step.error.message)}</div>`;
    if (step.response) {
      html += `<div>响应：<b>${step.response.status}</b>（${step.response.elapsed_ms} ms）</div>`;
      const body = step.response.is_json ? step.response.json : step.response.text;
      if (body !== null && body !== undefined && String(JSON.stringify(body)).length > 0)
        html += `<pre>${esc(typeof body === "string" ? body : JSON.stringify(body, null, 1))}</pre>`;
    }
    for (const a of step.assertions || [])
      html += `<div class="assert ${a.ok ? "ok" : "bad"}">${a.ok ? "✓" : "✗"} ${esc(a.desc)}（期望 ${esc(a.expected)}，实际 ${esc(a.actual)}）</div>`;
    if (step.extracted && Object.keys(step.extracted).length)
      html += `<div class="muted">提取变量：${esc(JSON.stringify(step.extracted))}</div>`;
    html += `</div>`;
  }
  html += `</div>`;
  return html;
}

init();
