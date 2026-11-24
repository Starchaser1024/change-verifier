/* 不可变验收规则版本管理、检查卡片编辑器与可选 AI 建议 */

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
