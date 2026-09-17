"""媒体处理：ffmpeg 抽音频、whisper-cli 转写、以及转写结果的解析与分块。"""

from __future__ import annotations

import json
import re
import subprocess
import time
from collections import Counter
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
# ffmpeg：抽音频（含反相检测）
# --------------------------------------------------------------------------- #
def _mean_volume(
    settings: Settings, media: Path, pan: str | None = None, duration: float | None = None
) -> float | None:
    """用 ffmpeg volumedetect 估算平均音量（dB）。pan 形如 "c0=c0"，用于只取某个声道。"""
    audio_filter = f"pan=mono|{pan},volumedetect" if pan else "volumedetect"
    cmd = [settings.ffmpeg_bin, "-hide_banner", "-nostats"]
    if duration:
        cmd += ["-t", str(duration)]
    cmd += ["-i", str(media), "-af", audio_filter, "-f", "null", "-"]
    try:
        proc = subprocess.run(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=600
        )
        match = _VOLUME_RE.search(proc.stdout or "")
        return float(match.group(1)) if match else None
    except Exception:
        return None


def probe_mean_volume(settings: Settings, wav: Path) -> float | None:
    return _mean_volume(settings, wav)


def choose_mono_filter(
    settings: Settings, media: Path, log: LogFn = _noop, sample: float = 120.0
) -> str | None:
    """选单声道混音方式，避免左右声道反相时 `-ac 1` 相互抵消。

    返回 pan 表达式（如 "c0=c0"），None 表示直接 `-ac 1` 平均即可。
    """
    left = _mean_volume(settings, media, "c0=c0", sample)
    right = _mean_volume(settings, media, "c0=c1", sample)
    if left is None or right is None:
        return None
    summed = _mean_volume(settings, media, "c0=0.5*c0+0.5*c1", sample)
    if summed is None:
        return None
    loudest = max(left, right)
    # 正常同相：混单与单声道差不多（<=4dB）；一强一弱会低 ~6dB；反相会低很多
    if summed >= loudest - 4:
        return None
    channel = "左" if left >= right else "右"
    log(f"⚠️ 检测到左右声道反相（左 {left:.1f}dB / 右 {right:.1f}dB / 混单 {summed:.1f}dB），改用{channel}声道提取")
    return "c0=c0" if left >= right else "c0=c1"


def extract_wav(settings: Settings, video: Path, wav: Path, log: LogFn = _noop) -> Path:
    wav.parent.mkdir(parents=True, exist_ok=True)
    pan = choose_mono_filter(settings, video, log)
    cmd = [
        settings.ffmpeg_bin,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(video),
        "-vn",
    ]
    if pan:
        cmd += ["-af", f"pan=mono|{pan}"]
    else:
        cmd += ["-ac", "1"]
    cmd += ["-ar", "16000", "-c:a", "pcm_s16le", str(wav)]
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
_VOLUME_RE = re.compile(r"mean_volume:\s*(-?[\d.]+) dB")


def run_whisper(
    settings: Settings,
    wav: Path,
    out_prefix: Path,
    log: LogFn = _noop,
    progress: Callable[[int], None] | None = None,
    prompt: str = "",
    model: Path | None = None,
    extra_args: list[str] | None = None,
) -> Path:
    out_prefix.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        settings.whisper_bin,
        "-m",
        str(model or settings.whisper_model),
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
    if extra_args:
        cmd += list(extra_args)
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
    started = time.time()
    last_reported = -10
    for line in proc.stdout:
        line = line.rstrip("\n")
        if progress:
            match = _PROGRESS_RE.search(line)
            if match:
                percent = int(match.group(1))
                progress(percent)
                # 每跳 >=5% 记一条日志（同时会更新界面上的当前消息），避免刷屏
                if percent >= last_reported + 5 or percent >= 100:
                    elapsed = time.time() - started
                    eta = elapsed / percent * (100 - percent) if percent > 0 else 0.0
                    log(
                        f"whisper 转写中：{percent}%（已用 {elapsed:.0f}s，"
                        f"约剩 {eta:.0f}s）"
                    )
                    last_reported = percent
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
_HALLUCINATION_STRIP = re.compile(r"[\s，。、！？!?,.—–\-…：:；;\"'“”‘’()（）\[\]【】《》<>]")


def _normalize_text(text: str) -> str:
    return _HALLUCINATION_STRIP.sub("", text or "")


def strip_hallucinations(
    segments: list[Segment],
    min_count: int = 5,
    ratio: float = 0.12,
    log: LogFn = _noop,
) -> tuple[list[Segment], int]:
    """去掉 whisper 在静音/噪声上产生的重复幻觉。

    同一句文本（去标点后）出现次数超过 max(min_count, ratio * 总段数)，就认为整句都是幻觉，
    全部丢弃；另外折叠连续出现 >=4 次的相同文本。返回（过滤后的片段, 丢弃数量）。
    """
    if not segments:
        return segments, 0
    total = len(segments)
    counts = Counter(_normalize_text(seg.text) for seg in segments)
    threshold = max(min_count, int(total * ratio))
    repeated = {text for text, count in counts.items() if text and count > threshold}

    kept: list[Segment] = []
    run_text: str | None = None
    run_count = 0
    for seg in segments:
        text = _normalize_text(seg.text)
        if text in repeated:
            continue
        if text and text == run_text:
            run_count += 1
            if run_count >= 4:
                continue  # 连续重复，折叠掉后续的
        else:
            run_text, run_count = text, 1
        kept.append(seg)

    removed = total - len(kept)
    if removed:
        example, count = counts.most_common(1)[0]
        log(
            f"⚠️ 检测到 whisper 重复幻觉：忽略 {removed}/{total} 段"
            f"（如“{example[:20]}”重复 {count} 次）"
        )
    if not kept:
        kept = segments[:1]
        log("⚠️ 所有片段都被判为幻觉，至少保留第一段")
    return kept, removed


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
