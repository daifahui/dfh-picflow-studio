"""前端网页测试（Playwright，真实 Edge 无头浏览器，Alpine 版三视图）。

后端真实运行但模型调用全部被桩掉；部分纯 UI 用例由 page.route 注入静态
SSE 事件流。不触碰任何模型服务。
"""
from __future__ import annotations

import json
import threading
import time

import pytest
import uvicorn
from playwright.sync_api import sync_playwright

from server import main

PORT = 9189
BASE = f"http://127.0.0.1:{PORT}"


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    """真后端 + 模型桩。桩必须先于服务器可接受请求就位——E2E 里 UI 点击
    会触发 worker，任何未桩的模型触点都会真的拉起 llama-server/ComfyUI。"""
    from server import comfy_client, config, llm_caption
    from server.state import manager
    from tests.conftest import make_png
    mp = pytest.MonkeyPatch()
    ws_root = tmp_path_factory.mktemp("e2e-ws")
    mp.setattr(config, "OUTPUT_ROOT", ws_root)
    mp.setattr(config, "WORKSPACE_POINTER",
               tmp_path_factory.mktemp("e2e-cfg") / "ws-pointer.json")
    with manager.lock:                    # manager 是模块级单例，workspace 必须同步隔离
        manager.workspace = ws_root
        manager._tasks.clear()
        manager._queue.clear()
    mp.setattr(llm_caption, "ensure_llm", lambda: None)
    mp.setattr(llm_caption, "stop_llm", lambda: None)
    mp.setattr(llm_caption, "caption_image",
               lambda path, note="": {"rewritten_prompt": "a test prompt, anime style",
                                      "wh_ratio": "1:1", "tags": ["test", "anime"]})
    mp.setattr(llm_caption, "generate_task_name", lambda contract: "AI 名称")
    mp.setattr(comfy_client, "ensure_comfy", lambda: None)
    mp.setattr(comfy_client, "stop_comfy", lambda: None)
    mp.setattr(comfy_client, "generate_one",
               lambda contract, params, seed, prefix: make_png(32, 32))

    cfg = uvicorn.Config(main.app, host="127.0.0.1", port=PORT, log_level="warning")
    srv = uvicorn.Server(cfg)
    threading.Thread(target=srv.run, daemon=True).start()
    deadline = time.time() + 15
    import httpx
    while time.time() < deadline:
        try:
            if httpx.get(f"{BASE}/api/defaults", timeout=2).status_code == 200:
                break
        except Exception:
            time.sleep(0.2)
    yield srv
    srv.should_exit = True
    mp.undo()


@pytest.fixture(scope="module")
def pw():
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="msedge", headless=True)
        yield browser
        browser.close()


@pytest.fixture()
def page(pw, server):
    ctx = pw.new_context()
    pg = ctx.new_page()
    yield pg
    ctx.close()
    from server.state import manager          # 服务器与测试同进程，直接重置任务态
    manager.pause_flag = True                 # 先放掉可能卡在审阅闸的 worker
    manager.resume_event.set()
    time.sleep(0.3)
    with manager.lock:
        manager._tasks.clear()
        manager._queue.clear()
        manager.running = False
        manager.gate = False
        manager.pause_flag = False
        manager.resume_event.clear()


def import_and_start(page, image_dir, mode="auto", start=True, name="t"):
    page.goto(BASE)
    page.fill("#import-path", str(image_dir))
    if name:
        page.fill("#import-name", name)
    page.select_option("#import-mode", mode)
    page.click("#btn-import-start" if start else "#btn-import")


def mock_sse(page, events):
    """仅在纯 UI 测试使用；集成测试走真实 SSE（后端已桩掉模型）。"""
    page.route("**/api/task/events", lambda route: route.fulfill(
        status=200, content_type="text/event-stream",
        body="retry: 600000\n\n" + events))


def sse_payload(*events) -> str:
    return "".join(f"data: {json.dumps(e, ensure_ascii=False)}\n\n" for e in events)


def test_home_renders_tabs_and_health(page):
    page.goto(BASE)
    assert "Prompt Studio" in page.title()
    page.wait_for_function(
        "document.querySelectorAll('#tabs button').length === 3", timeout=10000)
    page.wait_for_function(
        "document.querySelector('#health').textContent.includes('未启动')",
        timeout=10000)


def test_import_and_auto_queue_run(page, image_dir):
    """端到端 UI：导入 → 开始队列 →（桩模型）→ 完成结果渲染。"""
    import_and_start(page, image_dir, name="auto-run")
    page.wait_for_selector("#queue-table tbody tr", timeout=10000)
    page.wait_for_function(
        "document.querySelector('#queue-table tbody').textContent.includes('完成')",
        timeout=20000)

    page.click('button:has-text("③ 结果")')
    page.wait_for_selector("#result-grid .cell", timeout=10000)
    assert page.locator("#result-grid .cell").count() == 3


def test_import_review_then_gate_resume(page, image_dir):
    """停在审阅模式：队列到审阅闸 → 进审阅页改词 → 放行 → 完成。"""
    import_and_start(page, image_dir, mode="review", name="gate-run")
    page.wait_for_function(
        "document.querySelector('#queue-table tbody').textContent.includes('待审阅')",
        timeout=20000)
    page.wait_for_function(
        "document.querySelector('.gate-hint') && "
        "getComputedStyle(document.querySelector('.gate-hint')).display !== 'none'",
        timeout=10000)

    page.click("#queue-table tbody tr")                # 进审阅页
    page.wait_for_selector(".cell", timeout=10000)
    page.click(".cell >> nth=0")
    page.wait_for_function(
        "document.querySelector('#detail').classList.contains('open')")
    page.locator("#detail-prompt").fill("edited from web test")
    page.click("#btn-save")
    page.wait_for_timeout(300)
    page.click("#detail-close")

    page.click("#btn-resume")                          # 放行
    page.wait_for_function(
        "document.querySelector('#queue-table tbody').textContent.includes('完成')",
        timeout=20000)


def test_detail_sidebar_edit_persists(page, image_dir):
    import_and_start(page, image_dir, mode="review", name="edit-run")
    page.wait_for_selector("#queue-table tbody tr", timeout=10000)
    page.wait_for_function(
        "document.querySelector('#queue-table tbody').textContent.includes('待审阅')",
        timeout=20000)
    page.click("#queue-table tbody tr")
    page.wait_for_selector(".cell.captioned", timeout=10000)

    page.click(".cell >> nth=0")
    ta = page.locator("#detail-prompt")
    page.wait_for_function(
        "document.querySelector('#detail-prompt').value !== ''")
    page.wait_for_function(
        "document.querySelector('#detail-tags').value === 'test, anime'")
    ta.fill("edited from web test")
    page.click("#btn-save")
    page.click("#detail-close")
    page.wait_for_timeout(200)
    page.click(".cell >> nth=0")                       # 重开侧栏验证已持久化
    page.wait_for_function(
        "document.querySelector('#detail-prompt').value === 'edited from web test'")


def test_view_mode_switcher_and_persistence(page, image_dir):
    import_and_start(page, image_dir, mode="review", name="view-run")
    page.wait_for_selector("#queue-table tbody tr", timeout=10000)
    page.click("#queue-table tbody tr")
    page.wait_for_selector(".cell", timeout=10000)

    page.select_option("#view-mode", "details")
    page.wait_for_function(
        "document.querySelector('#grid').classList.contains('view-details')")
    page.reload()
    page.wait_for_function(
        "document.querySelector('#grid').classList.contains('view-details')",
        timeout=8000)                                  # 刷新后记忆保持


def test_lightbox_opens_and_closes(page, image_dir):
    import_and_start(page, image_dir, mode="review", name="lb-run")
    page.wait_for_selector("#queue-table tbody tr", timeout=10000)
    page.click("#queue-table tbody tr")
    try:
        page.wait_for_selector(".cell.captioned", timeout=10000)
    except Exception:
        print("\n[dbg] queue rows:", page.locator("#queue-table tbody tr").count())
        print("[dbg] queue text:", page.inner_text("#queue-table")[:300])
        print("[dbg] view-review display:",
              page.evaluate(
                  "getComputedStyle(document.querySelector('#view-review')).display"))
        print("[dbg] grid html:", page.inner_html("#grid")[:400])
        print("[dbg] sel:", page.evaluate(
            "document.querySelector('body')._x_dataStack?.[0]?.sel"))
        raise
    page.click(".cell >> nth=0")
    page.click("#detail-src")
    page.wait_for_function(
        "document.querySelector('#lightbox').classList.contains('open')")
    page.keyboard.press("Escape")
    page.wait_for_function(
        "!document.querySelector('#lightbox').classList.contains('open')")


def test_sse_events_drive_queue_progress(page):
    """纯 UI：注入静态 SSE 流，队列表与进度文本按事件渲染。"""
    events = sse_payload(
        {"event": "queue", "workspace": "W", "running": True, "gate": False,
         "paused": False, "queue": [
             {"id": "T1", "name": "注入任务", "phase": "generating",
              "mode": "auto", "source": "S", "total": 4,
              "counts": {"pending": 0, "captioned": 2, "done": 2, "failed": 0},
              "images_dir": "W/T1", "prompts_dir": "W/T1/prompts"}]},
        {"event": "progress", "task_id": "T1", "task_name": "注入任务",
         "phase": "generating", "seq": 3, "done": 2, "total": 4,
         "filename": "0003.png"},
    )
    mock_sse(page, events)
    page.goto(BASE)
    page.wait_for_function(
        "[...document.querySelectorAll('#queue-table .name-edit')]"
        ".some(i => i.value.includes('注入任务'))",
        timeout=8000)
    page.wait_for_function(
        "document.querySelector('.cell-current') && "
        "document.querySelector('.cell-current').textContent.includes('2/4')",
        timeout=8000)
    page.click("#queue-table tbody tr")                # 打开审阅页看进度条
    page.wait_for_function(
        "document.querySelector('#progress-text').textContent.includes('2/4')",
        timeout=8000)
