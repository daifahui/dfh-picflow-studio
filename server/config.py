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

# 档位表（§18.2）：key → 启动脚本 / alias / 标签。alias 与脚本 --alias 一致，
# 用于 /v1/models 对比判断是否需要换档。
LM_PROFILES = {
    "iq2_m_64k": {
        "cmd": r"D:\work_space\ai\llm\qwen_28b\start-qwen-iq2-m-64k.cmd",
        "alias": "qwen3.8-27b-uncensored",
        "label": "IQ2_M · 64K 大上下文（快，可贴图）",
    },
    "q4_k_m": {
        "cmd": r"D:\work_space\ai\llm\qwen_28b\start-qwen-q4-k-m.cmd",
        "alias": "qwen3.8-27b-uncensored@q4_k_m",
        "label": "Q4_K_M · 高质量（慢，16K 上下文）",
    },
}
LM_API_BASE = "http://127.0.0.1:1234"
LM_MODEL_ALIAS = LM_PROFILES["iq2_m_64k"]["alias"]           # 兼容旧引用

# ---- ComfyUI 设施（阶段二 生图）----
COMFYUI_DIR = Path(r"D:\Program Files\Comfyui-WF-2026.8.8\ComfyUI")
COMFYUI_PYTHON = Path(r"D:\Program Files\Comfyui-WF-2026.8.8\python\python.exe")
COMFY_MODELS_DIR = COMFYUI_DIR / "models" / "diffusion_models"
COMFY_API_BASE = "http://127.0.0.1:8188"

# ---- 多图编辑类任务（asset / faceswap，§18.3）----
MULTI_MAX_IMAGES = 6        # 用户定版上限 6 张（节点能力远超，但按 6 收敛保质量/显存）
DEFAULT_TEMPLATES = {
    "asset": (
        "<image1> 是人物参考，<image2> 是服装参考，<image3> 及之后是风格/姿势参考。"
        "请生成该人物的完整人物资产图：保持 <image1> 的人物长相、发型与体型，"
        "换上 <image2> 的服装，整体氛围与姿势参考后续图片，全身、干净背景、"
        "角色设定集风格、高细节、光源统一。"),
    "faceswap": (
        "<image1> 是主图（保留其姿势、构图、光照与身体），<image2> 及之后是目标人脸参考"
        "（可多张以提升还原度）。请把目标人脸自然地换到主图人物上：严格保持目标脸的"
        "五官与身份特征，与主图的头部角度、肤色、光照无缝融合，写实风格，无接缝痕迹。"),
}

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
    "lm_profile": "iq2_m_64k",     # 语言模型档位（§18.2，LM_PROFILES key）
    "prompt_template": "",         # 多图编辑类任务的提示词模板（空 = 按类型取默认）
    "close_services_after": True,  # 生图完成后关闭本服务器代管拉起的 ComfyUI
}

def known_unets() -> list[str]:
    """扫描 diffusion_models 目录的可用生图模型（ComfyUI 未运行时的静态回退，§18.2）。
    qwen 系排最前（默认模型恒在首位），其余字母序（§19 R1）。"""
    if not COMFY_MODELS_DIR.is_dir():
        return [DEFAULT_PARAMS["comfy_model"]]
    names = sorted(p.name for p in COMFY_MODELS_DIR.iterdir()
                   if p.suffix.lower() in (".safetensors", ".gguf"))
    qwen = [n for n in names if n.lower().startswith("qwen")]
    return qwen + [n for n in names if not n.lower().startswith("qwen")]
