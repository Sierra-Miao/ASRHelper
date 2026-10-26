"use strict";
const $ = (selector) => document.querySelector(selector);
const state = { file: null, objectUrl: null, uploading: false, job: null, tab: "original", page: 1, pageSize: 40, settings: null, defaultPrompt: "", timer: null, clearKey: false };
const activeStates = new Set(["queued", "loading", "decoding", "transcribing", "exporting", "correcting"]);
let toastTimer;
function toast(message) { const el = $("#toast"); el.textContent = message; el.hidden = false; clearTimeout(toastTimer); toastTimer = setTimeout(() => el.hidden = true, 5000); }
async function api(url, options = {}) {
  const response = await fetch(url, options);
  let data;
  try { data = await response.json(); } catch { throw new Error("服务返回了无效响应，请查看启动窗口。"); }
  if (!response.ok) { const detail = typeof data.detail === "string" ? data.detail : "请检查输入内容后重试"; throw new Error(detail); }
  return data;
}
function clock(ms) { const total = Math.max(0, Math.round(ms)); const h = Math.floor(total / 3600000), m = Math.floor(total / 60000) % 60, s = Math.floor(total / 1000) % 60; return `${String(h).padStart(2, "0")}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}.${String(total % 1000).padStart(3, "0")}`; }
function sizeText(bytes) { return bytes < 1024 * 1024 ? `${(bytes / 1024).toFixed(1)} KB` : `${(bytes / 1024 / 1024).toFixed(1)} MB`; }
function selectFile(file) {
  if (!file) return;
  const extension = file.name.split(".").pop().toLowerCase();
  if (!["wav", "mp3", "m4a", "flac", "ogg", "opus", "aac", "wma", "aiff", "aif", "mp4", "webm", "mkv", "mov"].includes(extension)) return toast("请选择支持的音频或视频文件。");
  if (!file.size) return toast("这个文件是空的，请重新选择。");
  if (file.size > 512 * 1024 * 1024) return toast("文件超过 512 MB，请先拆分或压缩。");
  clearFile(); state.file = file; state.objectUrl = URL.createObjectURL(file);
  $("#selected-name").textContent = file.name; $("#selected-size").textContent = sizeText(file.size);
  $("#selected-file").hidden = false; $("#audio-preview").src = state.objectUrl; $("#audio-preview").hidden = false;
  $("#start-button").disabled = state.uploading;
}
function clearFile() { $("#audio-preview").pause(); $("#audio-preview").removeAttribute("src"); if (state.objectUrl) URL.revokeObjectURL(state.objectUrl); state.file = null; state.objectUrl = null; $("#file-input").value = ""; $("#selected-file").hidden = true; $("#audio-preview").hidden = true; $("#start-button").disabled = true; }
$("#dropzone").addEventListener("click", () => $("#file-input").click());
$("#file-input").addEventListener("change", (event) => selectFile(event.target.files[0]));
$("#remove-file").addEventListener("click", clearFile);
for (const name of ["dragenter", "dragover"]) $("#dropzone").addEventListener(name, (event) => { event.preventDefault(); $("#dropzone").classList.add("dragover"); });
for (const name of ["dragleave", "drop"]) $("#dropzone").addEventListener(name, (event) => { event.preventDefault(); $("#dropzone").classList.remove("dragover"); });
$("#dropzone").addEventListener("drop", (event) => { if (event.dataTransfer.files.length > 1) toast("每次处理一个文件，已选择第一个。"); selectFile(event.dataTransfer.files[0]); });
window.addEventListener("dragover", (event) => event.preventDefault()); window.addEventListener("drop", (event) => event.preventDefault());
$("#speakers").addEventListener("change", () => { $("#custom-speakers").hidden = $("#speakers").value !== "custom"; });

function uploadForm(form) {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest(); xhr.open("POST", "/api/jobs"); xhr.responseType = "json";
    xhr.upload.onprogress = (event) => { if (event.lengthComputable) $("#start-text").textContent = `上传中 ${Math.round(event.loaded / event.total * 100)}%`; };
    xhr.onload = () => xhr.status >= 200 && xhr.status < 300 ? resolve(xhr.response) : reject(new Error(typeof xhr.response?.detail === "string" ? xhr.response.detail : "上传失败，请检查文件后重试"));
    xhr.onerror = () => reject(new Error("无法连接本地服务，请确认启动窗口仍然打开。")); xhr.send(form);
  });
}
$("#start-button").addEventListener("click", async () => {
  if (!state.file || state.uploading) return;
  const speakerValue = $("#speakers").value === "custom" ? $("#custom-speakers").value : $("#speakers").value;
  if (speakerValue && (!Number.isInteger(Number(speakerValue)) || Number(speakerValue) < 1 || Number(speakerValue) > 30)) return toast("说话人数请输入 1–30 之间的整数。");
  if ($("#speakers").value === "custom" && !speakerValue) return toast("请填写说话人数。");
  const form = new FormData(); form.append("file", state.file); if (speakerValue) form.append("speakers", speakerValue); form.append("use_ai", String($("#use-ai").checked));
  state.uploading = true; $("#start-button").disabled = true; $("#start-text").textContent = "正在上传…";
  try { const job = await uploadForm(form); clearFile(); state.tab = "original"; state.page = 1; $("#search").value = ""; setJob(job); pollJob(job.id); await loadHistory(); }
  catch (error) { toast(error.message); }
  finally { state.uploading = false; $("#start-text").textContent = "开始转写"; $("#start-button").disabled = !state.file; }
});

function setJob(job) {
  state.job = job; localStorage.setItem("tingjian.currentJob", job.id); renderJob();
}
function renderJob() {
  const job = state.job;
  if (job?.device) $("#sidebar-model").textContent = `FunASR · ${job.device}`;
  if (!job) { $("#task-progress").hidden = true; $("#download-actions").hidden = true; $("#result-empty").hidden = false; $("#result-content").hidden = true; $("#result-message").hidden = true; $("#result-description").textContent = "每一句对话，都有迹可循"; return; }
  const busy = activeStates.has(job.status), hasRows = job.rows?.length > 0;
  $("#task-progress").hidden = false; $("#progress-title").textContent = job.stage; $("#progress-filename").textContent = job.filename;
  $("#progress-spinner").classList.toggle("finished", !busy); $("#task-status").textContent = job.status === "failed" ? "未完成" : busy ? "处理中" : "已完成";
  $("#task-status").classList.toggle("failed", job.status === "failed");
  const stageIndex = { queued: 0, decoding: 0, loading: 1, transcribing: 1, exporting: 2, correcting: 3, completed: 4, failed: -1 }[job.status];
  document.querySelectorAll("[data-stage]").forEach(el => { const index = Number(el.dataset.stage); el.classList.toggle("current", index === stageIndex); el.classList.toggle("done", index < stageIndex); el.classList.toggle("skipped", index === 3 && !job.use_ai); });
  $("#result-empty").hidden = hasRows; $("#result-content").hidden = !hasRows;
  $("#download-actions").hidden = !job.downloads?.original;
  for (const key of ["original", "corrected", "bundle"]) { const el = $(`#download-${key}`); const href = job.downloads?.[key]; el.hidden = !href; if (href) el.href = href; else el.removeAttribute("href"); }
  const messages = [job.error, job.ai_error, ...(job.warnings || [])].filter(Boolean);
  $("#result-message").hidden = !messages.length; $("#result-message").textContent = messages.join("\n"); $("#result-message").classList.toggle("error", job.status === "failed");
  $("#tab-corrected").disabled = !job.corrected_rows?.length;
  if (state.tab === "corrected" && !job.corrected_rows?.length) state.tab = "original";
  $("#retry-ai").hidden = busy || !job.downloads?.original;
  $("#retry-ai").textContent = job.corrected_rows?.length || job.ai_error ? "重新 AI 校对" : "使用 AI 校对";
  if (hasRows) { const speakers = new Set(job.rows.map(row => row.speaker)); $("#result-description").textContent = `${clock(job.duration_ms).split(".")[0]} · ${speakers.size} 位说话人 · ${job.rows.length} 个句段`; renderTable(); }
}
function renderTable() {
  for (const variant of ["original", "corrected"]) { $(`#tab-${variant}`).classList.toggle("active", variant === state.tab); $(`#tab-${variant}`).setAttribute("aria-selected", String(variant === state.tab)); }
  const all = state.tab === "corrected" ? state.job.corrected_rows : state.job.rows;
  const query = $("#search").value.toLocaleLowerCase();
  const rows = (all || []).filter(row => `${row.speaker} ${row.content}`.toLocaleLowerCase().includes(query));
  const pages = Math.max(1, Math.ceil(rows.length / state.pageSize)); state.page = Math.min(pages, Math.max(1, state.page));
  const tbody = $("#result-rows"); tbody.replaceChildren();
  const speakerNames = [...new Set((state.job.rows || []).map(row => row.speaker))];
  rows.slice((state.page - 1) * state.pageSize, state.page * state.pageSize).forEach(row => {
    const tr = document.createElement("tr");
    for (const text of [clock(row.start_ms), clock(row.end_ms)]) { const td = document.createElement("td"); td.textContent = text; tr.append(td); }
    const speaker = document.createElement("td"), badge = document.createElement("span"); badge.className = `speaker-label color-${speakerNames.indexOf(row.speaker) % 4}`; badge.textContent = row.speaker; speaker.append(badge); tr.append(speaker);
    const content = document.createElement("td"); content.textContent = row.content;
    if (state.tab === "corrected" && !state.job.rows.some(original => original.content === row.content && original.start_ms === row.start_ms && original.end_ms === row.end_ms)) content.classList.add("changed-cell");
    tr.append(content); tbody.append(tr);
  });
  if (!rows.length) { const tr = document.createElement("tr"), td = document.createElement("td"); td.colSpan = 4; td.textContent = "没有找到匹配内容"; tr.append(td); tbody.append(tr); }
  $("#row-summary").textContent = query ? `找到 ${rows.length} / ${all.length} 条记录` : `共 ${rows.length} 条记录 · 时间精确到毫秒`;
  $("#page-number").textContent = `${state.page} / ${pages}`; $("#prev-page").disabled = state.page <= 1; $("#next-page").disabled = state.page >= pages;
}
for (const variant of ["original", "corrected"]) $(`#tab-${variant}`).addEventListener("click", () => { state.tab = variant; state.page = 1; renderTable(); });
$("#search").addEventListener("input", () => { state.page = 1; renderTable(); });
$("#prev-page").addEventListener("click", () => { state.page--; renderTable(); }); $("#next-page").addEventListener("click", () => { state.page++; renderTable(); });
$("#retry-ai").addEventListener("click", async () => {
  if (!state.settings?.enabled || !state.settings?.ready) { toast("先启用并保存 AI 设置，再开始校对。"); return openSettings(); }
  const button = $("#retry-ai"); button.disabled = true;
  try { const job = await api(`/api/jobs/${state.job.id}/correct`, { method: "POST" }); setJob(job); pollJob(job.id); }
  catch (error) { toast(error.message); } finally { button.disabled = false; }
});
function pollJob(id) {
  clearTimeout(state.timer);
  async function next() {
    try { const job = await api(`/api/jobs/${id}`); if (state.job?.id !== id) return; setJob(job);
      if (activeStates.has(job.status)) state.timer = setTimeout(next, 1400);
      else { await loadHistory(); if (job.status === "completed") toast(job.ai_error ? "原始 Excel 已生成，AI 校对未完成。" : "转写完成，可以下载 Excel 了。"); }
    } catch (error) { if (state.job?.id === id) { toast(error.message); state.timer = setTimeout(next, 5000); } }
  }
  state.timer = setTimeout(next, 600);
}
async function loadHistory() {
  const jobs = await api("/api/jobs"); const container = $("#history"); container.replaceChildren(); $("#history-count").textContent = String(jobs.length);
  if (!jobs.length) { const p = document.createElement("p"); p.className = "history-empty"; p.textContent = "从第一段录音开始，留下值得回看的对话。"; container.append(p); }
  for (const job of jobs) { const button = document.createElement("button"); button.className = "history-item" + (state.job?.id === job.id ? " selected" : ""); const title = document.createElement("strong"); title.textContent = job.filename; title.title = job.filename;
    const meta = document.createElement("small"), date = document.createElement("span"), status = document.createElement("span"); date.textContent = new Date(job.created_at).toLocaleDateString("zh-CN", { month: "2-digit", day: "2-digit" }); status.className = "history-state"; status.textContent = activeStates.has(job.status) ? "处理中" : job.status === "failed" ? "未完成" : "已完成"; meta.append(date, status); button.append(title, meta);
    button.addEventListener("click", async () => { try { const detail = await api(`/api/jobs/${job.id}`); state.tab = "original"; state.page = 1; $("#search").value = ""; setJob(detail); pollJob(detail.id); await loadHistory(); } catch (error) { toast(error.message); } }); container.append(button);
  }
}
$("#new-task").addEventListener("click", () => { clearTimeout(state.timer); state.job = null; state.tab = "original"; state.page = 1; clearFile(); localStorage.removeItem("tingjian.currentJob"); renderJob(); loadHistory().catch(() => {}); window.scrollTo({ top: 0, behavior: "smooth" }); $("#dropzone").focus(); });

function applySettings(settings) {
  state.settings = settings; state.defaultPrompt = settings.default_prompt || state.defaultPrompt;
  const enabled = settings.enabled && settings.ready; $("#use-ai").disabled = !enabled; $("#use-ai").checked = enabled;
  $("#ai-description").textContent = enabled ? `已连接 ${settings.model}` : "修正错字，整理不自然的断句";
  $("#ai-config-label").textContent = enabled ? "管理模型 API 设置" : "配置你的模型 API";
}
function openSettings() {
  const settings = state.settings; if (!settings) return toast("正在连接本地服务，请稍后再试。");
  $("#settings-enabled").checked = settings.enabled; $("#base-url").value = settings.base_url; $("#api-model").value = settings.model; $("#api-key").value = ""; $("#api-key").type = "password"; $("#show-key").textContent = "显示";
  $("#api-key").placeholder = settings.has_api_key ? "已保留当前 Key，留空则不更改" : "sk-…（本地模型可留空）";
  $("#key-state").textContent = settings.has_api_key ? "已保存到本机 JSON，重启自动恢复" : "保存到本机 JSON，重启无需重填";
  $("#clear-key").hidden = !settings.has_api_key; $("#transport").value = settings.transport; $("#prompt").value = settings.prompt; $("#api-timeout").value = settings.timeout; $("#max-tokens").value = settings.max_tokens;
  state.clearKey = false; $("#settings-message").textContent = ""; updateTransportHint(); $("#settings-dialog").showModal();
}
document.querySelectorAll("[data-open-settings]").forEach(button => button.addEventListener("click", openSettings));
$("#close-settings").addEventListener("click", () => $("#settings-dialog").close());
$("#show-key").addEventListener("click", () => { const hidden = $("#api-key").type === "password"; $("#api-key").type = hidden ? "text" : "password"; $("#show-key").textContent = hidden ? "隐藏" : "显示"; });
$("#clear-key").addEventListener("click", () => { state.clearKey = true; $("#api-key").value = ""; $("#api-key").placeholder = "保存后将清除当前 Key"; $("#key-state").textContent = "等待清除"; });
$("#reset-prompt").addEventListener("click", () => $("#prompt").value = state.defaultPrompt);
function updateTransportHint() { $("#transport-hint").textContent = $("#transport").value === "table" ? "读取导出的 Excel，把完整表格内容交给模型校对，兼容常见 Chat Completions 接口。" : "发送实际 Excel 附件；服务商需同时支持 /files、/responses 和 .xlsx 输入。长表格会分批发送，完成后请求删除云端附件。"; }
$("#transport").addEventListener("change", updateTransportHint);
async function saveSettings() {
  const body = { enabled: $("#settings-enabled").checked, base_url: $("#base-url").value.trim(), model: $("#api-model").value.trim(), api_key: $("#api-key").value.trim(), keep_key: !state.clearKey,
    transport: $("#transport").value, prompt: $("#prompt").value.trim() || state.defaultPrompt, timeout: Number($("#api-timeout").value), max_tokens: Number($("#max-tokens").value) };
  if (body.enabled && (!body.base_url || !body.model)) throw new Error("请填写 API 地址和模型名称。");
  const settings = await api("/api/settings", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  applySettings(settings); $("#api-key").value = ""; state.clearKey = false;
  $("#key-state").textContent = settings.has_api_key ? "已保存到本机 JSON，重启自动恢复" : "保存到本机 JSON，重启无需重填";
  $("#api-key").placeholder = settings.has_api_key ? "已保留当前 Key，留空则不更改" : "sk-…（本地模型可留空）"; $("#clear-key").hidden = !settings.has_api_key;
  return settings;
}
$("#settings-form").addEventListener("submit", async (event) => { event.preventDefault(); const button = $("#save-settings"); button.disabled = true; try { await saveSettings(); $("#settings-dialog").close(); toast("设置已保存。启用 AI 后，转写表格会发送给所配置的服务商。"); } catch (error) { $("#settings-message").textContent = error.message; $("#settings-message").classList.add("error"); } finally { button.disabled = false; } });
$("#test-api").addEventListener("click", async () => { const button = $("#test-api"); button.disabled = true; $("#settings-message").classList.remove("error"); $("#settings-message").textContent = "正在发送一条简短请求…";
  try { await saveSettings(); const result = await api("/api/settings/test", { method: "POST" }); $("#settings-message").textContent = result.message; }
  catch (error) { $("#settings-message").textContent = error.message; $("#settings-message").classList.add("error"); } finally { button.disabled = false; }
});
async function init() {
  try { const [settings, health] = await Promise.all([api("/api/settings"), api("/api/health")]); applySettings(settings); $("#sidebar-model").textContent = `FunASR · ${health.device}`; await loadHistory(); const id = localStorage.getItem("tingjian.currentJob"); if (id) { try { const job = await api(`/api/jobs/${id}`); setJob(job); if (activeStates.has(job.status)) pollJob(id); } catch { localStorage.removeItem("tingjian.currentJob"); } } }
  catch (error) { toast("无法连接本地服务，请确认启动窗口仍然打开。"); }
}
init();
setInterval(() => loadHistory().catch(() => {}), 10000);
