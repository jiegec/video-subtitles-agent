"""漏识别补齐：找出 whisper 可能漏掉的内容并针对性重转写。

两类漏识别：
1. **时间缺口（gap）**：音频里有语音，但没有任何转写片段覆盖该时间段
   （whisper 整段跳过）。用 ffmpeg `silencedetect` 求出语音区间，再减去已有转写的覆盖范围。
2. **稀疏片段（sparse）**：片段覆盖了时间，但字数明显偏少（语速异常低），
   很可能是 whisper 漏词或压缩。这类会在原时间范围内重转，必要时替换原片段。

对每个可疑区间切出音频单独重转（可用更大模型 / 关闭上下文），把结果合并回时间轴。
重转结果记录在 `<视频名>.gaps.json`，并可在 Web 的生成轨迹里查看。
"""

from __future__ import annotations

import copy
import json
import re
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .config import Settings
from .subtitle import _is_special_token
from .transcribe import Segment, load_segments, probe_duration, run_whisper

REPORT_SUFFIX = ".gaps.json"

_SIL_START = re.compile(r"silence_start:\s*(-?\d+(?:\.\d+)?)")
_SIL_END = re.compile(r"silence_end:\s*(-?\d+(?:\.\d+)?)")


def report_path_for(whisper_json: Path) -> Path:
    name = whisper_json.name
    if name.endswith(".whisper.json"):
        name = name[: -len(".whisper.json")]
    else:
        name = whisper_json.stem
    return whisper_json.parent / (name + REPORT_SUFFIX)


@dataclass
class Region:
    start: float
    end: float
    kind: str  # "gap" | "sparse"
    original_text: str = ""
    recovered: list[dict] = field(default_factory=list)

    @property
    def duration(self) -> float:
        return self.end - self.start

    @property
    def recovered_text(self) -> str:
        return "".join((s.get("text") or "").strip() for s in self.recovered)

    def as_dict(self) -> dict:
        return {
            "start": round(self.start, 3),
            "end": round(self.end, 3),
            "duration": round(self.duration, 3),
            "kind": self.kind,
            "original_text": self.original_text,
            "recovered": self.recovered,
            "recovered_text": self.recovered_text,
        }


# --------------------------------------------------------------------------- #
# 检测
# --------------------------------------------------------------------------- #
def detect_speech_regions(
    settings: Settings,
    wav: Path,
    noise_db: float = -30.0,
    min_silence: float = 0.4,
    log: Callable[[str], None] = lambda _: None,
) -> list[tuple[float, float]]:
    """用 ffmpeg silencedetect 求出「有声音」的区间。"""
    cmd = [
        settings.ffmpeg_bin,
        "-hide_banner",
        "-nostats",
        "-i",
        str(wav),
        "-af",
        f"silencedetect=noise={noise_db}dB:d={min_silence}",
        "-f",
        "null",
        "-",
    ]
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    output = proc.stdout or ""
    duration = probe_duration(settings, wav) or 0.0

    starts = [float(v) for v in _SIL_START.findall(output)]
    ends = [float(v) for v in _SIL_END.findall(output)]
    silences: list[tuple[float, float]] = []
    for i, start in enumerate(starts):
        end = ends[i] if i < len(ends) else duration
        if end < start:
            end = start
        silences.append((max(0.0, start), max(0.0, end)))
    silences.sort()

    regions: list[tuple[float, float]] = []
    pos = 0.0
    for start, end in silences:
        if start - pos >= 0.05:
            regions.append((pos, start))
        pos = max(pos, end)
    if duration and duration - pos >= 0.05:
        regions.append((pos, duration))
    log(f"语音检测：{len(regions)} 个语音区间，共 {sum(b - a for a, b in regions):.0f}s")
    return regions


def _merge_intervals(intervals: list[tuple[float, float]]) -> list[tuple[float, float]]:
    merged: list[list[float]] = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(a, b) for a, b in merged]


def find_gaps(
    segments: list[Segment],
    speech_regions: list[tuple[float, float]],
    min_gap: float = 1.5,
    pad: float = 0.2,
) -> list[Region]:
    """语音区间中没有任何转写覆盖的时段。"""
    covered = _merge_intervals([(s.start - pad, s.end + pad) for s in segments])
    gaps: list[Region] = []
    for region_start, region_end in speech_regions:
        cursor = region_start
        for cov_start, cov_end in covered:
            if cov_end <= cursor:
                continue
            if cov_start >= region_end:
                break
            if cov_start > cursor:
                gaps.append(Region(cursor, min(cov_start, region_end), "gap"))
            cursor = max(cursor, min(cov_end, region_end))
            if cursor >= region_end:
                break
        if cursor < region_end:
            gaps.append(Region(cursor, region_end, "gap"))
    return [g for g in gaps if g.duration >= min_gap]


def find_sparse(
    segments: list[Segment],
    min_duration: float = 3.0,
    min_cps: float = 1.8,
) -> list[Region]:
    """覆盖了时间但字数明显偏少的片段（疑似漏词）。"""
    sparse: list[Region] = []
    for seg in segments:
        duration = seg.end - seg.start
        if duration < min_duration:
            continue
        chars = len(re.sub(r"\s", "", seg.text))
        if chars / duration < min_cps:
            sparse.append(Region(seg.start, seg.end, "sparse", original_text=seg.text))
    return sparse


def find_low_confidence(
    segments: list[Segment],
    threshold: float = 0.45,
    min_duration: float = 1.0,
    max_gap: float = 0.6,
) -> list[Region]:
    """连续的低置信度 token 区间（whisper 自己也不确定的地方）。"""
    regions: list[Region] = []
    for seg in segments:
        run_start: float | None = None
        run_end = 0.0
        for token in seg.tokens:
            text = token.get("text", "")
            if not text or _is_special_token(text):
                continue
            offsets = token.get("offsets") or {}
            start = float(offsets.get("from", 0)) / 1000.0
            end = float(offsets.get("to", start * 1000)) / 1000.0
            probability = float(token.get("p", 1.0))
            if probability < threshold:
                if run_start is None or start - run_end > max_gap:
                    if run_start is not None and run_end - run_start >= min_duration:
                        regions.append(Region(run_start, run_end, "low_conf"))
                    run_start, run_end = start, end
                else:
                    run_end = max(run_end, end)
            elif run_start is not None:
                if run_end - run_start >= min_duration:
                    regions.append(Region(run_start, run_end, "low_conf"))
                run_start = None
        if run_start is not None and run_end - run_start >= min_duration:
            regions.append(Region(run_start, run_end, "low_conf"))
    return regions


_PRIORITY = {"gap": 3, "sparse": 2, "low_conf": 1}


def merge_regions(regions: list[Region], max_gap: float = 0.4) -> list[Region]:
    """合并重叠/相邻的可疑区间，保留优先级更高的类型。"""
    merged: list[Region] = []
    for region in sorted(regions, key=lambda r: r.start):
        if merged and region.start <= merged[-1].end + max_gap:
            last = merged[-1]
            last.end = max(last.end, region.end)
            if _PRIORITY[region.kind] > _PRIORITY[last.kind]:
                last.kind = region.kind
            if region.original_text and not last.original_text:
                last.original_text = region.original_text
        else:
            merged.append(Region(region.start, region.end, region.kind, region.original_text))
    return merged


# --------------------------------------------------------------------------- #
# 重转写
# --------------------------------------------------------------------------- #
def _shift_offsets(item: dict, delta_ms: int) -> dict:
    out = copy.deepcopy(item)
    offsets = dict(out.get("offsets") or {})
    offsets["from"] = int(offsets.get("from", 0)) + delta_ms
    offsets["to"] = int(offsets.get("to", 0)) + delta_ms
    out["offsets"] = offsets
    tokens = []
    for token in out.get("tokens") or []:
        token = dict(token)
        token_offsets = dict(token.get("offsets") or {})
        token_offsets["from"] = int(token_offsets.get("from", 0)) + delta_ms
        token_offsets["to"] = int(token_offsets.get("to", 0)) + delta_ms
        token["offsets"] = token_offsets
        tokens.append(token)
    out["tokens"] = tokens
    return out


def _slice_wav(settings: Settings, wav: Path, region: Region, dest: Path) -> None:
    cmd = [
        settings.ffmpeg_bin,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-ss",
        f"{region.start:.3f}",
        "-t",
        f"{region.duration:.3f}",
        "-i",
        str(wav),
        "-c:a",
        "pcm_s16le",
        str(dest),
    ]
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"切片失败：{proc.stdout.strip()[-300:]}")


def recover_regions(
    settings: Settings,
    wav: Path,
    regions: list[Region],
    model: Path | None = None,
    extra_args: list[str] | None = None,
    prompt: str = "",
    log: Callable[[str], None] = lambda _: None,
    progress: Callable[[int], None] | None = None,
) -> list[Region]:
    """对每个可疑区间切片重转，结果写回 region.recovered（绝对时间）。"""
    if not regions:
        return regions
    extra_args = extra_args if extra_args is not None else ["-mc", "0"]
    with tempfile.TemporaryDirectory(prefix="subtitle-recover-") as tmp:
        tmp_dir = Path(tmp)
        for index, region in enumerate(regions):
            slice_path = tmp_dir / f"region_{index:04d}.wav"
            prefix = tmp_dir / f"region_{index:04d}"
            try:
                _slice_wav(settings, wav, region, slice_path)
                json_path = run_whisper(
                    settings,
                    slice_path,
                    prefix,
                    model=model,
                    extra_args=extra_args,
                    prompt=prompt,
                )
                data = json.loads(json_path.read_text(encoding="utf-8"))
                delta = int(round(region.start * 1000))
                region.recovered = [
                    _shift_offsets(item, delta)
                    for item in data.get("transcription", [])
                    if (item.get("text") or "").strip()
                ]
                if region.recovered:
                    log(f"  补回 {region.start:.1f}s：{region.recovered_text[:40]}")
                else:
                    log(f"  区间 {region.start:.1f}-{region.end:.1f}s 重转后仍为空")
            except Exception as exc:  # noqa: BLE001
                log(f"  ⚠️ 区间 {region.start:.1f}-{region.end:.1f}s 重转失败：{exc}")
            if progress:
                progress(int((index + 1) * 100 / len(regions)))
    return regions


# --------------------------------------------------------------------------- #
# 报告与合并
# --------------------------------------------------------------------------- #
def build_report(settings: Settings, regions: list[Region], params: dict) -> dict:
    recovered_chars = sum(len(r.recovered_text) for r in regions)
    recovered_segments = sum(len(r.recovered) for r in regions)
    return {
        "version": 1,
        "created_at": time.time(),
        "params": params,
        "regions": [r.as_dict() for r in regions],
        "stats": {
            "gaps": sum(1 for r in regions if r.kind == "gap"),
            "sparse": sum(1 for r in regions if r.kind == "sparse"),
            "recovered_segments": recovered_segments,
            "recovered_chars": recovered_chars,
        },
    }


def save_report(path: Path, report: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def load_report(path: Path) -> dict | None:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _dict_to_segment(item: dict) -> Segment:
    offsets = item.get("offsets") or {}
    start = float(offsets.get("from", 0)) / 1000.0
    end = float(offsets.get("to", start * 1000)) / 1000.0
    return Segment(
        start=start,
        end=end,
        text=(item.get("text") or "").strip(),
        tokens=item.get("tokens") or [],
    )


def _overlap_ratio(seg: Segment, interval: tuple[float, float]) -> float:
    start, end = interval
    overlap = min(seg.end, end) - max(seg.start, start)
    if overlap <= 0:
        return 0.0
    span = max(seg.end - seg.start, 1e-6)
    return overlap / span


def load_merged_segments(whisper_json: Path, report: dict | None) -> list[Segment]:
    """把原转写与「时间缺口」的补齐结果合并成一个时间轴。

    只有 gap（有语音但完全没转写）才插入时间轴；sparse / low_conf 属于「已有转写但可能不准」，
    用 build_alts() 作为「另一版识别」交给 LLM 参考，避免直接替换丢失内容。
    """
    original = load_segments(whisper_json)
    if not report:
        return original
    inserts: list[Segment] = []
    for region in report.get("regions") or []:
        if region.get("kind") != "gap":
            continue
        for item in region.get("recovered") or []:
            segment = _dict_to_segment(item)
            if segment.text:
                inserts.append(segment)
    merged = list(original)
    for segment in inserts:
        if any(_overlap_ratio(segment, (o.start, o.end)) > 0.5 for o in original):
            continue
        merged.append(segment)
    return sorted(merged, key=lambda s: s.start)


def build_alts(blocks, report: dict | None, max_chars: int = 400) -> dict[int, str]:
    """把 sparse / low_conf 的重转结果按时间挂到对应 block 上，作为另一版识别供 LLM 参考。"""
    if not report:
        return {}
    hints: list[tuple[float, float, str]] = []
    for region in report.get("regions") or []:
        if region.get("kind") not in ("sparse", "low_conf"):
            continue
        text = "".join((s.get("text") or "").strip() for s in (region.get("recovered") or []))
        if text:
            hints.append((float(region["start"]), float(region["end"]), text))
    alts: dict[int, str] = {}
    for index, block in enumerate(blocks):
        pieces: list[str] = []
        for start, end, text in hints:
            if end <= block.start or start >= block.end:
                continue
            if text not in pieces:
                pieces.append(text)
        if not pieces:
            continue
        joined = "".join(pieces)[:max_chars]
        # 与原文完全重复的不需要再给
        if joined and joined not in block.text:
            alts[index] = joined
    return alts
