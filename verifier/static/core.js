/* 全局状态、常量与通用 API / 提示工具（原生 JS，无构建步骤） */

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
