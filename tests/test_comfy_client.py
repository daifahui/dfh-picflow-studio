"""comfy_client 单元测试：wh_ratio 换算、API 图构建、generate_one 流程（respx mock）。"""
import json
from pathlib import Path

import httpx
import pytest
import respx

from server import comfy_client, config

BASE = config.COMFY_API_BASE


@pytest.mark.parametrize("ratio,expect", [
    ("9:16", (768, 1368)),
    ("4:5", (912, 1144)),
    ("1:1", (1024, 1024)),
    ("16:9", (1368, 768)),
    ("3:2", (1256, 840)),
])
def test_wh_ratio_to_px_matches_resolution_selector(ratio, expect):
    assert comfy_client.wh_ratio_to_px(ratio) == expect


def test_wh_ratio_to_px_respects_megapixels():
    w, h = comfy_client.wh_ratio_to_px("1:1", megapixels=4.0)
    assert (w, h) == (2048, 2048)


@pytest.mark.parametrize("bad", ["", "16", "a:b", "9:16:1", "0:0", "9 : "])
def test_wh_ratio_to_px_rejects_garbage(bad):
    with pytest.raises(ValueError):
        comfy_client.wh_ratio_to_px(bad)


def test_build_graph_structure_and_values():
    contract = {"rewritten_prompt": "PROMPT<keep>特殊:字符", "wh_ratio": "9:16"}
    params = dict(config.DEFAULT_PARAMS)
    g = comfy_client.build_graph(contract, params, "0001")
    assert set(g) == {"1", "2", "3", "4", "5", "6", "7", "8"}
    assert g["5"]["inputs"]["width"] == 768 and g["5"]["inputs"]["height"] == 1368
    assert g["4"]["inputs"]["prompt"] == "PROMPT<keep>特殊:字符"
    assert g["4"]["inputs"]["negative_prompt"] == ""
    assert g["6"]["inputs"]["seed"] == params["seed"]
    assert g["6"]["inputs"]["steps"] == 32 and g["6"]["inputs"]["cfg"] == 1.0
    assert g["8"]["inputs"]["filename_prefix"] == "0001"
    # 连线完整：KSampler 的四路输入指向正确节点
    ks = g["6"]["inputs"]
    assert ks["model"] == ["1", 0] and ks["positive"] == ["4", 0]
    assert ks["negative"] == ["4", 1] and ks["latent_image"] == ["5", 0]
    assert g["8"]["inputs"]["images"] == ["7", 0]


@respx.mock
def test_generate_one_success_flow():
    pid = "pid-123"
    respx.post(f"{BASE}/prompt").mock(return_value=httpx.Response(200, json={"prompt_id": pid}))
    history = {"status": {"status_str": "success"},
               "outputs": {"8": {"images": [
                   {"filename": "0001_00001_.png", "subfolder": "", "type": "output"}]}}}
    route_hist = respx.get(f"{BASE}/history/{pid}").mock(
        return_value=httpx.Response(200, json={}))          # 先空，稍后改
    png = b"\x89PNG fake bytes"
    respx.get(f"{BASE}/view").mock(return_value=httpx.Response(200, content=png))

    import threading, time
    def fill_history():
        time.sleep(2.5)   # 让轮询先空转两拍，覆盖"未完成分支"
        route_hist.return_value = httpx.Response(200, json={pid: history})
    threading.Thread(target=fill_history, daemon=True).start()

    out = comfy_client.generate_one({"rewritten_prompt": "x", "wh_ratio": "1:1"},
                                    dict(config.DEFAULT_PARAMS), 42, "0001")
    assert out == png
    assert route_hist.call_count >= 2


@respx.mock
def test_generate_one_raises_on_comfy_error():
    pid = "pid-err"
    respx.post(f"{BASE}/prompt").mock(return_value=httpx.Response(200, json={"prompt_id": pid}))
    respx.get(f"{BASE}/history/{pid}").mock(return_value=httpx.Response(200, json={
        pid: {"status": {"status_str": "error", "messages": [["err", "boom"]]},
              "outputs": {}}}))
    with pytest.raises(RuntimeError, match="ComfyUI"):
        comfy_client.generate_one({"rewritten_prompt": "x", "wh_ratio": "1:1"},
                                  dict(config.DEFAULT_PARAMS), 1, "p")


@respx.mock
def test_generate_one_raises_on_queue_reject():
    respx.post(f"{BASE}/prompt").mock(return_value=httpx.Response(
        200, json={"error": {"message": "validation failed"}}))
    with pytest.raises(RuntimeError, match="入队被拒"):
        comfy_client.generate_one({"rewritten_prompt": "x", "wh_ratio": "1:1"},
                                  dict(config.DEFAULT_PARAMS), 1, "p")


# ---------- 多图编辑图（§18.3） ----------
def test_build_edit_graph_structure():
    params = dict(config.DEFAULT_PARAMS)
    g = comfy_client.build_edit_graph("put <image2> on <image1>",
                                      ["a.png", "b.png", "c.png"], params, 42)
    # LoadImage × 3 + 8 主干节点
    assert set(g) == {"1", "2", "3", "4", "5", "6", "7", "8",
                      "101", "102", "103"}
    assert g["101"]["class_type"] == "LoadImage"
    assert g["101"]["inputs"]["image"] == "a.png"
    enc = g["5"]["inputs"]
    assert enc["prompt"] == "put <image2> on <image1>"
    for i in (1, 2, 3):
        assert enc[f"images.image_{i}"] == [f"10{i}", 0]
    assert "images.image_4" not in enc                     # 未提供的参考不出现在图里
    ks = g["6"]["inputs"]
    assert ks["latent_image"] == ["5", 2]                  # 参考 latent 直连（edit 路径）
    assert ks["model"] == ["4", 0] and ks["positive"] == ["5", 0]
    assert g["4"]["class_type"] == "QwenImage21Cache"
    assert g["1"]["class_type"] == "UNETLoader"            # safetensors → 官方 loader


def test_build_edit_graph_gguf_uses_gguf_loader():
    params = dict(config.DEFAULT_PARAMS, comfy_model="qwen-image-2.1-UC-Q8_0.gguf")
    g = comfy_client.build_edit_graph("p", ["a.png"], params, 1)
    assert g["1"]["class_type"] == "UnetLoaderGGUF"
    assert g["1"]["inputs"]["unet_name"] == "qwen-image-2.1-UC-Q8_0.gguf"


@respx.mock
def test_generate_edit_uploads_references(tmp_path):
    src = tmp_path / "a.png"
    src.write_bytes(b"fake image bytes")
    up = respx.post(f"{BASE}/upload/image").mock(return_value=httpx.Response(
        200, json={"name": "ps_a.png", "subfolder": "", "type": "input"}))
    respx.post(f"{BASE}/prompt").mock(return_value=httpx.Response(
        200, json={"prompt_id": "pe1"}))
    respx.get(f"{BASE}/history/pe1").mock(return_value=httpx.Response(200, json={
        "pe1": {"status": {"status_str": "success"},
                "outputs": {"8": {"images": [
                    {"filename": "e.png", "subfolder": "", "type": "output"}]}}}}))
    respx.get(f"{BASE}/view").mock(return_value=httpx.Response(200, content=b"PNG"))
    out = comfy_client.generate_edit([src], "p", dict(config.DEFAULT_PARAMS), 1, "0001")
    assert out == b"PNG"
    assert up.call_count == 1
    import json as _json
    sent = _json.loads(respx.post(f"{BASE}/prompt").calls.last.request.read())
    assert sent["prompt"]["101"]["inputs"]["image"] == "ps_a.png"
