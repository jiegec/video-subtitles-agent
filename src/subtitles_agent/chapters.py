"""章节列表生成：在最终字幕的基础上，让 LLM 划分章节，并保存为纯文本。

输出格式（每行一条）：

    00:00:00 开场介绍
    00:05:12 基本概念

约束：最多 ``max_count`` 条、标题不超过 ``max_title_chars`` 个字符、第一条从 00:00:00 开始。
章节时间会吸附到最接近的字幕句子起点，保证与字幕对齐。
"""

from __future__ import annotations

import bisect
import re
from pathlib import Path
from typing import Callable

from .config import Settings
from .subtitles import Cue

TimeFn = Callable[[str], None]

_LINE = re.compile(r"(\d{1,2}):(\d{2}):(\d{2})\s*[-–—:：、. ]*\s*(.+?)\s*$")
_TRIM = " \t\"'“”‘’《》【】()（）[]-–—:：,，.。!！?？、"

CHAPTERS_SYSTEM = (
    "你是视频内容编辑，擅长为课程、讲座、访谈等视频划分章节。"
    "请严格按要求输出，不要解释、不要 markdown。"
)


def format_time(seconds: float) -> str:
    total = max(0, int(round(seconds)))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def build_timeline(cues: list[Cue], budget: int = 24000, window_sec: float = 20.0) -> str:
    """把字幕压成 ``[HH:MM:SS] 文本`` 的时间轴，并控制总字数。"""
    windows: list[list] = []  # [start, text]
    for cue in cues:
        text = (cue.text or "").strip()
        if not text:
            continue
        if windows and cue.start - windows[-1][0] < window_sec:
            windows[-1][1] += text
        else:
            windows.append([cue.start, text])
    if not windows:
        return ""
    per_window = max(20, budget // len(windows))
    return "\n".join(f"[{format_time(w[0])}] {w[1][:per_window]}" for w in windows)


def _clean_title(raw: str, max_chars: int) -> str:
    title = (raw or "").strip().strip(_TRIM).strip()
    title = re.sub(r"^\d+[.、)）]\s*", "", title)
    title = title.strip(_TRIM).strip()
    return title[:max_chars]


def _parse_lines(text: str, max_title_chars: int = 999) -> list[dict]:
    result: list[dict] = []
    for line in (text or "").splitlines():
        match = _LINE.search(line.strip())
        if not match:
            continue
        seconds = int(match.group(1)) * 3600 + int(match.group(2)) * 60 + int(match.group(3))
        title = _clean_title(match.group(4), max_title_chars)
        if title:
            result.append({"start": seconds, "title": title})
    return result


def parse_chapters_text(text: str, max_title_chars: int = 999) -> list[dict]:
    """解析已保存的章节文件（不做吸附/截断）。"""
    return _parse_lines(text, max_title_chars)


def _snap(seconds: int, starts: list[int]) -> int:
    if not starts:
        return seconds
    index = bisect.bisect_left(starts, seconds)
    candidates = [starts[i] for i in (index, index - 1) if 0 <= i < len(starts)]
    return min(candidates, key=lambda value: abs(value - seconds))


def parse_chapters(
    text: str,
    cue_starts: list[float],
    max_count: int = 10,
    max_title_chars: int = 16,
) -> list[dict]:
    """解析 LLM 输出并规范化：吸附字幕时间、去重、限条数、首条归零。"""
    raw = _parse_lines(text, max_title_chars)
    if not raw:
        return []
    raw.sort(key=lambda item: item["start"])
    starts = sorted({int(round(s)) for s in cue_starts})
    chapters: list[dict] = []
    seen: set[int] = set()
    for item in raw:
        snapped = _snap(item["start"], starts)
        if snapped in seen:
            continue
        seen.add(snapped)
        chapters.append({"start": snapped, "title": item["title"]})
        if len(chapters) >= max_count:
            break
    if chapters:
        chapters[0]["start"] = 0
    return chapters


def render_chapters(chapters: list[dict]) -> str:
    if not chapters:
        return ""
    return "\n".join(f"{format_time(c['start'])} {c['title']}" for c in chapters) + "\n"


def load_chapters(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    try:
        return parse_chapters_text(path.read_text(encoding="utf-8"))
    except Exception:
        return []


def save_chapters(path: Path, chapters: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(render_chapters(chapters), encoding="utf-8")
    tmp.replace(path)


def _prompt(timeline: str, max_count: int, max_title_chars: int) -> str:
    return f"""下面是视频字幕的时间轴（每行形如 [HH:MM:SS] 文本）。请据此把视频划分成章节。

要求：
1. 最多 {max_count} 个章节，选择内容明显转换的位置；
2. 每行格式为：HH:MM:SS 标题（时间和标题之间一个空格）；
3. 标题概括该章节内容，不超过 {max_title_chars} 个汉字，不要标点、书名号或编号；
4. 按时间从早到晚排列；
5. 第一行必须从 00:00:00 开始；
6. 只输出这些行，不要输出解释、markdown 或代码块。

字幕时间轴：
{timeline}
"""


def generate_chapters(
    settings: Settings,
    cues: list[Cue],
    log: TimeFn = lambda _: None,
) -> tuple[list[dict], dict]:
    """调用 LLM 生成章节；返回 (章节列表, 元信息)。"""
    from .llm import LLMClient

    if not settings.has_llm:
        raise RuntimeError("未配置 LLM，无法生成章节")
    timeline = build_timeline(cues, settings.chapters_input_chars)
    if not timeline:
        raise RuntimeError("字幕为空，无法生成章节")
    log(f"章节输入：{len(cues)} 条字幕 → {len(timeline)} 字时间轴")

    client = LLMClient(settings)
    try:
        result = client.complete(
            _prompt(timeline, settings.chapters_max_count, settings.chapters_max_title_chars),
            system_prompt=CHAPTERS_SYSTEM,
            max_tokens=1024,
            temperature=0.3,
        )
    finally:
        client.close()

    chapters = parse_chapters(
        result.content,
        [cue.start for cue in cues],
        max_count=settings.chapters_max_count,
        max_title_chars=settings.chapters_max_title_chars,
    )
    if not chapters:
        raise RuntimeError(f"模型未返回有效章节：{result.content.strip()[:120]}")
    meta = {
        "model": result.model,
        "elapsed": round(result.elapsed, 2),
        "usage": result.usage,
        "timeline_chars": len(timeline),
    }
    return chapters, meta
