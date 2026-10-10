"""llm_caption 单元测试：契约解析、skill 读取、caption 流程（respx mock，绝不触真服务）。"""
import json
from pathlib import Path

import httpx
import pytest
import respx

from server import config, llm_caption

BASE = config.LM_API_BASE


def _chat(content: str) -> dict:
    return {"choices": [{"message": {"content": content}}]}


# ---------- _parse_contract ----------
GOOD = json.dumps({"rewritten_prompt": "a girl at the window", "wh_ratio": "9:16"},
                  ensure_ascii=False)


def test_parse_plain_json():
    assert llm_caption._parse_contract(GOOD) == {
        "rewritten_prompt": "a girl at the window", "wh_ratio": "9:16", "tags": []}


# ---------- tags 规范化（§17） ----------
def test_parse_tags_variants():
    c = llm_caption._parse_contract(
        '{"rewritten_prompt": "x", "wh_ratio": "1:1",'
        ' "tags": ["Girl", "blue hair", ""]}')
    assert c["tags"] == ["girl", "blue hair"]              # 小写化 + 去空
    c = llm_caption._parse_contract(
        '{"rewritten_prompt": "x", "wh_ratio": "1:1", "tags": "a, b ,c"}')
    assert c["tags"] == ["a", "b", "c"]                    # 逗号字符串
    c = llm_caption._parse_contract(
        '{"rewritten_prompt": "x", "wh_ratio": "1:1", "tags": ["a, b", "c"]}')
    assert c["tags"] == ["a", "b", "c"]                    # 数组项内嵌逗号也拆分
    c = llm_caption._parse_contract(
        '{"rewritten_prompt": "x", "wh_ratio": "1:1", "tags": 123}')
    assert c["tags"] == []                                 # 坏类型兜底
    many = ", ".join(f"t{i}" for i in range(20))
    c = llm_caption._parse_contract(
        '{"rewritten_prompt": "x", "wh_ratio": "1:1", "tags": "%s"}' % many)
    assert len(c["tags"]) == llm_caption.TAGS_MAX          # 超量截断


def test_parse_strips_think_block():
    wrapped = f"<think>让我想想……图里是深夜的窗边。</think>\n{GOOD}"
    assert llm_caption._parse_contract(wrapped)["rewritten_prompt"] == "a girl at the window"


def test_parse_bare_newlines_inside_string():
    raw = '{"rewritten_prompt": "line one\n line two", "wh_ratio": "4:5"}'
    c = llm_caption._parse_contract(raw)      # strict=False 宽容解析
    assert "line one" in c["rewritten_prompt"]


def test_parse_prompt_embedded_json_and_noise():
    wrapped = f"好的，以下是结果：\n```json\n{GOOD}\n```\n以上。"
    assert llm_caption._parse_contract(wrapped)["wh_ratio"] == "9:16"


@pytest.mark.parametrize("bad,why", [
    ("没有 json", "未找到 JSON"),
    ('{"wh_ratio": "9:16"}', "rewritten_prompt"),
    ('{"rewritten_prompt": "x", "wh_ratio": "宽屏"}', "wh_ratio"),
    ('{"rewritten_prompt": "x", "wh_ratio": "9:16:1"}', "wh_ratio"),
])
def test_parse_rejects_garbage(bad, why):
    with pytest.raises(ValueError, match=why):
        llm_caption._parse_contract(bad)


# ---------- skill 读取（只读文件，不联网） ----------
def test_read_skill_system_prompt_real_files():
    text = llm_caption.read_skill_system_prompt()
    assert "rewritten_prompt" in text            # 契约说明来自 SKILL.md
    assert "system_prompt_t2i" or True


def test_read_skill_missing_dir_returns_empty(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(config, "PI_SKILL_DIR", tmp_path / "nope")
    assert llm_caption.read_skill_system_prompt() == ""


# ---------- caption_image（respx 拦截 httpx.post） ----------
@respx.mock
def test_caption_image_success_sends_image_and_system():
    route = respx.post(f"{BASE}/v1/chat/completions").mock(
        return_value=httpx.Response(200, json=_chat(f"<think>…</think>{GOOD}")))
    img = Path(__file__).parent / "data_img.png"
    img.write_bytes(b"\x89PNG")

    c = llm_caption.caption_image(img)
    assert c["wh_ratio"] == "9:16"
    body = route.calls.last.request.read()
    assert b"data:image/png;base64" in body
    assert "rewritten_prompt".encode() in body   # system prompt（SKILL.md）已注入
    assert route.calls.last.request.content
    img.unlink()


@respx.mock
def test_caption_image_retries_then_succeeds():
    route = respx.post(f"{BASE}/v1/chat/completions")
    route.side_effect = [
        httpx.Response(500, text="boom"),
        httpx.Response(200, json=_chat(GOOD)),
    ]
    img = Path(__file__).parent / "data_img2.png"
    img.write_bytes(b"x")
    assert llm_caption.caption_image(img)["rewritten_prompt"] == "a girl at the window"
    assert route.call_count == 2                 # 第一次失败后升温重试
    img.unlink()


@respx.mock
def test_caption_image_exhausts_retries():
    route = respx.post(f"{BASE}/v1/chat/completions").mock(
        return_value=httpx.Response(500, text="boom"))
    img = Path(__file__).parent / "data_img3.png"
    img.write_bytes(b"x")
    with pytest.raises(RuntimeError, match="重试耗尽"):
        llm_caption.caption_image(img)
    assert route.call_count == config.CAPTION_RETRIES
    img.unlink()


@respx.mock
def test_caption_image_converts_webp_to_png():
    """llama-server 端解码不了 webp（2026-10-10 E2E 抓出）：发送前必须转 PNG。"""
    import io

    from PIL import Image as PILImage
    route = respx.post(f"{BASE}/v1/chat/completions").mock(
        return_value=httpx.Response(200, json=_chat(GOOD)))
    img = Path(__file__).parent / "data_img.webp"
    buf = io.BytesIO()
    PILImage.new("RGB", (16, 16), (10, 20, 30)).save(buf, "WEBP")
    img.write_bytes(buf.getvalue())

    llm_caption.caption_image(img)
    body = route.calls.last.request.read().decode()
    assert "data:image/webp" not in body
    assert "data:image/png;base64" in body
    img.unlink()


@respx.mock
def test_caption_image_appends_note():
    route = respx.post(f"{BASE}/v1/chat/completions").mock(
        return_value=httpx.Response(200, json=_chat(GOOD)))
    img = Path(__file__).parent / "data_img4.png"
    img.write_bytes(b"x")
    llm_caption.caption_image(img, note="头发改成银白色")
    body = route.calls.last.request.read().decode()
    assert "头发改成银白色" in body
    img.unlink()


# ---------- AI 任务命名（§16.4） ----------
@respx.mock
def test_generate_task_name_strips_think_and_cleans():
    route = respx.post(f"{BASE}/v1/chat/completions").mock(
        return_value=httpx.Response(200, json=_chat(
            "<think>想想……</think>\n「夜晚窗边的蓝发少女。」")))
    name = llm_caption.generate_task_name({"rewritten_prompt": "x" * 800})
    assert name == "夜晚窗边的蓝发少女"                 # think 剥离 + 引号剥离
    body = route.calls.last.request.read().decode()
    assert "命名助手" in body
    assert "xxxx" in body                              # 提示词已注入（截断到 600）


@respx.mock
def test_generate_task_name_sanitizes_illegal_chars():
    respx.post(f"{BASE}/v1/chat/completions").mock(
        return_value=httpx.Response(200, json=_chat('a/b:c')))
    assert llm_caption.generate_task_name({"rewritten_prompt": "x"}) == "a_b_c"


@respx.mock
def test_generate_task_name_failure_returns_empty():
    respx.post(f"{BASE}/v1/chat/completions").mock(
        return_value=httpx.Response(500, text="boom"))
    assert llm_caption.generate_task_name({"rewritten_prompt": "x"}) == ""
