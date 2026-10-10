"""pytest 公共 fixture。

铁律：任何测试都不得拉起真实 llama-server / ComfyUI。
凡 worker 会触发的外部调用，一律在 fixture 中 monkeypatch 为桩。
"""
from __future__ import annotations

import io
import json
import sys
import time
import zipfile
from pathlib import Path

import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from server import comfy_client, llm_caption, main  # noqa: E402
from server.state import manager  # noqa: E402

FAKE_CONTRACT = {"rewritten_prompt": "a test prompt, anime style", "wh_ratio": "1:1"}


def make_png(w: int = 64, h: int = 64) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (200, 30, 30)).save(buf, "PNG")
    return buf.getvalue()


@pytest.fixture()
def reset_manager():
    """每个测试独享空工作区态。"""
    def _clear():
        with manager.lock:
            manager._tasks.clear()
            manager._queue.clear()
            manager.running = False
            manager.gate = False
            manager.pause_flag = False
            manager.resume_event.clear()
    _clear()
    yield
    _clear()


@pytest.fixture()
def image_dir(tmp_path: Path) -> Path:
    """3 张图（乱序文件名），验证自然排序。"""
    d = tmp_path / "imgs"
    d.mkdir()
    for name in ("b10.png", "a2.jpg", "a1.webp"):
        (d / name).write_bytes(make_png())
    return d


@pytest.fixture()
def zip_archive(tmp_path: Path, image_dir: Path) -> Path:
    zp = tmp_path / "pack.zip"
    with zipfile.ZipFile(zp, "w") as zf:
        for p in sorted(image_dir.iterdir()):
            zf.write(p, p.name)
    return zp


@pytest.fixture()
def stub_external(monkeypatch):
    """桩掉两个模型服务与 AI 命名的全部触点。"""
    monkeypatch.setattr(llm_caption, "ensure_llm", lambda: None)
    monkeypatch.setattr(llm_caption, "stop_llm", lambda: None)
    monkeypatch.setattr(llm_caption, "caption_image",
                        lambda path, note="": dict(FAKE_CONTRACT))
    monkeypatch.setattr(llm_caption, "generate_task_name",
                        lambda contract: "AI 名称")
    monkeypatch.setattr(comfy_client, "ensure_comfy", lambda: None)
    monkeypatch.setattr(comfy_client, "stop_comfy", lambda: None)
    monkeypatch.setattr(comfy_client, "generate_one",
                        lambda contract, params, seed, prefix: make_png(32, 32))
    return FAKE_CONTRACT


@pytest.fixture()
def client(reset_manager, tmp_path, monkeypatch):
    """TestClient；工作区与输出目录隔离到 tmp，startup 的 load 不受仓库真实
    outputs/ 残留影响。"""
    from server import config
    ws_root = tmp_path / "outputs"
    ws_root.mkdir()
    monkeypatch.setattr(config, "OUTPUT_ROOT", ws_root)
    monkeypatch.setattr(config, "WORKSPACE_POINTER", tmp_path / "ws-pointer.json")
    with manager.lock:
        manager.workspace = ws_root
    with TestClient(main.app) as c:
        yield c


# ---------- 队列测试辅助 ----------
def import_task(client: TestClient, source: Path, name: str = "t",
                mode: str = "auto", **kw) -> str:
    """导入一个任务，返回 task_id（默认关 AI 命名保证 id 稳定）。"""
    spec = {"source": str(source), "name": name, "mode": mode,
            "ai_name": kw.pop("ai_name", False), **kw}
    r = client.post("/api/queue/items", json={"items": [spec]})
    assert r.status_code == 200, r.text
    return r.json()["created"][0]["id"]


def wait_task_phase(client: TestClient, tid: str, phase: str,
                    timeout: float = 10.0) -> dict:
    deadline = time.time() + timeout
    d = {}
    while time.time() < deadline:
        d = client.get(f"/api/tasks/{tid}").json()
        if d["task"]["phase"] == phase:
            return d
        time.sleep(0.05)
    raise AssertionError(
        f"等待 task={tid} phase={phase} 超时，实际：{d.get('task')}")


def wait_ws(client: TestClient, pred, timeout: float = 10.0) -> dict:
    deadline = time.time() + timeout
    ws = {}
    while time.time() < deadline:
        ws = client.get("/api/workspace").json()
        if pred(ws):
            return ws
        time.sleep(0.05)
    raise AssertionError(f"等待工作区状态超时：{json.dumps(ws, ensure_ascii=False)}")
