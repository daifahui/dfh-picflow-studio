"""Prompt Studio 服务端入口：路由 + 两阶段 worker。

运行边界：llama-server / ComfyUI 只会被任务流程（用户在网页点击按钮）拉起；
开发测试期手动运行它们需用户批准（设计文档 §12）。
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
from .state import Item, manager

app = FastAPI(title="Prompt Studio")


@app.on_event("startup")
async def _startup() -> None:
    sse.hub.bind_loop(__import__("asyncio").get_running_loop())
    manager.load_latest()


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
# 任务
# ======================================================================
@app.get("/api/defaults")
def defaults() -> dict:
    return config.DEFAULT_PARAMS


@app.get("/api/health")
def health() -> dict:
    return {"llm": llm_caption.is_llm_up(), "comfy": comfy_client.is_comfy_up()}


@app.get("/api/task")
def get_task() -> dict:
    t = manager.current()
    if not t:
        return {"task": None}
    with manager.lock:
        return {"task": t.summary(),
                "items": [_item_dict(i) for i in t.items]}


def _item_dict(it: Item) -> dict:
    return {
        "seq": it.seq, "src_filename": it.src_filename, "status": it.status,
        "contract": it.contract, "note": it.note, "error": it.error,
        "out_image": it.out_image, "wh_ratio": (it.contract or {}).get("wh_ratio", ""),
        "caption_ms": it.caption_ms, "generate_ms": it.generate_ms,
    }


@app.post("/api/task")
def create_task(body: dict = Body(...)) -> dict:
    if manager.current() and not manager.current().cancel_flag:
        raise HTTPException(409, "已有活动任务（先停止或等它完成）")
    source = str(body.get("source", "")).strip()
    if not source or not Path(source).exists():
        raise HTTPException(400, f"输入源不存在：{source}")

    params = dict(config.DEFAULT_PARAMS)
    params.update({k: v for k, v in (body.get("params") or {}).items()
                   if k in params})
    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    name = body.get("name") or stamp
    task_dir = config.OUTPUT_ROOT / f"{stamp}_{name}"
    src = Path(source)

    if src.is_dir():
        files = input_ingest.scan_dir(src)
        if not files:
            raise HTTPException(400, "文件夹内没有图片")
        items, source_root = input_ingest.build_items(
            files, src, keep_in_place=True, task_source_dir=task_dir / "source")
    elif src.suffix.lower() in (".zip", ".7z"):
        files = input_ingest.extract_archive(src, task_dir / "source")
        if not files:
            raise HTTPException(400, "压缩包内没有图片")
        items, source_root = input_ingest.build_items(
            files, src, keep_in_place=False, task_source_dir=task_dir / "source")
    else:
        raise HTTPException(400, "输入源须为文件夹或 zip/7z 压缩包")

    task = manager.create(name, str(src), Path(source_root), params, items, task_dir)
    sse.hub.publish("log", text=f"任务创建：{len(items)} 张图片 → {task_dir.name}")
    return get_task()


@app.post("/api/task/stop")
def stop_task() -> dict:
    t = manager.current()
    if not t:
        raise HTTPException(404, "没有活动任务")
    with manager.lock:
        t.cancel_flag = True
        t.phase = "stopped"
    sse.hub.publish("log", text="已请求停止（当前单张完成后生效）")
    return {"ok": True}


# ======================================================================
# Worker（阶段一 / 阶段二）
# ======================================================================
def _check_cancel() -> None:
    t = manager.current()
    if t and t.cancel_flag:
        raise _Cancelled()


class _Cancelled(Exception):
    pass


def _run_caption(seqs: list[int] | None) -> None:
    t = manager.current()
    try:
        manager.worker_busy.acquire()
        with manager.lock:
            t.phase = "captioning"
            t.cancel_flag = False
        sse.hub.publish("phase", phase="captioning")
        llm_caption.ensure_llm()
        targets = [it for it in t.items if not seqs or it.seq in seqs]
        n_total = len(targets)
        for i, it in enumerate(targets, 1):
            if t.cancel_flag:
                break
            if it.status == "captioned" and not seqs:
                continue  # 断点续跑：跳过已完成
            manager.set_status(it, "captioning")
            sse.hub.publish("progress", phase="captioning", seq=it.seq,
                            done=i - 1, total=n_total, filename=it.src_filename)
            t0 = time.time()
            try:
                contract = llm_caption.caption_image(Path(it.src_path), it.note)
                with manager.lock:
                    it.contract = contract
                    it.caption_ms = int((time.time() - t0) * 1000)
                _append_summary(it)
                manager.set_status(it, "captioned")
                sse.hub.publish("item", **_item_dict(it))
            except Exception as e:  # noqa: BLE001
                manager.set_status(it, "caption_failed", str(e)[:300])
                sse.hub.publish("item", **_item_dict(it))
        with manager.lock:
            t.phase = "reviewing"
        manager.save()
        sse.hub.publish("phase", phase="reviewing")
    except _Cancelled:
        with manager.lock:
            t.phase = "stopped"
        manager.save()
        sse.hub.publish("phase", phase="stopped")
    finally:
        manager.worker_busy.release()


def _run_generate(seqs: list[int] | None) -> None:
    t = manager.current()
    try:
        manager.worker_busy.acquire()
        with manager.lock:
            t.phase = "generating"
            t.cancel_flag = False
        sse.hub.publish("phase", phase="generating")
        if llm_caption.owns_llm():
            llm_caption.stop_llm()     # 显存互斥：进 ComfyUI 前释放代管的 caption 模型（外部实例只复用）
        comfy_client.ensure_comfy()
        targets = [it for it in t.items if it.contract and
                   (not seqs or it.seq in seqs)]
        n_total = len(targets)
        for i, it in enumerate(targets, 1):
            if t.cancel_flag:
                break
            manager.set_status(it, "generating")
            sse.hub.publish("progress", phase="generating", seq=it.seq,
                            done=i - 1, total=n_total, filename=it.src_filename)
            t0 = time.time()
            try:
                with manager.lock:
                    seed = t.params["seed"] if len(targets) > 1 and not seqs \
                        else t.params["seed"] + it.seq  # 单张重跑换种子求变化
                png = comfy_client.generate_one(
                    it.contract, t.params, seed, prefix=f"{it.seq:04d}")
                out_dir = Path(t.dir) / "images"
                out_dir.mkdir(exist_ok=True)
                out_name = f"{Path(it.src_filename).stem}.png"
                (out_dir / out_name).write_bytes(png)
                with manager.lock:
                    it.out_image = out_name
                    it.generate_ms = int((time.time() - t0) * 1000)
                manager.set_status(it, "done")
                sse.hub.publish("item", **_item_dict(it))
            except Exception as e:  # noqa: BLE001
                manager.set_status(it, "generate_failed", str(e)[:300])
                sse.hub.publish("item", **_item_dict(it))
        if t.params.get("close_services_after", True):
            comfy_client.stop_comfy()
        with manager.lock:
            t.phase = "done" if not t.cancel_flag else "stopped"
        manager.save()
        sse.hub.publish("phase", phase=t.phase)
    except _Cancelled:
        comfy_client.stop_comfy()
        with manager.lock:
            t.phase = "stopped"
        manager.save()
        sse.hub.publish("phase", phase="stopped")
    finally:
        manager.worker_busy.release()


def _append_summary(it: Item) -> None:
    t = manager.current()
    line = json.dumps({"seq": it.seq, "file": it.src_filename, **(it.contract or {})},
                      ensure_ascii=False)
    with open(Path(t.dir) / "prompts_summary.jsonl", "a", encoding="utf-8") as f:
        f.write(line + "\n")
    pdir = Path(t.dir) / "prompts"
    pdir.mkdir(exist_ok=True)
    (pdir / f"{it.seq:04d}.json").write_text(
        json.dumps({"file": it.src_filename, **it.contract}, ensure_ascii=False, indent=1),
        encoding="utf-8")


@app.post("/api/task/caption")
def start_caption(body: dict = Body(default={})) -> dict:
    t = manager.current()
    if not t:
        raise HTTPException(404, "先创建任务")
    if not manager.worker_busy.acquire(blocking=False):
        raise HTTPException(409, "有任务正在运行")
    manager.worker_busy.release()
    threading.Thread(target=_run_caption,
                     kwargs={"seqs": body.get("seqs")}, daemon=True).start()
    return {"ok": True}


@app.post("/api/task/generate")
def start_generate(body: dict = Body(default={})) -> dict:
    t = manager.current()
    if not t:
        raise HTTPException(404, "先创建任务")
    if not manager.worker_busy.acquire(blocking=False):
        raise HTTPException(409, "有任务正在运行")
    manager.worker_busy.release()
    seqs = body.get("seqs")
    threading.Thread(target=_run_generate,
                     kwargs={"seqs": seqs}, daemon=True).start()
    return {"ok": True}


@app.post("/api/task/auto")
def start_auto() -> dict:
    """全自动：caption 完成后自动接 generate（同一线程串行，含关 LLM 开 ComfyUI）。"""
    t = manager.current()
    if not t:
        raise HTTPException(404, "先创建任务")
    if not manager.worker_busy.acquire(blocking=False):
        raise HTTPException(409, "有任务正在运行")
    manager.worker_busy.release()

    def _auto() -> None:
        _run_caption(None)
        cur = manager.current()
        if cur and cur.phase == "reviewing":     # 未被中止才继续
            _run_generate(None)
    threading.Thread(target=_auto, daemon=True).start()
    return {"ok": True}


# ======================================================================
# 条目操作
# ======================================================================
@app.put("/api/task/items/{seq}")
def update_item(seq: int, body: dict = Body(...)) -> dict:
    it = manager.item(seq)
    if not it:
        raise HTTPException(404, "条目不存在")
    with manager.lock:
        if "rewritten_prompt" in body or "wh_ratio" in body:
            contract = dict(it.contract or {})
            if "rewritten_prompt" in body:
                contract["rewritten_prompt"] = str(body["rewritten_prompt"]).strip()
            if "wh_ratio" in body:
                contract["wh_ratio"] = str(body["wh_ratio"]).strip()
            it.contract = contract
        if "note" in body:
            it.note = str(body["note"])
        if it.status in ("caption_failed",):
            manager.set_status(it, "pending")
    manager.save()
    sse.hub.publish("item", **_item_dict(it))
    return {"ok": True}


@app.post("/api/task/items/{seq}/generate")
def generate_item(seq: int) -> dict:
    it = manager.item(seq)
    if not it or not it.contract:
        raise HTTPException(400, "该条目还没有契约提示词")
    return start_generate({"seqs": [seq]})


@app.post("/api/task/items/{seq}/recaption")
def recaption_item(seq: int, body: dict = Body(default={})) -> dict:
    """带注释重提：记录注释 → 状态回 pending → 只对该张重跑 caption。"""
    it = manager.item(seq)
    if not it:
        raise HTTPException(404, "条目不存在")
    with manager.lock:
        it.note = str(body.get("note", ""))
    manager.set_status(it, "pending")
    return start_caption({"seqs": [seq]})


# ======================================================================
# 图片服务（安全边界：只允许 source_root 与任务输出目录内的文件）
# ======================================================================
def _safe_file(path: Path) -> Path:
    t = manager.current()
    roots = [str(Path(t.source_root).resolve()), str(Path(t.dir).resolve())] if t else []
    rp = path.resolve()
    if any(str(rp).startswith(r) for r in roots) and rp.is_file():
        return rp
    raise HTTPException(404)


_MIME = {"png": "image/png", "webp": "image/webp", "jpg": "image/jpeg",
         "jpeg": "image/jpeg", "gif": "image/gif", "bmp": "image/bmp"}


def _mime(path: Path) -> str:
    return _MIME.get(path.suffix.lower().lstrip("."), "application/octet-stream")


@app.get("/api/img/src/{seq}")
def img_src(seq: int) -> FileResponse:
    it = manager.item(seq)
    if not it:
        raise HTTPException(404)
    p = _safe_file(Path(it.src_path))
    return FileResponse(p, media_type=_mime(p))


@app.get("/api/img/thumb/{seq}")
def img_thumb(seq: int) -> FileResponse:
    it = manager.item(seq)
    if not it:
        raise HTTPException(404)
    t = manager.current()
    thumb_dir = Path(t.dir) / "thumbs"
    thumb = thumb_dir / f"{it.seq:04d}.webp"
    if not thumb.exists():
        thumb_dir.mkdir(exist_ok=True)
        with Image.open(_safe_file(Path(it.src_path))) as im:
            im.thumbnail((512, 512))
            im.convert("RGB").save(thumb, "WEBP", quality=80)
    return FileResponse(thumb, media_type="image/webp")


@app.get("/api/img/out/{seq}")
def img_out(seq: int) -> FileResponse:
    it = manager.item(seq)
    if not it or not it.out_image:
        raise HTTPException(404)
    t = manager.current()
    p = _safe_file(Path(t.dir) / "images" / it.out_image)
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
