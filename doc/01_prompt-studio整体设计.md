# 01 · Prompt Studio 整体设计

> 项目代号：**prompt-studio**
> 位置：`D:\work_space\ai\llm\qwen_28b\prompt-studio\`（git 管理）
> 版本：v1.1 定稿（2026-10-10，需求经用户逐项确认，三个待定项已决策，见 §12）
> 状态：**设计定稿，即将进入 M1 开发——本文档撰写阶段未启动任何模型服务**

---

## 1. 背景与目标

用户已在本机验证过"本地语言模型出提示词 → ComfyUI 文生图"的单张链路（akane 案例）。
本项目把该链路封装为**带网页界面的本地批处理服务器**：

1. 用户指定一个**文件夹或压缩包**（zip/7z），内含任意数量图片（按自然顺序编号）；
2. **阶段一（caption）**：将每张图片按顺序发给本地语言模型（qwen3.8-27b IQ2_M 64K 视觉档），
   按 `qwen-image-21-prompt-writing` skill 输出契约格式的提示词（`rewritten_prompt` + `wh_ratio`），
   逐张落盘保存；
3. **阶段二（generate）**：将全部提示词发给 ComfyUI（Qwen-Image-2.1，UC 模型，指定工作流参数），
   逐张文生图，产物收进输出目录；
4. 网页全程展示进度；支持**审阅闸口**（先看/改提示词再生图）、**单张修补重跑**、**一键全自动**。

## 2. 调研结论（为什么不复用现成项目）

| 类型 | 代表 | 缺口 |
|---|---|---|
| 独立批量 caption 工具 | LM_Studio_Image_Captioner 等 | 只做阶段一，输出自由文本，不认契约 JSON / wh_ratio，不接 ComfyUI |
| ComfyUI 内 LLM 节点 | TA Directory Captioning、comfyui-lmstudio、ComfyUI_VLM_nodes、ComfyUI-GGUF-VLM | 无法做"先全部 caption→再全部生图"两阶段编排、逐图不同宽高比、契约解析、网页任务台 |
| LoRA 训练 caption 生态 | 各类 tagger | 目标是训练标签，非生图提示词 |

结论：自建轻量服务器。两大技术难点均已在本机验证通过（见 §3）。

## 3. 本机设施对接事实（已验证）

### 3.1 语言模型端（阶段一）

| 项 | 值 |
|---|---|
| 启动脚本 | `D:\work_space\ai\llm\qwen_28b\start-qwen-iq2-m-64k.cmd`（64K 档，IQ2_M） |
| 服务程序 | bonsai PrismML `llama-server.exe`（LM Studio 自带的是协议壳，不可用） |
| API 端点 | `http://127.0.0.1:1234/v1/chat/completions`（OpenAI 兼容） |
| 模型 alias | `qwen3.8-27b-uncensored` |
| 视觉输入 | 已配 `--mmproj`，图片走 base64 data URL（content 数组 `image_url` 形式） |
| 显存占用 | ~14.5 GiB |
| 性能参考 | prefill ~1550 tok/s，解码 ~81 tok/s |

**skill 注入方式（本设计的核心论点）**：skill 本质是文本文件，位于
`C:\Users\admin\.pi\agent\skills\qwen-image-21-prompt-writing\`：

- `SKILL.md` — 契约说明与方法论
- `reference/system_prompt_t2i.txt` — 官方改写系统提示词（caption 任务的核心）
- `reference/qwen-official-README.md` — 官方方法论

pi 只是"注入 SKILL.md + 让模型用工具读 reference/"。本服务器**在构建请求时把这些文件
内容拼接为 system prompt 一次性注入**，与 pi 同源且不依赖模型主动读文件。
官方 skill 自带的 `reference/pe_core.py` 证明该范式即 Qwen 官方 prompt-rewrite 服务的做法。

**已知坑（必须处理）**：
- qwen3.5 混合架构的 `<think>` 与正文共用 `max_tokens` 预算 → 每请求 max_tokens 给足
  （建议 ≥8192），并对响应做 `<think>` 剥离后再解析 JSON；
- 契约 JSON 字符串内可能含裸换行 → 用宽容解析（`json.loads(strict=False)`）；
- 解析失败视为该张失败，走重试。

### 3.2 ComfyUI 端（阶段二）

| 项 | 值 |
|---|---|
| 整合包 | `D:\Program Files\Comfyui-WF-2026.8.8`（core 0.37.4） |
| 启动命令 | 在 `ComfyUI` 目录：`python\python.exe -s main.py --disable-auto-launch`，约 50 秒就绪 |
| API | `POST http://127.0.0.1:8188/prompt`（API 格式图）+ `GET /history/{prompt_id}` 轮询 |
| DiT | `qwen-image-2.1-UC-int8_convrot.safetensors`（UC 无审查版） |
| 文本编码器 / VAE | `qwen3vl_8b_int8_convrot.safetensors` / `qwen_image_2.1_vae_bf16.safetensors` |
| API 图结构 | UNETLoader → TextEncodeQwenImage21(`resolution=1024`, negative="") + CLIPLoader(`type="qwen_image"`) + EmptyLatentImage + KSampler(euler/simple/cfg 1/steps 32) + VAEDecode + SaveImage |
| 单张耗时 | 1MP / 32 步 / RTX 5080 ≈ 12 秒 |
| 参考实现 | `D:\work_space\ai\llm\qwen_28b\comfy_run_akane_001\queue_two.py` |

**wh_ratio → 像素换算**：复刻 `ResolutionSelector` 算法（1MP、multiple=8）：
`scale = sqrt(1048576 / (w×h))`，`width = round(w×scale/8)×8`，height 同理。
已验证：9:16 → 768×1368；4:5 → 912×1144；1:1 → 1024×1024。

### 3.3 显存硬约束 → 两阶段强制串行

iq2_m（~14.5 GiB）与 ComfyUI DiT+编码器（~8 GiB）无法同时在 16G 卡上驻留。
因此：**阶段一结束 → 关闭本服务器代管的 llama-server → 启动 ComfyUI → 阶段二 → 完成后默认关闭 ComfyUI**。
若检测到端口已被用户手动占用（非本服务器拉起的进程），**只复用、不关闭**。

## 4. 总体架构

```
浏览器 http://127.0.0.1:9178（已避开本机在用的 1234/8080/8188）
   │ 任务创建 / 参数配置 / SSE 进度 / 审阅操作 / 结果画廊
   ▼
本地服务器（Python 3 + FastAPI + uvicorn，单进程；前端为原生 HTML/JS 单页，无构建步骤）
   ├─ 输入模块：扫描文件夹（自然排序）/ 解压 zip（内置 zipfile）与 7z（py7zr）
   ├─ Caption Worker：llama-server 生命周期代管 + skill 拼接 + 契约校验 + 落盘
   ├─ ComfyUI Worker：ComfyUI 生命周期代管 + 逐张 /prompt + /history 轮询 + 收图
   ├─ 状态机：每个图片条目 pending → captioned → generating → done / failed
   └─ 静态资源与图片服务（原图缩略、生成图、提示词 JSON）
```

## 5. 网页界面设计

### 5.1 任务页（首页）

- **输入源**（三选一）：
  - 「选择文件夹」按钮 → 服务器端弹出原生目录选择框（独立进程 tkinter，避免阻塞事件循环），回填路径；同时提供手填路径输入框；
  - 上传 zip/7z（multipart，服务器解压到任务临时目录）；
- **参数面板**（默认值见 §9，全部可改，折叠式）：
  - 生图：步数 / 采样器 / 调度器 / cfg / 目标百万像素 / 种子策略（同种子 / 每图随机）
  - 模型档位：v1 固定 iq2_m 64K 档；q4 档留配置位不做 UI
  - 输出目录：默认 `prompt-studio/outputs/<任务名时间戳>/`
- **两个启动按钮**：
  - 「提取提示词」——只跑阶段一，停在审阅页；
  - 「全自动（提取+生成）」——跳过审阅闸口一路跑完。

### 5.2 审阅页（阶段一完成后自动停在此）

- 图片缩略图网格（自然排序编号 0001、0002…），每张带状态角标（已提取 / 失败 / 已生成）；
- **点击单张** → 侧栏/弹窗：大图 + 对应契约提示词（`rewritten_prompt` 可编辑文本框 + `wh_ratio` 显示），
  三个操作：
  1. **[重新生图]**——用当前文本直接提交 ComfyUI 生成本张；
  2. **[加注释重新提取]**——输入注释，连同原图+原契约发回 LLM 重新产出契约，更新后自动重生本张；
  3. 状态信息（耗时 / 失败原因）。
- 顶部操作条：**[全部生成]**（全部入队 ComfyUI）、失败项筛选、重试失败项。

### 5.3 进度与结果

- 阶段进度条（N/M、当前文件名、预计剩余时间）、可展开实时日志；
- 结果页：原图 ↔ 生成图左右对照缩略图；单张可「重跑」；
- 生成完成后显示"ComfyUI 已关闭"状态（若为本服务器代管拉起）。

## 6. 状态机与失败策略

```
图片条目状态：pending → captioning → captioned → generating → done
                 ↘ caption_failed        ↘ generate_failed
```

- 单张 caption 失败：重试 2 次（含一次温度调高重试）→ 标记 failed，**继续下一张**；
- 单张生图失败：重试 1 次 → 标记 failed，继续；failed 项在网页可单独重跑；
- llama-server / ComfyUI 健康检查：启动后轮询就绪端点（1234 `/v1/models`、8188 `/system_stats`），
  超时（各 3 分钟）报错并停止任务；
- 任务级中止按钮：终止当前请求 + 关闭本服务器拉起的服务进程。

## 7. 输出目录结构（每图一一对应，可断点续跑）

```
outputs/<任务名时间戳>/
  source/                  ← 解压/链接过来的原图（按序重命名 0001_原文件名.ext）
  prompts/0001.json        ← {"file":"0001_原文件名.ext","rewritten_prompt":"…","wh_ratio":"9:16"}
  prompts_summary.jsonl    ← 全部契约汇总（追加式）
  images/0001.png          ← 生成图（与原图同编号）
  task.json                ← 任务元数据：参数、状态、时间、失败清单
```

## 8. 后端 API 草案

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/tasks` | 创建任务（源路径或已上传包 + 参数）→ task_id |
| POST | `/api/upload` | 上传 zip/7z |
| POST | `/api/tasks/{id}/caption` | 启动阶段一（全部 pending 项） |
| POST | `/api/tasks/{id}/generate` | 启动阶段二（body: 全部 / 指定编号列表） |
| GET  | `/api/tasks/{id}/events` | SSE：进度、日志、状态变更 |
| GET  | `/api/tasks/{id}/items` | 条目列表（缩略图 URL、提示词、状态） |
| PUT  | `/api/tasks/{id}/items/{n}` | 修改某张的契约提示词 |
| POST | `/api/tasks/{id}/items/{n}/recaption` | 带注释重新提取该张 |
| POST | `/api/tasks/{id}/items/{n}/generate` | 单张生图 |
| POST | `/api/tasks/{id}/stop` | 中止任务 |
| GET  | `/api/browse/dir` | 服务器端弹原生目录选择框，返回路径 |

## 9. 默认参数表

| 参数 | 默认值 | 来源 |
|---|---|---|
| LLM 档位 | iq2_m 64K 视觉档（脚本启动） | 用户双档方案中的贴图档 |
| max_tokens（caption 请求） | 16384（给足，防 think 吃满截断） | 2026-10-09 截断教训 |
| 生图步数 | 32 | akane 实跑 |
| 采样器 / 调度器 | euler / simple | 同上 |
| cfg | 1（负向提示词留空） | 同上 |
| 目标像素 | 1.0 MP（尺寸由 wh_ratio 换算） | 同上 |
| 种子策略 | 全任务同种子（默认 593103825222985）；单张重跑默认换新种子 | 对比公平性 + 重跑求变化 |
| TextEncodeQwenImage21.resolution | 1024 | 官方模板（仅影响参考图，t2i 无关） |
| filename 前缀 | 任务编号 | — |

## 10. 代码目录结构（规划）

```
prompt-studio/
  doc/                    ← 设计文档（本文件所在）
  server/
    main.py               ← FastAPI 入口 + 路由
    state.py              ← 任务/条目状态机
    input_ingest.py       ← 文件夹扫描、zip/7z 解压、自然排序
  llm_caption.py        ← llama-server 代管 + skill 拼接（实时读取 pi skill 目录）+ 契约请求/解析
  comfy_client.py       ← ComfyUI 代管 + API 图构建 + 轮询
  sse.py                ← 进度事件广播
  web/
    index.html / app.js / style.css
  start-server.cmd        ← 一键启动（含依赖安装检查）
  requirements.txt        ← fastapi uvicorn py7zr httpx pillow
```

> skill 读取策略（已定）：**实时读取** `C:\Users\admin\.pi\agent\skills\qwen-image-21-prompt-writing\`，
> 用户在 pi 侧更新 skill 后本服务器下次构建请求即生效；路径做成配置项，不拷贝快照。

## 11. 实施里程碑

| 阶段 | 内容 | 验收 |
|---|---|---|
| M1 骨架 | FastAPI + 静态页 + 输入摄取（文件夹/zip/7z、排序、缩略图） | 网页能看到图片网格 |
| M2 caption 管道 | llama-server 代管 + skill 注入 + 契约校验落盘 + SSE 进度 | 手动起 llama-server 后 10 张图全部出契约 JSON |
| M3 审阅页 | 点击看图/改词/单张重提 | 修改一张的提示词并保存生效 |
| M4 生图管道 | ComfyUI 代管 + 批量 /prompt + 收图对照 | 全自动跑通 10 张端到端 |
| M5 打磨 | 失败重试、单张注释重提、中止、断点续跑、日志 | 100 张规模稳定性 |

## 12. 决策记录（2026-10-10 用户逐项确认）

| 项 | 决策 |
|---|---|
| 项目名与位置 | `prompt-studio` @ `D:\work_space\ai\llm\qwen_28b\prompt-studio\`，git 管理，模型/输出等大文件不追踪 |
| skill 读取策略 | **实时读取** pi skills 目录，路径做配置项，不拷贝快照 |
| Web 端口 | **9178** |
| 大图量加载 | v1 缩略图懒加载，分页视实际需要 |
| q4 高质量档 | v1 不做 UI，固定 iq2_m 64K 档 |
| 任务历史 | v1 单活动任务 + 历史目录只读回看，多任务并行留待 v2 |
| **模型运行批准** | **开发/测试期间运行 llama-server 与 ComfyUI 必须经用户批准；网页上的「提取提示词/生成」按钮由用户点击，视为批准动作** |
