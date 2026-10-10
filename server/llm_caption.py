"""阶段一：llama-server 代管 + qwen-image-21-prompt-writing skill 拼接 + 契约提取。

skill 策略（设计文档 §12 已定）：实时读取 pi 的 skill 目录，不拷贝快照。
本模块拉起的 llama-server 进程会记录 pid，stop_llm() 只关自己拉起的进程。
"""
from __future__ import annotations

import base64
import io
import json
import re
import subprocess
import time
from pathlib import Path

import httpx
from PIL import Image

from . import config, sse

_skill_warned = False
_owned_proc: subprocess.Popen | None = None


# ---------- skill ----------
def read_skill_system_prompt() -> str:
    """SKILL.md + reference/system_prompt_t2i.txt 实时拼接（纯 API 无工具，
    靠预注入替代 pi 的"模型主动读文件"）。"""
    global _skill_warned
    parts: list[str] = []
    for rel in ("SKILL.md", "reference/system_prompt_t2i.txt"):
        p = config.PI_SKILL_DIR / rel
        try:
            parts.append(p.read_text(encoding="utf-8"))
        except OSError:
            if not _skill_warned:
                sse.hub.publish("log", text=f"[warn] skill 文件缺失：{p}")
                _skill_warned = True
    return "\n\n---\n\n".join(parts)


# ---------- llama-server 生命周期 ----------
def is_llm_up() -> bool:
    try:
        r = httpx.get(f"{config.LM_API_BASE}/v1/models", timeout=3)
        return r.status_code == 200
    except Exception:  # noqa: BLE001 —— 未启动时连接被拒，属正常态
        return False


def start_llama_server() -> None:
    """拉起 iq2_m 64K 档脚本（后台隐藏窗口，日志写 logs/llama-server.log）。"""
    global _owned_proc
    config.LOG_DIR.mkdir(exist_ok=True)
    log = open(config.LOG_DIR / "llama-server.log", "ab")
    _owned_proc = subprocess.Popen(
        ["cmd", "/c", "start", "/wait", "/b", "", str(config.LLAMA_SERVER_CMD)],
        stdout=log, stderr=subprocess.STDOUT,
        creationflags=subprocess.CREATE_NO_WINDOW,
        cwd=str(config.LLAMA_SERVER_CMD.parent),
    )


def ensure_llm() -> None:
    if is_llm_up():
        sse.hub.publish("log", text="llama-server 已在运行（复用现有实例，结束时不关闭）")
        return
    sse.hub.publish("log", text="启动 llama-server（iq2_m 64K 视觉档）…")
    start_llama_server()
    deadline = time.time() + config.SERVICE_READY_TIMEOUT
    while time.time() < deadline:
        if is_llm_up():
            sse.hub.publish("log", text="llama-server 就绪")
            return
        time.sleep(2)
    raise RuntimeError("llama-server 启动超时（详见 logs/llama-server.log）")


def owns_llm() -> bool:
    """当前 llama-server 是否为本服务器拉起（外部实例不属于代管范围）。"""
    return _owned_proc is not None


def stop_llm() -> None:
    """只关闭本模块拉起的进程；用户手动的实例不动。"""
    global _owned_proc
    if _owned_proc is None:
        return
    subprocess.run(["taskkill", "/T", "/F", "/PID", str(_owned_proc.pid)],
                   capture_output=True)
    _owned_proc = None
    sse.hub.publish("log", text="已关闭代管的 llama-server")


# ---------- caption 请求 ----------
_THINK = re.compile(r"<think>.*?</think>", re.S)
_JSON = re.compile(r"\{.*\}", re.S)
_RATIO = re.compile(r"^\s*\d+\s*:\s*\d+\s*$")

TAGS_MAX = 12          # 标签数上限（§17）
TAG_LEN_MAX = 40       # 单个标签长度上限


def _normalize_tags(raw) -> list[str]:
    """tags 容错规范化：数组或逗号分隔字符串 → 干净的小写短语数组（§17.1）。"""
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    out: list[str] = []
    for item in raw:
        for t in str(item).split(","):
            t = t.strip().lower()
            if t and t not in out:
                out.append(t[:TAG_LEN_MAX])
            if len(out) >= TAGS_MAX:
                return out
    return out


_PASSTHRU = {"png", "jpg", "jpeg"}     # llama-server 端 stb_image 原生支持的格式


def _b64_image_url(path: Path) -> str:
    suffix = path.suffix.lower().lstrip(".")
    if suffix in _PASSTHRU:
        data = base64.b64encode(path.read_bytes()).decode()
        return f"data:image/{'jpeg' if suffix == 'jpg' else suffix};base64,{data}"
    # webp/bmp/gif 等在 llama-server 端解码失败（2026-10-10 E2E 实证）→ 先转 PNG
    buf = io.BytesIO()
    with Image.open(path) as im:
        im.convert("RGB").save(buf, "PNG")
    data = base64.b64encode(buf.getvalue()).decode()
    return f"data:image/png;base64,{data}"


def _parse_contract(text: str) -> dict:
    text = _THINK.sub("", text)
    m = _JSON.search(text)
    if not m:
        raise ValueError("响应中未找到 JSON")
    contract = json.loads(m.group(0), strict=False)   # 裸换行宽容解析
    prompt = str(contract.get("rewritten_prompt", "")).strip()
    ratio = str(contract.get("wh_ratio", "")).strip()
    if not prompt:
        raise ValueError("rewritten_prompt 为空")
    if not _RATIO.match(ratio):
        raise ValueError(f"wh_ratio 非法：{ratio!r}")
    return {"rewritten_prompt": prompt, "wh_ratio": ratio,
            "tags": _normalize_tags(contract.get("tags"))}


def caption_image(image_path: Path, note: str = "") -> dict:
    """单张图 → 契约 JSON（rewritten_prompt / wh_ratio / tags）。带注释时按
    "修正改写"语气追加。失败重试并升温。"""
    user_text = (
        "请按系统提示词的契约处理这张图片，输出 rewritten_prompt 与 wh_ratio。\n"
        "另请额外输出 tags 字段：4 到 12 个英文小写短语标签（逗号分隔的数组），"
        "概括图中的主体、服装、动作、场景、画风与光影，例如 "
        '["1girl", "blue hair", "night city", "window side light"]。')
    if note:
        user_text += f"\n用户注释（务必体现在改写结果中）：{note}"
    payload = {
        "model": config.LM_MODEL_ALIAS,
        "messages": [
            {"role": "system", "content": read_skill_system_prompt()},
            {"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": _b64_image_url(image_path)}},
                {"type": "text", "text": user_text},
            ]},
        ],
        "max_tokens": config.CAPTION_MAX_TOKENS,
        "temperature": config.CAPTION_TEMPERATURE,
    }
    last_err: Exception | None = None
    for attempt in range(config.CAPTION_RETRIES):
        if attempt:
            payload["temperature"] = config.CAPTION_RETRY_TEMPERATURE
        try:
            r = httpx.post(f"{config.LM_API_BASE}/v1/chat/completions",
                           json=payload, timeout=600)
            r.raise_for_status()
            content = r.json()["choices"][0]["message"]["content"]
            return _parse_contract(content)
        except Exception as e:  # noqa: BLE001 —— 重试后向外抛
            last_err = e
            sse.hub.publish("log", text=f"[caption] 第 {attempt + 1} 次失败：{e}")
    raise RuntimeError(f"caption 重试耗尽：{last_err}")


# ---------- AI 任务命名（§16.4） ----------
def generate_task_name(contract: dict) -> str:
    """首图契约 → 4-12 字任务名。任何失败返回空串（保留占位名，不抛出）。"""
    payload = {
        "model": config.LM_MODEL_ALIAS,
        "messages": [
            {"role": "system", "content":
                "你是命名助手。根据生图提示词起一个4到12个字的中文短名，"
                "概括画面主体与场景。只输出名称本身，不要解释，不要引号和句号。"},
            {"role": "user", "content":
                f"提示词：{str(contract.get('rewritten_prompt', ''))[:600]}"},
        ],
        "max_tokens": 512,
        "temperature": 0.7,
    }
    try:
        r = httpx.post(f"{config.LM_API_BASE}/v1/chat/completions",
                       json=payload, timeout=120)
        r.raise_for_status()
        content = r.json()["choices"][0]["message"]["content"]
    except Exception:                                     # noqa: BLE001 —— 命名失败不致命
        return ""
    text = _THINK.sub("", content).strip()
    line = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
    line = line.strip("「」\"'。.，,！!？?：: 　")
    line = re.sub(r'[\\/:*?"<>|]', "_", line)
    return line[:24]
