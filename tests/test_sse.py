"""sse.Hub 单元测试：订阅、线程安全投递、编码、无事件循环时的安全性。"""
import asyncio
import threading

from server.sse import Hub


def test_publish_before_bind_is_noop():
    hub = Hub()
    hub.publish("log", text="x")            # 不应抛异常


def test_subscribe_publish_roundtrip():
    hub = Hub()
    loop = asyncio.new_event_loop()
    try:
        sid, q = hub.subscribe()            # 队列不绑定 loop 线程也可用
        hub.bind_loop(loop)
        hub.publish("progress", done=1, total=2)
        loop.run_until_complete(asyncio.sleep(0))    # 步进循环执行 call_soon 回调
        payload = q.get_nowait()
        assert payload == {"event": "progress", "done": 1, "total": 2}
    finally:
        loop.close()


def test_unsubscribe_stops_delivery():
    hub = Hub()
    loop = asyncio.new_event_loop()
    try:
        sid, q = hub.subscribe()
        hub.bind_loop(loop)
        hub.unsubscribe(sid)
        hub.publish("log", text="gone")
        assert q.empty()
    finally:
        loop.close()


def test_publish_from_other_thread():
    hub = Hub()
    loop = asyncio.new_event_loop()
    try:
        sid, q = hub.subscribe()
        hub.bind_loop(loop)
        t = threading.Thread(target=hub.publish, kwargs={"event": "item", "seq": 7})
        t.start(); t.join()
        # call_soon_threadsafe 需要 loop 在跑：手动步进一次
        loop.call_soon_threadsafe(lambda: None)
        loop.run_until_complete(asyncio.sleep(0.05))
        assert q.get_nowait()["seq"] == 7
    finally:
        loop.close()


def test_encode_sse_format():
    text = Hub.encode({"event": "log", "text": "中文"})
    assert text.startswith("data: ") and text.endswith("\n\n")
    assert "中文" in text
