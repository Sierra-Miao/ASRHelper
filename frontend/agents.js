"use strict";
let agentPrompt = "";
function renderAgentPanel(job, busy) {
  $("#agent-panel").hidden = !job?.downloads?.original;
  $("#agent-package").disabled = busy;
  $("#agent-download").hidden = !job?.downloads?.agent;
  if (job?.downloads?.agent) $("#agent-download").href = job.downloads.agent;
  $("#copy-agent-prompt").disabled = busy;
  $("#agent-notice").textContent = job?.agent_pending ? "交接包已就绪，在外部 Agent 中整理后直接获取 Excel 即可。" : "可直接使用 API，也可下载交接包，在 ChatGPT Work / DeepSeek Harness 中运行随附 Skill，直接生成 Excel。";
}
async function prepareAgentPackage() {
  if (!state.job) return;
  const result = await api(`/api/jobs/${state.job.id}/agent-package`, {method: "POST"});
  agentPrompt = result.prompt; setJob(result.job); return result;
}
$("#agent-package").addEventListener("click", async () => {
  try { await prepareAgentPackage(); toast("交接包已生成，点击“下载交接包”保存。"); } catch (error) { toast(error.message); }
});
$("#copy-agent-prompt").addEventListener("click", async () => {
  try { await prepareAgentPackage(); await navigator.clipboard.writeText(agentPrompt); toast("任务提示词已复制。请在客户端附加交接包后粘贴。"); }
  catch (error) { $("#agent-prompt-text").value = agentPrompt; $("#agent-prompt-text").hidden = !agentPrompt; toast("如未能复制，可从下方文本框手动复制。"); }
});
$("#agent-target").addEventListener("change", () => {
  $("#open-agent").href = $("#agent-target").value === "deepseek" ? "http://127.0.0.1:3080/" : "https://chatgpt.com/";
});
