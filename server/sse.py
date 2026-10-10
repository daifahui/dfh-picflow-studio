"""SSE 事件广播：worker 线程 publish，API 协程 subscribe。"""
from __future__ import annotations

import asyncio
import json
import threading


class Hub:
    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._subs: dict[int, asyncio.Queue] = {}
        self._next_id = 0
        self._lock = threading.Lock()

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        """在 FastAPI 启动事件里绑定主事件循环。"""
        self._loop = loop

    def subscribe(self) -> tuple[int, asyncio.Queue]:
        q: asyncio.Queue = asyncio.Queue(maxsize=512)
        with self._lock:
            self._next_id += 1
            sid = self._next_id
            self._subs[sid] = q
        return sid, q

    def unsubscribe(self, sid: int) -> None:
        with self._lock:
            self._subs.pop(sid, None)

    def publish(self, event: str, **data) -> None:
        """线程安全：从任意线程向所有订阅者投递。"""
        payload = {"event": event, **data}
        if self._loop is None:
            return
        with self._lock:
            queues = list(self._subs.values())
        for q in queues:
            def _put(q=q, payload=payload):
                if not q.full():
                    q.put_nowait(payload)
            self._loop.call_soon_threadsafe(_put)

    @staticmethod
    def encode(payload: dict) -> str:
        return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


hub = Hub()
