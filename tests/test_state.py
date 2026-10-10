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
    d = json.loads((Path(t.dir) / "task.json").read_text(encoding="utf-8"))
    assert d["items"][0]["status"] == "captioned"


def test_save_is_atomic_no_tmp_leftover(tmp_path: Path):
    t = _mk_task(tmp_path)
    manager.set_status(manager.item(1), "done")
    leftovers = list(Path(t.dir).glob("*.tmp"))
    assert leftovers == []


def test_load_latest_recovers_and_cancels(tmp_path: Path, monkeypatch):
    t = _mk_task(tmp_path)
    manager.set_status(manager.item(1), "captioned")
    with manager.lock:
        manager._task = None

    # 只认 outputs/*/task.json：把任务目录伪装成 outputs 下的最新目录
    fake_out = tmp_path / "outputs"
    (fake_out / "20260101-000000_x").mkdir(parents=True)
    (Path(t.dir) / "task.json").rename(fake_out / "20260101-000000_x" / "task.json")
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
