"""Prompt Studio 服务端入口：工作区/队列路由 + 批处理 worker。

运行边界：llama-server / ComfyUI 只会被任务流程（用户在网页点击按钮）拉起；
开发测试期手动运行它们需用户批准（设计文档 §12）。

队列执行策略（§16.4 阶段批处理）：全部任务 caption（LLM 只起一次）→
审阅闸（仅 mode=review 的任务）→ 全部 generate（ComfyUI 只起一次）。
"""
from __future__ import annotations

import asyncio
import datetime as _dt
import json
import subprocess
import threading
import time
from pathlib import Path

import uvicorn
from fastapi import Body, FastAPI, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image

from . import comfy_client, config, input_ingest, llm_caption, sse
from .state import Item, Task, manager, sanitize_name

app = FastAPI(title="Prompt Studio")


@app.on_event("startup")
async def _startup() -> None:
    sse.hub.bind_loop(asyncio.get_running_loop())
    manager.load()


# ======================================================================
# 静态资源
# ======================================================================
app.mount("/static", StaticFiles(directory=str(config.WEB_DIR)), name="static")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(config.WEB_DIR / "index.html")


# ======================================================================
# 输入源
# ======================================================================
@app.post("/api/browse/dir")
def browse_dir() -> dict:
    """服务器端弹原生目录选择框（独立进程，避免阻塞事件循环）。"""
    code = (
        "import tkinter as tk;from tkinter import filedialog;"
        "r=tk.Tk();r.withdraw();r.attributes('-topmost',True);"
        "print(filedialog.askdirectory() or '',end='')"
    )
    try:
        out = subprocess.run(["python", "-c", code], capture_output=True,
                             text=True, timeout=120,
                             creationflags=subprocess.CREATE_NO_WINDOW)
        return {"path": out.stdout.strip()}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, f"目录选择失败：{e}") from e


@app.post("/api/upload")
async def upload(file: UploadFile) -> dict:
    config.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    name = Path(file.filename or "upload.zip").name
    if not name.lower().endswith((".zip", ".7z")):
        raise HTTPException(400, "仅支持 zip / 7z")
    dest = config.UPLOAD_DIR / name
    with open(dest, "wb") as out:
        while chunk := await file.read(1 << 20):
            out.write(chunk)
    return {"path": str(dest), "size": dest.stat().st_size}


# ======================================================================
# 工作区与队列
# ======================================================================
@app.get("/api/defaults")
def defaults() -> dict:
    return config.DEFAULT_PARAMS


@app.get("/api/health")
def health() -> dict:
    return {"llm": llm_caption.is_llm_up(), "comfy": comfy_client.is_comfy_up()}


@app.get("/api/workspace")
def get_workspace() -> dict:
    return manager.state()


@app.post("/api/workspace")
def switch_workspace(body: dict = Body(...)) -> dict:
    if manager.running:
        raise HTTPException(409, "队列运行中，不能切换工作区")
    p = str(body.get("path") or "").strip()
    if not p:
        raise HTTPException(400, "缺少 path")
    path = Path(p)
    if not path.is_absolute():
        raise HTTPException(400, f"工作区须为绝对路径：{p}")
    path.mkdir(parents=True, exist_ok=True)
    manager.switch_workspace(path)
    sse.hub.publish("log", text=f"工作区切换：{path}")
    return manager.state()


@app.post("/api/queue/items")
def import_queue_items(body: dict = Body(...)) -> dict:
    """批量导入：items = [{source, name?, mode?, output_image_dir?,
    output_prompt_dir?, ai_name?}]，先全部校验再创建（原子）。"""
    specs = body.get("items")
    if not isinstance(specs, list) or not specs:
        raise HTTPException(400, "缺少 items")
    for spec in specs:                                # 先校验
        source = str(spec.get("source", "")).strip()
        if not source or not Path(source).exists():
            raise HTTPException(400, f"输入源不存在：{source}")
        for label, val in (("图片输出目录", spec.get("output_image_dir")),
                           ("提示词输出目录", spec.get("output_prompt_dir"))):
            val = str(val or "").strip()
            if val and not Path(val).is_absolute():
                raise HTTPException(400, f"{label}须为绝对路径：{val}")
        if str(spec.get("mode") or "auto") not in ("auto", "review"):
            raise HTTPException(400, f"mode 非法：{spec.get('mode')}")
    created = [_create_task_entry(spec) for spec in specs]
    return {"created": [t.summary() for t in created], **manager.state()}


def _create_task_entry(spec: dict) -> Task:
    source = str(spec.get("source", "")).strip()
    params = dict(config.DEFAULT_PARAMS)
    params.update({k: v for k, v in (spec.get("params") or {}).items()
                   if k in params})
    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    name = sanitize_name(spec.get("name") or stamp) or stamp
    task_dir = manager._uniquify(manager.workspace / name)
    img_out = str(spec.get("output_image_dir") or "").strip()
    prompt_out = str(spec.get("output_prompt_dir") or "").strip()
    images_dir = Path(img_out) if img_out else task_dir
    prompts_dir = Path(prompt_out) if prompt_out else images_dir / "prompts"
    for d in (task_dir, images_dir, prompts_dir):
        d.mkdir(parents=True, exist_ok=True)

    src = Path(source)
    if src.is_dir():
        files = input_ingest.scan_dir(src)
        if not files:
            raise HTTPException(400, f"文件夹内没有图片：{source}")
        items, source_root = input_ingest.build_items(
            files, src, keep_in_place=True,
            task_source_dir=task_dir / ".studio" / "source")
    elif src.suffix.lower() in (".zip", ".7z"):
        files = input_ingest.extract_archive(src, task_dir / ".studio" / "source")
        if not files:
            raise HTTPException(400, f"压缩包内没有图片：{source}")
        items, source_root = input_ingest.build_items(
            files, src, keep_in_place=False,
            task_source_dir=task_dir / ".studio" / "source")
    else:
        raise HTTPException(400, f"输入源须为文件夹或 zip/7z 压缩包：{source}")

    t = manager.create_task(
        name=task_dir.name, source_desc=str(src), source_root=Path(source_root),
        params=params, items=items, task_dir=task_dir,
        output_image_dir=img_out, output_prompt_dir=prompt_out,
        mode=str(spec.get("mode") or "auto"),
        ai_name=bool(spec.get("ai_name", True)))
    sse.hub.publish("log", text=f"任务导入：{t.name}（{len(items)} 张，{t.mode}）")
    return t


@app.post("/api/queue/start")
def queue_start() -> dict:
    _require_idle_worker()
    threading.Thread(target=_queue_worker, daemon=True).start()
    return {"ok": True}


@app.post("/api/queue/pause")
def queue_pause() -> dict:
    manager.pause_flag = True          # 当前单张完成后生效
    _pub_queue()
    return {"ok": True}


@app.post("/api/queue/resume")
def queue_resume() -> dict:
    """审阅闸放行或暂停后继续：worker 在跑就放行事件，否则重新拉起。"""
    if manager.running:
        manager.resume_event.set()
    else:
        _require_idle_worker()
        threading.Thread(target=_queue_worker, daemon=True).start()
    return {"ok": True}


@app.post("/api/queue/stop")
def queue_stop() -> dict:
    """停止整个队列：当前张完成后，未完成任务全部标记中止（保留在队列可重跑）。"""
    manager.pause_flag = True
    with manager.lock:
        for t in manager.all_tasks():
            if t.phase in ("idle", "captioning", "generating"):
                t.cancel_flag = True
    sse.hub.publish("log", text="已请求停止队列")
    return {"ok": True}


@app.delete("/api/tasks/{tid}")
def delete_task(tid: str) -> dict:
    if manager.running:
        raise HTTPException(409, "队列运行中")
    if not manager.task(tid):
        raise HTTPException(404, "任务不存在")
    if not manager.remove_task(tid):
        raise HTTPException(400, "仅未开始的任务可移出队列")
    _pub_queue()
    return {"ok": True}


def _require_idle_worker() -> None:
    if manager.running or not manager.worker_busy.acquire(blocking=False):
        raise HTTPException(409, "队列/任务正在运行")
    manager.worker_busy.release()


# ======================================================================
# Worker（显存互斥：全局单 worker；服务只被任务流程拉起）
# ======================================================================
class _Cancelled(Exception):
    pass


def _pub_queue() -> None:
    sse.hub.publish("queue", **manager.state())


def _pub_item(t: Task, it: Item) -> None:
    sse.hub.publish("item", task_id=t.id, **_item_dict(it))


def _item_dict(it: Item) -> dict:
    return {
        "seq": it.seq, "src_filename": it.src_filename, "status": it.status,
        "contract": it.contract, "note": it.note, "error": it.error,
        "out_image": it.out_image, "wh_ratio": (it.contract or {}).get("wh_ratio", ""),
        "caption_ms": it.caption_ms, "generate_ms": it.generate_ms,
    }


def _set_task_phase(t: Task, phase: str) -> None:
    with manager.lock:
        t.phase = phase
    manager.save_task(t)
    sse.hub.publish("phase", task_id=t.id, phase=phase)
    _pub_queue()


def _set_seed(t: Task, it: Item, batch: bool) -> int:
    with manager.lock:
        return t.params["seed"] if batch else t.params["seed"] + it.seq


# ---------- 条目级共用循环（调用方保证服务已就绪） ----------
def _caption_items(t: Task, seqs: list[int] | None) -> None:
    """阶段一循环：逐张契约提取；首张成功后按需 AI 命名。"""
    targets = [it for it in t.items if not seqs or it.seq in seqs]
    named = False
    for i, it in enumerate(targets, 1):
        if manager.pause_flag or t.cancel_flag:
            break
        if it.status == "captioned" and not seqs:
            continue                                  # 断点续跑：跳过已完成
        manager.set_status(t, it, "captioning")
        sse.hub.publish("progress", task_id=t.id, task_name=t.name,
                        phase="captioning", seq=it.seq, done=i - 1,
                        total=len(targets), filename=it.src_filename)
        t0 = time.time()
        try:
            contract = llm_caption.caption_image(Path(it.src_path), it.note)
            with manager.lock:
                it.contract = contract
                it.caption_ms = int((time.time() - t0) * 1000)
            _append_summary(t, it)
            if not named and t.ai_name and not t.renamed:
                named = True
                _maybe_ai_rename(t, contract)
            manager.set_status(t, it, "captioned")
            _pub_item(t, it)
        except Exception as e:  # noqa: BLE001
            manager.set_status(t, it, "caption_failed", str(e)[:300])
            _pub_item(t, it)


def _maybe_ai_rename(t: Task, contract: dict) -> None:
    new_name = llm_caption.generate_task_name(contract)
    if new_name and manager.rename_task(t, new_name, by_user=False):
        sse.hub.publish("log", text=f"[AI 命名] {t.source_desc} → {t.name}")
        _pub_queue()


def _generate_items(t: Task, seqs: list[int] | None) -> None:
    """阶段二循环：逐张提交 ComfyUI 并收图。"""
    targets = [it for it in t.items if it.contract and
               (not seqs or it.seq in seqs)]
    batch = not seqs and len(targets) > 1
    for i, it in enumerate(targets, 1):
        if manager.pause_flag or t.cancel_flag:
            break
        if it.status == "done" and not seqs:
            continue                                  # 断点续跑：跳过已完成
        manager.set_status(t, it, "generating")
        sse.hub.publish("progress", task_id=t.id, task_name=t.name,
                        phase="generating", seq=it.seq, done=i - 1,
                        total=len(targets), filename=it.src_filename)
        t0 = time.time()
        try:
            seed = _set_seed(t, it, batch)
            png = comfy_client.generate_one(it.contract, t.params, seed,
                                            prefix=f"{it.seq:04d}")
            out_dir = t.images_dir
            out_dir.mkdir(parents=True, exist_ok=True)
            out_name = f"{Path(it.src_filename).stem}.png"
            (out_dir / out_name).write_bytes(png)
            with manager.lock:
                it.out_image = out_name
                it.generate_ms = int((time.time() - t0) * 1000)
            manager.set_status(t, it, "done")
            _pub_item(t, it)
        except Exception as e:  # noqa: BLE001
            manager.set_status(t, it, "generate_failed", str(e)[:300])
            _pub_item(t, it)


def _append_summary(t: Task, it: Item) -> None:
    line = json.dumps({"seq": it.seq, "file": it.src_filename, **(it.contract or {})},
                      ensure_ascii=False)
    pdir = t.prompts_dir
    pdir.mkdir(parents=True, exist_ok=True)
    with open(pdir / "prompts_summary.jsonl", "a", encoding="utf-8") as f:
        f.write(line + "\n")
    (pdir / f"{it.seq:04d}.json").write_text(
        json.dumps({"file": it.src_filename, **it.contract}, ensure_ascii=False, indent=1),
        encoding="utf-8")
    _write_tags_sidecar(t, it)


def _write_tags_sidecar(t: Task, it: Item) -> None:
    """LoRA 训练惯例的标签 sidecar：<提示词输出>/tags/NNNN.txt（§17.2）。"""
    tags = (it.contract or {}).get("tags") or []
    tags_dir = t.prompts_dir / "tags"
    tags_dir.mkdir(parents=True, exist_ok=True)
    (tags_dir / f"{it.seq:04d}.txt").write_text(
        ", ".join(str(x) for x in tags), encoding="utf-8")


# ---------- 队列 worker（阶段批处理） ----------
def _queue_worker() -> None:
    try:
        manager.worker_busy.acquire()
        manager.running = True
        manager.pause_flag = False
        manager.resume_event.clear()
        _pub_queue()
        try:
            _queue_stage_caption()
            if not manager.pause_flag:
                _queue_stage_generate()
        finally:
            manager.running = False
            manager.gate = False
            manager.resume_event.clear()
            _pub_queue()
    finally:
        manager.worker_busy.release()


def _queue_stage_caption() -> None:
    llm_ready = False
    for t in manager.all_tasks():
        if manager.pause_flag:
            break
        # idle=新任务；captioning/reviewing=暂停或失败后续跑（内层跳过已完成项）
        if t.phase not in ("idle", "captioning", "reviewing"):
            continue
        if t.cancel_flag:
            _set_task_phase(t, "stopped")
            continue
        t.cancel_flag = False
        _set_task_phase(t, "captioning")
        if not llm_ready:
            llm_caption.ensure_llm()
            llm_ready = True
        _caption_items(t, None)
        _set_task_phase(t, "stopped" if t.cancel_flag else "reviewing")
    if llm_caption.owns_llm():
        llm_caption.stop_llm()                        # 阶段批处理：全部提取完统一释放


def _queue_stage_generate() -> None:
    gated = [t for t in manager.all_tasks()
             if t.mode == "review" and t.phase == "reviewing"]
    if gated:
        manager.gate = True
        _pub_queue()
        sse.hub.publish("log",
                        text=f"审阅闸：{'、'.join(t.name for t in gated)} 等待放行…")
        while not manager.resume_event.wait(timeout=0.5):
            if manager.pause_flag:                    # 闸内也可暂停
                manager.gate = False
                _pub_queue()
                return
        manager.gate = False
        manager.resume_event.clear()
        _pub_queue()

    comfy_ready = False
    for t in manager.all_tasks():
        if manager.pause_flag:
            break
        if t.phase not in ("reviewing", "generating"):
            continue
        if t.cancel_flag:
            _set_task_phase(t, "stopped")
            continue
        t.cancel_flag = False
        if not comfy_ready:
            if llm_caption.owns_llm():
                llm_caption.stop_llm()                # 双保险：进 ComfyUI 前释放代管 LLM
            comfy_client.ensure_comfy()
            comfy_ready = True
        _set_task_phase(t, "generating")
        _generate_items(t, None)
        _set_task_phase(t, "stopped" if t.cancel_flag else "done")
    if comfy_ready:
        participated = [t for t in manager.all_tasks() if t.phase == "done"]
        if all(t.params.get("close_services_after", True) for t in participated):
            comfy_client.stop_comfy()


# ---------- 单任务操作（审阅页：重提/单张生成/整任务生成；队列空闲时可用） ----------
def _single_op(kind: str, t: Task, seqs: list[int] | None) -> None:
    try:
        manager.worker_busy.acquire()
        try:
            t.cancel_flag = False
            if kind == "caption":
                _set_task_phase(t, "captioning")
                llm_caption.ensure_llm()
                _caption_items(t, seqs)
                _set_task_phase(t, "stopped" if t.cancel_flag else "reviewing")
            else:
                _set_task_phase(t, "generating")
                if llm_caption.owns_llm():
                    llm_caption.stop_llm()
                comfy_client.ensure_comfy()
                _generate_items(t, seqs)
                if t.params.get("close_services_after", True):
                    comfy_client.stop_comfy()
                _set_task_phase(t, "stopped" if t.cancel_flag else
                                ("reviewing" if _has_uncaptioned(t) else "done"))
        finally:
            manager.worker_busy.release()
    except Exception as e:  # noqa: BLE001 —— ensure 阶段失败的兜底（服务起不来等）
        sse.hub.publish("log", text=f"[error] {t.name}: {e}")
        with manager.lock:
            t.phase = "reviewing" if kind == "caption" else t.phase
        manager.save_task(t)
        _pub_queue()


def _has_uncaptioned(t: Task) -> bool:
    return any(not it.contract for it in t.items)


# ======================================================================
# 任务路由（id 化）
# ======================================================================
def _need_task(tid: str) -> Task:
    t = manager.task(tid)
    if not t:
        raise HTTPException(404, "任务不存在")
    return t


@app.get("/api/tasks/{tid}")
def get_task(tid: str) -> dict:
    t = _need_task(tid)
    with manager.lock:
        return {"task": t.summary(), "items": [_item_dict(i) for i in t.items]}


@app.post("/api/tasks/{tid}/caption")
def task_caption(tid: str, body: dict = Body(default={})) -> dict:
    t = _need_task(tid)
    _require_idle_worker()
    seqs = body.get("seqs")
    threading.Thread(target=_single_op, kwargs={"kind": "caption", "t": t,
                                                "seqs": seqs}, daemon=True).start()
    return {"ok": True}


@app.post("/api/tasks/{tid}/generate")
def task_generate(tid: str, body: dict = Body(default={})) -> dict:
    t = _need_task(tid)
    _require_idle_worker()
    seqs = body.get("seqs")
    threading.Thread(target=_single_op, kwargs={"kind": "generate", "t": t,
                                                "seqs": seqs}, daemon=True).start()
    return {"ok": True}


@app.post("/api/tasks/{tid}/stop")
def task_stop(tid: str) -> dict:
    t = _need_task(tid)
    with manager.lock:
        t.cancel_flag = True
    sse.hub.publish("log", text=f"已请求停止任务 {t.name}（当前单张完成后生效）")
    return {"ok": True}


@app.post("/api/tasks/{tid}/rename")
def task_rename(tid: str, body: dict = Body(...)) -> dict:
    t = _need_task(tid)
    if manager.running:
        raise HTTPException(409, "队列运行中，暂停后再改名")
    if not manager.rename_task(t, str(body.get("name") or ""), by_user=True):
        raise HTTPException(400, "重命名失败（名称为空/重复/目录被占用）")
    sse.hub.publish("log", text=f"任务重命名 → {t.name}")
    _pub_queue()
    return {"ok": True}


# ---------- 条目操作 ----------
@app.put("/api/tasks/{tid}/items/{seq}")
def update_item(tid: str, seq: int, body: dict = Body(...)) -> dict:
    found = manager.item_op(tid, seq)
    if not found:
        raise HTTPException(404, "条目不存在")
    t, it = found
    with manager.lock:
        if "rewritten_prompt" in body or "wh_ratio" in body or "tags" in body:
            contract = dict(it.contract or {})
            if "rewritten_prompt" in body:
                contract["rewritten_prompt"] = str(body["rewritten_prompt"]).strip()
            if "wh_ratio" in body:
                contract["wh_ratio"] = str(body["wh_ratio"]).strip()
            if "tags" in body:
                from .llm_caption import _normalize_tags
                contract["tags"] = _normalize_tags(body["tags"])
            it.contract = contract
        if "note" in body:
            it.note = str(body["note"])
        if it.status == "caption_failed":
            manager.set_status(t, it, "pending")
    manager.save_task(t)
    if "tags" in body and it.contract:
        _write_tags_sidecar(t, it)                    # 标签编辑同步重写 sidecar（§17.3）
    _pub_item(t, it)
    return {"ok": True}


@app.post("/api/tasks/{tid}/items/{seq}/generate")
def generate_item(tid: str, seq: int) -> dict:
    found = manager.item_op(tid, seq)
    if not found:
        raise HTTPException(404, "条目不存在")
    t, it = found
    if not it.contract:
        raise HTTPException(400, "该条目还没有契约提示词")
    return task_generate(tid, {"seqs": [seq]})


@app.post("/api/tasks/{tid}/items/{seq}/recaption")
def recaption_item(tid: str, seq: int, body: dict = Body(default={})) -> dict:
    found = manager.item_op(tid, seq)
    if not found:
        raise HTTPException(404, "条目不存在")
    t, it = found
    with manager.lock:
        it.note = str(body.get("note", ""))
    manager.set_status(t, it, "pending")
    return task_caption(tid, {"seqs": [seq]})


# ======================================================================
# 图片服务（安全边界：source_root / 任务目录 / 双输出目录 之内）
# ======================================================================
def _safe_file(t: Task, path: Path) -> Path:
    roots = [str(Path(t.source_root).resolve()), str(Path(t.dir).resolve()),
             str(t.images_dir.resolve()), str(t.prompts_dir.resolve())]
    rp = path.resolve()
    if any(str(rp).startswith(r) for r in roots) and rp.is_file():
        return rp
    raise HTTPException(404)


_MIME = {"png": "image/png", "webp": "image/webp", "jpg": "image/jpeg",
         "jpeg": "image/jpeg", "gif": "image/gif", "bmp": "image/bmp"}


def _mime(path: Path) -> str:
    return _MIME.get(path.suffix.lower().lstrip("."), "application/octet-stream")


@app.get("/api/tasks/{tid}/img/src/{seq}")
def img_src(tid: str, seq: int) -> FileResponse:
    found = manager.item_op(tid, seq)
    if not found:
        raise HTTPException(404)
    t, it = found
    p = _safe_file(t, Path(it.src_path))
    return FileResponse(p, media_type=_mime(p))


@app.get("/api/tasks/{tid}/img/thumb/{seq}")
def img_thumb(tid: str, seq: int) -> FileResponse:
    found = manager.item_op(tid, seq)
    if not found:
        raise HTTPException(404)
    t, it = found
    thumb_dir = t.thumbs_dir
    thumb = thumb_dir / f"{it.seq:04d}.webp"
    if not thumb.exists():
        thumb_dir.mkdir(parents=True, exist_ok=True)
        with Image.open(_safe_file(t, Path(it.src_path))) as im:
            im.thumbnail((512, 512))
            im.convert("RGB").save(thumb, "WEBP", quality=80)
    return FileResponse(thumb, media_type="image/webp")


@app.get("/api/tasks/{tid}/img/out/{seq}")
def img_out(tid: str, seq: int) -> FileResponse:
    found = manager.item_op(tid, seq)
    if not found or not found[1].out_image:
        raise HTTPException(404)
    t, it = found
    p = _safe_file(t, t.images_dir / it.out_image)
    return FileResponse(p, media_type=_mime(p))


# ======================================================================
# SSE
# ======================================================================
@app.get("/api/task/events")
async def events() -> StreamingResponse:
    sid, q = sse.hub.subscribe()

    async def gen():
        try:
            yield "retry: 3000\n\n"
            while True:
                try:
                    payload = await asyncio.wait_for(q.get(), timeout=15)
                    yield sse.hub.encode(payload)
                except asyncio.TimeoutError:
                    yield ": ping\n\n"      # 保活
        finally:
            sse.hub.unsubscribe(sid)

    return StreamingResponse(gen(), media_type="text/event-stream")


if __name__ == "__main__":
    uvicorn.run(app, host=config.WEB_HOST, port=config.WEB_PORT)
