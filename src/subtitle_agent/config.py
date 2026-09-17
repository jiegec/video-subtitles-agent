"""集中配置：从环境变量 / .env / ~/.pi 读取。

优先级：
1. 显式环境变量
2. 仓库根目录下的 .env
3. ~/.pi/agent/{auth.json,models.json,settings.json}（复用 pi 的 OpenAI 兼容配置）
4. 代码内默认值
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from .video_config import CONFIG_SUFFIX

REPO_ROOT = Path(__file__).resolve().parents[2]


def _parse_dotenv(path: Path) -> dict[str, str]:
    data: dict[str, str] = {}
    if not path.is_file():
        return data
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            data[key] = value
    return data


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _pi_defaults() -> dict[str, str]:
    """从 pi 的配置里推断 LLM 连接信息，方便开箱即用。"""
    result: dict[str, str] = {}
    pi_dir = Path.home() / ".pi" / "agent"
    auth = _read_json(pi_dir / "auth.json")
    models = _read_json(pi_dir / "models.json")
    settings = _read_json(pi_dir / "settings.json")

    provider = settings.get("defaultProvider") or "axonhub-openai"
    providers = models.get("providers", {})

    # 优先找 openai 兼容的 provider
    chosen = provider if provider in providers else None
    if chosen is None:
        for name, spec in providers.items():
            if spec.get("api") == "openai-completions":
                chosen = name
                break
    if chosen:
        spec = providers.get(chosen, {})
        if spec.get("baseUrl"):
            result["LLM_BASE_URL"] = spec["baseUrl"]
        auth_entry = auth.get(chosen) or {}
        if auth_entry.get("key"):
            result["LLM_API_KEY"] = auth_entry["key"]

    default_model = settings.get("defaultModel")
    if default_model:
        result["LLM_MODEL"] = default_model
    return result


@dataclass
class Settings:
    project_dir: Path
    whisper_bin: str
    whisper_model: Path
    whisper_lang: str
    whisper_threads: int
    whisper_prompt: str
    ffmpeg_bin: str

    llm_base_url: str
    llm_api_key: str
    llm_model: str
    llm_concurrency: int
    llm_max_tokens: int
    llm_timeout: float
    llm_thinking: str

    default_max_chars: int
    default_min_duration: float
    llm_block_chars: int
    frame_interval: int
    frame_max_per_batch: int
    frame_max_count: int
    context_max_chars: int

    recover_noise_db: float
    recover_min_silence: float
    recover_min_gap: float
    recover_min_cps: float
    recover_low_conf_threshold: float
    recover_max_regions: int
    recover_model: Path | None

    srt_backup_keep: int
    srt_backup_interval: float

    # ---- 派生路径 ----
    @property
    def has_llm(self) -> bool:
        return bool(self.llm_api_key and self.llm_base_url and self.llm_model)

    def artifact_paths(self, rel_path: str) -> dict[str, Path]:
        video = self.project_dir / rel_path
        stem = video.with_suffix("")
        return {
            "video": video,
            "wav": stem.with_suffix(".wav"),
            "whisper_json": video.parent / (video.stem + ".whisper.json"),
            "srt": video.parent / (video.stem + ".srt"),
            "frames_dir": video.parent / (video.stem + ".frames"),
            "context_cache": video.parent / (video.stem + ".context.json"),
            "trajectory": video.parent / (video.stem + ".trajectory.json"),
            "gaps": video.parent / (video.stem + ".gaps.json"),
            "backups_dir": video.parent / (video.stem + ".backups"),
            "config": video.parent / (video.stem + CONFIG_SUFFIX),
        }


def load_settings() -> Settings:
    env: dict[str, str] = {}
    env.update(_pi_defaults())
    env.update(_parse_dotenv(REPO_ROOT / ".env"))
    env.update(
        {
            k: v
            for k, v in os.environ.items()
            if k in _KNOWN_KEYS
            or k.startswith("LLM_")
            or k.startswith("WHISPER_")
            or k.startswith("SUBTITLE_")
            or k in {"PROJECT_DIR", "FFMPEG_BIN"}
        }
    )

    def get(key: str, default: str) -> str:
        value = env.get(key, default)
        return value if value not in (None, "") else default

    project_dir = Path(get("PROJECT_DIR", str(REPO_ROOT / "project"))).expanduser()
    if not project_dir.is_absolute():
        project_dir = (REPO_ROOT / project_dir).resolve()

    model = Path(get("WHISPER_MODEL", str(REPO_ROOT / "ggml-medium.bin"))).expanduser()
    if not model.is_absolute():
        model = (REPO_ROOT / model).resolve()

    recover_model_raw = get("RECOVER_MODEL", "")
    recover_model: Path | None = None
    if recover_model_raw:
        recover_model = Path(recover_model_raw).expanduser()
        if not recover_model.is_absolute():
            recover_model = (REPO_ROOT / recover_model).resolve()

    return Settings(
        project_dir=project_dir,
        whisper_bin=get("WHISPER_BIN", "whisper-cli"),
        whisper_model=model,
        whisper_lang=get("WHISPER_LANG", "zh"),
        whisper_threads=int(
            get("WHISPER_THREADS", str(max(2, (os.cpu_count() or 4) // 2)))
        ),
        whisper_prompt=get("WHISPER_PROMPT", "以下是简体中文的语音内容。"),
        ffmpeg_bin=get("FFMPEG_BIN", "ffmpeg"),
        llm_base_url=get("LLM_BASE_URL", "https://api.openai.com/v1").rstrip("/"),
        llm_api_key=get("LLM_API_KEY", ""),
        llm_model=get("LLM_MODEL", "deepseek-v4-flash"),
        llm_concurrency=max(1, int(get("LLM_CONCURRENCY", "4"))),
        llm_max_tokens=int(get("LLM_MAX_TOKENS", "16384")),
        llm_timeout=float(get("LLM_TIMEOUT", "180")),
        llm_thinking=get("LLM_THINKING", "off"),
        default_max_chars=int(get("SUBTITLE_MAX_CHARS", "18")),
        default_min_duration=float(get("SUBTITLE_MIN_DURATION", "0.8")),
        llm_block_chars=int(get("LLM_BLOCK_CHARS", "220")),
        frame_interval=int(get("FRAME_INTERVAL", "45")),
        frame_max_per_batch=int(get("FRAME_MAX_PER_BATCH", "4")),
        frame_max_count=int(get("FRAME_MAX_COUNT", "240")),
        context_max_chars=int(get("CONTEXT_MAX_CHARS", "12000")),
        recover_noise_db=float(get("RECOVER_NOISE_DB", "-30")),
        recover_min_silence=float(get("RECOVER_MIN_SILENCE", "0.4")),
        recover_min_gap=float(get("RECOVER_MIN_GAP", "1.5")),
        recover_min_cps=float(get("RECOVER_MIN_CPS", "1.8")),
        recover_low_conf_threshold=float(get("RECOVER_LOW_CONF_THRESHOLD", "0.45")),
        recover_max_regions=int(get("RECOVER_MAX_REGIONS", "80")),
        recover_model=recover_model,
        srt_backup_keep=int(get("SRT_BACKUP_KEEP", "100")),
        srt_backup_interval=float(get("SRT_BACKUP_INTERVAL", "120")),
    )


_KNOWN_KEYS = {
    "PROJECT_DIR",
    "WHISPER_BIN",
    "WHISPER_MODEL",
    "WHISPER_LANG",
    "WHISPER_THREADS",
    "WHISPER_PROMPT",
    "FFMPEG_BIN",
    "LLM_BASE_URL",
    "LLM_API_KEY",
    "LLM_MODEL",
    "LLM_CONCURRENCY",
    "LLM_MAX_TOKENS",
    "LLM_TIMEOUT",
    "LLM_THINKING",
    "SUBTITLE_MAX_CHARS",
    "SUBTITLE_MIN_DURATION",
    "LLM_BLOCK_CHARS",
    "FRAME_INTERVAL",
    "FRAME_MAX_PER_BATCH",
    "FRAME_MAX_COUNT",
    "CONTEXT_MAX_CHARS",
    "RECOVER_NOISE_DB",
    "RECOVER_MIN_SILENCE",
    "RECOVER_MIN_GAP",
    "RECOVER_MIN_CPS",
    "RECOVER_LOW_CONF_THRESHOLD",
    "RECOVER_MAX_REGIONS",
    "RECOVER_MODEL",
    "SRT_BACKUP_KEEP",
    "SRT_BACKUP_INTERVAL",
}
