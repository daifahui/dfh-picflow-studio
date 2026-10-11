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
    imp: { path: "", name: "", mode: "auto", aiName: true, kind: "t2i",
           template: "", imgDir: "", promptDir: "", params: {},
           dropFiles: null, dropName: "" },
    modelOptions: { lm: [], unets: [], unet_source: "static",
                    templates: {}, multi_max: 6 },
    paramFields: [
      ["steps", "步数", "number"], ["sampler", "采样器", "text"],
      ["scheduler", "调度器", "text"], ["cfg", "cfg", "number"],
      ["megapixels", "百万像素", "number"], ["seed", "种子", "number"],
      ["close_services_after", "完成后关闭服务", "checkbox"],
    ],
    wsInput: "",
    detailOpen: false,
    detail: { seq: 0, prompt: "", ratio: "", tags: "", note: "", out_image: "" },
    lightboxUrl: "",
    typed: "",                            // 打字机渐显的最新提示词（队列页）
    viewMode: localStorage.getItem("ps-view-mode") || "lg",
    resultTick: 0,
    /* 队列排序（§20 R5）：key=created/name/status，持久化到 localStorage */
    queueSort: (() => {
      try {
        const s = JSON.parse(localStorage.getItem("ps-queue-sort") || "");
        if (s && ["created", "name", "status"].includes(s.key))
          return { key: s.key, dir: s.dir === "desc" ? "desc" : "asc" };
      } catch { /* 损坏则回落默认 */ }
      return { key: "created", dir: "asc" };
    })(),
    PHASE_ORDER: ["idle", "captioning", "reviewing", "generating", "done", "stopped"],

    /* ---------- 初始化 ---------- */
    async init() {
      // 闭包/定时器/原生监听里改组件状态必须走 $data 代理，否则不触发渲染
      const P = window.Alpine.$data(document.body);
      // SSE 必须最先连接：init 里其余请求（/api/models 探测 ComfyUI 等）有耗时，
      // 晚创建会错过任务事件的爆发期
      const es = new EventSource("/api/task/events");
      this._es = es;                       // 调试与生命周期管理用
      es.onmessage = (ev) => {
        try {
          // 外部回调 → CustomEvent 桥接，保证在 Alpine 响应式作用域内求值
          window.dispatchEvent(new CustomEvent("ps-sse", { detail: JSON.parse(ev.data) }));
        } catch { /* 非 JSON 心跳等 */ }
      };
      es.onerror = () => P.addLog("[warn] SSE 断线，3s 后自动重连");
      try {
        const d = await this.api("/api/defaults");
        P.imp.params = { ...d };
        P.modelOptions = await this.api("/api/models");
        // 生图模型选中校准（§19 R1）：Alpine x-model 在 options 渲染前赋值会丢失，
        // 先赋临时值制造变更，nextTick 后恢复目标值（默认值不在列表时回退首项）
        const want = P.modelOptions.unets.includes(P.imp.params.comfy_model)
          ? P.imp.params.comfy_model : P.modelOptions.unets[0];
        P.imp.params.comfy_model = "__calibrating__";
        P.$nextTick(() => { P.imp.params.comfy_model = want; });
        if (P.imp.kind !== "t2i" && !P.imp.template) P.onKindChange();
        const ws = await this.api("/api/workspace");
        P.applyWs(ws);
      } catch (e) { P.addLog("[warn] 初始化失败：" + e.message); }
      P.pollHealth();
      setInterval(() => P.pollHealth(), 5000);
      P.wirePanelSync();
      // Esc 关闭 lightbox 由 body 上的 @keydown.escape.window 指令处理（在
      // Alpine 作用域内求值；原生监听闭包里的赋值不触发渲染，勿改回）
    },

    onSse(d) { this.onEvent(d); },

    /* ---------- 卡片高度同步（§20 R2/R4） ----------
       日志卡高度 = 左列「工作区卡顶→导入卡底」实际占位（含卡片间距）；
       队列卡最小高度 = 右栏「预览卡顶→提示词卡底」实际占位。
       直接改 DOM 样式（不经 Alpine 状态，无渲染依赖）；被测卡片尺寸
       变化（导入卡展开/收起、图片加载等）由 ResizeObserver 自动跟随。 */
    wirePanelSync() {
      if (this._panelRO) return;
      const q = (sel) => document.querySelector(sel);
      const wsCard = q("#ws-card"), impCard = q("#import-card"),
            logCard = q("#log-card"), prevCard = q("#preview-card"),
            promptCard = q("#prompt-card"), queueCard = q("#queue-card");
      if (!wsCard || !impCard || !logCard || !prevCard ||
          !promptCard || !queueCard) return;
      const sync = () => {
        const wsTop = wsCard.getBoundingClientRect().top;
        const impBottom = impCard.getBoundingClientRect().bottom;
        logCard.style.height =
          impBottom > wsTop ? `${Math.round(impBottom - wsTop)}px` : "";
        const prevTop = prevCard.getBoundingClientRect().top;
        const promptBottom = promptCard.getBoundingClientRect().bottom;
        queueCard.style.minHeight =
          promptBottom > prevTop ? `${Math.round(promptBottom - prevTop)}px` : "";
      };
      this._panelRO = new ResizeObserver(sync);
      for (const el of [wsCard, impCard, prevCard, promptCard])
        this._panelRO.observe(el);
      sync();
    },

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
      // 去重（§19 R3）：同一文本同一秒只显示一次，防重复订阅/重复投递刷屏
      const key = `${text}|${new Date().toTimeString().slice(0, 8)}`;
      this._logKeys = this._logKeys || new Set();
      if (this._logKeys.has(key)) return;
      this._logKeys.add(key);
      if (this._logKeys.size > 400) this._logKeys.clear();
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
      const P = window.Alpine.$data(document.body);
      try { P.health = await P.api("/api/health"); }
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
      const P = window.Alpine.$data(document.body);
      P.typed = "";
      clearInterval(P._twTimer);
      let i = 0;
      const step = Math.max(1, Math.round(text.length / 120));
      P._twTimer = setInterval(() => {
        i = Math.min(text.length, i + step);
        P.typed = text.slice(0, i);
        if (i >= text.length) clearInterval(P._twTimer);
      }, 40);
    },

    /* ---------- 工作区 ---------- */
    async browseWorkspace() {
      const r = await this.post("/api/browse/dir");
      if (r.path) this.wsInput = r.path;
      else this.addLog("[warn] 服务器未能返回目录（可能无桌面环境），请手输路径");
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
      else this.addLog("[warn] 服务器未能返回目录（可能无桌面环境），请手输路径");
    },
    onKindChange() {
      this.imp.template = this.modelOptions.templates[this.imp.kind] || "";
    },
    kindLabel(k) {
      return { t2i: "文生图", asset: "人物资产", faceswap: "换脸" }[k] || k || "";
    },
    kindHint() {
      return this.imp.kind === "asset"
        ? "图片顺序即角色：<image1>=人物，<image2>=服装，<image3+>=风格/姿势参考（≤6 张）"
        : "图片顺序即角色：<image1>=主图，<image2+>=目标人脸（多脸可提升还原度，≤6 张）";
    },
    isMulti() { return this.curKind() !== "t2i"; },
    curKind() {
      const t = this.ws.queue.find((x) => x.id === this.sel);
      return t ? t.kind : "t2i";
    },
    onDrop(ev) {
      const files = [...(ev.dataTransfer?.files || [])];
      if (!files.length) return;
      const archives = files.filter((f) => /\.(zip|7z)$/i.test(f.name));
      const images = files.filter((f) => /\.(png|jpe?g|webp|bmp|gif)$/i.test(f.name));
      if (archives.length) {
        this.imp.dropFiles = [archives[0]];        // 压缩包取第一个
        this.imp.dropName = archives[0].name +
          (archives.length > 1 ? `（另有 ${archives.length - 1} 个未采用）` : "");
      } else if (images.length) {
        this.imp.dropFiles = images;               // 图片组 → 多图任务批量上传
        this.imp.dropName = `${images.length} 张图片`;
        if (this.imp.kind === "t2i") this.imp.kind = "asset";
        this.onKindChange();
      } else {
        alert("拖入的文件不可用：需要 zip / 7z 压缩包或图片");
      }
    },
    clearDrop() { this.imp.dropFiles = null; this.imp.dropName = ""; },
    async importTask(start) {
      try {
        let source = this.imp.path.trim();
        const fileEl = document.querySelector("#import-file").files[0];
        const multiEl = document.querySelector("#import-files");
        const dropped = this.imp.dropFiles || [];
        if (dropped.length > 1 || (dropped.length === 1 && !/\.(zip|7z)$/i.test(dropped[0].name))) {
          const fd = new FormData();               // 拖入的图片组 → 批量上传
          for (const f of dropped) fd.append("files", f);
          const up = await this.api("/api/upload/batch", { method: "POST", body: fd });
          source = up.dir;
        } else if (dropped.length === 1) {
          const fd = new FormData();               // 拖入的压缩包 → 上传
          fd.append("file", dropped[0]);
          const up = await this.api("/api/upload", { method: "POST", body: fd });
          source = up.path;
        } else if (this.imp.kind !== "t2i" && multiEl && multiEl.files.length) {
          const fd = new FormData();
          for (const f of multiEl.files) fd.append("files", f);
          const up = await this.api("/api/upload/batch", { method: "POST", body: fd });
          source = up.dir;
        } else if (fileEl) {
          const fd = new FormData();
          fd.append("file", fileEl);
          const up = await this.api("/api/upload", { method: "POST", body: fd });
          source = up.path;
        }
        if (!source) throw new Error("请填写路径或上传文件");
        const params = { ...this.imp.params };
        if (this.imp.kind !== "t2i") params.prompt_template = this.imp.template;
        const st = await this.post("/api/queue/items", { items: [{
          source, name: this.imp.name.trim(), mode: this.imp.mode,
          ai_name: this.imp.aiName, kind: this.imp.kind,
          output_image_dir: this.imp.imgDir.trim(),
          output_prompt_dir: this.imp.promptDir.trim(),
          params,
        }] });
        this.applyWs(st);
        this.addLog("已加入队列：" + st.created[0].name);
        this.imp.path = ""; this.imp.name = "";
        this.imp.imgDir = ""; this.imp.promptDir = "";
        this.clearDrop();
        document.querySelector("#import-file").value = "";
        if (multiEl) multiEl.value = "";
        if (start) await this.queueStart();
        this.show("queue");
      } catch (e) { alert(e.message); }
    },

    /* ---------- 右栏：图片预览（§19 R7） ---------- */
    livePreviewUrl() {
      const cur = this.ws.queue.find(
        (t) => t.phase === "captioning" || t.phase === "generating");
      const p = cur && this.progress[cur.id];
      return cur && p ? `/api/tasks/${cur.id}/img/thumb/${p.seq}` : "";
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
    /* ---------- 队列排序（§20 R5） ---------- */
    sortValue(t) {
      if (this.queueSort.key === "name") return t.name || "";
      if (this.queueSort.key === "status")
        return this.PHASE_ORDER.indexOf(t.phase);
      return typeof t.created_ts === "number" ? t.created_ts : 0;
    },
    sortedQueue() {
      const sign = this.queueSort.dir === "asc" ? 1 : -1;
      return [...this.ws.queue].sort((a, b) => {
        const va = this.sortValue(a), vb = this.sortValue(b);
        if (va < vb) return -sign;
        if (va > vb) return sign;
        return (a.created_ts || 0) - (b.created_ts || 0);   // 同值按创建先后稳定
      });
    },
    setQueueSort(key) {
      const s = this.queueSort;
      if (s.key === key) s.dir = s.dir === "asc" ? "desc" : "asc";
      else { s.key = key; s.dir = "asc"; }
      localStorage.setItem("ps-queue-sort", JSON.stringify(s));
    },
    sortInd(key) {
      return this.queueSort.key === key
        ? (this.queueSort.dir === "asc" ? "↑" : "↓") : "";
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
      // 不依赖可能过期的 ws 状态：队列里所有未缓存任务都拉一份条目
      for (const t of this.ws.queue) {
        if (!this.itemsCache[t.id]) this.refreshTaskItems(t.id);
      }
    },
    doneItems() {
      this.resultTick;                                 // 响应式依赖
      const rows = [];
      for (const t of this.ws.queue) {
        if (this.resultFilter !== "all" && this.resultFilter !== t.id) continue;
        for (const it of this.itemsCache[t.id] || []) {
          if (it.status === "done" && it.out_image) rows.push({ tid: t.id, it });
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
                      ratio: it.contract?.wh_ratio || "",
                      tags: (it.contract?.tags || []).join(", "),
                      note: it.note || "", out_image: it.out_image || "" };
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
                               tags: this.detail.tags,
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
