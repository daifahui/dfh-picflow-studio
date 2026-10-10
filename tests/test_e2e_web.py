"""前端网页测试（Playwright，真实 Edge/Chromium 无头浏览器）。

后端真实运行但模型调用全部被桩掉；SSE 由 page.route 注入静态事件流，
覆盖"事件驱动 UI"这条纯 mock 无法覆盖的路径。不触碰任何模型服务。
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

SSE_EVENTS = "".join([
    "data: " + json.dumps({"event": "phase", "phase": "generating"}) + "\n\n",
    "data: " + json.dumps({"event": "progress", "phase": "generating",
                           "done": 2, "total": 4, "filename": "0002.png"}) + "\n\n",
])


@pytest.fixture(scope="module")
def server(tmp_path_factory):
    """真后端 + 模型桩。桩必须先于服务器可接受请求就位——E2E 里 UI 点击
    会触发 worker，任何未桩的模型触点都会真的拉起 llama-server/ComfyUI。"""
    from server import comfy_client, config, llm_caption
    from tests.conftest import make_png
    mp = pytest.MonkeyPatch()
    mp.setattr(config, "OUTPUT_ROOT", tmp_path_factory.mktemp("e2e-outputs"))
    mp.setattr(llm_caption, "ensure_llm", lambda: None)
    mp.setattr(llm_caption, "stop_llm", lambda: None)
    mp.setattr(llm_caption, "caption_image",
               lambda path, note="": {"rewritten_prompt": "a test prompt, anime style",
                                      "wh_ratio": "1:1"})
    mp.setattr(comfy_client, "ensure_comfy", lambda: None)
    mp.setattr(comfy_client, "stop_comfy", lambda: None)
    mp.setattr(comfy_client, "generate_one",
               lambda contract, params, seed, prefix: make_png(32, 32))

    config = uvicorn.Config(main.app, host="127.0.0.1", port=PORT, log_level="warning")
    srv = uvicorn.Server(config)
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
    with manager.lock:
        manager._task = None


def mock_sse(page, events=SSE_EVENTS):
    """仅在纯 UI 测试使用；集成测试走真实 SSE（后端已桩掉模型）。"""
    page.route("**/api/task/events", lambda route: route.fulfill(
        status=200, content_type="text/event-stream", body="retry: 600000\n\n" + events))


def test_home_renders_tabs_and_health(page):
    mock_sse(page)
    page.goto(BASE)
    assert "Prompt Studio" in page.title()
    assert page.locator("#tabs button").count() == 3
    page.wait_for_function(
        "document.querySelector('#health').textContent.includes('未启动')",
        timeout=10000)


def test_params_form_filled_with_defaults(page):
    mock_sse(page)
    page.goto(BASE)
    expect = page.locator('input[data-p="steps"]')
    expect.wait_for(state="attached")
    assert expect.input_value() == "32"
    assert page.locator('input[data-p="seed"]').input_value() != ""


def test_view_switching(page):
    mock_sse(page)
    page.goto(BASE)
    page.click('button[data-view="review"]')
    assert page.locator("#view-review").is_visible()
    assert not page.locator("#view-task").is_visible()
    page.click('button[data-view="task"]')
    assert page.locator("#view-task").is_visible()


def test_sse_events_drive_progress_bar(page):
    mock_sse(page)
    page.goto(BASE)
    page.click('button[data-view="review"]')
    page.wait_for_function(
        "document.querySelector('#progress-bar').style.width === '50%'",
        timeout=8000)
    assert "2/4" in page.locator("#progress-text").inner_text()
    assert "阶段二" in page.locator("#phase-label").inner_text()


def test_create_task_and_caption_via_real_backend(page, image_dir):
    """端到端 UI：填路径 → 点提取 → 真后端（桩模型）+ 真 SSE → 网格渲染已提取。"""
    page.goto(BASE)
    page.fill("#src-path", str(image_dir))
    page.click("#btn-caption")
    page.wait_for_selector(".cell", timeout=15000)
    page.wait_for_function(
        "document.querySelectorAll('.cell.captioned').length === 3", timeout=15000)
    assert page.locator("#item-count").inner_text().startswith("（3")


def test_detail_sidebar_edit_and_save(page, image_dir):
    page.goto(BASE)
    page.fill("#src-path", str(image_dir))
    page.click("#btn-caption")
    page.wait_for_selector(".cell.captioned", timeout=15000)

    page.click(".cell >> nth=0")
    assert page.locator("#detail").evaluate("el => el.classList.contains('open')")
    ta = page.locator("#detail-prompt")
    assert ta.input_value() == "a test prompt, anime style"
    ta.fill("edited from web test")
    page.click("#btn-save")

    page.click("#detail-close")
    page.wait_for_function(
        "!document.querySelector('#detail').classList.contains('open')")
    page.click(".cell >> nth=0")           # 重开侧栏验证已持久化
    page.wait_for_function(
        "document.querySelector('#detail-prompt').value === 'edited from web test'")


def test_generate_all_renders_result_view(page, image_dir):
    page.goto(BASE)
    page.fill("#src-path", str(image_dir))
    page.click("#btn-caption")
    page.wait_for_selector(".cell.captioned", timeout=15000)

    page.once("dialog", lambda d: d.accept())
    page.click("#btn-generate-all")
    page.wait_for_function(
        "document.querySelector('#phase-label').textContent.includes('完成')",
        timeout=20000)
    page.click('button[data-view="result"]')
    page.wait_for_selector("#result-grid .cell", timeout=5000)
    assert page.locator("#result-grid .cell").count() == 3


def test_view_mode_switcher_and_persistence(page, image_dir):
    """§16.3：网格视图切换（大/中/小图标、列表、详细信息），选择存 localStorage。"""
    page.goto(BASE)
    page.fill("#src-path", str(image_dir))
    page.click("#btn-caption")
    page.wait_for_selector(".cell", timeout=15000)

    page.select_option("#view-mode", "details")
    page.wait_for_function(
        "document.querySelector('#grid').classList.contains('view-details')")
    page.wait_for_function(
        "document.querySelector('#result-grid').classList.contains('view-details')")
    page.reload()
    page.wait_for_function(
        "document.querySelector('#grid').classList.contains('view-details')",
        timeout=8000)                                  # 刷新后记忆保持


def test_lightbox_opens_and_closes(page, image_dir):
    """§16.3：详情侧栏大图点击 → 全屏 lightbox，Esc 关闭。"""
    page.goto(BASE)
    page.fill("#src-path", str(image_dir))
    page.click("#btn-caption")
    page.wait_for_selector(".cell.captioned", timeout=15000)
    page.click(".cell >> nth=0")
    page.click("#detail-src")
    page.wait_for_function(
        "document.querySelector('#lightbox').classList.contains('open')")
    page.keyboard.press("Escape")
    page.wait_for_function(
        "!document.querySelector('#lightbox').classList.contains('open')")
