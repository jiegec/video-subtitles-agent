"""字幕数据结构、SRT 读写、以及「保留时间轴」的重排/对齐算法。

核心思路：
- whisper 输出的每个 token 都带有起止时间，我们把它展开成「字符 -> 时间」的时间轴。
- LLM 改写后的文本通过 difflib 与原字符时间轴做字符级对齐，得到每个新字符的时间。
- 再按最大字数切分成字幕行，时间取该行首字符的开始与末字符的结束，
  这样即使 LLM 删除/纠错/增字，时间也能贴合原始语音，不会整体漂移。
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass

# 断句优先级：句末 > 句中 > 空格
STRONG_BREAKS = "。！？!?；;…"
WEAK_BREAKS = "，,、：:）)】」》"
SPACE_BREAKS = " "
TRAILING_PUNCT = "。，、；：！？!?…）)】」》〉"

_SRT_TIME_RE = re.compile(
    r"(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})\s*-->\s*(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})"
)


@dataclass
class Cue:
    start: float
    end: float
    text: str

    def as_dict(self) -> dict:
        return {"start": round(self.start, 3), "end": round(self.end, 3), "text": self.text}


# --------------------------------------------------------------------------- #
# SRT
# --------------------------------------------------------------------------- #
def format_timestamp(seconds: float) -> str:
    if seconds < 0:
        seconds = 0.0
    ms_total = int(round(seconds * 1000))
    hours, rem = divmod(ms_total, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    secs, ms = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{ms:03d}"


def parse_timestamp(h: str, m: str, s: str, ms: str) -> float:
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms.ljust(3, "0")) / 1000.0


def cues_to_srt(cues: list[Cue]) -> str:
    blocks = []
    for index, cue in enumerate(cues, start=1):
        text = cue.text.strip() or " "
        blocks.append(
            f"{index}\n{format_timestamp(cue.start)} --> {format_timestamp(cue.end)}\n{text}\n"
        )
    return "\n".join(blocks)


def parse_srt(content: str) -> list[Cue]:
    cues: list[Cue] = []
    for block in re.split(r"\n\s*\n", content.strip()):
        lines = [ln for ln in block.splitlines() if ln.strip() != ""]
        if not lines:
            continue
        time_line_index = None
        for i, line in enumerate(lines):
            if _SRT_TIME_RE.search(line):
                time_line_index = i
                break
        if time_line_index is None:
            continue
        match = _SRT_TIME_RE.search(lines[time_line_index])
        assert match is not None
        start = parse_timestamp(*match.groups()[:4])
        end = parse_timestamp(*match.groups()[4:])
        text = "\n".join(lines[time_line_index + 1 :]).strip()
        cues.append(Cue(start=start, end=end, text=text))
    return cues


# --------------------------------------------------------------------------- #
# whisper token -> 字符时间轴
# --------------------------------------------------------------------------- #
def _is_special_token(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return False
    return (stripped.startswith("[") and stripped.endswith("]")) or (
        stripped.startswith("<|") and stripped.endswith("|>")
    )


def build_char_timeline(tokens: list[dict]) -> list[tuple[str, float, float]]:
    """把 whisper token 列表展开为 [(char, start_sec, end_sec), ...]。"""
    timeline: list[tuple[str, float, float]] = []
    for token in tokens:
        text = token.get("text", "")
        if not text or _is_special_token(text):
            continue
        offsets = token.get("offsets") or {}
        start = float(offsets.get("from", 0)) / 1000.0
        end = float(offsets.get("to", start * 1000)) / 1000.0
        if end < start:
            end = start
        count = len(text)
        for k, ch in enumerate(text):
            cs = start + (end - start) * k / count
            ce = start + (end - start) * (k + 1) / count
            timeline.append((ch, cs, ce))
    return timeline


# --------------------------------------------------------------------------- #
# 字符级对齐：把改写文本映射回原始时间轴
# --------------------------------------------------------------------------- #
def align_times(
    timeline: list[tuple[str, float, float]],
    new_text: str,
    fallback_start: float,
    fallback_end: float,
) -> list[tuple[float, float]]:
    times: list[tuple[float, float] | None] = [None] * len(new_text)
    orig_text = "".join(item[0] for item in timeline)

    def fill_insert(j1: int, j2: int, i1: int) -> None:
        left = timeline[i1 - 1][2] if i1 - 1 >= 0 else fallback_start
        right = timeline[i1][1] if i1 < len(timeline) else fallback_end
        if right < left:
            right = left
        span = max(j2 - j1, 1)
        for k in range(j2 - j1):
            times[j1 + k] = (left + (right - left) * k / span, left + (right - left) * (k + 1) / span)

    if not timeline or not new_text:
        span = max(len(new_text), 1)
        for i in range(len(new_text)):
            times[i] = (
                fallback_start + (fallback_end - fallback_start) * i / span,
                fallback_start + (fallback_end - fallback_start) * (i + 1) / span,
            )
        return times  # type: ignore[return-value]

    matcher = difflib.SequenceMatcher(None, orig_text, new_text, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            for k in range(j2 - j1):
                times[j1 + k] = (timeline[i1 + k][1], timeline[i1 + k][2])
        elif tag == "replace":
            olen = i2 - i1
            nlen = j2 - j1
            if olen <= 0:
                fill_insert(j1, j2, i1)
            else:
                for k in range(nlen):
                    oi = i1 + min(olen - 1, int(k * olen / nlen))
                    times[j1 + k] = (timeline[oi][1], timeline[oi][2])
        elif tag == "insert":
            fill_insert(j1, j2, i1)
        # delete: 无新字符，忽略

    # 填充缺失，并保证单调不减
    last = fallback_start
    result: list[tuple[float, float]] = []
    for i in range(len(new_text)):
        value = times[i]
        if value is None:
            value = (last, last)
        start, end = value
        if start < last:
            start = last
        if end < start:
            end = start
        result.append((start, end))
        last = end
    return result


# --------------------------------------------------------------------------- #
# 按最大字数切分
# --------------------------------------------------------------------------- #
def split_spans(text: str, max_chars: int) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    n = len(text)
    i = 0
    while i < n:
        while i < n and text[i] in " \t":
            i += 1
        if i >= n:
            break
        if n - i <= max_chars:
            spans.append((i, n))
            break
        limit = min(i + max_chars, n)
        brk = None
        for charset in (STRONG_BREAKS, WEAK_BREAKS, SPACE_BREAKS):
            for k in range(limit, i + 1, -1):
                if text[k - 1] in charset:
                    brk = k
                    break
            if brk is not None:
                break
        if brk is None:
            brk = limit
            # 硬切时把紧随其后的收尾标点并进上一句，避免下一句以标点开头
            while brk < n and text[brk] in TRAILING_PUNCT:
                brk += 1
        spans.append((i, brk))
        i = brk
    return spans


# --------------------------------------------------------------------------- #
# 由「改写文本 + 时间轴」生成字幕
# --------------------------------------------------------------------------- #
def reflow(
    new_text: str,
    timeline: list[tuple[str, float, float]],
    block_start: float,
    block_end: float,
    max_chars: int,
    min_duration: float,
    start_pad: float = 0.06,
    end_pad: float = 0.18,
) -> list[Cue]:
    new_text = new_text.strip()
    if not new_text:
        return []
    times = align_times(timeline, new_text, block_start, block_end)
    spans = split_spans(new_text, max_chars)
    cues: list[Cue] = []
    for a, b in spans:
        chunk = new_text[a:b].strip()
        if not chunk:
            continue
        start = times[a][0] - start_pad
        end = times[b - 1][1] + end_pad
        if end - start < min_duration:
            end = start + min_duration
        cues.append(Cue(start=max(start, 0.0), end=max(end, 0.0), text=chunk))
    return _fix_timing(cues, min_duration, block_start, block_end)


def _fix_timing(
    cues: list[Cue], min_duration: float, block_start: float, block_end: float
) -> list[Cue]:
    if not cues:
        return cues
    cues[0].start = max(cues[0].start, block_start)
    cues[-1].end = min(cues[-1].end, max(block_end, cues[-1].start + 0.2))
    for i, cue in enumerate(cues):
        if i > 0:
            prev = cues[i - 1]
            if cue.start < prev.end:
                # 让上一句收敛到本句开始，避免重叠
                prev.end = max(prev.start + 0.2, min(prev.end, cue.start))
                if cue.start < prev.end:
                    cue.start = prev.end
        if cue.end < cue.start + min_duration:
            cue.start = max(block_start, cue.start)
            cue.end = cue.start + min_duration
    return cues


def merge_short_cues(
    cues: list[Cue],
    max_chars: int,
    gap: float = 0.5,
    min_chars: int = 4,
) -> list[Cue]:
    """把过短的字幕合并到相邻字幕，减少闪烁。"""
    if not cues:
        return cues
    merged: list[Cue] = [cues[0]]
    for cue in cues[1:]:
        last = merged[-1]
        combined = last.text + cue.text
        if (
            len(cue.text) <= min_chars
            and cue.start - last.end <= gap
            and len(combined) <= max_chars
        ):
            last.text = combined
            last.end = cue.end
        elif (
            len(last.text) <= min_chars
            and cue.start - last.end <= gap
            and len(combined) <= max_chars
        ):
            last.text = combined
            last.end = cue.end
        else:
            merged.append(cue)
    return merged


def normalize_cues(cues: list[Cue]) -> list[Cue]:
    """排序、去重、保证时间单调不重叠。"""
    cues = sorted((c for c in cues if c.text.strip()), key=lambda c: c.start)
    for i, cue in enumerate(cues):
        if cue.end <= cue.start:
            cue.end = cue.start + 0.5
        if i > 0 and cue.start < cues[i - 1].end:
            cues[i - 1].end = min(cues[i - 1].end, cue.start)
            if cues[i - 1].end <= cues[i - 1].start:
                cues[i - 1].end = cues[i - 1].start + 0.1
    return cues
