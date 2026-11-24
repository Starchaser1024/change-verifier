/* 改动验收器 前端入口（无构建步骤，按领域拆分为 core / tasks / rules / runs） */

async function init() {
  try {
    const llm = await api("/api/llm/status");
    state.llm = llm;
    const chip = document.getElementById("llm-status");
    chip.textContent = llm.available ? "LLM 建议：已配置（" + llm.model + "）" : "LLM 建议：未配置（可手动编写规则）";
    chip.className = "llm-chip " + (llm.available ? "on" : "off");
  } catch (e) { /* 忽略 */ }
  await loadTasks();
}

init();
