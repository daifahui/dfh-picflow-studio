"""state 单元测试：WorkspaceManager 注册/重命名/持久化/启动加载。"""
import json
from pathlib import Path

import pytest

from server import config
from server.state import Item, Task, manager


@pytest.fixture()
def ws(tmp_path: Path, monkeypatch) -> Path:
    wsp = tmp_path / "ws"
    wsp.mkdir()
    monkeypatch.setattr(config, "OUTPUT_ROOT", wsp)
    monkeypatch.setattr(config, "WORKSPACE_POINTER", tmp_path / "ptr.json")
    with manager.lock:
        manager._tasks.clear()
        manager._queue.clear()
        manager.workspace = wsp
    yield wsp
    with manager.lock:
        manager._tasks.clear()
        manager._queue.clear()


def _mk_task(ws: Path, name: str = "t1") -> Task:
    items = [Item(seq=1, src_filename="0001_a.png", src_path=str(ws / "a.png")),
             Item(seq=2, src_filename="0002_b.png", src_path=str(ws / "b.png"))]
    return manager.create_task(name, "src", ws, dict(config.DEFAULT_PARAMS),
                               items, ws / name)


def test_create_and_lookup(ws: Path):
    t = _mk_task(ws)
    assert manager.task("t1") is t
    assert manager.item(t, 2).src_filename == "0002_b.png"
    assert manager.item(t, 99) is None
    assert manager.all_tasks() == [t]
    assert t.summary()["total"] == 2
    assert t.counts()["pending"] == 2


def test_set_status_persists_task_json(ws: Path):
    t = _mk_task(ws)
    manager.set_status(t, manager.item(t, 1), "captioned")
    d = json.loads((ws / "t1" / ".studio" / "task.json").read_text(encoding="utf-8"))
    assert d["items"][0]["status"] == "captioned"


def test_save_is_atomic_no_tmp_leftover(ws: Path):
    t = _mk_task(ws)
    manager.set_status(t, manager.item(t, 1), "done")
    assert list((ws / "t1" / ".studio").glob("*.tmp")) == []


def test_workspace_json_roundtrip(ws: Path):
    _mk_task(ws, "a")
    _mk_task(ws, "b")
    d = json.loads((ws / "workspace.json").read_text(encoding="utf-8"))
    assert d["queue"] == ["a", "b"]
    with manager.lock:
        manager._tasks.clear()
        manager._queue.clear()
    assert manager.load() is True
    assert [t.id for t in manager.all_tasks()] == ["a", "b"]


def test_remove_task_only_idle(ws: Path):
    t = _mk_task(ws, "gone")
    assert manager.remove_task("gone") is True
    assert manager.task("gone") is None
    t2 = _mk_task(ws, "busy")
    t2.phase = "generating"
    assert manager.remove_task("busy") is False


def test_rename_remaps_internal_paths_and_flags(ws: Path):
    t = _mk_task(ws, "old")
    t.source_root = str(ws / "old" / ".studio" / "source")
    t.items[0].src_path = str(ws / "old" / ".studio" / "source" / "0001_a.png")
    assert manager.rename_task(t, "new", by_user=True) is True
    assert t.id == "new" and t.name == "new" and t.renamed is True
    assert t.dir == str(ws / "new")
    assert t.source_root == str(ws / "new" / ".studio" / "source")
    assert t.items[0].src_path == \
        str(ws / "new" / ".studio" / "source" / "0001_a.png")
    assert manager.task("new") is t and manager.task("old") is None
    assert (ws / "new" / ".studio" / "task.json").exists()


def test_rename_sanitizes_and_rejects_same(ws: Path):
    t = _mk_task(ws, "x")
    assert manager.rename_task(t, 'a/b:c?', by_user=True) is True
    assert t.name == "a_b_c_"
    assert manager.rename_task(t, "a_b_c_", by_user=True) is False


def test_load_scans_legacy_dirs_and_persists_index(ws: Path):
    legacy = ws / "2026-old"
    legacy.mkdir()
    (legacy / "task.json").write_text(json.dumps({
        "id": "2026-old", "name": "2026-old", "source_desc": "s",
        "source_root": str(ws), "params": {}, "created_at": 0.0,
        "phase": "done", "dir": str(legacy), "items": []}), encoding="utf-8")
    assert manager.load() is True                    # v10 旧目录被补登记
    assert manager.task("2026-old").layout == "v10"
    assert (ws / "workspace.json").exists()          # 并落索引
    assert manager.task("2026-old").cancel_flag is True


def test_load_empty_workspace(ws: Path):
    assert manager.load() is False


def test_switch_workspace_overrides_stale_pointer(ws: Path):
    """回归（2026-10-11 踩坑）：指针文件残留旧值时，切换必须以新路径为准，
    不能被 load() 按旧指针覆盖回去。"""
    import json as _json
    config.WORKSPACE_POINTER.write_text(
        _json.dumps({"workspace": str(ws)}), encoding="utf-8")   # 旧指针指向当前 ws
    other = ws.parent / "ws2"
    other.mkdir()
    manager.switch_workspace(other)
    assert manager.workspace == other                     # 新路径生效
    manager.load()                                        # load 按新指针读取，不回跳
    assert manager.workspace == other


# ---------- 路径解析（M6 回归） ----------
def test_v11_default_paths(ws: Path):
    t = _mk_task(ws)
    assert t.images_dir == Path(t.dir)
    assert t.prompts_dir == Path(t.dir) / "prompts"
    assert t.task_json_path == Path(t.dir) / ".studio" / "task.json"
    assert t.thumbs_dir == Path(t.dir) / ".studio" / "thumbs"
    t.output_image_dir = r"D:\out"
    assert t.prompts_dir == Path(r"D:\out") / "prompts"
    t.output_prompt_dir = r"E:\words"
    assert t.prompts_dir == Path(r"E:\words")


def test_from_dict_legacy_layout(tmp_path: Path):
    d = {"id": "x", "name": "x", "source_desc": "s", "source_root": str(tmp_path),
         "params": {}, "created_at": 0.0, "phase": "done", "dir": str(tmp_path),
         "items": []}
    t = Task.from_dict(d)
    assert t.layout == "v10"
    assert t.images_dir == tmp_path / "images"
    assert t.prompts_dir == tmp_path / "prompts"
    assert t.task_json_path == tmp_path / "task.json"


def test_all_tasks_sorted_by_created(ws: Path):
    """§19 R6：任务按创建时间早→晚返回（重命名/乱序导入不再打乱显示顺序）。"""
    import time as _t
    a = _mk_task(ws, "bbb")
    _t.sleep(0.02)
    c = _mk_task(ws, "aaa")
    assert manager.all_tasks() == [a, c]                 # 非字母序，按创建时间
    assert a.summary()["created"] and ":" in a.summary()["created"]


def test_summary_has_created_ts(ws: Path):
    """§20 R5：summary 携带 epoch 时间戳，供前端跨天稳定排序。"""
    import time as _t
    before = _t.time()
    a = _mk_task(ws, "a")
    s = a.summary()
    assert isinstance(s["created_ts"], float)
    assert before - 1 <= s["created_ts"] <= _t.time() + 1
    assert s["created_ts"] == a.created_at
