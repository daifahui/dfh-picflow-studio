"""state 单元测试：TaskManager 增删查、持久化、断点回看。"""
import json
from pathlib import Path

import pytest

from server import config
from server.state import Item, Task, manager


def _mk_task(tmp_path: Path) -> Task:
    items = [Item(seq=1, src_filename="0001_a.png", src_path=str(tmp_path / "a.png")),
             Item(seq=2, src_filename="0002_b.png", src_path=str(tmp_path / "b.png"))]
    return manager.create("t1", "src", tmp_path, dict(config.DEFAULT_PARAMS),
                          items, tmp_path)


def test_create_and_item_lookup(tmp_path: Path):
    t = _mk_task(tmp_path)
    assert manager.current() is t
    assert manager.item(2).src_filename == "0002_b.png"
    assert manager.item(99) is None
    assert t.summary()["total"] == 2


def test_set_status_persists_task_json(tmp_path: Path):
    t = _mk_task(tmp_path)
    manager.set_status(manager.item(1), "captioned")
    d = json.loads((Path(t.dir) / ".studio" / "task.json").read_text(encoding="utf-8"))
    assert d["items"][0]["status"] == "captioned"


def test_save_is_atomic_no_tmp_leftover(tmp_path: Path):
    t = _mk_task(tmp_path)
    manager.set_status(manager.item(1), "done")
    leftovers = list((Path(t.dir) / ".studio").glob("*.tmp"))
    assert leftovers == []


def test_v11_default_paths(tmp_path: Path):
    """新布局：图片在任务根、提示词在 prompts/、簿记在 .studio/；双输出目录可覆盖。"""
    t = _mk_task(tmp_path)
    assert t.images_dir == Path(t.dir)
    assert t.prompts_dir == Path(t.dir) / "prompts"
    assert t.task_json_path == Path(t.dir) / ".studio" / "task.json"
    assert t.thumbs_dir == Path(t.dir) / ".studio" / "thumbs"

    t.output_image_dir = r"D:\out"
    assert t.prompts_dir == Path(r"D:\out") / "prompts"     # 默认跟随图片输出目录
    t.output_prompt_dir = r"E:\words"
    assert t.prompts_dir == Path(r"E:\words")               # 显式指定优先


def test_from_dict_legacy_layout(tmp_path: Path):
    """v1.0 旧 task.json（无 output_image_dir 字段）→ 自动识别 v10 只读布局。"""
    d = {"id": "x", "name": "x", "source_desc": "s", "source_root": str(tmp_path),
         "params": {}, "created_at": 0.0, "phase": "done", "dir": str(tmp_path),
         "items": []}
    t = Task.from_dict(d)
    assert t.layout == "v10"
    assert t.images_dir == tmp_path / "images"
    assert t.prompts_dir == tmp_path / "prompts"
    assert t.task_json_path == tmp_path / "task.json"
    assert t.thumbs_dir == tmp_path / "thumbs"


def test_load_latest_recovers_and_cancels(tmp_path: Path, monkeypatch):
    t = _mk_task(tmp_path)
    manager.set_status(manager.item(1), "captioned")
    with manager.lock:
        manager._task = None

    # 只认 outputs/*/task.json：把任务目录伪装成 outputs 下的旧布局目录
    fake_out = tmp_path / "outputs"
    (fake_out / "20260101-000000_x").mkdir(parents=True)
    (Path(t.dir) / ".studio" / "task.json").rename(
        fake_out / "20260101-000000_x" / "task.json")
    monkeypatch.setattr(config, "OUTPUT_ROOT", fake_out)

    assert manager.load_latest() is True
    cur = manager.current()
    assert cur.name == "t1"
    assert cur.items[0].status == "captioned"
    assert cur.cancel_flag is True        # 历史任务默认中止态，防止误续跑


def test_load_latest_when_no_outputs(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(config, "OUTPUT_ROOT", tmp_path / "none")
    with manager.lock:
        manager._task = None
    assert manager.load_latest() is False
