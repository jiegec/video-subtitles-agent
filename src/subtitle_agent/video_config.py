"""per-video（per .mp4）配置文件。

每个视频都有自己的工程配置，文件放在视频旁边：

    <视频目录>/<视频名>.subtitle.json

结构：
{
  "version": 1,
  "title": "第一周 第一小节",
  "context": "本视频讲了……",
  "glossary": [{"term": "哈希表", "aliases": ["哈西表"], "note": "数据结构"}],
  "documents": ["资料/第一周.pdf", "https://..."],
  "frame_interval": 45
}

配置可以在 Web 里编辑、导出、上传，也可以直接复用另一个视频的配置。
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from pathlib import Path

LOCK = threading.Lock()
CONFIG_SUFFIX = ".subtitle.json"
VIDEO_EXTENSIONS = {".mp4", ".mkv", ".mov", ".webm", ".avi", ".flv", ".m4v", ".ts"}


def config_path_for(video: Path) -> Path:
    return video.parent / (video.stem + CONFIG_SUFFIX)


def default_config() -> dict:
    return {
        "version": 1,
        "title": "",
        "context": "",
        "glossary": [],
        "documents": [],
        "frame_interval": 45,
    }


def normalize_glossary(raw) -> list[dict]:
    """统一成 [{"term","aliases","note"}]。"""
    result: list[dict] = []
    if not raw:
        return result
    if isinstance(raw, dict):
        raw = [{"term": k, "note": v} if isinstance(v, str) else {"term": k, **v} for k, v in raw.items()]
    if not isinstance(raw, list):
        return result
    for item in raw:
        if isinstance(item, str):
            text = item.strip()
            if not text:
                continue
            if "=" in text:
                term, _, note = text.partition("=")
                result.append({"term": term.strip(), "aliases": [], "note": note.strip()})
            else:
                result.append({"term": text, "aliases": [], "note": ""})
        elif isinstance(item, dict) and item.get("term"):
            aliases = item.get("aliases") or []
            if isinstance(aliases, str):
                aliases = [a.strip() for a in aliases.replace("，", ",").split(",") if a.strip()]
            result.append(
                {
                    "term": str(item["term"]).strip(),
                    "aliases": [str(a).strip() for a in aliases if str(a).strip()],
                    "note": str(item.get("note", "")).strip(),
                }
            )
    return result


def normalize_documents(raw) -> list[str]:
    if not raw:
        return []
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    return [str(x).strip() for x in raw if str(x).strip()]


@dataclass
class EffectiveContext:
    title: str = ""
    context: str = ""
    glossary: list[dict] = field(default_factory=list)
    documents: list[str] = field(default_factory=list)
    frame_interval: int = 45


class VideoConfig:
    def __init__(self, path: Path):
        self.path = path
        self.raw: dict = default_config()
        self.load()

    # ------------------------------------------------------------------ #
    def load(self) -> None:
        if self.path.is_file():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    merged = default_config()
                    merged.update(data)
                    self.raw = merged
            except Exception:
                self.raw = default_config()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with LOCK:
            tmp = self.path.with_suffix(self.path.suffix + ".tmp")
            tmp.write_text(
                json.dumps(self.raw, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            tmp.replace(self.path)

    def update(self, data: dict) -> None:
        if not isinstance(data, dict):
            raise ValueError("配置必须是 JSON 对象")
        merged = default_config()
        merged.update(self.raw)
        for key in ("title", "context"):
            if key in data:
                merged[key] = str(data.get(key) or "")
        if "glossary" in data:
            merged["glossary"] = normalize_glossary(data.get("glossary"))
        if "documents" in data:
            merged["documents"] = normalize_documents(data.get("documents"))
        if "frame_interval" in data:
            try:
                merged["frame_interval"] = max(5, int(data.get("frame_interval")))
            except (TypeError, ValueError):
                pass
        self.raw = merged
        self.save()

    def apply(self, data: dict) -> None:
        """整体替换（用于复用/导入另一个配置）。"""
        if not isinstance(data, dict):
            raise ValueError("配置必须是 JSON 对象")
        merged = default_config()
        merged.update({k: v for k, v in data.items() if k in merged})
        self.raw = merged
        self.save()

    # ------------------------------------------------------------------ #
    def effective(self, default_frame_interval: int = 45) -> EffectiveContext:
        fallback_title = self.path.name
        if fallback_title.endswith(CONFIG_SUFFIX):
            fallback_title = fallback_title[: -len(CONFIG_SUFFIX)]
        return EffectiveContext(
            title=str(self.raw.get("title") or fallback_title),
            context=str(self.raw.get("context") or ""),
            glossary=normalize_glossary(self.raw.get("glossary")),
            documents=normalize_documents(self.raw.get("documents")),
            frame_interval=int(self.raw.get("frame_interval") or default_frame_interval),
        )

    def effective_dict(self, default_frame_interval: int = 45) -> dict:
        v = self.effective(default_frame_interval)
        return {
            "title": v.title,
            "context": v.context,
            "glossary": v.glossary,
            "documents": v.documents,
            "frame_interval": v.frame_interval,
        }


def list_configs(project_dir: Path) -> list[dict]:
    """列出 project 目录下所有视频配置文件，供「复用配置」选择。"""
    if not project_dir.is_dir():
        return []
    configs: list[dict] = []
    seen: set[str] = set()

    def add(video: Path | None, path: Path) -> None:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            data = {}
        title = data.get("title") or (video.stem if video else path.name)
        configs.append(
            {
                "config_path": str(path.relative_to(project_dir)),
                "video_path": str(video.relative_to(project_dir)) if video else "",
                "video_exists": bool(video and video.is_file()),
                "title": title,
                "glossary_count": len(normalize_glossary(data.get("glossary"))),
                "document_count": len(normalize_documents(data.get("documents"))),
            }
        )
        seen.add(str(path.resolve()))

    # 先按视频找配置，确保能读出视频文件名
    for video in sorted(project_dir.rglob("*")):
        if not video.is_file() or video.suffix.lower() not in VIDEO_EXTENSIONS:
            continue
        path = config_path_for(video)
        if path.is_file():
            add(video, path)

    # 再补充没有对应视频的孤儿配置
    for path in sorted(project_dir.rglob(f"*{CONFIG_SUFFIX}")):
        if not path.is_file() or str(path.resolve()) in seen:
            continue
        stem = path.name[: -len(CONFIG_SUFFIX)]
        video = next(
            (c for c in path.parent.glob(stem + ".*") if c.suffix.lower() in VIDEO_EXTENSIONS),
            None,
        )
        add(video, path)

    return configs
