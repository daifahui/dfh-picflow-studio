"""comfy_client 单元测试：wh_ratio 换算、API 图构建、generate_one 流程（respx mock）。"""
import json

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
