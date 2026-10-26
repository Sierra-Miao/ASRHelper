"use strict";
let modelState = null;
let modelPollTimer;
let ensembleDraft = null;
function updateStartAvailability() {
  const current = modelState?.models.find(item => item.id === modelState.selected);
  $("#start-button").disabled = !state.file || state.uploading || !current?.ready || modelState?.action.status === "loading";
}
function renderModels() {
  if (!modelState) return;
  const current = modelState.models.find(item => item.id === modelState.selected);
  const online = current?.id === "ensemble" && modelState.ensemble.reference === "qwen";
  const primaryName = modelState.ensemble.primary === "paraformer" ? "Paraformer" : "SenseVoice";
  $("#current-asr-model").textContent = current?.id === "ensemble" ? `${primaryName} + ${online ? "Qwen3-ASR（在线）" : "Fun-ASR-Nano"}` : current?.name || "尚未选择模型";
  $("#model-readiness").textContent = modelState.action.status === "loading" ? "模型正在下载或加载，请稍候…" : current?.ready ? online ? "主稿在本机识别；在线参考将上传音频并可能产生费用" : "已下载 · 本机识别 · 可在模型管理中切换" : "尚未就绪，请在模型管理中下载或配置后启用";
  if (!ensembleDraft) ensembleDraft = {...modelState.ensemble};
  $("#ensemble-primary").value = ensembleDraft.primary;
  $("#ensemble-reference").value = ensembleDraft.reference;
  $("#qwen-form").hidden = ensembleDraft.reference !== "qwen";
  $("#activate-ensemble").disabled = modelState.busy;
  const memory = modelState.memory;
  $("#model-catalog-note").textContent = (memory?.total_gb ? `本机系统内存 ${memory.total_gb} GB，当前可用 ${memory.available_gb} GB。` : "") + modelState.note;
  $("#model-action-message").textContent = [modelState.action.message, modelState.action.error].filter(Boolean).join("\n");
  $("#model-action-message").classList.toggle("error", modelState.action.status === "failed");
  const container = $("#model-cards"); container.replaceChildren();
  for (const item of [...modelState.models].sort((a, b) => Number(b.selected) - Number(a.selected))) {
    const card = document.createElement("article"); card.className = "model-card"; card.classList.toggle("selected", item.selected);
    const heading = document.createElement("h3"); heading.textContent = item.name;
    const badge = document.createElement("span"); badge.className = "tiny-badge"; badge.textContent = item.selected ? "当前选择" : item.id === "ensemble" ? "按所选组合启用" : item.ready ? "已下载" : "未下载";
    heading.append(" ", badge); card.append(heading);
    const details = document.createElement("dl");
    for (const [key, label] of [["parameters", "参数量"], ["download", "下载大小"], ["memory", "硬件建议"], ["languages", "语言"], ["scenarios", "适用场景"], ["numbers", "数字输出"], ["timing", "时间与说话人"], ["requirements", "依赖要求"]]) {
      const term = document.createElement("dt"), value = document.createElement("dd"); term.textContent = label; value.textContent = item[key]; details.append(term, value);
    }
    card.append(details);
    const footer = document.createElement("div"); footer.className = "model-card-footer";
    const link = document.createElement("a"); link.href = item.source; link.target = "_blank"; link.rel = "noopener noreferrer"; link.textContent = "官方模型资料";
    const button = document.createElement("button"); button.type = "button"; button.className = "secondary-button";
    const changed = item.id === "ensemble" && (ensembleDraft.primary !== modelState.ensemble.primary || ensembleDraft.reference !== modelState.ensemble.reference);
    button.textContent = item.selected && modelState.loaded && !changed ? "正在使用" : item.id === "ensemble" ? "启用此组合（下载缺失模型）" : item.ready ? "切换并加载" : "下载并切换";
    button.disabled = modelState.busy || (item.selected && modelState.loaded && !changed);
    button.addEventListener("click", () => activateModel(item)); footer.append(link, button); card.append(footer); container.append(card);
  }
  updateStartAvailability();
}
async function loadModels() {
  modelState = await api("/api/models"); renderModels();
  clearTimeout(modelPollTimer);
  if (modelState.action.status === "loading" || modelState.busy) modelPollTimer = setTimeout(() => loadModels().catch(error => toast(error.message)), 2500);
}
async function activateModel(item) {
  try {
    modelState = await api("/api/models/activate", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({profile: item.id, allow_download: item.id === "ensemble" || !item.ready, ...(item.id === "ensemble" ? ensembleDraft : {})})});
    renderModels(); await loadModels();
  } catch (error) { toast(error.message); await loadModels().catch(() => {}); }
}
document.querySelectorAll("[data-open-models]").forEach(button => button.addEventListener("click", async () => {
  $("#models-dialog").showModal(); try { ensembleDraft = null; await loadModels(); fillQwenForm(); } catch (error) { toast(error.message); }
}));
$("#close-models").addEventListener("click", () => $("#models-dialog").close());
$("#activate-ensemble").addEventListener("click", () => activateModel({id: "ensemble"}));
for (const id of ["ensemble-primary", "ensemble-reference"]) $("#" + id).addEventListener("change", () => {
  ensembleDraft = {primary: $("#ensemble-primary").value, reference: $("#ensemble-reference").value}; renderModels();
});
function fillQwenForm() {
  const qwen = modelState.qwen;
  for (const [id, key] of [["url", "base_url"], ["model", "model"], ["language", "language"], ["timeout", "timeout"], ["chunk", "chunk_seconds"], ["context", "context"]]) $("#qwen-" + id).value = qwen[key];
  $("#qwen-key").value = ""; $("#qwen-clear-key").checked = false;
  $("#qwen-key-state").textContent = qwen.has_api_key ? "已保存；留空保留" : "尚未保存";
}
$("#qwen-form").addEventListener("submit", async event => {
  event.preventDefault(); $("#qwen-save").disabled = true;
  try {
    const body = {api_key: $("#qwen-key").value, keep_key: !$("#qwen-clear-key").checked,
      base_url: $("#qwen-url").value, model: $("#qwen-model").value, language: $("#qwen-language").value,
      timeout: Number($("#qwen-timeout").value), chunk_seconds: Number($("#qwen-chunk").value), context: $("#qwen-context").value};
    await api("/api/qwen-settings", {method: "PUT", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)});
    await loadModels(); fillQwenForm(); $("#qwen-message").textContent = "已保存到本机 JSON。选择组合并点击启用后生效；保存配置不会调用云端。";
  } catch (error) { $("#qwen-message").textContent = error.message; }
  finally { $("#qwen-save").disabled = false; }
});
