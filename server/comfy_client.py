"""阶段二：ComfyUI 代管 + API 图构建 + 逐张提交/轮询/收图。

API 图与 2026-10-10 akane 实跑验证过的结构一致（参考
D:/work_space/ai/llm/qwen_28b/comfy_run_akane_001/queue_two.py）。
"""
from __future__ import annotations

import json
import math
import re
import subprocess
import time
from pathlib import Path

import httpx

from . import config, sse

_owned_proc: subprocess.Popen | None = None
_RATIO = re.compile(r"^\s*(\d+)\s*:\s*(\d+)\s*$")


# ---------- 生命周期 ----------
def is_comfy_up() -> bool:
    try:
        return httpx.get(f"{config.COMFY_API_BASE}/system_stats", timeout=3).status_code == 200
    except Exception:  # noqa: BLE001 —— 未启动时连接被拒，属正常态
        return False


def start_comfy() -> None:
    global _owned_proc
    config.LOG_DIR.mkdir(exist_ok=True)
    log = open(config.LOG_DIR / "comfyui.log", "ab")
    _owned_proc = subprocess.Popen(
        [str(config.COMFYUI_PYTHON), "-s", "main.py", "--disable-auto-launch"],
        stdout=log, stderr=subprocess.STDOUT,
        creationflags=subprocess.CREATE_NO_WINDOW,
        cwd=str(config.COMFYUI_DIR),
    )


def ensure_comfy() -> None:
    if is_comfy_up():
        sse.publish("log", text="ComfyUI 已在运行（复用现有实例，结束时不关闭）")
        return
    sse.publish("log", text="启动 ComfyUI（约 1 分钟）…")
    start_comfy()
    deadline = time.time() + config.SERVICE_READY_TIMEOUT
    while time.time() < deadline:
        if is_comfy_up():
            sse.publish("log", text="ComfyUI 就绪")
            return
        time.sleep(2)
    raise RuntimeError("ComfyUI 启动超时（详见 logs/comfyui.log）")


def stop_comfy() -> None:
    global _owned_proc
    if _owned_proc is None:
        return
    subprocess.run(["taskkill", "/T", "/F", "/PID", str(_owned_proc.pid)],
                   capture_output=True)
    _owned_proc = None
    sse.publish("log", text="已关闭代管的 ComfyUI")


# ---------- wh_ratio → 像素（复刻 ResolutionSelector：1MP、multiple=8） ----------
def wh_ratio_to_px(ratio: str, megapixels: float = 1.0) -> tuple[int, int]:
    m = _RATIO.match(ratio)
    if not m:
        raise ValueError(f"wh_ratio 非法：{ratio!r}")
    rw, rh = int(m.group(1)), int(m.group(2))
    scale = math.sqrt(megapixels * 1024 * 1024 / (rw * rh))
    return round(rw * scale / 8) * 8, round(rh * scale / 8) * 8


# ---------- API 图 ----------
def build_graph(contract: dict, params: dict, filename_prefix: str) -> dict:
    width, height = wh_ratio_to_px(contract["wh_ratio"], params["megapixels"])
    return {
        "1": {"class_type": "UNETLoader", "inputs": {
            "unet_name": params["comfy_model"], "weight_dtype": "default"}},
        "2": {"class_type": "CLIPLoader", "inputs": {
            "clip_name": params["clip_model"], "type": "qwen_image", "device": "default"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": params["vae_model"]}},
        "4": {"class_type": "TextEncodeQwenImage21", "inputs": {
            "clip": ["2", 0], "prompt": contract["rewritten_prompt"],
            "negative_prompt": "", "resolution": params["te_resolution"]}},
        "5": {"class_type": "EmptyLatentImage", "inputs": {
            "width": width, "height": height, "batch_size": 1}},
        "6": {"class_type": "KSampler", "inputs": {
            "model": ["1", 0], "positive": ["4", 0], "negative": ["4", 1],
            "latent_image": ["5", 0], "seed": int(params["seed"]),
            "steps": params["steps"], "cfg": params["cfg"],
            "sampler_name": params["sampler"], "scheduler": params["scheduler"],
            "denoise": 1}},
        "7": {"class_type": "VAEDecode", "inputs": {"samples": ["6", 0], "vae": ["3", 0]}},
        "8": {"class_type": "SaveImage", "inputs": {
            "images": ["7", 0], "filename_prefix": filename_prefix}},
    }


def generate_one(contract: dict, params: dict, seed: int, prefix: str) -> bytes:
    """提交一张并等待完成，返回 PNG 字节。"""
    params = dict(params, seed=seed)
    graph = build_graph(contract, params, prefix)
    r = httpx.post(f"{config.COMFY_API_BASE}/prompt",
                   json={"prompt": graph, "client_id": "prompt-studio"}, timeout=60)
    r.raise_for_status()
    resp = r.json()
    if "prompt_id" not in resp:
        raise RuntimeError(f"入队被拒：{resp}")
    prompt_id = resp["prompt_id"]

    deadline = time.time() + 600
    while time.time() < deadline:
        hist = httpx.get(f"{config.COMFY_API_BASE}/history/{prompt_id}", timeout=30).json()
        if prompt_id in hist:
            status = hist[prompt_id].get("status", {})
            if status.get("status_str") == "error":
                raise RuntimeError(f"ComfyUI 执行出错：{json.dumps(status.get('messages', []))[:500]}")
            outputs = hist[prompt_id].get("outputs", {})
            for node_out in outputs.values():
                for im in node_out.get("images", []):
                    img = httpx.get(f"{config.COMFY_API_BASE}/view", params={
                        "filename": im["filename"], "subfolder": im.get("subfolder", ""),
                        "type": im.get("type", "output")}, timeout=60)
                    img.raise_for_status()
                    return img.content
            raise RuntimeError("执行完成但未找到输出图")
        time.sleep(2)
    raise RuntimeError("生图超时（600s）")
