"""全局配置：外部设施路径、API 端点、生成默认参数。

约束：
- llama-server 与 ComfyUI 一律不自动启动（除用户在网页上点击任务按钮触发的任务流程）；
  开发/测试期运行它们需用户明确批准。
"""
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent          # prompt-studio/
WEB_DIR = BASE_DIR / "web"
OUTPUT_ROOT = BASE_DIR / "outputs"                          # 默认工作区
WORKSPACE_POINTER = BASE_DIR / "workspace.json"             # 上次工作区路径指针（gitignore）
UPLOAD_DIR = BASE_DIR / "uploads"                           # 上传的压缩包暂存
LOG_DIR = BASE_DIR / "logs"

# ---- 语言模型设施（阶段一 caption）----
PI_SKILL_DIR = Path(r"C:\Users\admin\.pi\agent\skills\qwen-image-21-prompt-writing")
LLAMA_SERVER_CMD = Path(r"D:\work_space\ai\llm\qwen_28b\start-qwen-iq2-m-64k.cmd")
LM_API_BASE = "http://127.0.0.1:1234"
LM_MODEL_ALIAS = "qwen3.8-27b-uncensored"                   # 与启动脚本 --alias 一致

# ---- ComfyUI 设施（阶段二 生图）----
COMFYUI_DIR = Path(r"D:\Program Files\Comfyui-WF-2026.8.8\ComfyUI")
COMFYUI_PYTHON = Path(r"D:\Program Files\Comfyui-WF-2026.8.8\python\python.exe")
COMFY_API_BASE = "http://127.0.0.1:8188"

WEB_HOST = "127.0.0.1"
WEB_PORT = 9178

# ---- caption 请求 ----
CAPTION_MAX_TOKENS = 16384        # qwen3.5 思考与正文共用预算，必须给足防 JSON 截断
CAPTION_TEMPERATURE = 0.7
CAPTION_RETRY_TEMPERATURE = 1.0
CAPTION_RETRIES = 2
SERVICE_READY_TIMEOUT = 180       # llama-server / ComfyUI 就绪等待秒数

# ---- 生图默认参数（2026-10-10 akane 实跑定值）----
DEFAULT_PARAMS = {
    "steps": 32,
    "sampler": "euler",
    "scheduler": "simple",
    "cfg": 1.0,
    "megapixels": 1.0,
    "seed": 593103825222985,
    "te_resolution": 1024,        # TextEncodeQwenImage21.resolution，t2i 无参考图时不影响输出
    "comfy_model": "qwen-image-2.1-UC-int8_convrot.safetensors",
    "clip_model": "qwen3vl_8b_int8_convrot.safetensors",
    "vae_model": "qwen_image_2.1_vae_bf16.safetensors",
    "close_services_after": True,  # 生图完成后关闭本服务器代管拉起的 ComfyUI
}
