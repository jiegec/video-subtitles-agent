"""视频截图：抽取关键帧作为多模态 LLM 的画面上下文。

用 `fps=1/interval` 均匀抽帧并缩放到 640 宽，产物放在
`<video_stem>.frames/` 下，配一个 manifest.json 记录间隔与视频 mtime，
视频没变就复用。
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .config import Settings
from .transcribe import probe_duration


@dataclass
class Frame:
    index: int
    time: float
    path: Path


def manifest_path(frames_dir: Path) -> Path:
    return frames_dir / "manifest.json"


def existing_frames(frames_dir: Path) -> list[Frame]:
    manifest = manifest_path(frames_dir)
    if not manifest.is_file():
        return []
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except Exception:
        return []
    frames = []
    for item in data.get("frames", []):
        path = frames_dir / item["file"]
        if path.is_file():
            frames.append(
                Frame(index=int(item["index"]), time=float(item["time"]), path=path)
            )
    return frames


def ensure_frames(
    settings: Settings,
    video: Path,
    frames_dir: Path,
    interval: int,
    log: Callable[[str], None] = lambda _: None,
    force: bool = False,
) -> list[Frame]:
    interval = max(5, int(interval))
    max_count = max(1, settings.frame_max_count)
    duration = probe_duration(settings, video)
    if duration and duration / interval > max_count:
        new_interval = int(duration / max_count) + 1
        if new_interval > interval:
            log(f"视频较长（{duration / 60:.0f} 分钟），截图间隔从 {interval}s 调整为 {new_interval}s（上限 {max_count} 张）")
            interval = new_interval
    manifest = manifest_path(frames_dir)
    if not force and manifest.is_file():
        try:
            data = json.loads(manifest.read_text(encoding="utf-8"))
            same_interval = int(data.get("interval", 0)) == interval
            same_mtime = (
                abs(float(data.get("video_mtime", 0)) - video.stat().st_mtime) < 1
            )
            if same_interval and same_mtime:
                frames = existing_frames(frames_dir)
                if frames:
                    log(f"复用已有截图 {len(frames)} 张")
                    return frames
        except Exception:
            pass

    frames_dir.mkdir(parents=True, exist_ok=True)
    for old in frames_dir.glob("frame_*.jpg"):
        old.unlink()
    pattern = str(frames_dir / "frame_%05d.jpg")
    # -skip_frame nokey 只解码关键帧，速度快很多，再做均匀采样
    cmd = [
        settings.ffmpeg_bin,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-skip_frame",
        "nokey",
        "-i",
        str(video),
        "-vf",
        f"fps=1/{interval},scale=640:-2",
        "-q:v",
        "4",
        pattern,
    ]
    log(f"抽取视频截图：$ {' '.join(cmd)}")
    proc = subprocess.run(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
    )
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg 抽帧失败：\n{proc.stdout.strip()[-800:]}")

    files = sorted(frames_dir.glob("frame_*.jpg"))
    frames = [
        Frame(index=i + 1, time=(i) * interval, path=path)
        for i, path in enumerate(files)
    ]
    manifest.write_text(
        json.dumps(
            {
                "interval": interval,
                "video_mtime": video.stat().st_mtime,
                "frames": [
                    {"index": f.index, "time": f.time, "file": f.path.name}
                    for f in frames
                ],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    log(f"共生成 {len(frames)} 张截图 → {frames_dir.name}/")
    return frames


def select_frames(
    frames: list[Frame],
    start: float,
    end: float,
    limit: int = 4,
    margin: float = 6.0,
) -> list[Frame]:
    """挑选落在 [start-margin, end+margin] 内的截图，最多 limit 张。"""
    if not frames or limit <= 0:
        return []
    lo = start - margin
    hi = end + margin
    inside = [f for f in frames if lo <= f.time <= hi]
    if not inside:
        # 找最接近的一张
        nearest = min(frames, key=lambda f: abs(f.time - (start + end) / 2))
        return [nearest]
    if len(inside) <= limit:
        return inside
    # 均匀采样
    step = (len(inside) - 1) / (limit - 1) if limit > 1 else 0
    picked = []
    for i in range(limit):
        picked.append(inside[round(i * step)])
    # 去重并保持顺序
    result: list[Frame] = []
    seen = set()
    for f in picked:
        if f.index not in seen:
            seen.add(f.index)
            result.append(f)
    return result
