"""媒体处理：ffmpeg 抽音频、whisper-cli 转写、以及转写结果的解析与分块。"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from .config import Settings
from .video_config import VIDEO_EXTENSIONS

LogFn = Callable[[str], None]


def _noop(_: str) -> None:
    pass


@dataclass
class Segment:
    start: float
    end: float
    text: str
    tokens: list[dict]

    @property
    def char_count(self) -> int:
        return len(self.text)


@dataclass
class Block:
    start: float
    end: float
    text: str
    tokens: list[dict]

    @property
    def char_count(self) -> int:
        return len(self.text)


# --------------------------------------------------------------------------- #
# ffmpeg
# --------------------------------------------------------------------------- #
def extract_wav(settings: Settings, video: Path, wav: Path, log: LogFn = _noop) -> Path:
    wav.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        settings.ffmpeg_bin,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(video),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-c:a",
        "pcm_s16le",
        str(wav),
    ]
    log(f"$ {' '.join(cmd)}")
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg 抽取音频失败：\n{proc.stdout.strip()}")
    log(f"音频已生成：{wav.name} ({wav.stat().st_size / 1e6:.1f} MB)")
    return wav


def probe_duration(settings: Settings, media: Path) -> float | None:
    ffprobe = str(Path(settings.ffmpeg_bin).with_name("ffprobe"))
    try:
        out = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                str(media),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=30,
        )
        return float(out.stdout.strip())
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# whisper-cli
# --------------------------------------------------------------------------- #
_PROGRESS_RE = re.compile(r"progress\s*=\s*(\d+)%")


def run_whisper(
    settings: Settings,
    wav: Path,
    out_prefix: Path,
    log: LogFn = _noop,
    progress: Callable[[int], None] | None = None,
    prompt: str = "",
) -> Path:
    out_prefix.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        settings.whisper_bin,
        "-m",
        str(settings.whisper_model),
        "-f",
        str(wav),
        "-l",
        settings.whisper_lang,
        "-t",
        str(settings.whisper_threads),
        "-ojf",  # 输出带 token 时间戳的完整 JSON
        "-of",
        str(out_prefix),
        "-pp",
    ]
    if prompt:
        cmd += ["--prompt", prompt]
    log(f"$ {' '.join(cmd)}")
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    assert proc.stdout is not None
    tail: list[str] = []
    for line in proc.stdout:
        line = line.rstrip("\n")
        if progress:
            match = _PROGRESS_RE.search(line)
            if match:
                progress(int(match.group(1)))
        if line.strip():
            tail.append(line)
            if len(tail) > 400:
                tail.pop(0)
    code = proc.wait()
    if code != 0:
        raise RuntimeError("whisper-cli 运行失败：\n" + "\n".join(tail[-40:]))
    json_path = out_prefix.parent / (out_prefix.name + ".json")
    if not json_path.is_file():
        raise RuntimeError(f"whisper-cli 未生成 JSON：{json_path}")
    log(f"转写完成：{json_path.name}")
    return json_path


# --------------------------------------------------------------------------- #
# 解析与分块
# --------------------------------------------------------------------------- #
def load_segments(whisper_json: Path) -> list[Segment]:
    data = json.loads(whisper_json.read_text(encoding="utf-8"))
    segments: list[Segment] = []
    for item in data.get("transcription", []):
        offsets = item.get("offsets") or {}
        start = float(offsets.get("from", 0)) / 1000.0
        end = float(offsets.get("to", start * 1000)) / 1000.0
        text = (item.get("text") or "").strip()
        tokens = item.get("tokens") or []
        if not text:
            continue
        segments.append(Segment(start=start, end=end, text=text, tokens=tokens))
    return segments


def group_blocks(
    segments: list[Segment],
    block_chars: int = 220,
    max_gap: float = 3.0,
) -> list[Block]:
    """把相邻 segment 合并成适合一次 LLM 处理的块，兼顾上下文与耗时。"""
    blocks: list[Block] = []
    current: list[Segment] = []
    current_len = 0

    def flush() -> None:
        nonlocal current, current_len
        if not current:
            return
        text = _join_text(seg.text for seg in current)
        tokens: list[dict] = []
        for seg in current:
            tokens.extend(seg.tokens)
        if text.strip():
            blocks.append(
                Block(
                    start=current[0].start,
                    end=current[-1].end,
                    text=text,
                    tokens=tokens,
                )
            )
        current = []
        current_len = 0

    for seg in segments:
        if current:
            gap = seg.start - current[-1].end
            if current_len + seg.char_count > block_chars or gap > max_gap:
                flush()
        current.append(seg)
        current_len += seg.char_count
    flush()
    return blocks


def _join_text(parts: Iterable[str]) -> str:
    chunks: list[str] = []
    for part in parts:
        part = part.strip()
        if not part:
            continue
        if chunks and _needs_space(chunks[-1], part):
            chunks.append(" ")
        chunks.append(part)
    return "".join(chunks)


def _needs_space(prev: str, nxt: str) -> bool:
    return bool(prev) and prev[-1].isascii() and prev[-1].isalnum() and nxt[0].isascii() and nxt[0].isalnum()


def build_initial_prompt(
    settings: Settings, glossary: list[dict] | None = None, limit: int = 220
) -> str:
    """拼出 whisper 的 initial prompt：简体中文指令 + 术语表，帮助识别专有名词。"""
    instruction = (settings.whisper_prompt or "").strip()
    terms = [str(g.get("term", "")).strip() for g in (glossary or []) if g.get("term")]
    # 从后往前丢弃术语，保证不把某个词从中间截断
    while terms:
        candidate = " ".join(
            part for part in (instruction, "相关术语：" + "、".join(terms) + "。") if part
        )
        if len(candidate) <= limit:
            return candidate
        terms.pop()
    return instruction[:limit]


# --------------------------------------------------------------------------- #
# 目录扫描
# --------------------------------------------------------------------------- #
def is_video(path: Path) -> bool:
    return path.suffix.lower() in VIDEO_EXTENSIONS


def scan_videos(project_dir: Path) -> list[Path]:
    if not project_dir.is_dir():
        return []
    videos = [p for p in project_dir.rglob("*") if p.is_file() and is_video(p)]
    return sorted(videos, key=lambda p: str(p).lower())
