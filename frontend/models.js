"use strict";
let modelState = null;
let modelPollTimer;
function updateStartAvailability() {
  const current = modelState?.models.find(item => item.id === modelState.selected);
  $("#start-button").disabled = !state.file || state.uploading || !current?.ready || modelState?.action.status === "loading";
}
function renderModels() {
  if (!modelState) return;
  const current = modelState.models.find(item => item.id === modelState.selected);
  $("#current-asr-model").textContent = current?.name || "尚未选择模型";
  $("#model-readiness").textContent = modelState.action.status === "loading" ? "模型正在下载或加载，请稍候…" : current?.ready ? "已下载 · 本机识别 · 可在模型管理中切换" : "尚未下载，请先选择模型并点击“下载并切换”";
  const memory = modelState.memory;
  $("#model-catalog-note").textContent = (memory?.total_gb ? `本机系统内存 ${memory.total_gb} GB，当前可用 ${memory.available_gb} GB。` : "") + modelState.note;
  $("#model-action-message").textContent = [modelState.action.message, modelState.action.error].filter(Boolean).join("\n");
  $("#model-action-message").classList.toggle("error", modelState.action.status === "failed");
  const container = $("#model-cards"); container.replaceChildren();
  for (const item of [...modelState.models].sort((a, b) => Number(b.selected) - Number(a.selected))) {
    const card = document.createElement("article"); card.className = "model-card"; card.classList.toggle("selected", item.selected);
    const heading = document.createElement("h3"); heading.textContent = item.name;
    const badge = document.createElement("span"); badge.className = "tiny-badge"; badge.textContent = item.selected ? "当前选择" : item.ready ? "已下载" : "未下载";
    heading.append(" ", badge); card.append(heading);
    const details = document.createElement("dl");
    for (const [key, label] of [["parameters", "参数量"], ["download", "下载大小"], ["memory", "硬件建议"], ["languages", "语言"], ["scenarios", "适用场景"], ["numbers", "数字输出"], ["timing", "时间与说话人"], ["requirements", "依赖要求"]]) {
      const term = document.createElement("dt"), value = document.createElement("dd"); term.textContent = label; value.textContent = item[key]; details.append(term, value);
    }
    card.append(details);
    const footer = document.createElement("div"); footer.className = "model-card-footer";
    const link = document.createElement("a"); link.href = item.source; link.target = "_blank"; link.rel = "noopener noreferrer"; link.textContent = "官方模型资料";
    const button = document.createElement("button"); button.type = "button"; button.className = "secondary-button";
    button.textContent = item.selected && modelState.loaded ? "正在使用" : item.ready ? "切换并加载" : "下载并切换";
    button.disabled = modelState.busy || (item.selected && modelState.loaded);
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
    modelState = await api("/api/models/activate", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({profile: item.id, allow_download: !item.ready})});
    renderModels(); await loadModels();
  } catch (error) { toast(error.message); await loadModels().catch(() => {}); }
}
document.querySelectorAll("[data-open-models]").forEach(button => button.addEventListener("click", async () => {
  $("#models-dialog").showModal(); try { await loadModels(); } catch (error) { toast(error.message); }
}));
$("#close-models").addEventListener("click", () => $("#models-dialog").close());
