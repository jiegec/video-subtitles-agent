# 视频字幕 Agent

[![CI](https://github.com/jiegec/video-subtitles-agent/actions/workflows/ci.yml/badge.svg)](https://github.com/jiegec/video-subtitles-agent/actions/workflows/ci.yml)

基于 **whisper-cli / 腾讯云 ASR + LLM** 的视频字幕生成与编辑工具。所有操作都在一个 Web 界面里完成：

- 扫描 `project/` 目录下的视频
- 用 `ffmpeg` 抽取音频（`.wav` 保存在视频旁边）
- 语音识别后端可选：本地 `whisper-cli`（带 token 级时间戳），或**腾讯云录音文件识别**（云端，自动分片上传）
- 用 LLM 纠正识别错误、去除口语、按最大字数重新断句；设置里可**一键测试 LLM 连接**
- **结合视频画面截图**（多模态）与**资料 / 术语表**提高纠错准确率
- 网页里预览、逐句编辑（**自动保存**），支持 0.5×–3× 倍速播放（快捷键 `[` / `]`）
- 自动保留 SRT 历史版本到 `<视频名>.backups/`，可一键恢复；重新生成时先展示与历史版本的 diff，可选择覆盖 / 合并 / 保留历史版本
- **章节列表**：确认字幕后可单独生成章节（默认最多 10 条，每行 `HH:MM:SS 标题`，首条从 `00:00:00` 开始），保存为 `<视频名>.chapters.txt` 并在网页显示
- **生成轨迹**：可视化语音识别原文 → LLM 清洗 → 重排对齐的全过程，包括实际注入的上下文、LLM 输入输出/token/耗时与每个 LLM 请求
- **漏识别补齐**：检测 whisper 漏掉/听错的内容（时间缺口、稀疏、低置信）并针对性重转，作为「另一版识别」交给 LLM 合并
- 自动处理**左右声道反相**、**只有单边有声音**与 whisper **重复幻觉**等音频情况（混单会让反相或单边音频抵消、减半）
- 最终生成与视频同目录的 `.srt`

## 快速开始

```bash
uv sync
uv run subtitles-agent            # 默认 http://127.0.0.1:8765
uv run subtitles-agent --port 9000
```

打开网页后：

0. 点右上角「⚙ 设置」填写 whisper / LLM / 字幕参数等（保存在 `config.toml`，保存立即生效）
1. 左侧选择视频
2. 「上下文与资料」标签里填写视频说明、术语表，添加参考资料（PDF / Word / Markdown / 文本 / 网页链接，或直接上传），也可以**从其他视频复用配置**或**导入一个现成的 JSON 配置**
3. 「生成设置」里设置每句最大字数等参数
4. 点「全流程生成字幕」：每次完整执行（抽音频 → 转写 → 补齐漏识别 → 截图 → LLM 清洗）
5. 视频带有字幕时自动弹出与历史版本的 diff，可选择**覆盖 / 合并 / 保留历史版本**；首次生成时直接产出新字幕
6. 在下方字幕编辑器里预览、修改，**修改会自动保存**；点「历史」可查看/恢复历史版本
7. 「生成轨迹」标签可以看到语音识别原文、注入的上下文、LLM 输入输出/token/耗时与前后 diff
8. 确认字幕后，在右侧「章节」标签点「生成章节列表」，生成并保存到 `<视频名>.chapters.txt`（独立操作，与全流程分开执行）

## 依赖要求

- Python 3.11+
- `ffmpeg` / `ffprobe`
- `whisper-cli`（whisper.cpp）与本地模型，默认 `./ggml-medium.bin`（whisper 后端使用）
- 腾讯云 SDK（`tencentcloud-sdk-python`）是强制依赖，`uv sync` 会一并安装

## 配置

所有配置都放在**一个 `config.toml` 文件**里。

- **推荐**：启动后点网页右上角的「⚙ 设置」，在弹出的表单里填写并保存，配置立即写入 `config.toml` 并生效。
- 也可以直接用文本编辑器改 `config.toml`（相对路径都相对于本文件所在目录），然后重启服务。
- 语音识别后端在「设置 → 语音识别」里选择：`whisper`（本地）或 `tencent`（腾讯云）；选腾讯云时可点「测试密钥」验证 SecretId / SecretKey 的签名与权限（只做一次空任务查询）。
- LLM 默认 `source = "pi"`：接口地址 / 密钥 / 模型取自 `~/.pi/agent/*`，`config.toml` 中对应字段留空。自定义配置时设 `source = "manual"` 并填写下面三项。设置里的「测试连接」会发一个最小请求验证连接。
- 优先级：一般配置 `config.toml` > 内置默认值；LLM 的 `base_url` / `api_key` / `model` 在 `source = "pi"` 时取自 `~/.pi/agent/*`，在 `source = "manual"` 时取自 `config.toml`。

`config.toml` 的结构如下（首次保存后自动生成）：

```toml
[paths]
project_dir = "project"          # 视频目录
ffmpeg_bin = "ffmpeg"
whisper_bin = "whisper-cli"
whisper_model = "ggml-medium.bin"

[whisper]
lang = "zh"
threads = 5
prompt = "以下是简体中文的语音内容。"

[asr]
backend = "whisper"             # whisper | tencent

[tencent]                        # backend = "tencent" 时填写
secret_id = ""
secret_key = ""
region = "ap-shanghai"
engine = "16k_zh_en_2.0"
chunk_seconds = 600              # 每段秒数，保证单段 < 5MB（base64 上传限制）

[llm]
source = "pi"                    # pi = 从 ~/.pi/agent 读取；manual = 手动填写下面三项
base_url = ""                    # source = manual 时生效
api_key = ""
model = ""
concurrency = 4
max_tokens = 16384
timeout = 180.0
thinking = "off"               # off / low / medium / high / auto

[subtitles]
max_chars = 18
min_duration = 0.8
block_chars = 220

[frames]
interval = 45
max_per_batch = 4
max_count = 240

[context]
max_chars = 12000

[recover]
model = ""                     # 补转用模型，留空同 whisper_model
noise_db = -30.0
min_silence = 0.4
min_gap = 1.5
min_cps = 1.8
low_conf_threshold = 0.45
max_regions = 80

[backup]
keep = 100
interval = 120.0

[chapters]
max_count = 10            # 章节数量上限
max_title_chars = 16      # 每个章节标题最大字符数
input_chars = 24000       # 送给 LLM 的时间轴字数上限
```

> 章节列表：字幕确认后，切到右侧「章节」标签，点「生成章节列表」。它是独立操作，与全流程生成分开执行。

## per-video 配置（JSON）

每个视频都有自己的工程配置，保存在视频旁边：`<视频名>.subtitles.json`。全部可在网页里编辑，也可以复用/导入/导出：

```json
{
  "version": 1,
  "title": "第一周 第一小节",
  "context": "这个视频讲了……",
  "glossary": [
    { "term": "哈希表", "note": "数据结构" }
  ],
  "documents": ["资料/第一周.pdf", "https://example.com/notes"],
  "frame_interval": 45
}
```

- **复用**：在网页里选择另一个视频的配置一键覆盖到当前视频
- **导入 / 导出**：上传一个现成的 `.subtitles.json`，或把当前配置导出保存
- 术语表会同时用于 whisper 的 `--prompt`、腾讯云的热词表（提高专有名词识别率）和 LLM 纠错
- 资料很长时，LLM 会把它压缩成「摘要 + 术语表」并缓存到 `<视频名>.context.json`，后续请求注入这份摘要

## 产物

对每个视频 `xxx.mp4`：

| 文件 | 说明 |
| --- | --- |
| `xxx.wav` | ffmpeg 抽取的 16kHz 单声道音频 |
| `xxx.asr.json` | 语音识别结果（whisper 或腾讯云，统一 JSON，含词/字级时间戳） |
| `xxx.frames/` | 视频截图与 `manifest.json` |
| `xxx.context.json` | 参考资料摘要缓存 |
| `xxx.gaps.json` | 漏识别补齐报告（可疑区间与重转结果） |
| `xxx.subtitles.json` | 本视频的工程配置（可复用/导入导出） |
| `xxx.trajectory.json` | 生成轨迹（原始字幕 / 上下文 / LLM 输入输出 / 重排结果） |
| `xxx.backups/` | SRT 历史版本（自动保存/重新生成前备份） |
| `xxx.chapters.txt` | 章节列表（每行 `HH:MM:SS 标题`，默认最多 10 条，首条从 00:00:00 开始） |
| `xxx.srt` | 最终字幕（编辑时自动保存） |

## 生成轨迹（trajectory）

字幕列表上方可切换到「生成轨迹」，看到每一步到底发生了什么：

- **本次实际注入的上下文**：参数、术语表、参考资料（命中情况与字数）、可用截图（缩略图）、以及完整注入给 LLM 的参考文本
- **每个文本块**：左侧语音识别原始文本（删除/改写标红），右侧 LLM 清洗后文本（新增/改写标绿/黄），下方是该块重排后的字幕（含时间）
- **LLM 调用（调试）**：每次调用的阶段（资料摘要 / 清洗批次）、输入与输出 token、缓存命中、思考 token、耗时、`finish_reason`、重试次数、附带的截图数量，以及总计
- **实际请求**：展开“查看实际请求”可看到该批附带的截图、发送的完整 user 消息（含参考上下文与待处理文本）以及模型原始返回

轨迹保存在视频旁边的 `xxx.trajectory.json`，由生成流程自动写入。对应接口：

| 接口 | 说明 |
| --- | --- |
| `GET /api/trajectory?path=` | 读取生成轨迹 |
| `GET /api/frame?path=&name=` | 读取某张视频截图（缩略图用） |

## 漏识别补齐（补回转写遗漏）

LLM 基于你给它的文本纠错，转写阶段漏掉的内容通过重新转写补回。
补齐回到音频本身：检测「whisper 可能漏了」的区间，重新转写，再交给 LLM 合并。

检测三类可疑区间：

1. **时间缺口**：音频里有语音、转写时间轴上对应片段为空的时段。用 `ffmpeg silencedetect` 求语音区间再减去原转写。
2. **稀疏片段**：片段覆盖了时间，但字数明显偏少（语速异常低），疑似漏词。
3. **低置信片段**：whisper 自己的 token 概率低于阈值且连续成段，通常就是听错的地方。

对每个区间：切出音频 → 单独重转（隔离上下文 `-mc 0`，可配置更大模型）→
- 时间缺口直接插入时间轴；
- 稀疏 / 低置信作为**另一版识别（`alt`）**交给 LLM，两个版本互补，保留原文内容。

在网页里点「全流程生成字幕」即可（补齐包含在流程内），结果写入 `xxx.gaps.json` 并在生成轨迹里展示。

## 常见情况排查

**生成的字幕偏少或为空时怎么排查？**

原因通常来自音频本身：

- **左右声道差异**：视频是立体声，但可能遇到两种情况：① 左右声道相位相反，`ffmpeg -ac 1` 混成单声道会互相抵消，得到近乎静音的 wav；② 只有一边声道有声音，另一边静音。程序只对真正的双声道音频检测（单声道 / 多声道直接默认混音），并按「电平差大 → 单边有声音；电平接近但混单明显变低 → 反相」自动在日志里提示「改用左/右声道提取」。
- **whisper 重复幻觉**：在静音/噪声上反复输出同一句话。若某句文本出现次数超过总段数的 12%（且 ≥ 5 次），会被判定为幻觉并移除，日志会提示。
- 建议看「生成轨迹」里的「🤖 LLM 调用」与实际请求，对照音频电平与模型输出。

**想控制每次重跑的代价？**

「全流程生成字幕」每次完整执行，保证结果可复现。调整参数后重新执行全流程即可。

## 时间轴是怎么保证正确的

LLM 会删词、改字，字幕时间需要重新推导。做法是：

1. 把 whisper 每个 token 展开成「字符 → 起止时间」的时间轴
2. LLM 改写后，用 `difflib` 做**字符级对齐**，得到每个新字符的时间
3. 再按最大字数切句，时间取该句首字符的开始与末字符的结束
4. 切句时优先在标点、连接词（“所以/但是/然后”等）、英文单词边界处断开，并在前后 slack 个字内寻找自然断点；每句保持约 4 个字以上，按字数硬切作为兜底

因此删除口语、纠正同音字时，字幕时间与原始语音保持一致。

## 开发

```bash
uv sync
uv run ruff check src tests   # lint
uv run pytest -q              # 单元测试（纯函数与接口层）
uv build                      # 构建 sdist + wheel
```

CI（GitHub Actions）在 Python 3.11 / 3.12 / 3.13 上跑 lint、单元测试、字节码编译与前端语法检查，并构建发行包。

## 项目结构

```
src/subtitles_agent/
  config.py          # 全局配置（读写 config.toml）
  video_config.py    # per-video JSON 工程配置
  context.py         # 参考资料读取 / 摘要
  frames.py          # 视频截图
  recover.py         # 漏识别检测与补转
  backup.py          # SRT 历史版本
  transcribe.py      # ffmpeg / whisper-cli / ASR 后端调度
  tencent_asr.py     # 腾讯云录音文件识别后端
  llm.py             # OpenAI 兼容 LLM 客户端（支持多模态）
  subtitles.py        # SRT 读写 + 时间轴对齐/重排
  trajectory.py      # 生成轨迹（diff + 落盘）
  chapters.py        # 章节列表生成（基于最终字幕）
  pipeline.py        # 任务编排
  library.py         # 视频库
  web/               # FastAPI + 前端
```
