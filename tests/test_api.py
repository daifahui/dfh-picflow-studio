"""接口级测试（TestClient）：工作区/队列/任务路由/全流程状态机/安全边界。

外部模型服务全部由 stub_external 桩掉（见 conftest），本文件验证的是
路由 → 工作区状态机 → 队列 worker → 落盘 的完整逻辑链路。
"""
import json
import pathlib

from tests.conftest import (FAKE_CONTRACT, import_task, make_png,
                            wait_task_phase, wait_ws)


# ---------- 基础端点 ----------
def test_defaults(client):
    d = client.get("/api/defaults").json()
    assert d["steps"] == 32 and d["cfg"] == 1.0 and d["megapixels"] == 1.0


def test_health_reflects_probe(client, monkeypatch):
    from server import comfy_client, llm_caption
    monkeypatch.setattr(llm_caption, "is_llm_up", lambda: False)
    monkeypatch.setattr(comfy_client, "is_comfy_up", lambda: True)
    assert client.get("/api/health").json() == {"llm": False, "comfy": True}


def test_workspace_initially_empty(client):
    ws = client.get("/api/workspace").json()
    assert ws["queue"] == [] and ws["running"] is False


# ---------- 任务导入 ----------
def test_import_from_folder(client, image_dir):
    r = client.post("/api/queue/items",
                    json={"items": [{"source": str(image_dir), "name": "t"}]})
    assert r.status_code == 200, r.text
    created = r.json()["created"]
    assert created[0]["total"] == 3
    d = client.get(f"/api/tasks/{created[0]['id']}").json()
    assert [i["src_filename"] for i in d["items"]] == \
        ["a1.webp", "a2.jpg", "b10.png"]               # 自然排序


def test_import_from_zip(client, zip_archive):
    r = client.post("/api/queue/items",
                    json={"items": [{"source": str(zip_archive)}]})
    names = [i["src_filename"] for i in
             client.get(f"/api/tasks/{r.json()['created'][0]['id']}").json()["items"]]
    assert names == ["0001_a1.webp", "0002_a2.jpg", "0003_b10.png"]


def test_import_multiple_and_queue_order(client, image_dir, tmp_path):
    second = tmp_path / "imgs2"
    second.mkdir()
    (second / "x.png").write_bytes(b"not really png but fine")
    r = client.post("/api/queue/items", json={"items": [
        {"source": str(image_dir), "name": "first"},
        {"source": str(second), "name": "second"}]})
    assert [t["name"] for t in r.json()["queue"]] == ["first", "second"]


def test_import_rejects_missing_source(client):
    r = client.post("/api/queue/items",
                    json={"items": [{"source": "D:/definitely/not/here"}]})
    assert r.status_code == 400


def test_import_rejects_bad_mode(client, image_dir):
    r = client.post("/api/queue/items",
                    json={"items": [{"source": str(image_dir), "mode": "x"}]})
    assert r.status_code == 400


def test_import_rejects_relative_output_dir(client, image_dir):
    r = client.post("/api/queue/items", json={"items": [
        {"source": str(image_dir), "output_image_dir": "rel/dir"}]})
    assert r.status_code == 400


def test_delete_task_only_when_idle(client, image_dir):
    tid = import_task(client, image_dir)
    assert client.delete(f"/api/tasks/{tid}").json() == {"ok": True}
    assert client.get(f"/api/tasks/{tid}").status_code == 404
    assert client.delete(f"/api/tasks/{tid}").status_code == 404


def test_rename_task_marks_renamed(client, image_dir):
    tid = import_task(client, image_dir, name="old-name")
    r = client.post(f"/api/tasks/{tid}/rename", json={"name": "new name"})
    assert r.status_code == 200
    ws = client.get("/api/workspace").json()
    assert ws["queue"][0]["name"] == "new name"
    assert ws["queue"][0]["id"] == "new name"          # id 跟随目录名
    assert client.post(f"/api/tasks/{tid}/rename",
                       json={"name": "x"}).status_code == 404   # 旧 id 失效


def test_upload_zip_and_reject_other(client, zip_archive):
    r = client.post("/api/upload", files={
        "file": ("pack.zip", zip_archive.read_bytes(), "application/zip")})
    assert r.status_code == 200 and r.json()["path"].endswith("pack.zip")
    assert client.post("/api/upload", files={
        "file": ("virus.exe", b"MZ", "application/x-dos")}).status_code == 400


# ---------- 工作区 ----------
def test_switch_workspace(client, image_dir, tmp_path):
    new_ws = tmp_path / "ws2"
    r = client.post("/api/workspace", json={"path": str(new_ws)})
    assert r.status_code == 200 and r.json()["workspace"] == str(new_ws)
    tid = import_task(client, image_dir, name="in-ws2")
    from server.state import manager
    assert str(new_ws) in manager.task(tid).dir
    assert client.post("/api/workspace",
                       json={"path": "rel/path"}).status_code == 400


# ---------- 队列全流程（阶段批处理） ----------
def test_queue_batched_pipeline_order(client, image_dir, stub_external, monkeypatch):
    """两任务串行：caption(任务1+2) → 关 LLM → 开 ComfyUI → generate(1+2) → 关。"""
    from server import comfy_client, llm_caption
    calls = []

    def fake_ensure_llm():
        llm_caption._owned_proc = object()      # 模拟真实 ensure 登记代管进程
        calls.append("llm_up")

    def fake_stop_llm():
        llm_caption._owned_proc = None          # 模拟真实 stop 释放所有权
        calls.append("llm_down")

    monkeypatch.setattr(llm_caption, "ensure_llm", fake_ensure_llm)
    monkeypatch.setattr(llm_caption, "stop_llm", fake_stop_llm)
    monkeypatch.setattr(comfy_client, "ensure_comfy",
                        lambda: calls.append("comfy_up"))
    monkeypatch.setattr(comfy_client, "stop_comfy",
                        lambda: calls.append("comfy_down"))
    monkeypatch.setattr(llm_caption, "caption_image",
                        lambda path, note="": calls.append("cap") or dict(FAKE_CONTRACT))
    monkeypatch.setattr(comfy_client, "generate_one",
                        lambda c, p, s, prefix: calls.append("gen") or make_png(8, 8))

    t1 = import_task(client, image_dir, name="a")
    t2 = import_task(client, image_dir, name="b")
    client.post("/api/queue/start")
    wait_task_phase(client, t1, "done")
    wait_task_phase(client, t2, "done")

    assert calls[0] == "llm_up"
    assert calls.count("llm_up") == 1 and calls.count("llm_down") == 1
    assert calls.index("llm_down") < calls.index("comfy_up")   # 显存先换后进
    assert calls.count("comfy_up") == 1 and calls.count("comfy_down") == 1
    assert calls.count("cap") == 6 and calls.count("gen") == 6
    assert calls.index("comfy_down") == len(calls) - 1
    # 服务各只起一次的关键断言
    assert calls[:8] == ["llm_up"] + ["cap"] * 6 + ["llm_down"]
    assert t1 and t2


def test_queue_review_gate_blocks_generate(client, image_dir, stub_external,
                                           monkeypatch):
    from server import comfy_client
    comfy_calls = []
    monkeypatch.setattr(comfy_client, "ensure_comfy",
                        lambda: comfy_calls.append("up"))
    tid = import_task(client, image_dir, name="g", mode="review")
    client.post("/api/queue/start")
    wait_task_phase(client, tid, "reviewing")
    wait_ws(client, lambda ws: ws["gate"] is True)
    assert comfy_calls == []                            # 闸未放行，不碰 ComfyUI
    client.post("/api/queue/resume")
    wait_task_phase(client, tid, "done")
    wait_ws(client, lambda ws: ws["gate"] is False)
    assert comfy_calls == ["up"]


def test_queue_pause_between_items_then_resume(client, image_dir, stub_external,
                                               monkeypatch):
    """第一张提取完即暂停 → 队列停住 → 继续 → 全部完成（断点续跑不重提）。"""
    from server import llm_caption
    n = {"cap": 0}
    real_contract = dict(FAKE_CONTRACT)
    def fake_caption(path, note=""):
        n["cap"] += 1
        if n["cap"] == 1:
            from server.state import manager
            manager.pause_flag = True                   # 模拟用户在第一张后按暂停
        return dict(real_contract)
    monkeypatch.setattr(llm_caption, "caption_image", fake_caption)

    tid = import_task(client, image_dir, name="p")
    client.post("/api/queue/start")
    wait_ws(client, lambda ws: not ws["running"] and ws["paused"], timeout=15)
    assert n["cap"] == 1
    assert client.post("/api/queue/resume").status_code == 200
    wait_task_phase(client, tid, "done", timeout=15)
    assert n["cap"] == 3                                # 续跑只补剩下的


def test_queue_stop_marks_tasks_stopped(client, image_dir, stub_external):
    tid = import_task(client, image_dir, name="s")
    client.post("/api/queue/stop")                       # 未启动也可预挂取消
    client.post("/api/queue/start")
    wait_task_phase(client, tid, "stopped")
    ws = client.get("/api/workspace").json()
    assert ws["running"] is False


def test_auto_single_task_degenerates_to_old_full_flow(client, image_dir,
                                                       stub_external):
    """单任务 + 全自动 = 原 v1 全流程。"""
    tid = import_task(client, image_dir, name="one", mode="auto")
    client.post("/api/queue/start")
    data = wait_task_phase(client, tid, "done", timeout=15)
    items = data["items"]
    assert all(i["status"] == "done" for i in items)
    task_dir = pathlib.Path(data["task"]["images_dir"])
    assert (task_dir / items[0]["out_image"]).exists()
    assert len(list((task_dir / "prompts").glob("*.json"))) == 3


# ---------- AI 命名 ----------
def test_ai_naming_renames_after_first_contract(client, image_dir, stub_external):
    tid = import_task(client, image_dir, name="pkg-2024", ai_name=True)
    client.post("/api/queue/start")
    wait_ws(client, lambda ws: ws["queue"] and ws["queue"][0]["name"] == "AI 名称",
            timeout=15)
    wait_task_phase(client, "AI 名称", "done")
    from server.state import manager
    t = manager.task("AI 名称")
    assert t.dir.endswith("AI 名称")                     # 目录随名重命名
    assert not manager.task(tid)                        # 旧 id 失效


def test_user_rename_blocks_ai_naming(client, image_dir, stub_external):
    tid = import_task(client, image_dir, name="mine", ai_name=True)
    client.post(f"/api/tasks/{tid}/rename", json={"name": "my-own"})
    client.post("/api/queue/start")
    wait_task_phase(client, "my-own", "done")
    ws = client.get("/api/workspace").json()
    assert ws["queue"][0]["name"] == "my-own"           # 手动改名优先


# ---------- 审阅页单任务操作 ----------
def test_caption_only_stops_at_reviewing(client, image_dir, stub_external):
    tid = import_task(client, image_dir, name="c")
    client.post(f"/api/tasks/{tid}/caption")
    data = wait_task_phase(client, tid, "reviewing")
    assert all(i["status"] == "captioned" for i in data["items"])


def test_generate_one_after_caption(client, image_dir, stub_external):
    tid = import_task(client, image_dir, name="g1")
    client.post(f"/api/tasks/{tid}/caption")
    wait_task_phase(client, tid, "reviewing")
    client.post(f"/api/tasks/{tid}/items/2/generate")
    data = wait_task_phase(client, tid, "done")
    assert data["items"][1]["status"] == "done"
    assert data["items"][0]["status"] == "captioned"     # 其余不受影响


def test_generate_closes_owned_llm_before_comfy(client, image_dir, stub_external,
                                                monkeypatch):
    """显存互斥回归（§3.3）：单任务生成前必须先关代管 LLM。"""
    from server import llm_caption
    calls = []
    monkeypatch.setattr(llm_caption, "stop_llm", lambda: calls.append("stop_llm"))
    monkeypatch.setattr(llm_caption, "_owned_proc", object())
    tid = import_task(client, image_dir, name="own")
    client.post(f"/api/tasks/{tid}/caption")
    wait_task_phase(client, tid, "reviewing")
    client.post(f"/api/tasks/{tid}/generate")
    wait_task_phase(client, tid, "done")
    assert calls == ["stop_llm"]


def test_recaption_records_note_and_reruns(client, image_dir, stub_external,
                                           monkeypatch):
    seen = {}
    from server import llm_caption
    def fake_caption(path, note=""):
        seen["note"] = note
        return dict(FAKE_CONTRACT)
    monkeypatch.setattr(llm_caption, "caption_image", fake_caption)

    tid = import_task(client, image_dir, name="rc")
    client.post(f"/api/tasks/{tid}/caption")
    wait_task_phase(client, tid, "reviewing")
    client.post(f"/api/tasks/{tid}/items/1/recaption", json={"note": "头发改银白"})
    wait_task_phase(client, tid, "reviewing")
    assert seen["note"] == "头发改银白"


# ---------- 条目编辑 ----------
def test_update_item_contract_and_note(client, image_dir, stub_external):
    tid = import_task(client, image_dir, name="edit")
    client.post(f"/api/tasks/{tid}/caption")
    wait_task_phase(client, tid, "reviewing")
    r = client.put(f"/api/tasks/{tid}/items/1", json={
        "rewritten_prompt": "edited prompt", "wh_ratio": "3:4", "note": "n1"})
    assert r.status_code == 200
    it = client.get(f"/api/tasks/{tid}").json()["items"][0]
    assert it["contract"] == {"rewritten_prompt": "edited prompt", "wh_ratio": "3:4"}
    assert it["note"] == "n1"


def test_update_item_404(client, image_dir):
    tid = import_task(client, image_dir, name="e2")
    assert client.put(f"/api/tasks/{tid}/items/9",
                      json={"note": "x"}).status_code == 404


# ---------- 输出双目录（§16.2，M6 回归） ----------
def test_output_dirs_custom_created_and_reported(client, image_dir, tmp_path):
    img_out = tmp_path / "prod" / "imgs"
    prompt_out = tmp_path / "prod" / "words"
    r = client.post("/api/queue/items", json={"items": [{
        "source": str(image_dir), "name": "od",
        "output_image_dir": str(img_out), "output_prompt_dir": str(prompt_out)}]})
    t = r.json()["created"][0]
    assert t["images_dir"] == str(img_out)
    assert t["prompts_dir"] == str(prompt_out)
    assert img_out.exists() and prompt_out.exists()


def test_generate_writes_to_custom_output_dirs(client, image_dir, stub_external,
                                               tmp_path):
    img_out = tmp_path / "custom_imgs"
    prompt_out = tmp_path / "custom_words"
    tid = import_task(client, image_dir, name="cust",
                      output_image_dir=str(img_out),
                      output_prompt_dir=str(prompt_out))
    client.post(f"/api/tasks/{tid}/caption")
    wait_task_phase(client, tid, "reviewing")
    client.post(f"/api/tasks/{tid}/generate")
    data = wait_task_phase(client, tid, "done")
    assert (img_out / data["items"][0]["out_image"]).exists()
    assert (prompt_out / "0001.json").exists()
    assert client.get(f"/api/tasks/{tid}/img/out/1").status_code == 200


# ---------- 图片服务与安全边界 ----------
def test_img_seq_404(client, image_dir):
    tid = import_task(client, image_dir, name="i404")
    assert client.get(f"/api/tasks/{tid}/img/src/99").status_code == 404
    assert client.get(f"/api/tasks/{tid}/img/thumb/99").status_code == 404


def test_img_src_served_with_mime(client, image_dir):
    tid = import_task(client, image_dir, name="mime")
    r = client.get(f"/api/tasks/{tid}/img/src/1")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/")


# 注：SSE 端点的无限流在 TestClient 下无法优雅关闭（会挂死），
# 其全链路行为由 tests/test_e2e_web.py 的真实浏览器测试覆盖，
# 事件广播的单元逻辑见 tests/test_sse.py。
