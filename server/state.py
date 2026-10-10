"""工作区与任务状态机：多任务注册表 + 队列 + 持久化（workspace.json / task.json）。"""
from __future__ import annotations

import json
import re
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field, asdict, fields as dc_fields
from pathlib import Path

from . import config

# 条目状态流转：pending → captioning → captioned → generating → done
#                                    ↘ caption_failed  ↘ generate_failed
STATES = {"pending", "captioning", "captioned", "generating", "done",
          "caption_failed", "generate_failed"}

# 任务 phase：idle → captioning → reviewing → generating → done / stopped
# 任务 mode：auto=全自动（审阅不过闸） / review=停在审阅等放行

_ILLEGAL = re.compile(r'[\\/:*?"<>|]')


def sanitize_name(name: str) -> str:
    """目录名安全化：去路径非法字符与首尾空白。"""
    return _ILLEGAL.sub("_", str(name)).strip() or ""


@dataclass
class Item:
    seq: int                      # 1 起始的自然排序编号
    src_filename: str             # 0001_原文件名.jpg（source 目录内的规范名）
    src_path: str                 # 原图绝对路径
    status: str = "pending"
    contract: dict | None = None  # {"rewritten_prompt":…, "wh_ratio":"9:16"}
    note: str = ""                # 用户注释（recaption 用）
    error: str = ""
    prompt_id: str = ""           # ComfyUI prompt_id
    out_image: str = ""           # 生成图相对图片输出目录的文件名
    caption_ms: int = 0
    generate_ms: int = 0


@dataclass
class Task:
    id: str
    name: str
    source_desc: str              # 文件夹路径或压缩包文件名
    source_root: str              # 原图允许访问的根目录（图片服务安全边界）
    params: dict
    created_at: float = field(default_factory=time.time)
    phase: str = "idle"           # idle / captioning / reviewing / generating / done / stopped
    cancel_flag: bool = False
    items: list[Item] = field(default_factory=list)
    dir: str = ""                 # 任务工作目录绝对路径（内含 .studio/ 簿记）
    layout: str = "v11"           # v11=新布局（图在任务根）；v10=旧布局（只读兼容）
    output_image_dir: str = ""    # 图片输出目录（空 = 任务目录本身）
    output_prompt_dir: str = ""   # 提示词输出目录（空 = <图片输出>/prompts）
    mode: str = "auto"            # auto=全自动 / review=停在审阅
    ai_name: bool = True          # 首图契约产出后允许 AI 改名（用户手动改名后失效）
    renamed: bool = False         # 用户手动改过名（AI 不再覆盖）

    # ---- 路径解析（v11 双输出目录 / v10 旧布局兼容，见设计文档 §7/§16.2）----
    @property
    def images_dir(self) -> Path:
        if self.output_image_dir:
            return Path(self.output_image_dir)
        d = Path(self.dir)
        return d / "images" if self.layout == "v10" else d

    @property
    def prompts_dir(self) -> Path:
        if self.output_prompt_dir:
            return Path(self.output_prompt_dir)
        if self.layout == "v10":
            return Path(self.dir) / "prompts"
        return Path(self.output_image_dir or self.dir) / "prompts"

    @property
    def thumbs_dir(self) -> Path:
        d = Path(self.dir)
        return d / "thumbs" if self.layout == "v10" else d / ".studio" / "thumbs"

    @property
    def task_json_path(self) -> Path:
        d = Path(self.dir)
        return d / "task.json" if self.layout == "v10" else d / ".studio" / "task.json"

    @classmethod
    def from_dict(cls, d: dict) -> "Task":
        """容错反序列化：容忍未知字段（向前兼容），旧文件自动识别为 v10 布局。"""
        items = [Item(**i) for i in d.pop("items")]
        if "output_image_dir" not in d:
            d["layout"] = "v10"
        known = {f.name for f in dc_fields(cls)}
        return cls(items=items, **{k: v for k, v in d.items() if k in known})

    def counts(self) -> dict:
        c = {"pending": 0, "captioned": 0, "done": 0, "failed": 0}
        for it in self.items:
            if it.status == "done":
                c["done"] += 1
            elif it.status in ("captioned", "generating"):
                c["captioned"] += 1
            elif it.status.endswith("_failed"):
                c["failed"] += 1
            else:
                c["pending"] += 1
        return c

    def summary(self) -> dict:
        return {"id": self.id, "name": self.name, "phase": self.phase,
                "mode": self.mode, "source": self.source_desc,
                "total": len(self.items), "counts": self.counts(),
                "images_dir": str(self.images_dir),
                "prompts_dir": str(self.prompts_dir)}


class WorkspaceManager:
    """工作区：多任务注册表 + 队列顺序 + 单 worker 互斥（显存互斥 → 队列串行）。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._tasks: OrderedDict[str, Task] = OrderedDict()
        self._queue: list[str] = []            # 执行顺序（含已完成项，执行时跳过）
        self.workspace: Path = config.OUTPUT_ROOT
        self.running = False                   # worker 线程在跑
        self.gate = False                      # 审阅闸：等待放行
        self.pause_flag = False                # 当前张完成后暂停
        self.resume_event = threading.Event()  # 审阅闸放行 / 恢复运行
        self._worker_busy = threading.Semaphore(1)

    # ---------- 访问 ----------
    @property
    def lock(self) -> threading.RLock:
        return self._lock

    @property
    def worker_busy(self) -> threading.Semaphore:
        return self._worker_busy

    def task(self, tid: str) -> Task | None:
        with self._lock:
            return self._tasks.get(tid)

    def all_tasks(self) -> list[Task]:
        with self._lock:
            return [self._tasks[tid] for tid in self._queue if tid in self._tasks]

    def item(self, t: Task, seq: int) -> Item | None:
        for it in t.items:
            if it.seq == seq:
                return it
        return None

    def state(self) -> dict:
        """队列页聚合状态。"""
        with self._lock:
            return {"workspace": str(self.workspace),
                    "running": self.running, "gate": self.gate,
                    "paused": self.pause_flag,
                    "queue": [t.summary() for t in self.all_tasks()]}

    # ---------- 任务注册 ----------
    def create_task(self, name: str, source_desc: str, source_root: Path,
                    params: dict, items: list[Item], task_dir: Path,
                    output_image_dir: str = "", output_prompt_dir: str = "",
                    mode: str = "auto", ai_name: bool = True) -> Task:
        with self._lock:
            t = Task(
                id=task_dir.name, name=name, source_desc=source_desc,
                source_root=str(source_root), params=params, items=items,
                dir=str(task_dir),
                output_image_dir=output_image_dir,
                output_prompt_dir=output_prompt_dir,
                mode=mode, ai_name=ai_name,
            )
            self._tasks[t.id] = t
            if t.id not in self._queue:
                self._queue.append(t.id)
            self.save_task(t)
            self.save()
            return t

    def item_op(self, tid: str, seq: int) -> tuple[Task, Item] | None:
        t = self.task(tid)
        if not t:
            return None
        it = self.item(t, seq)
        return (t, it) if it else None

    def set_status(self, t: Task, item: Item, status: str, error: str = "") -> None:
        assert status in STATES, status
        with self._lock:
            item.status = status
            item.error = error
        self.save_task(t)

    def remove_task(self, tid: str) -> bool:
        """仅未开始的任务可移出队列（目录保留在磁盘上）。"""
        with self._lock:
            t = self._tasks.get(tid)
            if not t or t.phase != "idle":
                return False
            self._tasks.pop(tid, None)
            if tid in self._queue:
                self._queue.remove(tid)
            self.save()
        return True

    # ---------- 重命名（含 AI 命名） ----------
    def rename_task(self, t: Task, new_name: str, by_user: bool) -> bool:
        new_name = sanitize_name(new_name)
        if not new_name or new_name == t.name:
            return False
        old_dir = Path(t.dir)
        new_dir = self._uniquify(self.workspace / new_name)
        try:
            old_dir.rename(new_dir)
        except OSError as e:
            from . import sse
            sse.hub.publish("log", text=f"[rename] 重命名失败（目录被占用？）：{e}")
            return False

        def remap(p: str) -> str:
            return new_dir.joinpath(*Path(p).parts[len(old_dir.parts):]).__str__() \
                if str(p).startswith(str(old_dir)) else p

        with self._lock:
            self._tasks.pop(t.id, None)
            t.id = new_dir.name
            t.name = new_dir.name
            t.dir = str(new_dir)
            t.source_root = remap(t.source_root)
            for it in t.items:
                it.src_path = remap(it.src_path)
            if by_user:
                t.renamed = True
            self._tasks[t.id] = t
            if t.id not in self._queue:
                self._queue.append(t.id)
            self.save_task(t)
            self.save()
        return True

    @staticmethod
    def _uniquify(base: Path) -> Path:
        if not base.exists():
            return base
        i = 2
        while (base.parent / f"{base.name}-{i}").exists():
            i += 1
        return base.parent / f"{base.name}-{i}"

    # ---------- 持久化 ----------
    def save_task(self, t: Task) -> None:
        d = asdict(t)
        path = t.task_json_path
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(path)

    def save(self) -> None:
        """workspace.json：队列顺序 + 任务索引（任务详情仍在各自 task.json）。"""
        self.workspace.mkdir(parents=True, exist_ok=True)
        data = {"queue": list(self._queue),
                "tasks": {tid: {"dir": t.dir, "name": t.name}
                          for tid, t in self._tasks.items()}}
        tmp = self.workspace / "workspace.json.tmp"
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(self.workspace / "workspace.json")

    def load(self) -> bool:
        """启动加载：指针文件 → 工作区 → workspace.json → 各 task.json；
        工作区内未登记的旧任务目录（v10/v11）自动补登记为只读回看项。"""
        pointer = config.WORKSPACE_POINTER
        if pointer.exists():
            try:
                p = Path(json.loads(pointer.read_text(encoding="utf-8"))["workspace"])
                if p.is_dir():
                    self.workspace = p
            except Exception:
                pass
        with self._lock:
            self._tasks.clear()
            self._queue.clear()
        ws_file = self.workspace / "workspace.json"
        if ws_file.exists():
            try:
                data = json.loads(ws_file.read_text(encoding="utf-8"))
                for tid, meta in data.get("tasks", {}).items():
                    tj = Path(meta["dir"]) / ".studio" / "task.json"
                    if not tj.exists():
                        tj = Path(meta["dir"]) / "task.json"
                    try:
                        t = Task.from_dict(json.loads(tj.read_text(encoding="utf-8")))
                        t.cancel_flag = True       # 启动载入的任务一律先视为中止
                        with self._lock:
                            self._tasks[t.id] = t
                    except Exception:
                        continue
                with self._lock:
                    for tid in data.get("queue", []):
                        if tid in self._tasks and tid not in self._queue:
                            self._queue.append(tid)
                    for tid in self._tasks:        # 索引里有但队列外遗漏的任务
                        if tid not in self._queue:
                            self._queue.append(tid)
            except Exception:
                pass
        # 旧任务目录补登记（历史版本创建、或 workspace.json 丢失）
        for tj in sorted(self.workspace.glob("*/.studio/task.json"), key=_dirkey) + \
                sorted(self.workspace.glob("*/task.json"), key=_dirkey):
            with self._lock:
                known = any(t.dir == str(tj.parent.parent if tj.parent.name == ".studio"
                                         else tj.parent) for t in self._tasks.values())
            if known:
                continue
            try:
                t = Task.from_dict(json.loads(tj.read_text(encoding="utf-8")))
                t.cancel_flag = True
                with self._lock:
                    self._tasks[t.id] = t
                    if t.id not in self._queue:
                        self._queue.append(t.id)
            except Exception:
                continue
        with self._lock:
            self.save()
            return bool(self._tasks)

    def switch_workspace(self, path: Path) -> None:
        """切换工作区（仅在 worker 空闲时由 API 调用）。"""
        with self._lock:
            self.workspace = path
            self.running = False
            self.gate = False
            self.pause_flag = False
        self.load()
        config.WORKSPACE_POINTER.write_text(
            json.dumps({"workspace": str(path)}, ensure_ascii=False), encoding="utf-8")


def _dirkey(p: Path) -> str:
    return p.parent.parent.name if p.parent.name == ".studio" else p.parent.name


manager = WorkspaceManager()
