"""集中配置：读写当前工作目录下的 ``config.toml``。

所有配置都放在这一个 TOML 文件里：

- 在网页右上角「设置」里修改，保存后写回 ``config.toml``，立即生效。
- LLM 默认 ``source = "pi"``：base_url/api_key/model 从 ``~/.pi/agent/*`` 实时读取，不写入 ``config.toml``；
  想自己指定就改成 ``source = "manual"``。
- 优先级：``config.toml`` > ``~/.pi/agent/*``（仅当 source = "pi"） > 代码内默认值。

也可以用文本编辑器直接改 ``config.toml``，重启服务即可生效。
"""

from __future__ import annotations

import copy
import json
import os
import tomllib
from dataclasses import dataclass, fields
from pathlib import Path

from .video_config import CONFIG_SUFFIX

CONFIG_FILENAME = "config.toml"


def config_path() -> Path:
    """配置文件位置：当前工作目录下的 config.toml。"""
    return Path.cwd() / CONFIG_FILENAME


DEFAULT_CONFIG: dict = {
    "paths": {
        "project_dir": "project",
        "ffmpeg_bin": "ffmpeg",
        "whisper_bin": "whisper-cli",
        "whisper_model": "ggml-medium.bin",
    },
    "whisper": {
        "lang": "zh",
        "threads": max(2, (os.cpu_count() or 4) // 2),
        "prompt": "以下是简体中文的语音内容。",
    },
    "asr": {
        "backend": "whisper",  # whisper | tencent
    },
    "tencent": {
        "secret_id": "",
        "secret_key": "",
        "region": "ap-shanghai",
        "engine": "16k_zh_en_2.0",
        "chunk_seconds": 600,
    },
    "llm": {
        "source": "pi",
        "base_url": "https://api.openai.com/v1",
        "api_key": "",
        "model": "deepseek-v4-flash",
        "concurrency": 4,
        "max_tokens": 16384,
        "timeout": 180.0,
        "thinking": "off",
    },
    "subtitles": {
        "max_chars": 18,
        "min_duration": 0.8,
        "block_chars": 220,
    },
    "frames": {
        "interval": 45,
        "max_per_batch": 4,
        "max_count": 240,
    },
    "context": {
        "max_chars": 12000,
    },
    "recover": {
        "model": "",
        "noise_db": -30.0,
        "min_silence": 0.4,
        "min_gap": 1.5,
        "min_cps": 1.8,
        "low_conf_threshold": 0.45,
        "max_regions": 80,
    },
    "backup": {
        "keep": 100,
        "interval": 120.0,
    },
    "chapters": {
        "max_count": 10,
        "max_title_chars": 16,
        "input_chars": 24000,
    },
}

CONFIG_HEADER = """# 视频字幕 Agent 配置
# 可以直接编辑本文件，也可以在网页右上角「设置」里修改（保存后立即生效）。
# 所有相对路径都相对于本文件所在目录。
"""


# --------------------------------------------------------------------------- #
# TOML 读写（读取用标准库 tomllib，写入用下面这个极简序列化器）
# --------------------------------------------------------------------------- #
def _read_toml(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _toml_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    raise TypeError(f"无法写入 TOML 的值类型：{type(value)!r}")


def dumps_toml(data: dict) -> str:
    """把两层嵌套的 dict 序列化成带分节的 TOML。"""
    blocks = [CONFIG_HEADER.rstrip()]
    scalars = [
        f"{key} = {_toml_value(value)}"
        for key, value in data.items()
        if not isinstance(value, dict)
    ]
    if scalars:
        blocks.append("\n".join(scalars))
    for key, table in data.items():
        if not isinstance(table, dict):
            continue
        rows = [f"[{key}]"]
        rows.extend(f"{sub_key} = {_toml_value(sub_value)}" for sub_key, sub_value in table.items())
        blocks.append("\n".join(rows))
    return "\n\n".join(blocks).rstrip() + "\n"


def _deep_merge(base: dict, override: dict) -> dict:
    result = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


# --------------------------------------------------------------------------- #
# ~/.pi 默认值（方便开箱即用，不是必需的）
# --------------------------------------------------------------------------- #
def _read_json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _pi_defaults() -> dict:
    """从 pi 的配置里推断 LLM 连接信息。"""
    result: dict = {}
    pi_dir = Path.home() / ".pi" / "agent"
    auth = _read_json(pi_dir / "auth.json")
    models = _read_json(pi_dir / "models.json")
    settings = _read_json(pi_dir / "settings.json")

    provider = settings.get("defaultProvider") or "axonhub-openai"
    providers = models.get("providers", {})

    chosen = provider if provider in providers else None
    if chosen is None:
        for name, spec in providers.items():
            if spec.get("api") == "openai-completions":
                chosen = name
                break
    llm: dict = {}
    if chosen:
        spec = providers.get(chosen, {})
        if spec.get("baseUrl"):
            llm["base_url"] = spec["baseUrl"]
        auth_entry = auth.get(chosen) or {}
        if auth_entry.get("key"):
            llm["api_key"] = auth_entry["key"]

    default_model = settings.get("defaultModel")
    if default_model:
        llm["model"] = default_model
    if llm:
        result["llm"] = llm
    return result


LLM_PI_MANAGED = ("base_url", "api_key", "model")


def _detect_llm_source(file_config: dict) -> str:
    """判断 LLM 使用 pi 还是手动配置。

    - 显式写了 ``source`` 就用它；
    - 老配置没有 ``source``：
      * 填了 base_url/api_key/model，且它们与 pi 的值一致，说明只是把 pi 的配置拷贝了一份，
        当作 ``pi``（下次保存会清掉这些拷贝）；
      * 否则当作手动配置；
    - 什么都没填（首次运行）则默认从 pi 获取。
    """
    llm = file_config.get("llm") if isinstance(file_config, dict) else None
    if isinstance(llm, dict):
        source = llm.get("source")
        if source in ("pi", "manual"):
            return source
        filled = {key: str(llm.get(key) or "").strip() for key in LLM_PI_MANAGED}
        if any(filled.values()):
            pi_llm = _pi_defaults().get("llm", {})
            same_as_pi = filled["api_key"] != "" and all(
                not filled[key] or filled[key] == str(pi_llm.get(key) or "").strip()
                for key in LLM_PI_MANAGED
            )
            return "pi" if same_as_pi else "manual"
    return "pi"


def _resolve_settings(file_config: dict, base_dir: Path) -> "Settings":
    """按优先级合并配置：默认值 < pi（模型来源为 pi 时） < config.toml。

    模型来源为 pi 时，base_url/api_key/model 一律从 pi 实时读取，
    不读取也不保存 config.toml 里的这三个字段。
    """
    source = _detect_llm_source(file_config)
    merged = _deep_merge(DEFAULT_CONFIG, {})
    if source == "pi":
        merged = _deep_merge(merged, _pi_defaults())

    file_cfg = copy.deepcopy(file_config) if isinstance(file_config, dict) else {}
    llm_file = file_cfg.get("llm")
    if source == "pi" and isinstance(llm_file, dict):
        for key in LLM_PI_MANAGED:
            llm_file.pop(key, None)
    merged = _deep_merge(merged, file_cfg)
    merged.setdefault("llm", {})["source"] = source
    return _settings_from_dict(merged, base_dir)


# --------------------------------------------------------------------------- #
# 路径 / 数值工具
# --------------------------------------------------------------------------- #
def _resolve_path(raw: str, base_dir: Path) -> Path:
    path = Path(str(raw)).expanduser()
    if not path.is_absolute():
        path = (base_dir / path).resolve()
    return path


def _display_path(path: Path | None, base_dir: Path) -> str:
    if path is None:
        return ""
    try:
        return str(path.relative_to(base_dir))
    except ValueError:
        return str(path)


def _as_int(value, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_str(value, default: str) -> str:
    if value is None:
        return default
    text = str(value)
    return text if text != "" else default


def _table(data: dict, key: str) -> dict:
    value = data.get(key)
    return value if isinstance(value, dict) else {}


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #
@dataclass
class Settings:
    project_dir: Path
    whisper_bin: str
    whisper_model: Path
    whisper_lang: str
    whisper_threads: int
    whisper_prompt: str
    ffmpeg_bin: str

    asr_backend: str

    tencent_secret_id: str
    tencent_secret_key: str
    tencent_region: str
    tencent_engine: str
    tencent_chunk_seconds: int

    llm_source: str
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

    chapters_max_count: int
    chapters_max_title_chars: int
    chapters_input_chars: int

    config_dir: Path

    # ---- 派生属性 ----
    @property
    def has_llm(self) -> bool:
        return bool(self.llm_api_key and self.llm_base_url and self.llm_model)

    def artifact_paths(self, rel_path: str) -> dict[str, Path]:
        video = self.project_dir / rel_path
        stem = video.with_suffix("")
        asr_json = video.parent / (video.stem + ".asr.json")
        return {
            "video": video,
            "wav": stem.with_suffix(".wav"),
            "asr_json": asr_json,
            "srt": video.parent / (video.stem + ".srt"),
            "frames_dir": video.parent / (video.stem + ".frames"),
            "context_cache": video.parent / (video.stem + ".context.json"),
            "trajectory": video.parent / (video.stem + ".trajectory.json"),
            "gaps": video.parent / (video.stem + ".gaps.json"),
            "backups_dir": video.parent / (video.stem + ".backups"),
            "chapters": video.parent / (video.stem + ".chapters.txt"),
            "config": video.parent / (video.stem + CONFIG_SUFFIX),
        }

    # ---- 序列化 ----
    def to_dict(self) -> dict:
        """导出成嵌套 dict（路径尽量显示成相对路径）。"""
        base = self.config_dir
        return {
            "paths": {
                "project_dir": _display_path(self.project_dir, base),
                "ffmpeg_bin": self.ffmpeg_bin,
                "whisper_bin": self.whisper_bin,
                "whisper_model": _display_path(self.whisper_model, base),
            },
            "whisper": {
                "lang": self.whisper_lang,
                "threads": self.whisper_threads,
                "prompt": self.whisper_prompt,
            },
            "asr": {
                "backend": self.asr_backend,
            },
            "tencent": {
                "secret_id": self.tencent_secret_id,
                "secret_key": self.tencent_secret_key,
                "region": self.tencent_region,
                "engine": self.tencent_engine,
                "chunk_seconds": self.tencent_chunk_seconds,
            },
            "llm": {
                "source": self.llm_source,
                "base_url": "" if self.llm_source == "pi" else self.llm_base_url,
                "api_key": "" if self.llm_source == "pi" else self.llm_api_key,
                "model": "" if self.llm_source == "pi" else self.llm_model,
                "concurrency": self.llm_concurrency,
                "max_tokens": self.llm_max_tokens,
                "timeout": self.llm_timeout,
                "thinking": self.llm_thinking,
            },
            "subtitles": {
                "max_chars": self.default_max_chars,
                "min_duration": self.default_min_duration,
                "block_chars": self.llm_block_chars,
            },
            "frames": {
                "interval": self.frame_interval,
                "max_per_batch": self.frame_max_per_batch,
                "max_count": self.frame_max_count,
            },
            "context": {
                "max_chars": self.context_max_chars,
            },
            "recover": {
                "model": _display_path(self.recover_model, base),
                "noise_db": self.recover_noise_db,
                "min_silence": self.recover_min_silence,
                "min_gap": self.recover_min_gap,
                "min_cps": self.recover_min_cps,
                "low_conf_threshold": self.recover_low_conf_threshold,
                "max_regions": self.recover_max_regions,
            },
            "backup": {
                "keep": self.srt_backup_keep,
                "interval": self.srt_backup_interval,
            },
            "chapters": {
                "max_count": self.chapters_max_count,
                "max_title_chars": self.chapters_max_title_chars,
                "input_chars": self.chapters_input_chars,
            },
        }

    def updated_with(self, data: dict) -> "Settings":
        """用一份（可能不完整的）配置覆盖当前值，返回新的 Settings。"""
        # self.to_dict() 已经是当前 config.toml 的内容，再叠加本次改动后重新解析
        merged = _deep_merge(self.to_dict(), data)
        return _resolve_settings(merged, self.config_dir)

    def copy_from(self, other: "Settings") -> None:
        """就地更新自身，使已有引用（library / jobs）也能拿到新配置。"""
        for field in fields(self):
            setattr(self, field.name, getattr(other, field.name))


def _settings_from_dict(data: dict, base_dir: Path) -> Settings:
    paths = _table(data, "paths")
    whisper = _table(data, "whisper")
    asr = _table(data, "asr")
    tencent = _table(data, "tencent")
    llm = _table(data, "llm")
    subtitles = _table(data, "subtitles")
    frames = _table(data, "frames")
    context = _table(data, "context")
    recover = _table(data, "recover")
    backup = _table(data, "backup")
    chapters = _table(data, "chapters")

    project_dir = _resolve_path(
        _as_str(paths.get("project_dir"), DEFAULT_CONFIG["paths"]["project_dir"]), base_dir
    )
    whisper_model = _resolve_path(
        _as_str(paths.get("whisper_model"), DEFAULT_CONFIG["paths"]["whisper_model"]), base_dir
    )
    recover_model_raw = str(recover.get("model") or "").strip()
    recover_model = _resolve_path(recover_model_raw, base_dir) if recover_model_raw else None

    return Settings(
        project_dir=project_dir,
        whisper_bin=_as_str(paths.get("whisper_bin"), DEFAULT_CONFIG["paths"]["whisper_bin"]),
        whisper_model=whisper_model,
        whisper_lang=_as_str(whisper.get("lang"), DEFAULT_CONFIG["whisper"]["lang"]),
        whisper_threads=max(
            1, _as_int(whisper.get("threads"), DEFAULT_CONFIG["whisper"]["threads"])
        ),
        whisper_prompt=_as_str(whisper.get("prompt"), DEFAULT_CONFIG["whisper"]["prompt"]),
        ffmpeg_bin=_as_str(paths.get("ffmpeg_bin"), DEFAULT_CONFIG["paths"]["ffmpeg_bin"]),
        asr_backend=(
            "tencent" if str(asr.get("backend") or "").lower() == "tencent" else "whisper"
        ),
        tencent_secret_id=str(tencent.get("secret_id") or ""),
        tencent_secret_key=str(tencent.get("secret_key") or ""),
        tencent_region=_as_str(
            tencent.get("region"), DEFAULT_CONFIG["tencent"]["region"]
        ),
        tencent_engine=_as_str(
            tencent.get("engine"), DEFAULT_CONFIG["tencent"]["engine"]
        ),
        tencent_chunk_seconds=min(
            1200,
            max(
                60,
                _as_int(tencent.get("chunk_seconds"), DEFAULT_CONFIG["tencent"]["chunk_seconds"]),
            ),
        ),
        llm_source=("manual" if llm.get("source") == "manual" else "pi"),
        llm_base_url=_as_str(llm.get("base_url"), DEFAULT_CONFIG["llm"]["base_url"]).rstrip("/"),
        llm_api_key=str(llm.get("api_key") or ""),
        llm_model=_as_str(llm.get("model"), DEFAULT_CONFIG["llm"]["model"]),
        llm_concurrency=max(1, _as_int(llm.get("concurrency"), DEFAULT_CONFIG["llm"]["concurrency"])),
        llm_max_tokens=max(1, _as_int(llm.get("max_tokens"), DEFAULT_CONFIG["llm"]["max_tokens"])),
        llm_timeout=max(1.0, _as_float(llm.get("timeout"), DEFAULT_CONFIG["llm"]["timeout"])),
        llm_thinking=_as_str(llm.get("thinking"), DEFAULT_CONFIG["llm"]["thinking"]),
        default_max_chars=max(
            1, _as_int(subtitles.get("max_chars"), DEFAULT_CONFIG["subtitles"]["max_chars"])
        ),
        default_min_duration=max(
            0.0,
            _as_float(subtitles.get("min_duration"), DEFAULT_CONFIG["subtitles"]["min_duration"]),
        ),
        llm_block_chars=max(
            1, _as_int(subtitles.get("block_chars"), DEFAULT_CONFIG["subtitles"]["block_chars"])
        ),
        frame_interval=max(1, _as_int(frames.get("interval"), DEFAULT_CONFIG["frames"]["interval"])),
        frame_max_per_batch=max(
            0, _as_int(frames.get("max_per_batch"), DEFAULT_CONFIG["frames"]["max_per_batch"])
        ),
        frame_max_count=max(
            1, _as_int(frames.get("max_count"), DEFAULT_CONFIG["frames"]["max_count"])
        ),
        context_max_chars=max(
            1, _as_int(context.get("max_chars"), DEFAULT_CONFIG["context"]["max_chars"])
        ),
        recover_noise_db=_as_float(
            recover.get("noise_db"), DEFAULT_CONFIG["recover"]["noise_db"]
        ),
        recover_min_silence=max(
            0.0,
            _as_float(recover.get("min_silence"), DEFAULT_CONFIG["recover"]["min_silence"]),
        ),
        recover_min_gap=max(
            0.0, _as_float(recover.get("min_gap"), DEFAULT_CONFIG["recover"]["min_gap"])
        ),
        recover_min_cps=max(
            0.0, _as_float(recover.get("min_cps"), DEFAULT_CONFIG["recover"]["min_cps"])
        ),
        recover_low_conf_threshold=_as_float(
            recover.get("low_conf_threshold"), DEFAULT_CONFIG["recover"]["low_conf_threshold"]
        ),
        recover_max_regions=max(
            1, _as_int(recover.get("max_regions"), DEFAULT_CONFIG["recover"]["max_regions"])
        ),
        recover_model=recover_model,
        srt_backup_keep=max(
            0, _as_int(backup.get("keep"), DEFAULT_CONFIG["backup"]["keep"])
        ),
        srt_backup_interval=max(
            0.0, _as_float(backup.get("interval"), DEFAULT_CONFIG["backup"]["interval"])
        ),
        chapters_max_count=max(
            1, _as_int(chapters.get("max_count"), DEFAULT_CONFIG["chapters"]["max_count"])
        ),
        chapters_max_title_chars=max(
            1,
            _as_int(
                chapters.get("max_title_chars"),
                DEFAULT_CONFIG["chapters"]["max_title_chars"],
            ),
        ),
        chapters_input_chars=max(
            2000,
            _as_int(chapters.get("input_chars"), DEFAULT_CONFIG["chapters"]["input_chars"]),
        ),
        config_dir=base_dir,
    )


# --------------------------------------------------------------------------- #
# 载入 / 保存
# --------------------------------------------------------------------------- #
def load_settings() -> Settings:
    """读取 config.toml；缺失的 LLM 项在来源为 pi 时从 ~/.pi 推断。"""
    path = config_path()
    return _resolve_settings(_read_toml(path), path.parent)


def save_settings(settings: Settings) -> Path:
    """把配置写入 config.toml（原子替换）。"""
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(dumps_toml(settings.to_dict()), encoding="utf-8")
    tmp.replace(path)
    return path
