"""任务与图片条目的状态机（v1：单活动任务），含 task.json 持久化。"""
from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field, asdict, fields as dc_fields
from pathlib import Path

from . import config

# 条目状态流转：pending → captioning → captioned → generating → done
#                                    ↘ caption_failed  ↘ generate_failed
STATES = {"pending", "captioning", "captioned", "generating", "done",
          "caption_failed", "generate_failed"}


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
    out_image: str = ""           # 生成图相对 images/ 的文件名
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

    def summary(self) -> dict:
        c = {"pending": 0, "captioned": 0, "done": 0, "failed": 0}
        for it in self.items:
            if it.status in ("done",):
                c["done"] += 1
            elif it.status in ("captioned", "generating"):
                c["captioned"] += 1
            elif it.status.endswith("_failed"):
                c["failed"] += 1
            else:
                c["pending"] += 1
        return {"id": self.id, "name": self.name, "phase": self.phase,
                "source": self.source_desc, "total": len(self.items), "counts": c,
                "images_dir": str(self.images_dir), "prompts_dir": str(self.prompts_dir)}


class TaskManager:
    """单活动任务管理。Worker 线程与 API 线程共用，读写均加锁。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._task: Task | None = None
        self._worker_busy = threading.Semaphore(1)

    # ---------- 访问 ----------
    @property
    def lock(self) -> threading.RLock:
        return self._lock

    @property
    def worker_busy(self) -> threading.Semaphore:
        return self._worker_busy

    def current(self) -> Task | None:
        with self._lock:
            return self._task

    def create(self, name: str, source_desc: str, source_root: Path, params: dict,
               items: list[Item], task_dir: Path,
               output_image_dir: str = "", output_prompt_dir: str = "") -> Task:
        with self._lock:
            self._task = Task(
                id=task_dir.name, name=name, source_desc=source_desc,
                source_root=str(source_root), params=params, items=items,
                dir=str(task_dir),
                output_image_dir=output_image_dir,
                output_prompt_dir=output_prompt_dir,
            )
            self.save()
            return self._task

    def item(self, seq: int) -> Item | None:
        with self._lock:
            for it in self._task.items if self._task else []:
                if it.seq == seq:
                    return it
        return None

    def set_status(self, item: Item, status: str, error: str = "") -> None:
        assert status in STATES, status
        with self._lock:
            item.status = status
            item.error = error
        self.save()

    # ---------- 持久化 ----------
    def save(self) -> None:
        t = self._task
        if not t:
            return
        d = asdict(t)
        path = t.task_json_path
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(path)

    def load_latest(self) -> bool:
        """启动时回看 outputs/ 下最新的 task.json（断点续跑入口）。
        同时认新旧两种布局：v11 在 <任务>/.studio/task.json，v10 在 <任务>/task.json。"""
        if not config.OUTPUT_ROOT.exists():
            return False
        candidates = list(config.OUTPUT_ROOT.glob("*/.studio/task.json")) + \
            list(config.OUTPUT_ROOT.glob("*/task.json"))
        candidates.sort(key=lambda p: p.parent.name if p.parent.name != ".studio"
                        else p.parent.parent.name, reverse=True)
        for path in candidates:
            try:
                t = Task.from_dict(json.loads(path.read_text(encoding="utf-8")))
                t.cancel_flag = True   # 历史任务默认视为已中止，防止误续跑
                with self._lock:
                    self._task = t
                return True
            except Exception:
                continue
        return False


manager = TaskManager()
