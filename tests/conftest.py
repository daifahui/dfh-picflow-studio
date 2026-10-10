"""pytest 公共 fixture。

铁律：任何测试都不得拉起真实 llama-server / ComfyUI。
凡 worker 会触发的外部调用，一律在 fixture 中 monkeypatch 为桩。
"""
from __future__ import annotations

import io
import json
import sys
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
    """每个测试独享空任务态。"""
    with manager.lock:
        manager._task = None
    yield
    with manager.lock:
        manager._task = None


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
    """桩掉两个模型服务的全部触点：ensure/no-op、caption 返回固定契约、
    generate 返回真 PNG 字节、stop 为 no-op。"""
    monkeypatch.setattr(llm_caption, "ensure_llm", lambda: None)
    monkeypatch.setattr(llm_caption, "stop_llm", lambda: None)
    monkeypatch.setattr(llm_caption, "caption_image",
                        lambda path, note="": dict(FAKE_CONTRACT))
    monkeypatch.setattr(comfy_client, "ensure_comfy", lambda: None)
    monkeypatch.setattr(comfy_client, "stop_comfy", lambda: None)
    monkeypatch.setattr(comfy_client, "generate_one",
                        lambda contract, params, seed, prefix: make_png(32, 32))
    return FAKE_CONTRACT


@pytest.fixture()
def client(reset_manager, tmp_path, monkeypatch):
    """TestClient；输出目录隔离到 tmp，startup 的 load_latest 不受仓库真实
    outputs/ 残留影响。"""
    from server import config
    monkeypatch.setattr(config, "OUTPUT_ROOT", tmp_path / "outputs")
    with TestClient(main.app) as c:
        yield c


def wait_phase(client: TestClient, phase: str, timeout: float = 10.0) -> dict:
    """轮询任务直至进入指定 phase（worker 在后台线程跑）。"""
    import time
    deadline = time.time() + timeout
    while time.time() < deadline:
        data = client.get("/api/task").json()
        if data["task"] and data["task"]["phase"] == phase:
            return data
        time.sleep(0.1)
    raise AssertionError(f"等待 phase={phase} 超时，实际：{json.dumps(data['task'], ensure_ascii=False)}")
