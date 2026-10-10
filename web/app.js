/* Prompt Studio 前端逻辑 */
"use strict";

const $ = (sel) => document.querySelector(sel);
const api = async (path, opts) => {
  const r = await fetch(path, opts);
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
  return r.json();
};

const state = { task: null, items: [], selected: 0 };

/* ---------- 视图切换 ---------- */
function show(view) {
  for (const v of ["task", "review", "result"])
    $("#view-" + v).classList.toggle("hidden", v !== view);
  for (const b of document.querySelectorAll("#tabs button"))
    b.classList.toggle("on", b.dataset.view === view);
}
document.querySelectorAll("#tabs button").forEach(
  (b) => (b.onclick = () => show(b.dataset.view)));

/* ---------- 健康状态 ---------- */
async function pollHealth() {
  try {
    const h = await api("/api/health");
    $("#health").innerHTML =
      `LLM <b class="${h.llm ? "on" : "off"}">${h.llm ? "运行中" : "未启动"}</b>　` +
      `ComfyUI <b class="${h.comfy ? "on" : "off"}">${h.comfy ? "运行中" : "未启动"}</b>`;
  } catch { $("#health").textContent = "服务未连接"; }
}
setInterval(pollHealth, 5000);

/* ---------- 参数表单 ---------- */
const PARAM_FIELDS = [
  ["steps", "步数", "number"], ["sampler", "采样器", "text"],
  ["scheduler", "调度器", "text"], ["cfg", "cfg", "number"],
  ["megapixels", "百万像素", "number"], ["seed", "种子", "number"],
  ["close_services_after", "完成后关闭服务", "checkbox"],
];
function buildParams(defaults) {
  $("#params").innerHTML = PARAM_FIELDS.map(([k, label, type]) => {
    if (type === "checkbox")
      return `<label>${label}<input type="checkbox" data-p="${k}" ${defaults[k] ? "checked" : ""}></label>`;
    return `<label>${label}<input type="${type}" data-p="${k}" value="${defaults[k] ?? ""}" step="any"></label>`;
  }).join("");
}
function readParams() {
  const out = {};
  for (const el of document.querySelectorAll("[data-p]")) {
    out[el.dataset.p] = el.type === "checkbox" ? el.checked : Number(el.value) || el.value;
  }
  return out;
}

/* ---------- 任务创建 ---------- */
$("#btn-browse").onclick = async () => {
  const r = await api("/api/browse/dir", { method: "POST" });
  if (r.path) $("#src-path").value = r.path;
};

async function createTask() {
  let source = $("#src-path").value.trim();
  const fileEl = $("#src-file").files[0];
  if (fileEl) {
    const fd = new FormData();
    fd.append("file", fileEl);
    const up = await api("/api/upload", { method: "POST", body: fd });
    source = up.path;
  }
  if (!source) throw new Error("请填写路径或上传压缩包");
  const body = { source, name: $("#task-name").value.trim(),
                 params: readParams() };
  const r = await api("/api/task", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body) });
  applyTask(r);
  show("review");
}

/* ---------- 三个启动按钮 ---------- */
const post = (p, body = {}) => api(p, {
  method: "POST", headers: { "Content-Type": "application/json" },
  body: JSON.stringify(body) });

$("#btn-caption").onclick = async () => {
  try { if (!state.task) await createTask(); await post("/api/task/caption"); }
  catch (e) { alert(e.message); }
};
$("#btn-auto").onclick = async () => {
  if (!confirm("全自动模式：提取全部提示词后直接开始生成全部图片，中途不审阅。继续？"))
    return;
  try { if (!state.task) await createTask(); await post("/api/task/auto"); }
  catch (e) { alert(e.message); }
};
$("#btn-generate-all").onclick = () => {
  if (!confirm("将全部已提取的提示词入队 ComfyUI 生成。继续？")) return;
  post("/api/task/generate").catch((e) => alert(e.message));
};
$("#btn-stop").onclick = () => post("/api/task/stop").catch((e) => alert(e.message));

/* ---------- 渲染 ---------- */
const STATUS_TAG = {
  pending: "待提取", captioning: "提取中", captioned: "已提取",
  generating: "生成中", done: "完成",
  caption_failed: "提取失败", generate_failed: "生成失败",
};

function cellHTML(it) {
  const cls = it.status.endsWith("failed") ? "failed"
            : it.status === "done" ? "done"
            : it.status === "captioned" ? "captioned" : it.status;
  return `<div class="cell ${cls}" data-seq="${it.seq}">
    <img loading="lazy" src="/api/img/thumb/${it.seq}" alt="">
    <span class="tag">${STATUS_TAG[it.status] || it.status}</span>
    <span class="name">${it.seq}_${it.src_filename}</span></div>`;
}

function renderGrid() {
  $("#item-count").textContent = `（${state.items.length} 张）`;
  $("#grid").innerHTML = state.items.map(cellHTML).join("");
  $("#grid").querySelectorAll(".cell").forEach(
    (c) => (c.onclick = () => openDetail(+c.dataset.seq)));
  renderResult();
}

function renderResult() {
  const done = state.items.filter((i) => i.status === "done");
  $("#result-grid").innerHTML = done.length
    ? done.map(cellHTML).join("")
    : `<p class="hint">尚无完成项</p>`;
  $("#result-grid").querySelectorAll(".cell").forEach(
    (c) => (c.onclick = () => openDetail(+c.dataset.seq)));
}

function applyTask(data) {
  state.task = data.task;
  state.items = data.items || [];
  renderGrid();
  updatePhase(state.task.phase);
}

/* ---------- 详情侧栏 ---------- */
function openDetail(seq) {
  const it = state.items.find((i) => i.seq === seq);
  if (!it) return;
  state.selected = seq;
  $("#detail-src").src = `/api/img/src/${seq}`;
  $("#detail-out").src = it.out_image ? `/api/img/out/${seq}` : "";
  $("#detail-out-wrap").style.visibility = it.out_image ? "visible" : "hidden";
  $("#detail-prompt").value = it.contract?.rewritten_prompt || "";
  $("#detail-ratio").value = it.contract?.wh_ratio || "";
  $("#detail-note").value = it.note || "";
  $("#detail-status").textContent =
    `${STATUS_TAG[it.status] || it.status}` +
    (it.error ? ` · ${it.error}` : "") +
    (it.caption_ms ? ` · 提取 ${(it.caption_ms / 1000).toFixed(1)}s` : "") +
    (it.generate_ms ? ` · 生成 ${(it.generate_ms / 1000).toFixed(1)}s` : "");
  $("#detail").classList.add("open");
}
$("#detail-close").onclick = () => $("#detail").classList.remove("open");

$("#btn-save").onclick = async () => {
  const seq = state.selected;
  await post(`/api/task/items/${seq}`, {
    rewritten_prompt: $("#detail-prompt").value,
    wh_ratio: $("#detail-ratio").value,
    note: $("#detail-note").value });
};
$("#btn-recaption").onclick = async () => {
  const seq = state.selected;
  await post(`/api/task/items/${seq}`, { note: $("#detail-note").value });
  await post(`/api/task/items/${seq}/recaption`, { note: $("#detail-note").value });
};
$("#btn-gen-one").onclick = async () => {
  const seq = state.selected;
  await post(`/api/task/items/${seq}`, {
    rewritten_prompt: $("#detail-prompt").value,
    wh_ratio: $("#detail-ratio").value });
  await post(`/api/task/items/${seq}/generate`);
};

/* ---------- SSE ---------- */
const PHASE_LABEL = {
  idle: "空闲", captioning: "阶段一：提取提示词", reviewing: "审阅就绪",
  generating: "阶段二：ComfyUI 生成", done: "全部完成", stopped: "已停止",
};

function updatePhase(phase) {
  $("#phase-label").textContent = PHASE_LABEL[phase] || phase;
  $("#btn-generate-all").disabled = phase === "generating" || phase === "captioning";
  $("#btn-stop").disabled = !["captioning", "generating"].includes(phase);
}

function onEvent(ev) {
  const d = JSON.parse(ev.data);
  if (d.event === "log") addLog(d.text);
  else if (d.event === "phase") {
    updatePhase(d.phase);
    addLog(`── 进入阶段：${PHASE_LABEL[d.phase] || d.phase}`);
    if (d.phase === "reviewing") show("review");
    if (d.phase === "done") { refreshTask(); show("result"); }
  }
  else if (d.event === "progress") {
    $("#progress-bar").style.width = `${(d.done / d.total) * 100}%`;
    $("#progress-text").textContent =
      `${d.done}/${d.total} · ${d.filename}`;
  }
  else if (d.event === "item") {
    const i = state.items.findIndex((x) => x.seq === d.seq);
    if (i >= 0) state.items[i] = d;
    renderGrid();
    if (state.selected === d.seq && $("#detail").classList.contains("open"))
      openDetail(d.seq);
  }
}

function addLog(text) {
  const el = $("#logs");
  const line = document.createElement("div");
  line.textContent = `[${new Date().toLocaleTimeString()}] ${text}`;
  el.appendChild(line);
  el.scrollTop = el.scrollHeight;
  while (el.children.length > 200) el.firstChild.remove();
}

async function refreshTask() {
  try { applyTask(await api("/api/task")); } catch {}
}

/* ---------- 初始化 ---------- */
(async function init() {
  buildParams(await api("/api/defaults"));
  await refreshTask();          // 回看历史任务
  pollHealth();
  const es = new EventSource("/api/task/events");
  es.onmessage = onEvent;
  es.onerror = () => addLog("[warn] SSE 断线，3s 后自动重连");
})();
