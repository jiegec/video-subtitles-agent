"""任务编排：截图 / 抽音频 -> whisper 转写 -> LLM 清洗 -> 生成 SRT，并管理后台任务。"""

from __future__ import annotations

import threading
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import frames as frames_mod
from . import recover as recover_mod
from .backup import backup_srt
from .config import Settings
from .context import build_reference, read_document
from .frames import Frame
from .llm import LLMClient, LLMError
from .recover import load_merged_segments
from .recover import load_report as load_gaps_report
from .subtitles import (
    Cue,
    build_char_timeline,
    cues_to_srt,
    merge_short_cues,
    normalize_cues,
    reflow,
)
from .trajectory import char_diff, diff_stats, save_trace, trace_path_for
from .transcribe import (
    Block,
    build_initial_prompt,
    extract_wav,
    group_blocks,
    probe_mean_volume,
    run_asr,
    strip_hallucinations,
)
from .video_config import VideoConfig

VALID_STEPS = ("recover", "frames", "extract_audio", "transcribe", "clean")


# --------------------------------------------------------------------------- #
# 清洗
# --------------------------------------------------------------------------- #
def _make_batches(blocks: list[Block], max_chars: int = 1000) -> list[list[int]]:
    batches: list[list[int]] = []
    current: list[int] = []
    current_len = 0
    for i, block in enumerate(blocks):
        if current and current_len + block.char_count > max_chars:
            batches.append(current)
            current = []
            current_len = 0
        current.append(i)
        current_len += block.char_count
    if current:
        batches.append(current)
    return batches


def clean_blocks(
    settings: Settings,
    blocks: list[Block],
    concurrency: int,
    log: Callable[[str], None],
    progress: Callable[[int], None] | None = None,
    reference: str = "",
    frames: list[Frame] | None = None,
    frames_per_batch: int = 4,
    alts: dict[int, str] | None = None,
) -> tuple[dict[int, str], list[dict]]:
    """对每个 block 调用 LLM 清洗，返回 ({block_index: cleaned_text}, batch_records)。

    batch_records 记录每个请求的输入输出，供 trajectory 展示。失败时回退原文。
    """
    results: dict[int, str] = {i: block.text for i, block in enumerate(blocks)}
    batch_records: list[dict] = []
    if not settings.has_llm:
        log("未配置 LLM，跳过清洗（仅做重排）")
        return results, batch_records

    frames = frames or []
    if frames:
        log(f"启用画面上下文：共 {len(frames)} 张截图，每批最多 {frames_per_batch} 张")
    if reference:
        log(f"注入参考上下文 {len(reference)} 字")

    batches = _make_batches(blocks)
    log(f"LLM 清洗：{len(blocks)} 个文本块，{len(batches)} 个请求，并发 {concurrency}")
    client = LLMClient(settings)
    done = 0
    lock = threading.Lock()

    alts = alts or {}

    def make_items(batch: list[int]) -> list[dict]:
        items = []
        for i in batch:
            item = {"id": i, "text": blocks[i].text}
            if alts.get(i):
                item["alt"] = alts[i]
            items.append(item)
        return items

    def process(batch: list[int]):
        items = make_items(batch)
        batch_frames: list[Frame] = []
        if frames:
            start = blocks[batch[0]].start
            end = blocks[batch[-1]].end
            batch_frames = frames_mod.select_frames(frames, start, end, limit=frames_per_batch)
        frame_meta = [
            {"index": f.index, "time": round(f.time, 3), "name": f.path.name} for f in batch_frames
        ]
        try:
            data, meta = client.clean_items(
                items,
                reference=reference,
                image_paths=[f.path for f in batch_frames],
            )
            return batch, data, meta, frame_meta, None
        except LLMError as exc:
            return batch, {}, {}, frame_meta, str(exc)

    try:
        with ThreadPoolExecutor(max_workers=concurrency) as pool:
            futures = [pool.submit(process, batch) for batch in batches]
            for future in as_completed(futures):
                batch, cleaned, meta, frame_meta, error = future.result()
                if error:
                    log(f"⚠️ 一个批次清洗失败，保留原文：{error}")
                else:
                    for i, value in cleaned.items():
                        if i in results:
                            results[i] = value
                batch_records.append(
                    {
                        "block_ids": batch,
                        "frames": frame_meta,
                        "attached_images": meta.get("attached_images", 0),
                        "request_items": make_items(batch),
                        "request_text": meta.get("request_text", ""),
                        "response_text": meta.get("response_text", ""),
                        "usage": meta.get("usage", {}),
                        "elapsed": meta.get("elapsed", 0.0),
                        "model": meta.get("model"),
                        "finish_reason": meta.get("finish_reason"),
                        "attempts": meta.get("attempts", 1),
                        "cleaned": {
                            str(i): cleaned.get(i, blocks[i].text) for i in batch
                        },
                        "error": error,
                    }
                )
                with lock:
                    done += 1
                    if progress:
                        progress(int(done * 100 / max(len(batches), 1)))
        log("LLM 清洗完成")
    finally:
        client.close()
    batch_records.sort(key=lambda r: r["block_ids"][0] if r["block_ids"] else 0)
    return results, batch_records


def build_cues(
    blocks: list[Block],
    cleaned: dict[int, str],
    max_chars: int,
    min_duration: float,
) -> tuple[list[Cue], list[list[Cue]]]:
    """返回 (最终字幕, 每个 block 重排后的字幕)。"""
    per_block: list[list[Cue]] = []
    flat: list[Cue] = []
    for i, block in enumerate(blocks):
        value = cleaned.get(i, block.text)
        if not value.strip():
            per_block.append([])
            continue
        timeline = build_char_timeline(block.tokens)
        block_cues = reflow(value, timeline, block.start, block.end, max_chars, min_duration)
        per_block.append(block_cues)
        # 拷贝一份参与全局合并，避免 merge/normalize 修改到轨迹里的每块结果
        flat.extend(Cue(c.start, c.end, c.text) for c in block_cues)
    final = normalize_cues(merge_short_cues(flat, max_chars))
    return final, per_block


def generate_srt(
    settings: Settings,
    video_config: VideoConfig,
    video: Path,
    rel_path: str,
    whisper_json: Path,
    srt_path: Path,
    *,
    max_chars: int,
    min_duration: float,
    use_llm: bool,
    block_chars: int,
    frame_interval: int | None,
    frames_dir: Path,
    context_cache: Path,
    gaps_path: Path,
    log: Callable[[str], None],
    progress: Callable[[int], None] | None = None,
    preloaded_frames: list[Frame] | None = None,
) -> list[Cue]:
    eff = video_config.effective(default_frame_interval=settings.frame_interval)
    interval = int(frame_interval or eff.frame_interval or settings.frame_interval)

    log(f"读取转写：{whisper_json.name}")
    gaps_report = load_gaps_report(gaps_path)
    segments = load_merged_segments(whisper_json, gaps_report)
    if not segments:
        raise RuntimeError("转写结果为空，无法生成字幕")
    if gaps_report:
        rec = gaps_report.get("stats", {})
        log(f"已合并漏识别补齐结果：+{rec.get('recovered_segments', 0)} 段")
    segments, hallucinated = strip_hallucinations(segments, log=log)
    log(f"共 {len(segments)} 个语音片段")
    blocks = group_blocks(segments, block_chars=block_chars)
    log(f"合并为 {len(blocks)} 个文本块")

    reference = ""
    frames: list[Frame] = []
    llm_events: list[dict] = []
    if use_llm and settings.has_llm:
        client = LLMClient(settings)
        try:
            reference = build_reference(
                settings,
                eff,
                settings.project_dir,
                context_cache,
                client=client,
                log=log,
                telemetry=llm_events,
            )
        finally:
            client.close()
        frames = preloaded_frames or frames_mod.ensure_frames(
            settings, video, frames_dir, interval, log
        )

    if use_llm:
        alts = recover_mod.build_alts(blocks, gaps_report)
        if alts:
            log(f"有 {len(alts)} 个文本块带上了另一版识别结果（alt）供 LLM 参考")
        cleaned, batch_records = clean_blocks(
            settings,
            blocks,
            settings.llm_concurrency,
            log,
            progress,
            reference=reference,
            frames=frames,
            frames_per_batch=settings.frame_max_per_batch,
            alts=alts,
        )
    else:
        alts = {}
        cleaned = {i: block.text for i, block in enumerate(blocks)}
        batch_records = []
        if progress:
            progress(100)

    cues, per_block_cues = build_cues(blocks, cleaned, max_chars, min_duration)
    backed_up = backup_srt(srt_path, keep=settings.srt_backup_keep)
    if backed_up:
        log(f"已备份旧字幕：{backed_up.name}")
    srt_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = srt_path.with_suffix(".srt.tmp")
    tmp.write_text(cues_to_srt(cues), encoding="utf-8")
    tmp.replace(srt_path)
    total_chars = sum(len(c.text) for c in cues)
    avg = total_chars / len(cues) if cues else 0
    log(
        f"已写入 {srt_path.name}：{len(cues)} 条字幕，平均 {avg:.1f} 字/条，最长 "
        f"{max((len(c.text) for c in cues), default=0)} 字"
    )

    _save_trajectory(
        settings,
        srt_path,
        rel_path,
        segments,
        blocks,
        cleaned,
        per_block_cues,
        cues,
        batch_records,
        eff,
        reference,
        frames,
        interval,
        gaps_report,
        alts,
        llm_events,
        hallucinated,
        max_chars=max_chars,
        min_duration=min_duration,
        use_llm=use_llm,
        block_chars=block_chars,
        log=log,
    )
    return cues


def _save_trajectory(
    settings: Settings,
    srt_path: Path,
    rel_path: str,
    segments,
    blocks: list[Block],
    cleaned: dict[int, str],
    per_block_cues: list[list[Cue]],
    cues: list[Cue],
    batch_records: list[dict],
    eff,
    reference: str,
    frames: list[Frame],
    interval: int,
    gaps_report: dict | None,
    alts: dict[int, str],
    llm_events: list[dict],
    hallucinated: int,
    *,
    max_chars: int,
    min_duration: float,
    use_llm: bool,
    block_chars: int,
    log: Callable[[str], None],
) -> None:
    block_to_batch: dict[int, int] = {}
    for bi, record in enumerate(batch_records):
        for block_id in record["block_ids"]:
            block_to_batch[block_id] = bi

    block_records: list[dict] = []
    total_deleted = 0
    total_inserted = 0
    for i, block in enumerate(blocks):
        raw_text = block.text
        cleaned_text = cleaned.get(i, raw_text)
        ops = char_diff(raw_text, cleaned_text)
        stats = diff_stats(ops)
        total_deleted += stats["deleted_chars"]
        total_inserted += stats["inserted_chars"]
        block_records.append(
            {
                "index": i,
                "start": round(block.start, 3),
                "end": round(block.end, 3),
                "raw_text": raw_text,
                "cleaned_text": cleaned_text,
                "diff": ops,
                "batch": block_to_batch.get(i),
                "alt_text": alts.get(i, ""),
                "cues": [c.as_dict() for c in per_block_cues[i]] if i < len(per_block_cues) else [],
            }
        )

    documents = []
    for raw in eff.documents:
        result = read_document(settings.project_dir, raw, lambda _: None)
        documents.append(
            {"raw": raw, "found": result is not None, "chars": len(result[1]) if result else 0}
        )

    llm_calls = list(llm_events)
    for batch_index, record in enumerate(batch_records):
        llm_calls.append(
            {
                "stage": "clean",
                "batch": batch_index,
                "block_ids": record.get("block_ids", []),
                "model": record.get("model"),
                "usage": record.get("usage", {}),
                "elapsed": record.get("elapsed", 0.0),
                "finish_reason": record.get("finish_reason"),
                "attempts": record.get("attempts", 1),
                "attached_images": record.get("attached_images", 0),
                "error": record.get("error"),
            }
        )

    def _usage_sum(key: str) -> int:
        return sum(int((call.get("usage") or {}).get(key) or 0) for call in llm_calls)

    llm_totals = {
        "calls": len(llm_calls),
        "failed": sum(1 for call in llm_calls if call.get("error")),
        "prompt_tokens": _usage_sum("prompt_tokens"),
        "completion_tokens": _usage_sum("completion_tokens"),
        "total_tokens": _usage_sum("total_tokens"),
        "reasoning_tokens": _usage_sum("reasoning_tokens"),
        "cached_tokens": _usage_sum("cached_tokens"),
        "seconds": round(sum(float(call.get("elapsed") or 0) for call in llm_calls), 2),
    }

    trace = {
        "version": 1,
        "video": rel_path,
        "params": {
            "max_chars": max_chars,
            "min_duration": min_duration,
            "use_llm": use_llm,
            "block_chars": block_chars,
            "use_frames": bool(frames),
            "frame_interval": interval,
            "model": settings.llm_model if settings.has_llm else None,
            "llm_base_url": settings.llm_base_url if settings.has_llm else None,
            "llm_thinking": settings.llm_thinking,
            "concurrency": settings.llm_concurrency,
            "frames_per_batch": settings.frame_max_per_batch,
            "asr_backend": settings.asr_backend,
            "asr_engine": (
                settings.tencent_engine
                if settings.asr_backend == "tencent"
                else Path(settings.whisper_model).name
            ),
            "whisper_model": str(settings.whisper_model),
        },
        "context": {
            "title": eff.title,
            "context": eff.context,
            "glossary": eff.glossary,
            "documents": documents,
            "reference": reference,
            "frames": [
                {"index": f.index, "time": round(f.time, 3), "name": f.path.name} for f in frames
            ],
        },
        "whisper": {
            "segment_count": len(segments),
            "segments": [
                {"start": round(s.start, 3), "end": round(s.end, 3), "text": s.text}
                for s in segments
            ],
        },
        "blocks": block_records,
        "batches": batch_records,
        "cues": [c.as_dict() for c in cues],
        "recovery": _recovery_summary(gaps_report),
        "llm": {
            "model": settings.llm_model if settings.has_llm else None,
            "calls": llm_calls,
            "totals": llm_totals,
        },
        "stats": {
            "segments": len(segments),
            "blocks": len(blocks),
            "batches": len(batch_records),
            "cues": len(cues),
            "deleted_chars": total_deleted,
            "inserted_chars": total_inserted,
            "hallucinated_segments": hallucinated,
            "llm_calls": llm_totals["calls"],
            "llm_total_tokens": llm_totals["total_tokens"],
            "llm_seconds": llm_totals["seconds"],
        },
    }
    path = trace_path_for(srt_path)
    save_trace(path, trace)
    log(f"生成轨迹：{path.name}（{len(blocks)} 块 / {len(batch_records)} 个 LLM 请求）")


def _recovery_summary(report: dict | None) -> dict | None:
    if not report:
        return None
    regions = []
    for region in report.get("regions") or []:
        regions.append(
            {
                "start": region.get("start"),
                "end": region.get("end"),
                "duration": region.get("duration"),
                "kind": region.get("kind"),
                "original_text": region.get("original_text", ""),
                "recovered_text": region.get("recovered_text", ""),
            }
        )
    return {"params": report.get("params", {}), "stats": report.get("stats", {}), "regions": regions}


# --------------------------------------------------------------------------- #
# 任务
# --------------------------------------------------------------------------- #
@dataclass
class Job:
    id: str
    path: str
    steps: list[str]
    options: dict
    status: str = "pending"
    progress: int = 0
    message: str = "排队中"
    logs: list[str] = field(default_factory=list)
    error: str | None = None
    created_at: float = field(default_factory=time.time)
    finished_at: float | None = None

    def log(self, message: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        self.logs.append(f"[{stamp}] {message}")
        if len(self.logs) > 800:
            del self.logs[: len(self.logs) - 800]
        self.message = message

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "path": self.path,
            "steps": self.steps,
            "status": self.status,
            "progress": self.progress,
            "message": self.message,
            "logs": self.logs,
            "error": self.error,
            "created_at": self.created_at,
            "finished_at": self.finished_at,
        }


class JobManager:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="job")
        self._active_by_path: dict[str, str] = {}

    def active_job(self, path: str) -> Job | None:
        job_id = self._active_by_path.get(path)
        if not job_id:
            return None
        job = self.jobs.get(job_id)
        if job and job.status in {"pending", "running"}:
            return job
        return None

    def submit(self, path: str, steps: list[str], options: dict) -> Job:
        with self._lock:
            active = self.active_job(path)
            if active:
                raise RuntimeError(f"该视频已有任务在运行：{active.id}")
            job = Job(id=uuid.uuid4().hex[:12], path=path, steps=steps, options=options)
            self.jobs[job.id] = job
            self._active_by_path[path] = job.id
        self._executor.submit(self._run, job)
        return job

    def get(self, job_id: str) -> Job | None:
        return self.jobs.get(job_id)

    def latest_job(self, path: str) -> Job | None:
        candidates = [job for job in self.jobs.values() if job.path == path]
        return max(candidates, key=lambda job: job.created_at) if candidates else None

    def _run(self, job: Job) -> None:
        settings = self.settings
        video = settings.project_dir / job.path
        artifacts = settings.artifact_paths(job.path)
        video_config = VideoConfig(artifacts["config"])
        started = time.time()
        job.status = "running"
        job.log(f"开始处理：{job.path}")
        try:
            options = job.options
            steps = list(job.steps)

            preloaded_frames: list[Frame] | None = None
            total = len(steps)
            for index, step in enumerate(steps):
                base = int(index * 100 / max(total, 1))
                span = 100 / max(total, 1)
                job.log(f"▶ 步骤 {index + 1}/{total}：{step}")

                def step_progress(value: int, base: int = base, span: float = span) -> None:
                    job.progress = min(99, int(base + span * value / 100))

                if step == "recover":
                    if not artifacts["wav"].is_file():
                        raise RuntimeError("缺少音频，请先抽取音频")
                    if not artifacts["whisper_json"].is_file():
                        raise RuntimeError("缺少 whisper 转写结果，请先转写")
                    eff = video_config.effective(settings.frame_interval)
                    base_segments = load_merged_segments(artifacts["whisper_json"], None)
                    min_gap = settings.recover_min_gap
                    min_cps = settings.recover_min_cps
                    low_conf_thr = settings.recover_low_conf_threshold
                    job.log(
                        f"检测漏识别（噪声 {settings.recover_noise_db}dB，最短缺口 {min_gap}s，"
                        f"低置信阈值 {low_conf_thr}）…"
                    )
                    speech = recover_mod.detect_speech_regions(
                        settings,
                        artifacts["wav"],
                        settings.recover_noise_db,
                        settings.recover_min_silence,
                        job.log,
                    )
                    gaps = recover_mod.find_gaps(base_segments, speech, min_gap)
                    sparse = recover_mod.find_sparse(base_segments, min_cps=min_cps)
                    low_conf = recover_mod.find_low_confidence(
                        base_segments, threshold=low_conf_thr
                    )
                    regions = recover_mod.merge_regions([*gaps, *sparse, *low_conf])
                    job.log(
                        f"可疑区间：缺口 {len(gaps)}、稀疏 {len(sparse)}、低置信 {len(low_conf)}"
                        f"；合并后 {len(regions)}"
                    )
                    if len(regions) > settings.recover_max_regions:
                        job.log(f"可疑区间较多，只处理时长最长的 {settings.recover_max_regions} 个")
                        regions.sort(key=lambda r: r.duration, reverse=True)
                        regions = regions[: settings.recover_max_regions]
                        regions.sort(key=lambda r: r.start)
                    if regions:
                        prompt = (
                            build_initial_prompt(settings, eff.glossary)
                            if settings.asr_backend == "whisper"
                            else ""
                        )
                        recover_mod.recover_regions(
                            settings,
                            artifacts["wav"],
                            regions,
                            model=settings.recover_model,
                            prompt=prompt,
                            glossary=eff.glossary,
                            log=job.log,
                            progress=step_progress,
                        )
                    report = recover_mod.build_report(
                        settings,
                        regions,
                        {
                            "noise_db": settings.recover_noise_db,
                            "min_silence": settings.recover_min_silence,
                            "min_gap": min_gap,
                            "min_cps": min_cps,
                            "low_conf_threshold": low_conf_thr,
                            "max_regions": settings.recover_max_regions,
                            "backend": settings.asr_backend,
                            "model": (
                                str(settings.recover_model or settings.whisper_model)
                                if settings.asr_backend == "whisper"
                                else f"tencent:{settings.tencent_engine}"
                            ),
                            "extra_args": ["-mc", "0"] if settings.asr_backend == "whisper" else [],
                        },
                    )
                    recover_mod.save_report(artifacts["gaps"], report)
                    stats = report["stats"]
                    job.log(
                        f"补齐完成：补回 {stats['recovered_segments']} 段 / "
                        f"{stats['recovered_chars']} 字 → {artifacts['gaps'].name}"
                    )
                    step_progress(100)
                elif step == "frames":
                    eff = video_config.effective(settings.frame_interval)
                    interval = int(
                        options.get("frame_interval") or eff.frame_interval or settings.frame_interval
                    )
                    preloaded_frames = frames_mod.ensure_frames(
                        settings,
                        video,
                        artifacts["frames_dir"],
                        interval,
                        job.log,
                        force=True,
                    )
                    step_progress(100)
                elif step == "extract_audio":
                    extract_wav(settings, video, artifacts["wav"], job.log)
                    step_progress(100)
                elif step == "transcribe":
                    if not artifacts["wav"].is_file():
                        extract_wav(settings, video, artifacts["wav"], job.log)
                    mean_db = probe_mean_volume(settings, artifacts["wav"])
                    if mean_db is not None:
                        job.log(f"音频平均音量：{mean_db:.1f} dB")
                        if mean_db < -45:
                            job.log("⚠️ 音频电平偏低，转写质量可能受影响")
                    prefix = artifacts["whisper_json"].with_suffix("")
                    eff = video_config.effective(settings.frame_interval)
                    if settings.asr_backend == "whisper":
                        prompt = build_initial_prompt(settings, eff.glossary)
                        if prompt:
                            job.log(f"whisper 提示词：{prompt[:100]}")
                    else:
                        prompt = ""
                        job.log(f"语音识别后端：腾讯云（{settings.tencent_engine}）")
                    run_asr(
                        settings,
                        artifacts["wav"],
                        prefix,
                        job.log,
                        progress=step_progress,
                        prompt=prompt,
                        glossary=eff.glossary,
                    )
                    step_progress(100)
                elif step == "clean":
                    generate_srt(
                        settings,
                        video_config,
                        video,
                        job.path,
                        artifacts["whisper_json"],
                        artifacts["srt"],
                        max_chars=int(options.get("max_chars", settings.default_max_chars)),
                        min_duration=float(options.get("min_duration", settings.default_min_duration)),
                        use_llm=True,
                        block_chars=int(options.get("block_chars", settings.llm_block_chars)),
                        frame_interval=options.get("frame_interval"),
                        frames_dir=artifacts["frames_dir"],
                        context_cache=artifacts["context_cache"],
                        gaps_path=artifacts["gaps"],
                        log=job.log,
                        progress=step_progress,
                        preloaded_frames=preloaded_frames,
                    )
                    step_progress(100)
                else:
                    raise RuntimeError(f"未知步骤：{step}")

            job.progress = 100
            job.status = "done"
            job.log(f"✅ 全部完成，用时 {time.time() - started:.1f}s")
        except Exception as exc:  # noqa: BLE001
            job.status = "error"
            job.error = str(exc)
            job.log(f"❌ 失败：{exc}")
            job.log(traceback.format_exc(limit=6))
        finally:
            job.finished_at = time.time()
            with self._lock:
                if self._active_by_path.get(job.path) == job.id:
                    self._active_by_path.pop(job.path, None)

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)
