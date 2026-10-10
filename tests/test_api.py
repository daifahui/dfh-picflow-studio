"""接口级测试（TestClient）：任务创建/条目操作/全流程状态机/安全边界。

外部模型服务全部由 stub_external 桩掉（见 conftest），本文件验证的是
路由 → 状态机 → worker 线程 → 落盘 的完整逻辑链路。
"""
import json
import pathlib

from tests.conftest import FAKE_CONTRACT, wait_phase


# ---------- 基础端点 ----------
def test_defaults(client):
    d = client.get("/api/defaults").json()
    assert d["steps"] == 32 and d["cfg"] == 1.0 and d["megapixels"] == 1.0


def test_health_reflects_probe(client, monkeypatch):
    from server import comfy_client, llm_caption
    monkeypatch.setattr(llm_caption, "is_llm_up", lambda: False)
    monkeypatch.setattr(comfy_client, "is_comfy_up", lambda: True)
    assert client.get("/api/health").json() == {"llm": False, "comfy": True}


def test_task_empty_initially(client):
    assert client.get("/api/task").json() == {"task": None}


# ---------- 任务创建 ----------
def test_create_task_from_folder(client, image_dir):
    r = client.post("/api/task", json={"source": str(image_dir), "name": "t"})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["task"]["total"] == 3
    assert [i["src_filename"] for i in data["items"]] == \
        ["a1.webp", "a2.jpg", "b10.png"]           # 自然排序


def test_create_task_from_zip(client, zip_archive):
    r = client.post("/api/task", json={"source": str(zip_archive)})
    assert r.status_code == 200
    names = [i["src_filename"] for i in r.json()["items"]]
    assert names == ["0001_a1.webp", "0002_a2.jpg", "0003_b10.png"]


def test_create_task_rejects_missing_source(client):
    r = client.post("/api/task", json={"source": "D:/definitely/not/here"})
    assert r.status_code == 400


def test_create_task_rejects_random_file(client, tmp_path):
    f = tmp_path / "x.bin"
    f.write_bytes(b"1")
    assert client.post("/api/task", json={"source": str(f)}).status_code == 400


def test_create_task_rejects_second_active(client, image_dir):
    client.post("/api/task", json={"source": str(image_dir)})
    assert client.post("/api/task", json={"source": str(image_dir)}).status_code == 409


def test_upload_zip_and_reject_other(client, zip_archive):
    r = client.post("/api/upload", files={
        "file": ("pack.zip", zip_archive.read_bytes(), "application/zip")})
    assert r.status_code == 200 and r.json()["path"].endswith("pack.zip")
    assert client.post("/api/upload", files={
        "file": ("virus.exe", b"MZ", "application/x-dos")}).status_code == 400


# ---------- 全流程状态机（caption → reviewing → generate → done） ----------
def test_auto_pipeline_end_to_end(client, image_dir, stub_external):
    client.post("/api/task", json={"source": str(image_dir)})
    assert client.post("/api/task/auto").json() == {"ok": True}

    data = wait_phase(client, "done", timeout=15)
    items = data["items"]
    assert all(i["status"] == "done" for i in items)
    assert all(i["contract"] == FAKE_CONTRACT for i in items)
    assert all(i["out_image"] for i in items)

    from server.state import manager
    task_dir = pathlib.Path(manager.current().dir)
    assert (task_dir / items[0]["out_image"]).exists()      # v11：生成图在任务根
    assert len(list((task_dir / "prompts").glob("*.json"))) == 3
    summary = task_dir / "prompts" / "prompts_summary.jsonl"
    lines = summary.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 3 and json.loads(lines[0])["seq"] == 1
    assert (task_dir / ".studio" / "task.json").exists()


# ---------- 输出双目录（§16.2） ----------
def test_output_dirs_custom_created_and_reported(client, image_dir, tmp_path):
    img_out = tmp_path / "prod" / "imgs"
    prompt_out = tmp_path / "prod" / "words"
    r = client.post("/api/task", json={
        "source": str(image_dir), "name": "od",
        "output_image_dir": str(img_out), "output_prompt_dir": str(prompt_out)})
    assert r.status_code == 200
    t = r.json()["task"]
    assert t["images_dir"] == str(img_out)
    assert t["prompts_dir"] == str(prompt_out)
    assert img_out.exists() and prompt_out.exists()          # 创建即建目录


def test_output_dir_must_be_absolute(client, image_dir):
    r = client.post("/api/task", json={
        "source": str(image_dir), "output_image_dir": "relative/dir"})
    assert r.status_code == 400


def test_task_name_path_chars_sanitized(client, image_dir):
    r = client.post("/api/task", json={"source": str(image_dir), "name": 'a/b:c*?'})
    assert r.status_code == 200
    from server.state import manager
    assert "/" not in manager.current().id and "\\" not in manager.current().id


def test_generate_writes_to_custom_output_dirs(client, image_dir, stub_external,
                                               tmp_path):
    img_out = tmp_path / "custom_imgs"
    prompt_out = tmp_path / "custom_words"
    client.post("/api/task", json={
        "source": str(image_dir), "name": "cust",
        "output_image_dir": str(img_out), "output_prompt_dir": str(prompt_out)})
    client.post("/api/task/caption")
    wait_phase(client, "reviewing")
    client.post("/api/task/generate")
    data = wait_phase(client, "done")
    assert (img_out / data["items"][0]["out_image"]).exists()
    assert (prompt_out / "0001.json").exists()
    assert (prompt_out / "prompts_summary.jsonl").exists()
    assert client.get("/api/img/out/1").status_code == 200   # 自定义目录可安全访问


def test_caption_only_stops_at_reviewing(client, image_dir, stub_external):
    client.post("/api/task", json={"source": str(image_dir)})
    client.post("/api/task/caption")
    data = wait_phase(client, "reviewing")
    assert all(i["status"] == "captioned" for i in data["items"])


def test_generate_one_after_caption(client, image_dir, stub_external):
    client.post("/api/task", json={"source": str(image_dir)})
    client.post("/api/task/caption")
    wait_phase(client, "reviewing")
    assert client.post("/api/task/items/2/generate").json() == {"ok": True}
    data = wait_phase(client, "done")
    assert data["items"][1]["status"] == "done"
    assert data["items"][0]["status"] == "captioned"     # 其余不受影响


def test_generate_stops_owned_llm_before_comfy(client, image_dir, stub_external,
                                               monkeypatch):
    """显存互斥回归（设计 §3.3）：本服务器代管的 llama-server 必须在
    ensure_comfy 之前关闭，与请求参数无关。"""
    from server import llm_caption
    calls: list[str] = []
    monkeypatch.setattr(llm_caption, "stop_llm", lambda: calls.append("stop_llm"))
    monkeypatch.setattr(llm_caption, "_owned_proc", object())   # 模拟 caption 阶段拉起的实例

    client.post("/api/task", json={"source": str(image_dir)})
    client.post("/api/task/caption")
    wait_phase(client, "reviewing")
    client.post("/api/task/generate")                    # 前端「全部生成」的原始调用（无附加参数）
    wait_phase(client, "done")
    assert calls == ["stop_llm"]


def test_generate_reuses_external_llm(client, image_dir, stub_external, monkeypatch):
    """外部手动启动的 llama-server（非代管）只复用、不关闭。"""
    from server import llm_caption
    calls: list[str] = []
    monkeypatch.setattr(llm_caption, "stop_llm", lambda: calls.append("stop_llm"))
    assert not llm_caption.owns_llm()                    # ensure_llm 已被桩掉，全程无代管进程

    client.post("/api/task", json={"source": str(image_dir)})
    client.post("/api/task/caption")
    wait_phase(client, "reviewing")
    client.post("/api/task/generate")
    wait_phase(client, "done")
    assert calls == []


def test_recaption_records_note_and_reruns(client, image_dir, stub_external, monkeypatch):
    seen = {}
    from server import llm_caption
    def fake_caption(path, note=""):
        seen["note"] = note
        return dict(FAKE_CONTRACT)
    monkeypatch.setattr(llm_caption, "caption_image", fake_caption)

    client.post("/api/task", json={"source": str(image_dir)})
    client.post("/api/task/caption")
    wait_phase(client, "reviewing")
    client.post("/api/task/items/1/recaption", json={"note": "头发改银白"})
    wait_phase(client, "reviewing")
    assert seen["note"] == "头发改银白"


# ---------- 条目编辑 ----------
def test_update_item_contract_and_note(client, image_dir, stub_external):
    client.post("/api/task", json={"source": str(image_dir)})
    client.post("/api/task/caption")
    wait_phase(client, "reviewing")
    r = client.put("/api/task/items/1", json={
        "rewritten_prompt": "edited prompt", "wh_ratio": "3:4", "note": "n1"})
    assert r.status_code == 200
    it = client.get("/api/task").json()["items"][0]
    assert it["contract"] == {"rewritten_prompt": "edited prompt", "wh_ratio": "3:4"}
    assert it["note"] == "n1"


def test_update_item_404(client):
    assert client.put("/api/task/items/9", json={"note": "x"}).status_code == 404


# ---------- 停止与安全边界 ----------
def test_stop_sets_flag(client, image_dir):
    client.post("/api/task", json={"source": str(image_dir)})
    assert client.post("/api/task/stop").json() == {"ok": True}
    assert client.get("/api/task").json()["task"]["phase"] == "stopped"


def test_img_seq_404(client, image_dir):
    client.post("/api/task", json={"source": str(image_dir)})
    assert client.get("/api/img/src/99").status_code == 404
    assert client.get("/api/img/thumb/99").status_code == 404


def test_img_src_served_with_mime(client, image_dir):
    client.post("/api/task", json={"source": str(image_dir)})
    r = client.get("/api/img/src/1")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/")


# 注：SSE 端点的无限流在 TestClient 下无法优雅关闭（会挂死），
# 其全链路行为由 tests/test_e2e_web.py 的真实浏览器测试覆盖，
# 事件广播的单元逻辑见 tests/test_sse.py。
