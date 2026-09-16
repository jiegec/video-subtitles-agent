"""视频库：扫描 project 目录、汇总每个视频的产物状态。"""

from __future__ import annotations

import os
import threading
from pathlib import Path

from .config import Settings
from .frames import existing_frames
from .subtitle import parse_srt
from .transcribe import probe_duration, scan_videos


class Library:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._duration_cache: dict[tuple[str, float], float | None] = {}
        self._lock = threading.Lock()

    def _duration(self, video: Path) -> float | None:
        try:
            mtime = video.stat().st_mtime
        except OSError:
            return None
        key = (str(video), mtime)
        with self._lock:
            if key in self._duration_cache:
                return self._duration_cache[key]
        value = probe_duration(self.settings, video)
        with self._lock:
            self._duration_cache[key] = value
        return value

    def resolve(self, rel_path: str) -> Path:
        """把相对路径解析成 project 目录内的路径，并阻止越权访问。

        注意：不解析最后一级的符号链接（视频常常是软链），只在字面路径上做越界检查。
        """
        root = Path(os.path.abspath(str(self.settings.project_dir)))
        candidate = Path(rel_path)
        if not candidate.is_absolute():
            candidate = root / candidate
        normalized = Path(os.path.normpath(str(candidate)))
        if normalized != root and root not in normalized.parents:
            raise PermissionError("路径不在 project 目录内")
        return normalized

    def describe(self, video: Path) -> dict:
        rel = str(video.relative_to(self.settings.project_dir))
        artifacts = self.settings.artifact_paths(rel)
        wav = artifacts["wav"]
        whisper_json = artifacts["whisper_json"]
        srt = artifacts["srt"]
        cue_count = 0
        srt_mtime = None
        if srt.is_file():
            try:
                cue_count = len(parse_srt(srt.read_text(encoding="utf-8")))
                srt_mtime = srt.stat().st_mtime
            except Exception:
                cue_count = 0
        frame_count = len(existing_frames(artifacts["frames_dir"]))
        return {
            "path": rel,
            "name": video.name,
            "dir": str(video.parent.relative_to(self.settings.project_dir)) or ".",
            "size": video.stat().st_size if video.exists() else 0,
            "duration": self._duration(video),
            "has_wav": wav.is_file(),
            "has_whisper": whisper_json.is_file(),
            "has_srt": srt.is_file(),
            "has_config": artifacts["config"].is_file(),
            "has_frames": frame_count > 0,
            "frame_count": frame_count,
            "cue_count": cue_count,
            "srt_mtime": srt_mtime,
            "wav_size": wav.stat().st_size if wav.is_file() else 0,
        }

    def list_videos(self) -> list[dict]:
        return [self.describe(v) for v in scan_videos(self.settings.project_dir)]

    def status(self, rel_path: str) -> dict:
        return self.describe(self.resolve(rel_path))
