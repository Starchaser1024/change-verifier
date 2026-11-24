/* 双环境验收运行触发、实时轮询、取消与前后并列证据对比视图 */

function renderRunsTab() {
  const box = document.getElementById("tab-runs");
  const versionInfo = state.versionId
    ? `将使用规则 v${state.version ? state.version.version : "?"}（${state.version && state.version.status === "confirmed" ? "已确认" : "草稿"}）`
    : "还没有规则版本";
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
