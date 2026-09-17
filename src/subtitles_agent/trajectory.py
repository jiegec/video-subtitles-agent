"""生成轨迹（trajectory）：记录 whisper 原始字幕、注入的上下文、LLM 输入输出、
以及最终重排结果，供 Web 端渲染「变化过程」。
"""

from __future__ import annotations

import difflib
import json
import time
from pathlib import Path

TRACE_SUFFIX = ".trajectory.json"


def trace_path_for(srt_path: Path) -> Path:
    return srt_path.with_suffix("").with_suffix(TRACE_SUFFIX)


def char_diff(before: str, after: str) -> list[dict]:
    """字符级 diff，返回 [{op, a, b}]，op ∈ equal/delete/insert/replace。"""
    before = before or ""
    after = after or ""
    matcher = difflib.SequenceMatcher(None, before, after, autojunk=False)
    ops: list[dict] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        ops.append({"op": tag, "a": before[i1:i2], "b": after[j1:j2]})
    return ops


def diff_stats(ops: list[dict]) -> dict:
    deleted = sum(len(o["a"]) for o in ops if o["op"] in ("delete", "replace"))
    inserted = sum(len(o["b"]) for o in ops if o["op"] in ("insert", "replace"))
    return {"deleted_chars": deleted, "inserted_chars": inserted}


def save_trace(path: Path, trace: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    trace["created_at"] = time.time()
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(trace, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def load_trace(path: Path) -> dict | None:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
