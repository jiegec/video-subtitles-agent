# 视频字幕 Agent

基于 **whisper-cli + LLM** 的视频字幕生成与编辑工具。所有操作都在一个 Web 界面里完成：

- 扫描 `project/` 目录下的视频
- 用 `ffmpeg` 抽取音频（`.wav` 保存在视频旁边）
- 用 `whisper-cli` 转写（带 token 级时间戳，`.whisper.json` 保存在视频旁边）
- 用 LLM 纠正识别错误、去除口语、按最大字数重新断句
- **结合视频画面截图**（多模态）与**资料 / 术语表**提高纠错准确率
- 网页里预览、逐句编辑、保存，支持 0.5×–3× 倍速播放（快捷键 `[` / `]`）
- **生成轨迹**：可视化 whisper 原文 → LLM 清洗 → 重排对齐的全过程，包括实际注入的上下文与每个 LLM 请求
- 最终生成与视频同目录的 `.srt`

## 快速开始

```bash
uv sync
uv run subtitle-agent            # 默认 http://127.0.0.1:8765
uv run subtitle-agent --port 9000
```

打开网页后：

1. 左侧选择视频
2. 「上下文与资料」标签里填写视频说明、术语表，添加参考资料（PDF / Word / Markdown / 文本 / 网页链接，或直接上传），也可以**从其他视频复用配置**或**导入一个现成的 JSON 配置**
3. 「生成设置」里设置每句最大字数等参数
4. 点「全流程生成字幕」
5. 在下方字幕编辑器里预览、修改、保存

## 依赖要求

- Python 3.11+
- `ffmpeg` / `ffprobe`
- `whisper-cli`（whisper.cpp）与已下载的模型，默认 `./ggml-medium.bin`

## 配置

LLM 走任意 OpenAI 兼容接口。优先级：环境变量 > `.env` > `~/.pi/agent/*`（可复用 pi 的配置）> 默认值。
可用 `.env.example` 作为模板：

```bash
cp .env.example .env
```

| 变量 | 说明 | 默认 |
| --- | --- | --- |
| `PROJECT_DIR` | 视频目录 | `./project` |
| `WHISPER_BIN` / `WHISPER_MODEL` | whisper 可执行文件与模型 | `whisper-cli` / `./ggml-medium.bin` |
| `WHISPER_LANG` / `WHISPER_THREADS` | 语言与线程数 | `zh` / CPU 一半 |
| `WHISPER_PROMPT` | whisper 初始提示词（可用于指定简体中文、专有名词） | `以下是简体中文的语音内容。` |
| `FFMPEG_BIN` | ffmpeg 路径 | `ffmpeg` |
| `LLM_BASE_URL` / `LLM_API_KEY` / `LLM_MODEL` | OpenAI 兼容接口 | 从 `~/.pi` 推断 |
| `LLM_CONCURRENCY` | 并发请求数 | `4` |
| `LLM_MAX_TOKENS` | 单次返回上限 | `16384` |
| `LLM_TIMEOUT` | 单次请求超时（秒） | `180` |
| `LLM_THINKING` | 推理强度 `off/low/medium/high/auto` | `off` |
| `SUBTITLE_MAX_CHARS` / `SUBTITLE_MIN_DURATION` | 每句最大字数 / 最短显示 | `18` / `0.8` |
| `LLM_BLOCK_CHARS` | 每次交给 LLM 的分块字数 | `220` |
| `FRAME_INTERVAL` | 截图间隔（秒） | `45` |
| `FRAME_MAX_PER_BATCH` | 每个 LLM 请求最多附几张截图 | `4` |
| `FRAME_MAX_COUNT` | 单个视频最多保留多少张截图（过长会自动拉大间隔） | `240` |
| `CONTEXT_MAX_CHARS` | 资料注入上限（超出自动摘要） | `12000` |

## per-video 配置（JSON）

每个视频都有自己的工程配置，保存在视频旁边：`<视频名>.subtitle.json`。全部可在网页里编辑，也可以复用/导入/导出：

```json
{
  "version": 1,
  "title": "第一周 第一小节",
  "context": "这个视频讲了……",
  "glossary": [
    { "term": "哈希表", "aliases": ["哈西表"], "note": "数据结构" }
  ],
  "documents": ["资料/第一周.pdf", "https://example.com/notes"],
  "use_frames": true,
  "frame_interval": 45
}
```

- **复用**：在网页里选择另一个视频的配置一键覆盖到当前视频
- **导入 / 导出**：上传一个现成的 `.subtitle.json`，或把当前配置导出保存
- 术语表会同时用于 whisper 的 `--prompt`（提高专有名词识别率）和 LLM 纠错
- 资料很长时，会先让 LLM 压缩成「摘要 + 术语表」并缓存到 `<视频名>.context.json`，避免每次请求都塞入整份文档

## 产物

对每个视频 `xxx.mp4`：

| 文件 | 说明 |
| --- | --- |
| `xxx.wav` | ffmpeg 抽取的 16kHz 单声道音频 |
| `xxx.whisper.json` | whisper 转写结果（含 token 时间戳） |
| `xxx.frames/` | 视频截图与 `manifest.json` |
| `xxx.context.json` | 参考资料摘要缓存 |
| `xxx.subtitle.json` | 本视频的工程配置（可复用/导入导出） |
| `xxx.trajectory.json` | 生成轨迹（原始字幕 / 上下文 / LLM 输入输出 / 重排结果） |
| `xxx.srt` | 最终字幕 |

## 生成轨迹（trajectory）

字幕列表上方可切换到「生成轨迹」，看到每一步到底发生了什么：

- **本次实际注入的上下文**：参数、术语表、参考资料（找到/未找到与字数）、可用截图（缩略图）、以及完整注入给 LLM 的参考文本
- **每个文本块**：左侧 whisper 原始文本（删除/改写标红），右侧 LLM 清洗后文本（新增/改写标绿/黄），下方是该块重排后的字幕（含时间）
- **实际请求**：展开“查看实际请求”可看到该批附带的截图、发送的完整 user 消息（含参考上下文与待处理文本）以及模型原始返回

轨迹保存在视频旁边的 `xxx.trajectory.json`，由生成流程自动写入。对应接口：

| 接口 | 说明 |
| --- | --- |
| `GET /api/trajectory?path=` | 读取生成轨迹 |
| `GET /api/frame?path=&name=` | 读取某张视频截图（缩略图用） |

## 时间轴是怎么保证正确的

LLM 会删词、改字，因此不能直接沿用 whisper 的分句时间。做法是：

1. 把 whisper 每个 token 展开成「字符 → 起止时间」的时间轴
2. LLM 改写后，用 `difflib` 做**字符级对齐**，得到每个新字符的时间
3. 再按最大字数切句，时间取该句首字符的开始与末字符的结束

因此即使删除了口语、纠正了同音字，字幕时间仍然贴合原始语音，不会整体漂移。

## 项目结构

```
src/subtitle_agent/
  config.py          # 配置加载
  video_config.py    # per-video JSON 工程配置
  context.py         # 参考资料读取 / 摘要
  frames.py          # 视频截图
  transcribe.py      # ffmpeg / whisper-cli
  llm.py             # OpenAI 兼容 LLM 客户端（支持多模态）
  subtitle.py        # SRT 读写 + 时间轴对齐/重排
  trajectory.py      # 生成轨迹（diff + 落盘）
  pipeline.py        # 任务编排
  library.py         # 视频库
  web/               # FastAPI + 前端
```
