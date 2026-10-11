# Prompt Studio

本地批量提示词提取 + Qwen-Image-2.1 文生图服务器（网页界面）：工作区 + 任务队列，
图片 → qwen3.8-27b 契约提示词（rewritten_prompt / wh_ratio / tags）→ ComfyUI 批量文生图。

## 用法

双击 `start-server.cmd`，浏览器打开 http://127.0.0.1:9178 。

1. **① 队列**：设定工作区目录 → 连续导入多个文件夹 / zip / 7z（可命名、可选 AI 自动命名、
   可分别指定图片输出与提示词输出目录）→ 「开始队列」；
   队列表实时显示每个任务的进度、当前图片与浮现中的提示词；
2. **② 审阅 / 进度**：点队列行进入；查看/编辑提示词与标签、加注释重新提取、单张生图；
   「停在审阅」模式的任务会挂起队列，审阅完点「继续生成」放行；
3. **③ 结果**：跨任务原图 ↔ 生成图画廊，支持视图切换（大/中/小图标、列表、详细信息）与
   点击放大；详情侧栏大图可全屏预览。

队列执行为**阶段批处理**：先全部提取提示词（llama-server 只起一次），再全部生成
（ComfyUI 只起一次）；16G 显存两阶段互斥串行，服务自动启停，外部手动启动的实例只复用不关闭。

## 输出

工作区（默认 `outputs/`，可指定任意目录）内每任务一个目录：

```
<工作区>/<任务名>/
  0001.png …               ← 生成图（可整体另指定图片输出目录）
  prompts/0001.json        ← 契约 JSON（rewritten_prompt / wh_ratio / tags）
  prompts/tags/0001.txt    ← LoRA 惯例标签 sidecar（与图同名成对）
  prompts/prompts_summary.jsonl
  .studio/                 ← 内部簿记（原图、缩略图、task.json）
```

## 运行边界（重要）

- 仅当网页任务流程运行时才会拉起 llama-server（iq2_m 64K 视觉档，端口 1234）与
  ComfyUI（端口 8188）；完成后自动关闭本服务器代管拉起的进程；
- 开发/测试期手动运行这两个模型服务需用户明确批准。

## 依赖的外部设施

机器相关路径（llama-server 启动脚本 / skill 目录 / ComfyUI 目录等）通过仓库根目录的
**`config.local.json`** 配置（gitignored；模板见 `config.local.example.json`，缺省值 =
作者机器布局）。换电脑 / 换目录零代码改动，完整流程见 **[SETUP.md](SETUP.md)**。

设计与实现细节（含测试体系、里程碑、决策记录）见 `doc/01_prompt-studio整体设计.md`。

## 测试

```
.venv\Scripts\python -m pytest tests\ -q     # 123 用例（pytest + respx + Playwright/Edge，模型全桩）
```
