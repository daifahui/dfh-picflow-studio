# 新机器装机指南（SETUP）

把 Prompt Studio 搬到另一台电脑（Windows + NVIDIA GPU）的完整流程。
仓库只含代码；模型与外部程序是机器级资产，需要按本指南在新机器重新就位。

## 0. 前置条件

| 项 | 要求 |
|---|---|
| 操作系统 | Windows 10/11（服务器端弹目录框、.cmd 启动脚本均为 Windows 语义） |
| 显卡 | NVIDIA，显存参考 16G（两阶段串行互斥就是按这个约束设计的） |
| Python | 3.11+（ PATH 中可用） |
| 磁盘 | 模型合计约 40–60 GB 下载量 |

## 1. 跑起网页服务器（1 分钟）

```
git clone <你的私有仓库地址>
cd prompt-studio
start-server.cmd          ← 首次自动创建 .venv 并安装 requirements.txt
```

浏览器打开 http://127.0.0.1:9178 即见界面（此时模型设施未配置，导入任务可用，
点「开始」前先完成第 2 步）。

## 2. 配置本机路径（config.local.json）

```
copy config.local.example.json config.local.json
```

编辑 `config.local.json`，把三个机器相关路径改成新机器实际位置（其余 key 可删）：

| key | 指向 |
|---|---|
| `lm_profiles.*.cmd` | llama-server 启动脚本（见第 3 步，或沿用你自己的启动方式） |
| `comfyui_dir` | ComfyUI 目录（`comfyui_python`/`comfy_models_dir` 缺省按整合包布局派生） |
| `pi_skill_dir` | qwen-image-21-prompt-writing skill 目录（见第 4 步） |

该文件已被 `.gitignore` 忽略——每台机器各存一份，不进仓库。

## 3. 外部程序与模型

### llama-server（阶段一 caption）

1. 下载 llama.cpp Windows CUDA 预编译版（GitHub Releases）；
2. 从 HuggingFace 下载语言模型 GGUF（与启动脚本参数一致）：
   - `qwen3.8-27b` IQ2_M（64K 上下文档，可贴图）与/或 Q4_K_M 档；
3. 写一个启动脚本（参照 `--alias` 与 `config.local.json` 中 `lm_profiles.*.alias`
   一致即可，端口 1234），把脚本路径填进配置。

### ComfyUI（阶段二 生图）

1. 下载 ComfyUI Windows 整合包（portable），解压到任意目录；
2. 安装 GGUF loader 等自定义节点（UNETLoader GGUF 支持）；
3. `models/diffusion_models/` 放生图模型：`qwen-image-2.1-UC-*.safetensors`（或 .gguf）；
   `models/text_encoders/` 放 `qwen3vl_8b_int8_convrot.safetensors`；
   `models/vae/` 放 `qwen_image_2.1_vae_bf16.safetensors`；
4. 目录填进 `config.local.json`。网页「输出目录与生图参数」里的生图/文本编码器/VAE
   下拉即从此目录扫描。

### prompt-writing skill

把作者机器的 `qwen-image-21-prompt-writing` skill 目录原样拷贝到新机器
（llm_caption 运行时实时读取该目录的模板与契约说明），路径填 `pi_skill_dir`。

## 4. 验证

1. 网页右下健康栏显示 LLM / ComfyUI「未启动」属正常（懒启动）；
2. 导入几张图 → 「加入并开始」→ 队列页应依次出现：提示词提取 → 生图 → 完成；
3. 可选跑测试（不触碰模型，全部桩掉）：

```
.venv\Scripts\python -m pytest tests\ -q        # 需 pip install -r requirements-dev.txt
```

## 5. 已知迁移限制

- **旧工作区数据不建议整包拷贝**：task.json / workspace.json 记录的是**绝对路径**
  （原图、输出目录），换机器后盘符不同则旧任务只读回看受限。新机器请指定新的
  工作区目录从头建；
- 服务器端「选择…」目录框依赖桌面环境；无桌面的机器会退化为日志提示，请手输路径；
- 端口占用：9178（网页）/ 1234（llama-server）/ 8188（ComfyUI），
  后两者可在 config.local.json 改 `lm_api_base` / `comfy_api_base`。
