"""字幕数据结构、SRT 读写、以及「保留时间轴」的重排/对齐算法。

核心思路：
- whisper 输出的每个 token 都带有起止时间，我们把它展开成「字符 -> 时间」的时间轴。
- LLM 改写后的文本通过 difflib 与原字符时间轴做字符级对齐，得到每个新字符的时间。
- 再按最大字数切分成字幕行，时间取该行首字符的开始与末字符的结束，
  这样 LLM 删除/纠错/增字时，时间与原始语音保持一致。
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass

from .trajectory import char_diff

# 断点字符：句末 > 句中 > 空格/换行
STRONG_BREAKS = "。！？!?；;…"
WEAK_BREAKS = "，,、：:"
SPACE_BREAKS = " \t\n"
ALL_BREAKS = STRONG_BREAKS + WEAK_BREAKS + SPACE_BREAKS
# 收尾标点（跟随上一行）/ 开标点（跟随下一行）
CLOSING_PUNCT = "。，、；：！？!?…）)】」》〉”’\"'"
OPENING_PUNCT = "（(【「《〈“‘\"'"
# 标点稀疏时，优先在这些连接词处断句（相当于中文的“词/小句边界”）
CLAUSE_STARTS = (
    "所以", "但是", "不过", "然后", "那么", "因为", "如果", "而且", "并且",
    "因此", "于是", "接着", "首先", "其次", "最后", "同时", "另外", "例如",
    "比如", "也就是说", "换句话说", "总之", "其实", "当然", "可是", "还有",
    "至于", "关于", "对于", "为了", "虽然", "尽管", "只要", "只有", "无论",
)
MIN_CUE_CHARS = 4

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
        # delete: 新字符为空，直接进入下一处

    # 填充空位，并保证时间单调向后
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
def _latest_break(text: str, min_pos: int, max_pos: int) -> int | None:
    """在 [min_pos, max_pos] 里从后往前找最近的断点，返回断点下一字符的下标。

    断点位于标点处与连接词开头（让下一行从“所以/但是”等词开始）。
    """
    for k in range(max_pos, min_pos - 1, -1):
        if text[k - 1] in ALL_BREAKS:
            return k
        for word in CLAUSE_STARTS:
            if text.startswith(word, k):
                return k
    return None


def _adjust_break(
    text: str, start: int, brk: int, min_chars: int, hard_max: int, n: int
) -> int:
    """微调断点：收尾标点归上一行、左括号随其后内容、英文单词与数字保持完整。"""
    while brk < n and brk < hard_max and text[brk] in CLOSING_PUNCT:
        brk += 1
    while brk - start > min_chars and text[brk - 1] in OPENING_PUNCT:
        brk -= 1
    if (
        brk < n
        and brk > start
        and text[brk - 1].isascii()
        and text[brk - 1].isalnum()
        and text[brk].isascii()
        and text[brk].isalnum()
    ):
        word_end = brk
        while word_end < n and text[word_end].isascii() and text[word_end].isalnum():
            word_end += 1
        if word_end <= hard_max:
            brk = word_end  # 往后到词尾
        else:
            word_start = brk
            while (
                word_start > start
                and text[word_start - 1].isascii()
                and text[word_start - 1].isalnum()
            ):
                word_start -= 1
            if word_start - start >= min_chars:
                brk = word_start  # 回退到词首，保证英文单词完整
            elif word_start == start:
                brk = word_end  # 这个单词比上限还长，整词成行，硬切反而更难读
    return brk


def split_spans(text: str, max_chars: int) -> list[tuple[int, int]]:
    """按最大字数切分，尽量在标点/连接词/英文单词边界处断开。

    - 优先在 [start+min_chars, start+max_chars] 内**靠近上限**的断点切；
    - 区间内断点为空时，向后最多看 slack 个字找自然断点；
    - 兜底按字数硬切，英文单词与数字保持完整，左括号随其后内容。
    """
    max_chars = max(1, int(max_chars))
    min_chars = min(max(MIN_CUE_CHARS, max_chars // 3), max_chars)
    slack = max(2, max_chars // 3)
    spans: list[tuple[int, int]] = []
    n = len(text)
    i = 0
    while i < n:
        while i < n and text[i] in " \t\n":
            i += 1
        if i >= n:
            break
        if n - i <= max_chars:
            spans.append((i, n))
            break
        limit = i + max_chars
        hard_max = min(limit + slack, n)
        brk = _latest_break(text, i + min_chars, limit)
        if brk is None:
            brk = _latest_break(text, limit + 1, hard_max)
        if brk is None:
            brk = limit
        brk = _adjust_break(text, i, brk, min_chars, hard_max, n)
        # 剩余尾巴较短时并回上一句，保证句长
        if n - brk < min_chars and n - i <= hard_max - i:
            brk = n
        if brk >= n:
            spans.append((i, n))
            break
        if brk <= i:
            brk = min(i + max_chars, n)
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
                # 上一句结束时间收敛到本句开始时间，保证时间单调
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
        very_short = len(cue.text) <= 2 or len(last.text) <= 2
        # 极短（1~2 字）字幕即使间隔较远也并入相邻句，保证展示时长
        allowed_gap = max(gap, 1.5) if very_short else gap
        allowed_chars = max_chars + 2 if len(cue.text) <= 2 else max_chars
        if (
            (len(cue.text) <= min_chars or len(last.text) <= min_chars)
            and cue.start - last.end <= allowed_gap
            and len(combined) <= allowed_chars
        ):
            last.text = combined
            last.end = cue.end
        else:
            merged.append(cue)
    return merged


def normalize_cues(cues: list[Cue]) -> list[Cue]:
    """排序、去重，并修正重叠与零长度区间。"""
    cues = sorted((c for c in cues if c.text.strip()), key=lambda c: c.start)
    for i, cue in enumerate(cues):
        if cue.end <= cue.start:
            cue.end = cue.start + 0.5
        if i > 0 and cue.start < cues[i - 1].end:
            cues[i - 1].end = min(cues[i - 1].end, cue.start)
            if cues[i - 1].end <= cues[i - 1].start:
                cues[i - 1].end = cues[i - 1].start + 0.1
    return cues


# --------------------------------------------------------------------------- #
# 新旧字幕对比 / 合并
# --------------------------------------------------------------------------- #
def _overlap(a: Cue, b: Cue) -> float:
    return max(0.0, min(a.end, b.end) - max(a.start, b.start))


def diff_cues(old: list[Cue], new: list[Cue]) -> dict:
    """按时间对齐两版字幕，返回逐条变化。"""
    used: set[int] = set()
    changes: list[dict] = []
    added = removed = changed = equal = 0
    for cue in new:
        best = -1
        best_overlap = 0.0
        for j, candidate in enumerate(old):
            if j in used:
                continue
            overlap = _overlap(candidate, cue)
            if overlap > best_overlap:
                best_overlap = overlap
                best = j
        duration = min(cue.end - cue.start, old[best].end - old[best].start) if best >= 0 else 0.0
        if best >= 0 and best_overlap >= 0.25 * max(duration, 0.1):
            used.add(best)
            previous = old[best]
            if previous.text.strip() == cue.text.strip():
                equal += 1
            else:
                changed += 1
                changes.append(
                    {
                        "type": "changed",
                        "old": previous.as_dict(),
                        "new": cue.as_dict(),
                        "diff": char_diff(previous.text, cue.text),
                    }
                )
        else:
            added += 1
            changes.append({"type": "added", "old": None, "new": cue.as_dict()})
    for j, previous in enumerate(old):
        if j not in used:
            removed += 1
            changes.append({"type": "removed", "old": previous.as_dict(), "new": None})
    changes.sort(key=lambda c: (c["new"] or c["old"])["start"])
    return {
        "stats": {
            "total_old": len(old),
            "total_new": len(new),
            "added": added,
            "removed": removed,
            "changed": changed,
            "equal": equal,
        },
        "changes": changes,
    }


def merge_cue_lists(new: list[Cue], old: list[Cue], min_overlap: float = 0.5) -> list[Cue]:
    """以当前字幕为主，合并历史备份中的独有字幕。"""
    merged = list(new)
    for previous in old:
        span = max(previous.end - previous.start, 1e-6)
        if any(_overlap(previous, cue) >= min_overlap * span for cue in new):
            continue
        merged.append(Cue(previous.start, previous.end, previous.text))
    return normalize_cues(merged)
