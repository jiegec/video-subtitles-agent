"""FastAPI Web 应用：视频库、任务、字幕编辑、以及视频预览流。"""

from __future__ import annotations

import json
import mimetypes
import re
from pathlib import Path

from fastapi import Body, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from ..config import load_settings
from ..context import build_reference, read_document
from ..library import Library
from ..llm import LLMClient
from ..pipeline import VALID_STEPS, JobManager
from ..subtitle import Cue, cues_to_srt, normalize_cues, parse_srt
from ..trajectory import load_trace
from ..video_config import CONFIG_SUFFIX, VideoConfig, config_path_for, list_configs

STATIC_DIR = Path(__file__).parent / "static"

settings = load_settings()
library = Library(settings)
jobs = JobManager(settings)

app = FastAPI(title="Video Subtitle Agent")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# --------------------------------------------------------------------------- #
# 页面
# --------------------------------------------------------------------------- #
@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/config")
def get_config() -> dict:
    return {
        "project_dir": str(settings.project_dir),
        "whisper_bin": settings.whisper_bin,
        "whisper_model": str(settings.whisper_model),
        "whisper_lang": settings.whisper_lang,
        "llm_enabled": settings.has_llm,
        "llm_model": settings.llm_model if settings.has_llm else None,
        "llm_base_url": settings.llm_base_url if settings.has_llm else None,
        "default_max_chars": settings.default_max_chars,
        "default_min_duration": settings.default_min_duration,
        "llm_block_chars": settings.llm_block_chars,
        "frame_interval": settings.frame_interval,
        "frame_max_per_batch": settings.frame_max_per_batch,
        "context_max_chars": settings.context_max_chars,
        "config_suffix": CONFIG_SUFFIX,
        "recover": {
            "noise_db": settings.recover_noise_db,
            "min_silence": settings.recover_min_silence,
            "min_gap": settings.recover_min_gap,
            "min_cps": settings.recover_min_cps,
            "sparse": settings.recover_sparse,
            "model": str(settings.recover_model or settings.whisper_model),
        },
        "valid_steps": list(VALID_STEPS),
    }


# --------------------------------------------------------------------------- #
# per-video 配置（背景说明 / 术语表 / 参考资料）
# --------------------------------------------------------------------------- #
def _config_for(rel_path: str) -> VideoConfig:
    try:
        video = library.resolve(rel_path)
    except PermissionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return VideoConfig(config_path_for(video))


def _config_rel(config_path: Path) -> str | None:
    try:
        return str(config_path.relative_to(settings.project_dir))
    except ValueError:
        return str(config_path)


@app.get("/api/video-config")
def get_video_config(path: str = Query(...)) -> dict:
    config = _config_for(path)
    return {
        "config_path": _config_rel(config.path),
        "exists": config.path.is_file(),
        "config": config.raw,
        "effective": config.effective_dict(settings.frame_interval),
    }


@app.put("/api/video-config")
def put_video_config(payload: dict = Body(...)) -> dict:
    path = payload.get("path")
    if not path:
        raise HTTPException(status_code=400, detail="缺少 path")
    config = _config_for(path)
    try:
        config.update(payload.get("config") or {})
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "config": config.raw, "config_path": _config_rel(config.path)}


@app.get("/api/video-configs")
def get_video_configs(path: str | None = None) -> dict:
    configs = list_configs(settings.project_dir)
    if path:
        configs = [c for c in configs if c["video_path"] != path]
    return {"configs": configs}


@app.post("/api/video-config/import")
def import_video_config(payload: dict = Body(...)) -> dict:
    path = payload.get("path")
    source = payload.get("source_config_path")
    if not path or not source:
        raise HTTPException(status_code=400, detail="缺少 path 或 source_config_path")
    target = _config_for(path)
    try:
        source_path = library.resolve(source)
    except PermissionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not source_path.is_file():
        raise HTTPException(status_code=404, detail="源配置文件不存在")
    try:
        data = json.loads(source_path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"源配置不是合法 JSON：{exc}") from exc
    try:
        target.apply(data)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "config": target.raw}


@app.post("/api/video-config/upload")
async def upload_video_config(
    path: str = Form(...),
    file: UploadFile = File(...),
) -> dict:
    target = _config_for(path)
    raw = await file.read()
    try:
        data = json.loads(raw.decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"不是合法的 JSON 配置：{exc}") from exc
    try:
        target.apply(data)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "config": target.raw}


@app.get("/api/video-config/download")
def download_video_config(path: str = Query(...)) -> FileResponse:
    config = _config_for(path)
    if not config.path.is_file():
        config.save()
    return FileResponse(
        config.path,
        media_type="application/json",
        filename=config.path.name,
    )


@app.post("/api/upload")
async def upload_document(
    path: str = Form(...),
    file: UploadFile = File(...),
) -> dict:
    try:
        video = library.resolve(path)
    except PermissionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not video.is_file():
        raise HTTPException(status_code=404, detail="视频不存在")
    filename = Path(file.filename or "document.txt").name
    if not filename:
        raise HTTPException(status_code=400, detail="文件名无效")
    target_dir = video.parent / "context"
    target_dir.mkdir(parents=True, exist_ok=True)
    dest = target_dir / filename
    dest.write_bytes(await file.read())
    rel = str(dest.relative_to(settings.project_dir))
    return {"ok": True, "rel_path": rel, "name": filename}


@app.get("/api/context-preview")
def context_preview(path: str = Query(...)) -> dict:
    try:
        library.resolve(path)
    except PermissionError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    config = _config_for(path)
    eff = config.effective(settings.frame_interval)
    artifacts = settings.artifact_paths(path)
    logs: list[str] = []
    documents = [
        {"raw": raw, "found": read_document(settings.project_dir, raw, logs.append) is not None}
        for raw in eff.documents
    ]
    client = LLMClient(settings) if settings.has_llm else None
    try:
        reference = build_reference(
            settings,
            eff,
            settings.project_dir,
            artifacts["context_cache"],
            client=client,
            log=logs.append,
        )
    finally:
        if client is not None:
            client.close()
    return {
        "reference": reference,
        "documents": documents,
        "effective": config.effective_dict(settings.frame_interval),
        "logs": logs,
        "chars": len(reference),
    }


# --------------------------------------------------------------------------- #
# 视频库
# --------------------------------------------------------------------------- #
@app.get("/api/videos")
def list_videos() -> dict:
    if not settings.project_dir.is_dir():
        return {"videos": [], "error": f"project 目录不存在：{settings.project_dir}"}
    return {"videos": library.list_videos()}


@app.get("/api/videos/status")
def video_status(path: str = Query(...)) -> dict:
    try:
        return library.status(path)
    except (PermissionError, FileNotFoundError) as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


# --------------------------------------------------------------------------- #
# 任务
# --------------------------------------------------------------------------- #
@app.post("/api/jobs")
def create_job(payload: dict = Body(...)) -> dict:
    path = payload.get("path")
    if not path:
        raise HTTPException(status_code=400, detail="缺少 path")
    steps = payload.get("steps") or list(VALID_STEPS)
    steps = [s for s in steps if s in VALID_STEPS]
    if not steps:
        raise HTTPException(status_code=400, detail="steps 无效")
    try:
        library.resolve(path)
    except PermissionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    options = {
        "max_chars": int(payload.get("max_chars", settings.default_max_chars)),
        "min_duration": float(payload.get("min_duration", settings.default_min_duration)),
        "use_llm": bool(payload.get("use_llm", True)),
        "block_chars": int(payload.get("block_chars", settings.llm_block_chars)),
        "use_frames": payload.get("use_frames"),
        "frame_interval": payload.get("frame_interval"),
        "recover_min_gap": float(payload.get("recover_min_gap", settings.recover_min_gap)),
        "recover_min_cps": float(payload.get("recover_min_cps", settings.recover_min_cps)),
        "recover_low_conf_threshold": float(
            payload.get("recover_low_conf_threshold", settings.recover_low_conf_threshold)
        ),
        "recover_sparse": bool(payload.get("recover_sparse", settings.recover_sparse)),
        "recover_low_conf": bool(payload.get("recover_low_conf", settings.recover_low_conf)),
        "force": bool(payload.get("force", False)),
    }
    try:
        job = jobs.submit(path, steps, options)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return job.as_dict()


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str) -> dict:
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在")
    return job.as_dict()


@app.post("/api/clear-context-cache")
def clear_context_cache(payload: dict = Body(...)) -> dict:
    path = payload.get("path")
    if not path:
        raise HTTPException(status_code=400, detail="缺少 path")
    try:
        library.resolve(path)
    except PermissionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    cache = settings.artifact_paths(path)["context_cache"]
    removed = False
    if cache.is_file():
        cache.unlink()
        removed = True
    return {"ok": True, "removed": removed}


# --------------------------------------------------------------------------- #
# 字幕读写
# --------------------------------------------------------------------------- #
@app.get("/api/subtitles")
def get_subtitles(path: str = Query(...)) -> dict:
    try:
        video = library.resolve(path)
    except PermissionError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    artifacts = settings.artifact_paths(path)
    srt = artifacts["srt"]
    cues: list[dict] = []
    if srt.is_file():
        cues = [c.as_dict() for c in parse_srt(srt.read_text(encoding="utf-8"))]
    return {
        "path": path,
        "exists": srt.is_file(),
        "srt_name": srt.name,
        "video_name": video.name,
        "cues": cues,
    }


@app.put("/api/subtitles")
def save_subtitles(payload: dict = Body(...)) -> dict:
    path = payload.get("path")
    raw_cues = payload.get("cues") or []
    if not path:
        raise HTTPException(status_code=400, detail="缺少 path")
    try:
        library.resolve(path)
    except PermissionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    cues: list[Cue] = []
    for item in raw_cues:
        try:
            cues.append(
                Cue(
                    start=float(item["start"]),
                    end=float(item["end"]),
                    text=str(item.get("text", "")).strip(),
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=f"字幕数据无效：{exc}") from exc
    cues = normalize_cues(cues)
    srt = settings.artifact_paths(path)["srt"]
    srt.parent.mkdir(parents=True, exist_ok=True)
    tmp = srt.with_suffix(".srt.tmp")
    tmp.write_text(cues_to_srt(cues), encoding="utf-8")
    tmp.replace(srt)
    return {"ok": True, "count": len(cues), "srt_name": srt.name}


@app.get("/api/subtitles/download")
def download_subtitles(path: str = Query(...)) -> FileResponse:
    try:
        library.resolve(path)
    except PermissionError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    srt = settings.artifact_paths(path)["srt"]
    if not srt.is_file():
        raise HTTPException(status_code=404, detail="字幕文件不存在")
    return FileResponse(
        srt,
        media_type="application/x-subrip",
        filename=srt.name,
    )


# --------------------------------------------------------------------------- #
# 生成轨迹（whisper 原文 / 上下文 / LLM 输入输出 / 重排结果）
# --------------------------------------------------------------------------- #
@app.get("/api/trajectory")
def get_trajectory(path: str = Query(...)) -> dict:
    try:
        library.resolve(path)
    except PermissionError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    trace = load_trace(settings.artifact_paths(path)["trajectory"])
    if trace is None:
        raise HTTPException(status_code=404, detail="还没有生成轨迹，请先运行一次生成")
    return trace


@app.get("/api/frame")
def get_frame(path: str = Query(...), name: str = Query(...)):
    try:
        library.resolve(path)
    except PermissionError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if not re.fullmatch(r"frame_\d+\.jpg", name):
        raise HTTPException(status_code=400, detail="非法的截图名")
    frame = settings.artifact_paths(path)["frames_dir"] / name
    if not frame.is_file():
        raise HTTPException(status_code=404, detail="截图不存在")
    return FileResponse(frame, media_type="image/jpeg")


# --------------------------------------------------------------------------- #
# 媒体流（支持 Range，供 <video> 播放）
# --------------------------------------------------------------------------- #
def _range_response(file_path: Path, request: Request) -> Response:
    file_size = file_path.stat().st_size
    content_type = mimetypes.guess_type(str(file_path))[0] or "application/octet-stream"
    range_header = request.headers.get("range")
    start, end = 0, file_size - 1

    if range_header:
        try:
            units, _, spec = range_header.partition("=")
            if units.strip() != "bytes":
                raise ValueError
            first, _, last = spec.partition("-")
            if first:
                start = int(first)
                end = int(last) if last else file_size - 1
            else:
                start = max(0, file_size - int(last))
                end = file_size - 1
        except ValueError:
            start, end = 0, file_size - 1
        start = max(0, min(start, file_size - 1))
        end = max(start, min(end, file_size - 1))
        length = end - start + 1

        def iterator():
            with file_path.open("rb") as fh:
                fh.seek(start)
                remaining = length
                while remaining > 0:
                    chunk = fh.read(min(1024 * 256, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    yield chunk

        return StreamingResponse(
            iterator(),
            status_code=206,
            media_type=content_type,
            headers={
                "Content-Range": f"bytes {start}-{end}/{file_size}",
                "Accept-Ranges": "bytes",
                "Content-Length": str(length),
                "Cache-Control": "no-store",
            },
        )

    def full_iterator():
        with file_path.open("rb") as fh:
            while True:
                chunk = fh.read(1024 * 256)
                if not chunk:
                    break
                yield chunk

    return StreamingResponse(
        full_iterator(),
        media_type=content_type,
        headers={
            "Content-Length": str(file_size),
            "Accept-Ranges": "bytes",
            "Cache-Control": "no-store",
        },
    )


@app.get("/api/media")
def get_media(request: Request, path: str = Query(...)):
    try:
        file_path = library.resolve(path)
    except PermissionError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if not file_path.is_file():
        raise HTTPException(status_code=404, detail="视频文件不存在")
    return _range_response(file_path, request)


@app.exception_handler(PermissionError)
def permission_handler(_: Request, exc: PermissionError) -> JSONResponse:
    return JSONResponse(status_code=400, content={"detail": str(exc)})
