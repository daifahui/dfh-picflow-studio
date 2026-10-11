# 01 · Prompt Studio 整体设计

> 项目代号：**prompt-studio**
> 位置：`D:\work_space\ai\llm\qwen_28b\prompt-studio\`（git 管理）
> 版本：v1.4（2026-10-11，新增 §18 模型选择 / 人物资产 / 换脸；v1.3 新增 §16/§17）
> 状态：**v2.0（工作区+队列+AI 命名+tags）已实测通过；§18 三特性已评审定稿，本轮实施**

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
  - 输出目录（v1.1，§16.2）：**图片输出**与**提示词输出**两个路径可分别指定；
    默认图片输出 `outputs/<任务名>/`，提示词输出 = 图片输出目录内的 `prompts/` 子目录
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
outputs/                      ← 工作区（v1.1 固定为 outputs/；v2.0 起可指定，见 §16.4）
  <任务名>/
    0001.png …                ← 图片输出目录（可整体另指定到任意位置，§16.2）
    prompts/0001.json         ← 提示词输出目录；默认 = <图片输出>/prompts/（可另指定）
    prompts_summary.jsonl     ← 全部契约汇总（追加式）
    .studio/                  ← 内部簿记（用户无需关心）
      source/                 ← 解压/链接来的原图（按序重命名 0001_原文件名.ext）
      thumbs/                 ← 缩略图缓存
      task.json               ← 任务元数据：参数、状态、时间、失败清单、双输出目录
```

> 兼容：v1.0 旧布局（`images/` 与 `prompts/` 平级于任务目录下）只读回看不受影响；
> task.json 缺新字段时按旧布局解释。

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

> v2.0 起本表全部端点 id 化（`/api/task/*` → `/api/tasks/{id}/*`）并新增工作区/队列端点，见 §16.4；本表为 v1 现行语义。

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

## 10. 代码目录结构（现状）

```
prompt-studio/
  doc/                    ← 设计文档（本文件所在）
  server/
    main.py               ← FastAPI 入口 + 路由 + 两阶段 worker
    config.py             ← 外部设施路径、端点、默认参数
    state.py              ← 任务/条目状态机（task.json 持久化、断点回看）
    input_ingest.py       ← 文件夹扫描、zip/7z 解压、自然排序
    llm_caption.py        ← llama-server 代管 + skill 拼接（实时读取 pi skill 目录）+ 契约请求/解析
    comfy_client.py       ← ComfyUI 代管 + API 图构建 + 轮询收图
    sse.py                ← 进度事件广播（线程安全）
  web/
    index.html / app.js / style.css   ← 原生单页，无构建
  tests/                  ← pytest 测试套（见 §14）
    conftest.py           ← 模型桩（铁律：测试绝不拉起真实模型）
    test_input_ingest.py / test_state.py / test_sse.py
    test_llm_caption.py / test_comfy_client.py / test_api.py
    test_e2e_web.py       ← Playwright 真浏览器测试
  logs/                   ← llama-server / ComfyUI 代管日志（gitignore）
  start-server.cmd        ← 一键启动（含依赖安装检查）
  requirements.txt        ← fastapi uvicorn httpx pillow py7zr python-multipart
  requirements-dev.txt    ← pytest pytest-cov respx playwright
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
| M6 v1.1 增强 | 输出双目录 + 预览增强（§16.2/§16.3） | 指定双输出目录跑通单任务；lightbox / 视图切换 / 渐进效果生效 |
| M7 v2.0 工作区 | 工作区 + 任务队列 + AI 自动命名（§16.4） | ≥3 个压缩包全自动串行跑完且服务各只启动一次；审阅闸任务正确暂停/恢复；断点回看按工作区索引 |

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
| **输出目录（2026-10-10 晚新增）** | 图片输出与提示词输出可分别指定；默认提示词 = 图片输出目录内 `prompts/` 子目录（§16.2） |
| **工作区 + 队列（2026-10-10 晚新增）** | 多压缩包批量导入、逐任务命名（AI 自动命名可选）、队列视图，立项 v2.0（§16.4）；队列执行策略推荐「阶段批处理」（建议，实施前确认） |
| **渐进效果（2026-10-10 晚新增）** | 只做 CSS 层（提示词打字机浮现、图片 blur-in）；真实「生成中」过程预览（ComfyUI websocket）不纳入（建议，实施前确认） |
| **标签 tags（2026-10-10 深夜新增）** | caption 契约一并产出英文短语标签（4~12 个，不额外调用 LLM、不参与生图）；落盘契约 JSON + LoRA 惯例 sidecar `tags/NNNN.txt`；侧栏可编辑（§17） |

---

## 13. 技术选型与协议栈（2026-10-10 调研定稿）

### 13.1 选型结论与依据

| 层 | 选型 | 依据 |
|---|---|---|
| 后端框架 | **FastAPI + uvicorn**（已在用） | 原生异步 + SSE 支持好；[官方测试指南](https://fastapi.tiangolo.com/advanced/async-tests) 完善 |
| 外呼 HTTP | httpx（同步） | worker 跑在线程里；[respx](https://marcelotryle.com/blog/contract-testing) 可完整 mock |
| 前端 | **原生 HTML/CSS/JS 单页，无构建** | 单页三视图 ~250 行，引入框架（Vue/React+Vite 需构建链，Alpine.js ~15KB/petite-vue ~6KB）收益低于维护成本；[HN 社区对小项目的共识](https://news.ycombinator.com/item?id=29502633)即"vanilla 优先"。观察点：app.js 超过 ~500 行再考虑 Alpine.js |
| 后端测试 | **pytest + pytest-cov + respx + starlette TestClient** | FastAPI 生态标准路线；respx 拦截 httpx 外呼，模型服务零触达 |
| 网页测试 | **Playwright**（channel=`msedge`，免下载 Chromium） | 无构建页面可直接 E2E；能抓路由/方法级真实 bug（本项目已验证，见 §14.4） |

### 13.2 协议栈

```
浏览器 (Edge/Chrome)
  │  REST JSON over HTTP/1.1 (127.0.0.1:9178)
  │  + SSE text/event-stream（进度/日志/条目状态推送，15s 心跳保活）
  │  + multipart/form-data（zip/7z 上传）
  ▼
FastAPI 后端 (server/)
  │  OpenAI 兼容 POST /v1/chat/completions（图片走 base64 data URL）
  └──────────────► llama-server :1234（阶段一；两阶段互斥串行）
  │  JSON API 图 POST /prompt + GET /history/{id} 轮询 + GET /view 取图
  └──────────────► ComfyUI :8188（阶段二）
```

## 14. 测试体系（已建成，76 用例全绿）

### 14.1 铁律

**任何测试不得拉起真实 llama-server / ComfyUI。** 全部模型触点
（`ensure_llm / caption_image / stop_llm / ensure_comfy / generate_one / stop_comfy`）
在 `tests/conftest.py`（接口级）与 `test_e2e_web.py` 的 `server` fixture（模块级）中打桩。
历史上 E2E 曾因漏桩真的拉起过 llama-server，已修正为模块级强制打桩。

### 14.2 测试分层

| 层 | 文件 | 覆盖 |
|---|---|---|
| 单元 | test_input_ingest / test_state / test_sse | 自然排序、zip 防护、状态机持久化/断点回看、SSE 线程投递 |
| 单元 | test_llm_caption / test_comfy_client | 契约解析（think 剥离/裸换行/坏 JSON 重试）、wh_ratio 换算参数化、API 图结构断言、respx mock 的 /prompt→/history→/view 全流程与错误路径 |
| 接口 | test_api（TestClient） | 任务创建（文件夹/zip/非法源/409）、上传、fake worker 全流程状态机（auto: caption→reviewing→generate→done + 落盘断言）、条目编辑、recaption 传注、停止、图片服务 MIME 与 404 |
| E2E | test_e2e_web（Playwright + 真实 Edge） | 三视图切换、参数表单默认值、SSE 事件驱动进度条（route 注入事件流）、真后端桩模型全链路（点击提取→网格渲染已提取）、侧栏编辑保存持久化、全部生成→结果页对照 |

### 14.3 运行

```
.venv\Scripts\python -m pytest tests\ -q                 # 全量（≈15s）
.venv\Scripts\python -m pytest tests\ --cov=server       # 覆盖率（当前 86%）
```

注：SSE 端点的无限流在 TestClient 下无法优雅关闭（会挂死），其全链路行为
由 E2E 真浏览器测试覆盖，广播逻辑由 test_sse 单元测试覆盖。

### 14.4 测试已发现并修复的真实缺陷（价值证明）

1. `llm_caption.py`/`comfy_client.py` 误引 `sse.publish`（应为 `sse.hub.publish`）→ 后端测试抓出；
2. E2E：侧栏 `<aside class="hidden">` 的 `display:none !important` 使详情侧栏永远无法弹出 → 真实用户可感知的前端 bug；
3. E2E：前端「保存修改」用 POST 而后端只注册 PUT → 405，保存静默失败；
4. `wh_ratio "0:0"` 触发 ZeroDivisionError 而非 ValueError → 补防御；
5. 真实端到端验收（2026-10-10 晚）：llama-server 端 stb_image 解码不了 webp，6 张全部提取失败
   → 发送前用 Pillow 把非 PNG/JPEG 格式转 PNG（并补单元测试）；
6. 真实端到端验收（同日）：generate 是否关闭代管 llama-server 曾由请求参数 `after_caption`
   决定而前端从不传 → 显存切换从未发生；改为按进程所有权判断（`owns_llm()`）；
   另修阶段终态不落盘 task.json。

### 14.5 测试期外部设施影响面

- 端口占用：E2E 用 9189/9190 等独立端口，不与 9178/1234/8188 冲突；
- llama-server 误启动事件（已处置）：一次 E2E 漏桩导致 llama-server 被拉起，
  发现后已 taskkill 并回填模块级桩；处置记录见 git 历史。

## 15. 缺口清单（当前确认缺少/待办）

| # | 缺口 | 影响 | 计划 |
|---|---|---|---|
| 1 | **真实模型端到端验收**（真 caption 一批图 + 真生图闭环） | 未验证 skill 注入的实际产出质量与显存串行切换 | **待用户批准后执行**（M2/M4 验收） |
| 2 | E2E 未覆盖：文件上传控件、服务器端 tkinter「选择文件夹」弹窗（需人工交互） | 上传路径仅接口级覆盖 | 低优先；上传可用 Playwright set_input_files 补 |
| 3 | 服务级故障注入未做（llama-server 启动超时、ComfyUI 中途崩溃） | 失败重试逻辑仅桩级验证 | v1.1 补 respx 超时用例 |
| 4 | 前端无框架，状态与 DOM 手动同步 | 超 ~500 行后维护成本上升 | 观察点；超阈值引入 Alpine.js |
| 5 | 无 CI / pre-commit 钩子 | 回归靠手动跑 | 可加 local hook：commit 前跑 pytest |
| 6 | `@app.on_event("startup")` 已被 FastAPI 标记 deprecated | 未来版本兼容 | 迁移到 lifespan（顺手改） |
| 7 | q4 档 UI、任务历史管理页 | 功能完整度 | v2；多任务队列已提前立项为 §16 M7 |
| 8 | 真实「生成中」渐进预览（ComfyUI websocket 预览通道） | 生成过程可视化 | 观察项，工作量大收益小，暂不排期（§16.3） |

---

## 16 · v1.1 / v2.0 改造评估（2026-10-10 晚，用户需求评审）

用户提出三类改进：①输入/输出目录可指定（图片输出、提示词输出分离）；②工作区 + 任务队列
（多压缩包批量导入、AI 自动命名、队列进度视图、渐进展示效果）；③预览增强（对比图点击放大、
资源管理器式视图切换）。评估结论：**①③小改低险，归入 v1.1（M6）先做；②是架构级改动
（单任务 → 多任务队列），正是 §15 缺口 #7 预留的 v2 范围提前立项，归入 v2.0（M7）**。

### 16.1 需求清单与评估结论

| # | 需求 | 评估 | 规模/风险 | 批次 |
|---|---|---|---|---|
| R1 | 输入仍为文件夹/zip/7z；**图片输出**与**提示词输出**两目录可分别指定，默认提示词 = 图片输出目录内 `prompts/` | `create_task` 增两个可选参数 + `_safe_file` 白名单扩容 + task.json 增字段（旧文件兼容） | 小 / 低 | **v1.1（M6）** |
| R3 | 详情侧栏对比图点击放大（lightbox）；网格右上角视图切换（大/中/小图标、列表、详细信息，仿 Win10 资源管理器）；美观性渐进效果 | 纯前端；网格加密度 class + localStorage 记忆；渐进效果仅 CSS 层零性能成本 | 小 / 低 | **v1.1（M6）** |
| R2 | **工作区（workspace）+ 任务队列**：多压缩包/文件夹批量导入、逐任务命名（AI 自动命名可选）、队列视图（进行到哪个包哪张图、进度）、输出收进工作区 | TaskManager 单任务 → WorkspaceManager 多任务 + 队列 worker + SSE 加任务作用域 + API 全面 id 化 + 网页信息架构重排 | 大 / 中 | **v2.0（M7）** |

### 16.2 R1：输出双目录（v1.1）

- API：`POST /api/task` 增 `output_image_dir` / `output_prompt_dir`（均可空 = 默认）。
- 默认：图片输出 `outputs/<任务名>/`；提示词输出 `<图片输出>/prompts/`。
  只指定图片目录时，提示词同样落到其 `prompts/` 子目录；两者都指定则完全独立。
- 内部簿记（source / thumbs / task.json / prompts_summary.jsonl）收进任务工作目录 `.studio/`，
  产品目录只留图和 prompts（§7 新布局）。
- 安全：`_safe_file` 允许根从「source_root ∪ 任务目录」扩为「∪ 图片输出目录 ∪ 提示词目录」，
  路径须为绝对路径且不存在目录逃逸。
- 兼容：旧 `outputs/` 目录按 v1.0 布局只读回看。

### 16.3 R3：预览增强（v1.1）

- **lightbox**：侧栏「原图 / 生成图」点击 → 全屏遮罩看原图分辨率，Esc / 点击关闭，滚轮缩放可选。
- **视图切换**：网格区右上角下拉（仿资源管理器「查看」）：大图标（现状）/ 中图标 / 小图标 /
  列表 / 详细信息（表格：文件名、状态、wh_ratio、提取与生成耗时）；选择存 localStorage。
- **渐进效果**（用户定位为纯美观，不影响性能才做——结论：CSS 层可做，零成本）：
  - 提示词浮现：`item` 事件到达时在侧栏/队列行按打字机式渐显（单张契约 ≤3k 字符，rAF 实现）；
  - 图片 blur-in：缩略图加载完成前以现有 `thumbs/` 做 LQIP 占位（blur + 降饱和），
    onload 后 ~200ms 过渡清晰；
  - **真实「生成中」过程预览不纳入**：需接 ComfyUI websocket 二进制预览通道，
    改动深、收益低，列为 §15 缺口 #8 观察项。

### 16.4 R2：工作区与任务队列（v2.0）

**概念**：
- **工作区**：一次批量作业的容器，一个真实目录（默认 `outputs/`，可指定），内含
  `workspace.json`（队列与任务索引）+ 每任务一个产品目录（§7 布局）。
- **队列项 = 任务**：来源（文件夹或 zip/7z，可批量导入多个）+ 任务名 + 输出目录（默认在工作区内）
  + 执行模式（**全自动** / **停在审阅**）。
- 执行仍受显存互斥硬约束 → **全局单 worker 串行**，队列只是把「什么时候跑哪个」显式化。

**队列执行策略（关键决策）**：

| 策略 | 流程 | 外部服务启动次数 | 结论 |
|---|---|---|---|
| A 逐任务两阶段（现状推广） | 任务1 caption→generate → 任务2 … | 2N（每任务各起一次 LLM + ComfyUI，每次 ~30-60s） | N 大时启动开销不可接受，不采用 |
| B **阶段批处理（推荐默认）** | 全部任务 caption（LLM 只起一次）→ 逐任务审阅闸（仅「停在审阅」模式）→ 全部 generate（ComfyUI 只起一次） | 2 | 批量场景最优；审阅集中在提取后进行 |

- 队列任务状态机：`queued → captioning → reviewing（闸）→ generating → done / failed / stopped`；
- 队列级控制：开始 / 暂停（当前张完成后停）/ 取消单项 / 重跑失败项；
- **AI 自动命名**：导入时以包名/文件夹名为占位名；该任务首张契约产出后，用一次轻量 LLM 请求
  （复用 caption 已就绪的 llama-server，~2s）生成 4-12 字名称并重命名产品目录；
  用户手动改过名则跳过；LLM 不可用时保留占位名。可设置关闭。

**代码与数据影响**：
- `state.py`：`TaskManager`（单任务）→ `WorkspaceManager`（任务表 + 队列游标 + 全局 worker）；
- `task.json` 增 `workspace` / `output_image_dir` / `output_prompt_dir` / `queue_state` 字段，
  旧文件按「单任务工作区」解释（兼容读）；
- SSE：所有事件加 `task_id` 作用域，前端按任务分发渲染；
- 取消/暂停语义跨任务叠加，`_Cancelled` 路径需按队列重新推演（现实现按单任务写死）；
- `app.js` 现 ~240 行，队列改造后必破 500 行 → 触发 §13 观察点，引入 Alpine.js。

**API（v2.0 草案，§8 全表 id 化）**：

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/workspace` | 设置/创建工作区目录 |
| GET | `/api/workspace` | 工作区信息 + 队列列表（各任务进度、当前文件） |
| POST | `/api/queue/items` | 批量导入（多个文件夹/压缩包 + 各自名称、模式、输出） |
| POST | `/api/queue/start` · `/api/queue/pause` | 队列级控制 |
| POST | `/api/tasks/{id}/caption` · `/generate` · `/stop` | 原 `/api/task/*` 全部 id 化 |
| PUT/POST | `/api/tasks/{id}/items/…` | 条目操作 id 化（语义同 §8） |

**网页信息架构**：
- 视图①任务 → **队列**：顶部工作区路径 + 拖拽/多选导入区；队列表
  （任务名[可编辑 + AI 命名按钮] / 来源 / 模式 / 状态 / 进度条 / 当前图片 / 操作）；
  底部全局 开始·暂停·停止；
- 视图②审阅/进度：点队列行进入该任务审阅页（现有布局复用，加任务切换返回）；
- 视图③结果：工作区级画廊，按任务筛选；侧栏对比 + lightbox + 视图切换（R3 产物直接复用）。

### 16.5 测试影响

- M6：新增输出目录参数校验、`_safe_file` 扩容后放行/拒绝、旧布局回看兼容用例；
  lightbox/视图切换补 Playwright 断言；
- M7：队列状态机（含暂停/恢复/取消叠加）为测试重点；conftest 桩不变，
  队列 worker 接入前先确认桩生效（§14.1 铁律）；E2E 补「多包导入 → 队列跑完」全链路（桩模型）。

### 16.6 风险清单

1. 队列取消/暂停语义跨任务叠加，是 M7 最大复杂度来源；
2. 输出目录开放后 `_safe_file` 白名单与路径规范化要防目录逃逸；
3. AI 命名依赖 LLM 就绪，纯生成队列（LLM 未起）时回退占位名；
4. 旧 task.json / 旧 outputs 布局兼容：`load_latest` 须容忍缺字段、缺目录；
5. 事件加 `task_id` 后，SSE 广播结构与前端分发逻辑同时改，需一次迁移到位避免中间态。

---

## 17 · 标签（tags）功能设计（2026-10-10 深夜，用户需求新增）

> 背景：图片处理工作流除了改写提示词，还需要给每张图打标签（分类检索、LoRA 训练
> 配套等）。设计原则：**不增加 LLM 调用次数**——tags 在既有 caption 请求的契约里
> 一并产出；tags 不参与生图（rewritten_prompt 才是生图输入），纯元数据。

### 17.1 契约扩展

| 字段 | 类型 | 说明 |
|---|---|---|
| `tags` | `string[]` | **4~12 个英文小写短语标签**（LoRA / SD 生态惯例），覆盖主体、服装、动作、场景、风格、光影；caption 请求的 user_text 中明确要求输出 |

解析与规范化（`_parse_contract`）：
- 容忍三种形态：JSON 数组 / 逗号分隔字符串 / 缺省 → 统一规范化为字符串数组；
- 每项 strip、去空、去重、转小写；单项 ≤40 字符，整体 ≤12 个（超出截断）；
- tags 解析失败不视为该张失败（空数组兜底），不触发重试。

### 17.2 落盘布局

```
<提示词输出目录>/
  0001.json               ← 契约 JSON（新增 "tags": [...] 字段）
  tags/0001.txt           ← sidecar：逗号+空格连接的标签串（LoRA 训练惯例，
                            与图同名成对，可直接喂给打标训练管线）
  prompts_summary.jsonl   ← 汇总行带 tags
```

### 17.3 编辑与展示

- 详情侧栏新增「标签」输入框（逗号分隔编辑），与 prompt/ratio 一起走
  `PUT /api/tasks/{id}/items/{seq}` 保存；保存后**同步重写 sidecar txt**；
- 注释重提（recaption）与断点续跑自然产出新 tags，无需特殊处理；
- tags 不下发 ComfyUI，生成链路零改动。

### 17.4 验收

- caption 后：0001.json 含合法 tags 数组、tags/0001.txt 存在且内容一致；
- 手动改 tags 保存：json 与 txt 同步更新；
- 契约缺 tags / 坏 tags：不阻塞提取（空数组兜底）。

---

## 18 · 模型选择 / 人物资产 / 换脸（2026-10-11 用户需求评审，v1.4 定稿）

### 18.0 需求与评估结论

| # | 需求 | 评估 | 规模/风险 |
|---|---|---|---|
| A | **模型选择进界面**：LLM 档位（后续多模型）与 ComfyUI 生图模型可选 | LLM 双档脚本已在（iq2_m 64K / q4_k_m）；生图模型从 ComfyUI `/object_info/UNETLoader` 动态拉取，未运行时回退静态目录扫描 | 小-中 / 低 |
| B | **人物资产生成**：多图输入（人物 + 衣服 + 多张参考），提示词开放可编辑（有默认），生成人物资产图 | 复用 Qwen-Image-2.1 官方 edit 管线；任务粒度从「每图一产物」变为「一组图一产物」 | 大 / 中 |
| C | **换脸**：与 B 同构（主图 + 多张目标脸，有数量上限），仅默认提示词不同 → 需要**多默认提示词模板**机制 | 与 B 共用 multi 管线，模板按任务类型区分 | 小（随 B）/ 低 |

**结论**：A 独立小改；B/C 合并为「多图编辑类任务」一个大特性，与现有 t2i 并列。
版本 v2.1。

### 18.1 已验证的对接口径（本轮实测探明）

- `TextEncodeQwenImage21`（comfy_extras/nodes_qwen.py）：autogrow 输入 `images.image_1..image_16`
  ——**参考图硬上限 16 张**，按编号排序拼入视觉序列，并输出「参考 latent」（第三个输出口）；
  参考图按 resolution 预算（1024）缩放。
- edit 等价 API 图：`UNETLoader → QwenImage21Cache(device=auto) → KSampler.model`；
  `TextEncodeQwenImage21(clip, vae, prompt, negative_prompt, resolution, images.image_N)`
  的 latent 直连 KSampler.latent_image（官方模板 custom_size=false 路径，输出尺寸跟随参考图）；
  KSampler(euler/simple/cfg1/模板默认 25 步) → VAEDecode → SaveImage。
- 参考图需先 `POST /upload/image` 传入 ComfyUI input 目录，LoadImage 引用返回的文件名。
- GGUF 模型（qwen-image-2.1-UC-Q8_0.gguf）走 `UnetLoaderGGUF` 节点（ComfyUI-GGUF 已装），
  按扩展名自动选 loader。
- 多图编号约定：`images.image_N` 顺序 = 提示词中 `<image1>..<imageN>`（与官方模板一致）。
- LLM 双档：iq2_m 64K（alias `qwen3.8-27b-uncensored`）/ q4_k_m 高质量（alias
  `qwen3.8-27b-uncensored@q4_k_m`，慢）；`/v1/models` 返回当前 alias，可据此判断换档。

### 18.2 A：模型选择

- `config.LM_PROFILES`：档位表（key → 启动脚本/alias/标签/说明）；
  `ensure_llm(profile_key)`：已运行且 alias 相同 → 复用；代管实例 alias 不同 → 关旧起新；
  外部实例 alias 不同 → 只复用并告警（§3.3 外部不关）。
- `GET /api/models`：`{"lm": [档位表], "unets": {...}}`；unets 优先 ComfyUI live
  `/object_info/UNETLoader`（运行时），否则扫描 `models/diffusion_models` 目录（safetensors
  + gguf），前端下拉。
- UI：导入参数区「语言模型档位」「生图模型」下拉；任务级存储（lm_profile / comfy_model），
  队列 caption 阶段按任务自动换档（换档代价 = LLM 重载，仅在档位变化时发生）。

### 18.3 B+C：多图编辑类任务（kind = asset / faceswap）

- **任务类型** `task.kind`：`t2i`（现有，N 图 → N 产物）/ `asset`（人物资产）/
  `faceswap`（换脸）——asset 与 faceswap 共用 multi 管线，仅默认模板不同。
- **输入**：文件夹 / zip·7z（复用现有摄取）或 **多文件上传**（新增 `POST /api/upload/batch`，
  前端 `<input multiple>`）；图片自然排序编号 = `<image1>..<imageN>`；
  数量上限 `MULTI_MAX_IMAGES = 8`（节点硬上限 16，留余量；导入超限 400）。
  角色约定写进模板文案（asset：`<image1>`=人物、`<image2>`=服装、`<image3+>`=参考；
  faceswap：`<image1>`=主图、`<image2+>`=目标脸，多脸可提升还原度）。
- **提示词模板**：`config.DEFAULT_TEMPLATES[kind]` 默认模板，导入时填入可编辑文本框
  （`params.prompt_template` 随任务保存）；caption 请求把模板与全部图片一并发给 LLM，
  产出具体化的 `rewritten_prompt`（编辑指令）+ `wh_ratio`（按主图）+ `tags`；
  审阅页可直接改写最终指令。
- **multi 管线**（一次 LLM 调用、一次生成、一张产物）：
  - caption：全部条目置 captioning → `caption_multi(全部图, 模板)` 一次调用 →
    同一契约写入全部条目 → 全部 captioned；失败 → 全部 caption_failed（重试策略同 t2i）；
  - generate：`upload_to_comfy` 逐张传入 → `build_edit_graph` → 一次采样 → 输出图挂到
    seq=1 条目（其余条目置 done 无产物，结果画廊自动只显 1 张）；
  - 审阅页：multi 任务点击任意输入图 → 侧栏显示该图 + 共享提示词/比例/标签（编辑广播到
    全部条目）+「生成」按钮（整组重生成）。
- **输出尺寸**：跟随参考图（edit latent 路径）；`wh_ratio` 仅展示与记录。
- **进度语义**：multi 任务进度条按「任务级百分比」（captioning→generating），条目进度不适用。

### 18.4 测试与验收

- 单元：build_edit_graph 结构断言（loader 按扩展名切换、image_N 键序、latent 直连）；
  caption_multi 请求构造（多 image_url、模板注入）与契约解析复用；
- 接口：multi 导入（kind 校验/超限 400/upload batch）、multi 全流程（一次 LLM 调用、
  一次生成、契约广播编辑）、/api/models 两态、换档逻辑（桩下 alias 断言）；
- E2E：multi 任务导入→队列→结果一条；
- **真实验收**：asset 一单（人物+服装+参考）+ faceswap 一单（主图+多脸），真模型出图。
