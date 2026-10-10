/* Prompt Studio 前端（Alpine.js 组件）：工作区/队列/审阅/结果 三视图。 */
"use strict";

function studio() {
  return {
    /* ---------- 状态 ---------- */
    view: "queue",
    health: { llm: false, comfy: false },
    ws: { workspace: "", running: false, gate: false, paused: false, queue: [] },
    itemsCache: {},                       // task_id -> items[]（懒加载 + SSE 增量）
    sel: "",                              // 审阅/详情当前任务
    resultFilter: "all",
    progress: {},                         // task_id -> {done,total,filename,phase}
    imp: { path: "", name: "", mode: "auto", aiName: true,
           imgDir: "", promptDir: "", params: {} },
    paramFields: [
      ["steps", "步数", "number"], ["sampler", "采样器", "text"],
      ["scheduler", "调度器", "text"], ["cfg", "cfg", "number"],
      ["megapixels", "百万像素", "number"], ["seed", "种子", "number"],
      ["close_services_after", "完成后关闭服务", "checkbox"],
    ],
    wsInput: "",
    detailOpen: false,
    detail: { seq: 0, prompt: "", ratio: "", note: "", out_image: "" },
    lightboxUrl: "",
    typed: "",                            // 打字机渐显的最新提示词（队列页）
    viewMode: localStorage.getItem("ps-view-mode") || "lg",
    resultTick: 0,

    /* ---------- 初始化 ---------- */
    async init() {
      try {
        const d = await this.api("/api/defaults");
        this.imp.params = { ...d };
        const ws = await this.api("/api/workspace");
        this.applyWs(ws);
      } catch (e) { this.addLog("[warn] 初始化失败：" + e.message); }
      this.pollHealth();
      setInterval(() => this.pollHealth(), 5000);
      const es = new EventSource("/api/task/events");
      es.onmessage = (ev) => {
        try {
          // 外部回调 → CustomEvent 桥接，保证在 Alpine 响应式作用域内求值
          window.dispatchEvent(new CustomEvent("ps-sse", { detail: JSON.parse(ev.data) }));
        } catch { /* 非 JSON 心跳等 */ }
      };
      es.onerror = () => this.addLog("[warn] SSE 断线，3s 后自动重连");
      window.addEventListener("keydown", (e) => {
        if (e.key === "Escape") { this.lightboxUrl = ""; }
      });
    },

    onSse(d) { this.onEvent(d); },

    /* ---------- 工具 ---------- */
    async api(path, opts) {
      const r = await fetch(path, opts);
      if (!r.ok) {
        const d = await r.json().catch(() => ({}));
        throw new Error(d.detail || r.statusText);
      }
      return r.json();
    },
    post(p, body = {}) {
      return this.api(p, { method: "POST",
        headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    },
    addLog(text) {
      const el = document.querySelector("#logs");
      if (!el) return;
      const line = document.createElement("div");
      line.textContent = `[${new Date().toLocaleTimeString()}] ${text}`;
      el.appendChild(line);
      el.scrollTop = el.scrollHeight;
      while (el.children.length > 200) el.firstChild.remove();
    },
    shortPath(p) {
      p = String(p || "");
      return p.length > 46 ? "…" + p.slice(-45) : p;
    },
    taskShort(tid) {
      const t = this.ws.queue.find((x) => x.id === tid);
      return t ? t.name : tid;
    },

    /* ---------- 视图 ---------- */
    show(v) {
      this.view = v;
      if (v === "result") this.ensureResultItems();
    },
    setViewMode(m) {
      this.viewMode = m;
      localStorage.setItem("ps-view-mode", m);
    },
    viewModeClass() { return "view-" + this.viewMode; },

    /* ---------- 健康轮询 ---------- */
    async pollHealth() {
      try { this.health = await this.api("/api/health"); }
      catch { /* 服务未连接 */ }
    },

    /* ---------- SSE（onSse 桥接已在 onmessage 完成 JSON 解析） ---------- */
    onEvent(d) {
      if (d.event === "queue") {
        this.applyWs(d);
      } else if (d.event === "item") {
        const items = this.itemsCache[d.task_id];
        if (items) {
          const i = items.findIndex((x) => x.seq === d.seq);
          if (i >= 0) items[i] = d; else items.push(d);
        }
        if (d.task_id === this.sel) this.resultTick++;       // 触发详情/网格刷新
        if (d.contract && this.ws.running &&
            this.progress[d.task_id] && this.typewrite) {
          this.typewrite(d.contract.rewritten_prompt);
        }
      } else if (d.event === "progress") {
        this.progress[d.task_id] = d;
      } else if (d.event === "phase") {
        if (d.task_id === this.sel) this.resultTick++;
      } else if (d.event === "log") {
        this.addLog(d.text);
      }
    },
    applyWs(ws) {
      if (!ws || !Array.isArray(ws.queue)) return;      // 动作类响应不含状态，忽略
      this.ws = ws;
      if (this.sel && !ws.queue.some((t) => t.id === this.sel)) {
        const first = ws.queue[0];
        if (first) this.sel = first.id; else this.sel = "";
      }
      if (!this.sel && ws.queue.length) this.sel = ws.queue[0].id;
      if (this.wsInput === "") this.wsInput = ws.workspace;
    },
    async refreshWs() {
      try { this.applyWs(await this.api("/api/workspace")); } catch { /* 忽略 */ }
    },
    typewrite(text) {
      this.typed = "";
      clearInterval(this._twTimer);
      let i = 0;
      const step = Math.max(1, Math.round(text.length / 120));
      this._twTimer = setInterval(() => {
        i = Math.min(text.length, i + step);
        this.typed = text.slice(0, i);
        if (i >= text.length) clearInterval(this._twTimer);
      }, 40);
    },

    /* ---------- 工作区 ---------- */
    async browseWorkspace() {
      const r = await this.post("/api/browse/dir");
      if (r.path) this.wsInput = r.path;
    },
    async applyWorkspace() {
      try {
        this.applyWs(await this.post("/api/workspace", { path: this.wsInput }));
        this.itemsCache = {};
      } catch (e) { alert(e.message); }
    },

    /* ---------- 导入 ---------- */
    async browseImport() {
      const r = await this.post("/api/browse/dir");
      if (r.path) this.imp.path = r.path;
    },
    async importTask(start) {
      try {
        let source = this.imp.path.trim();
        const fileEl = document.querySelector("#import-file").files[0];
        if (fileEl) {
          const fd = new FormData();
          fd.append("file", fileEl);
          const up = await this.api("/api/upload", { method: "POST", body: fd });
          source = up.path;
        }
        if (!source) throw new Error("请填写路径或上传压缩包");
        const st = await this.post("/api/queue/items", { items: [{
          source, name: this.imp.name.trim(), mode: this.imp.mode,
          ai_name: this.imp.aiName,
          output_image_dir: this.imp.imgDir.trim(),
          output_prompt_dir: this.imp.promptDir.trim(),
          params: this.imp.params,
        }] });
        this.applyWs(st);
        this.addLog("已加入队列：" + st.created[0].name);
        this.imp.path = ""; this.imp.name = "";
        this.imp.imgDir = ""; this.imp.promptDir = "";
        document.querySelector("#import-file").value = "";
        if (start) await this.queueStart();
        this.show("queue");
      } catch (e) { alert(e.message); }
    },

    /* ---------- 队列 ---------- */
    async queueStart() {
      try { await this.post("/api/queue/start"); await this.refreshWs(); }
      catch (e) { alert(e.message); }
    },
    async queueResume() {
      try { await this.post("/api/queue/resume"); await this.refreshWs(); }
      catch (e) { alert(e.message); }
    },
    async queuePause() {
      try { await this.post("/api/queue/pause"); await this.refreshWs(); }
      catch (e) { alert(e.message); }
    },
    async queueStop() {
      try { await this.post("/api/queue/stop"); await this.refreshWs(); }
      catch (e) { alert(e.message); }
    },
    async renameTask(tid, name) {
      try {
        await this.post(`/api/tasks/${tid}/rename`, { name });
        await this.refreshWs();
        if (this.sel === tid) this.sel = name;          // id 跟随目录名
      } catch (e) {
        alert(e.message);
        this.resultTick++;                      // 输入框回退到旧名
      }
    },
    async removeTask(tid) {
      try { await this.api(`/api/tasks/${tid}`, { method: "DELETE" }); await this.refreshWs(); }
      catch (e) { alert(e.message); }
    },
    queueLabel() {
      const n = this.ws.queue.length;
      if (!n) return "";
      if (this.ws.gate) return "（审阅闸：等待放行）";
      if (this.ws.running) return "（运行中）";
      if (this.ws.paused) return "（已暂停，点「继续」恢复）";
      return `（${n} 个任务）`;
    },
    phaseLabel(p) {
      return { idle: "排队中", captioning: "提取中", reviewing: "待审阅/已提取",
               generating: "生成中", done: "完成", stopped: "已停止" }[p] || p || "";
    },
    statusLabel(s) {
      return { pending: "待提取", captioning: "提取中", captioned: "已提取",
               generating: "生成中", done: "完成",
               caption_failed: "提取失败", generate_failed: "生成失败" }[s] || s;
    },
    pct(t) {
      const total = t.total || 1;
      return Math.round(((t.counts.done + t.counts.captioned) / total) * 100);
    },
    countsLabel(t) {
      const c = t.counts;
      const bits = [`${c.done}/${t.total} 完成`];
      if (c.failed) bits.push(`${c.failed} 失败`);
      return bits.join(" · ");
    },
    cellClass(it) {
      return it.status.endsWith("failed") ? "failed"
           : it.status === "done" ? "done"
           : it.status === "captioned" ? "captioned" : it.status;
    },
    metaText(it) {
      const bits = [];
      if (it.wh_ratio) bits.push(it.wh_ratio);
      if (it.caption_ms) bits.push(`提取 ${(it.caption_ms / 1000).toFixed(1)}s`);
      if (it.generate_ms) bits.push(`生成 ${(it.generate_ms / 1000).toFixed(1)}s`);
      return bits.join(" · ");
    },

    /* ---------- 审阅页 ---------- */
    async openTask(tid) {
      this.sel = tid;
      await this.refreshTaskItems(tid);
      this.show("review");
    },
    async refreshTaskItems(tid) {
      try {
        const d = await this.api(`/api/tasks/${tid}`);
        this.itemsCache[tid] = d.items;
        this.resultTick++;
      } catch { /* 任务可能已被移除 */ }
    },
    curItems() { return this.itemsCache[this.sel] || []; },
    taskName() {
      const t = this.ws.queue.find((x) => x.id === this.sel);
      return t ? t.name : this.sel || "（未选任务）";
    },
    taskPhase() {
      const t = this.ws.queue.find((x) => x.id === this.sel);
      return t ? t.phase : "";
    },
    taskPct() {
      const t = this.ws.queue.find((x) => x.id === this.sel);
      return t ? this.pct(t) : 0;
    },
    progressText() {
      const p = this.progress[this.sel];
      if (!p) return "等待任务";
      return `${p.done}/${p.total} · ${p.filename}`;
    },
    async generateAll() {
      try { await this.post(`/api/tasks/${this.sel}/generate`); }
      catch (e) { alert(e.message); }
    },
    async stopTask() {
      try { await this.post(`/api/tasks/${this.sel}/stop`); }
      catch (e) { alert(e.message); }
    },

    /* ---------- 结果页 ---------- */
    ensureResultItems() {
      for (const t of this.ws.queue) {
        const ok = t.phase === "done" || t.counts.done > 0;
        if (ok && !this.itemsCache[t.id]) this.refreshTaskItems(t.id);
      }
    },
    doneItems() {
      this.resultTick;                                 // 响应式依赖
      const rows = [];
      for (const t of this.ws.queue) {
        if (this.resultFilter !== "all" && this.resultFilter !== t.id) continue;
        for (const it of this.itemsCache[t.id] || []) {
          if (it.status === "done") rows.push({ tid: t.id, it });
        }
      }
      return rows;
    },

    /* ---------- 详情侧栏 ---------- */
    openDetail(seq, tid) {
      if (tid && tid !== this.sel) {
        this.sel = tid;
        if (!this.itemsCache[tid]) this.refreshTaskItems(tid);
      }
      const it = (this.itemsCache[this.sel] || []).find((x) => x.seq === seq);
      if (!it) return;
      this.detail = { seq, prompt: it.contract?.rewritten_prompt || "",
                      ratio: it.contract?.wh_ratio || "", note: it.note || "",
                      out_image: it.out_image || "" };
      this.detailOpen = true;
    },
    detailStatus() {
      const it = (this.itemsCache[this.sel] || []).find(
        (x) => x.seq === this.detail.seq);
      if (!it) return "";
      const bits = [this.statusLabel(it.status)];
      if (it.error) bits.push(it.error);
      if (it.caption_ms) bits.push(`提取 ${(it.caption_ms / 1000).toFixed(1)}s`);
      if (it.generate_ms) bits.push(`生成 ${(it.generate_ms / 1000).toFixed(1)}s`);
      return bits.join(" · ");
    },
    async saveDetail() {
      await this.api(`/api/tasks/${this.sel}/items/${this.detail.seq}`, {
        method: "PUT", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ rewritten_prompt: this.detail.prompt,
                               wh_ratio: this.detail.ratio,
                               note: this.detail.note }) });
    },
    async recaptionDetail() {
      await this.post(`/api/tasks/${this.sel}/items/${this.detail.seq}/recaption`,
                      { note: this.detail.note });
    },
    async genOne() {
      await this.post(`/api/tasks/${this.sel}/items/${this.detail.seq}`, {
        rewritten_prompt: this.detail.prompt, wh_ratio: this.detail.ratio });
      await this.post(`/api/tasks/${this.sel}/items/${this.detail.seq}/generate`);
    },
  };
}

/* 保持旧调试入口可用 */
window.studio = studio;
