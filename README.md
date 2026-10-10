# Prompt Studio

本地批量提示词提取 + Qwen-Image-2.1 文生图服务器（网页界面）。

## 用法

双击 `start-server.cmd`，浏览器打开 http://127.0.0.1:9178 。

1. **任务页**：填文件夹路径（或点「选择文件夹」/ 上传 zip·7z）→ 「提取提示词」或「全自动」；
2. **审阅页**：点任意图片可查看/编辑提示词、加注释重新提取、单张生图；「全部生成」批量生图；
3. **结果页**：原图 ↔ 生成图对照。

## 运行边界（重要）

- 网页上的「提取提示词 / 生成」按钮被点击时，服务器才会拉起 llama-server（iq2_m 64K 视觉档，
  端口 1234）与 ComfyUI（端口 8188），两阶段串行（16G 显存互斥），完成后自动关闭本服务器
  代管拉起的进程；已在运行的外部实例只复用、不关闭。
- 开发/测试期手动运行这两个模型服务需用户明确批准。

## 依赖的外部设施

| 设施 | 位置 |
|---|---|
| llama-server 启动脚本 | `D:\work_space\ai\llm\qwen_28b\start-qwen-iq2-m-64k.cmd` |
| prompt-writing skill（实时读取） | `C:\Users\admin\.pi\agent\skills\qwen-image-21-prompt-writing\` |
| ComfyUI 整合包 | `D:\Program Files\Comfyui-WF-2026.8.8\ComfyUI` |

详见 `doc/01_prompt-studio整体设计.md`。

## 输出

`outputs/<时间戳>_<任务名>/`：`source/`（规范化的原图）、`prompts/`（每图契约 JSON）、
`prompts_summary.jsonl`、`images/`（生成图）、`thumbs/`、`task.json`。
