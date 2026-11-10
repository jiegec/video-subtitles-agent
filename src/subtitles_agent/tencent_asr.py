"""腾讯云录音文件识别（ASR）后端，与 whisper 共存，可在网页里选择。

流程（参考录音文件识别 API）：
1. 用 ffmpeg 把 16k 单声道 wav 切成若干段并编码为 AAC，保证单段 < 5MB（base64 上传限制）；
2. 对每段调用 ``CreateRecTask``（SourceType=1，直接 POST base64 数据），再轮询 ``DescribeTaskStatus``；
3. 解析结构化的 ``ResultDetail``（词级时间戳），加上分段偏移后写成与 whisper ``-ojf`` 相同的 JSON 结构，
   供后续流程复用。

腾讯云 SDK 使用 ``tencentcloud-sdk-python``（随 ``uv sync`` 安装），
首次调用时加载，纯函数测试直接可用。
"""

from __future__ import annotations

import base64
import json
import math
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Callable

from .config import Settings
from .transcribe import probe_duration

LogFn = Callable[[str], None]

# CreateRecTask 的音频数据上限（base64 前）
MAX_AUDIO_BYTES = 5 * 1024 * 1024
POLL_INTERVAL = 3.0
POLL_MAX_WAIT = 1800.0  # 单段最长等待 30 分钟


def _noop(_: str) -> None:
    pass


def _sdk():
    try:
        from tencentcloud.asr.v20190614 import asr_client, models
        from tencentcloud.common import credential
        from tencentcloud.common.exception.tencent_cloud_sdk_exception import (
            TencentCloudSDKException,
        )
    except ImportError as exc:  # pragma: no cover - 惰性导入分支
        raise RuntimeError(
            "腾讯云语音识别依赖 tencentcloud-sdk-python，请执行 uv sync 安装依赖"
        ) from exc
    return credential, asr_client, models, TencentCloudSDKException


# --------------------------------------------------------------------------- #
# 结果解析（纯函数，便于测试）
# --------------------------------------------------------------------------- #
def _fill_missing(times: list[tuple[float, float] | None], start_ms: float, end_ms: float) -> None:
    """为标点、空格等字符按前后边界线性补齐词时间戳。"""
    n = len(times)
    i = 0
    while i < n:
        if times[i] is not None:
            i += 1
            continue
        j = i
        while j < n and times[j] is None:
            j += 1
        left = times[i - 1][1] if i > 0 and times[i - 1] else start_ms
        right = times[j][0] if j < n and times[j] else end_ms
        if right < left:
            right = left
        span = max(j - i, 1)
        for k in range(j - i):
            times[i + k] = (
                left + (right - left) * k / span,
                left + (right - left) * (k + 1) / span,
            )
        i = j


def tokens_from_sentence(text: str, words, start_ms: int, end_ms: int) -> list[dict]:
    """把一句文本拆成逐字 token，时间优先用词级时间戳，其余字符线性补齐。"""
    if not text:
        return []
    if end_ms < start_ms:
        end_ms = start_ms
    times: list[tuple[float, float] | None] = [None] * len(text)
    cursor = 0
    for word in words or []:
        wtext = (getattr(word, "Word", "") or "").strip()
        if not wtext:
            continue
        index = text.find(wtext, cursor)
        if index == -1:
            index = text.find(wtext)
        if index == -1:
            continue
        w_start = start_ms + int(getattr(word, "OffsetStartMs", 0) or 0)
        w_end = start_ms + int(getattr(word, "OffsetEndMs", 0) or 0)
        if w_end < w_start:
            w_end = w_start
        length = len(wtext)
        for offset in range(length):
            char_start = w_start + (w_end - w_start) * offset / length
            char_end = w_start + (w_end - w_start) * (offset + 1) / length
            if index + offset < len(text):
                times[index + offset] = (char_start, char_end)
        cursor = index + length
    _fill_missing(times, start_ms, end_ms)
    tokens: list[dict] = []
    for char, span in zip(text, times):
        assert span is not None  # 由 _fill_missing 补齐
        tokens.append(
            {
                "text": char,
                "offsets": {"from": int(span[0]), "to": int(span[1])},
                "p": 1.0,
            }
        )
    return tokens


def segments_from_status(status, offset_ms: int = 0) -> list[dict]:
    """从一次任务的 ``DescribeTaskStatus`` 结果里取出词级片段。

    只使用结构化的 ``ResultDetail``（``CreateRecTask`` 传 ``ResTextFormat=2`` 时返回）。
    """
    segments: list[dict] = []
    detail = getattr(status, "ResultDetail", None) or []
    for sentence in detail:
        text = (getattr(sentence, "FinalSentence", "") or "").strip()
        if not text:
            continue
        start = int(getattr(sentence, "StartMs", 0) or 0)
        end = int(getattr(sentence, "EndMs", start) or start)
        if end < start:
            end = start
        tokens = tokens_from_sentence(text, getattr(sentence, "Words", None), start, end)
        for token in tokens:
            token["offsets"]["from"] += offset_ms
            token["offsets"]["to"] += offset_ms
        segments.append(
            {
                "offsets": {"from": start + offset_ms, "to": end + offset_ms},
                "text": text,
                "tokens": tokens,
            }
        )
    if not segments and (getattr(status, "Result", "") or "").strip():
        raise RuntimeError(
            "腾讯云返回了结果但缺少 ResultDetail 词级时间戳（请确认 ResTextFormat=2 受支持）"
        )
    return segments


# --------------------------------------------------------------------------- #
# 音频切段与上传
# --------------------------------------------------------------------------- #
def _encode_chunk(
    settings: Settings, wav: Path, start: float, length: float | None, dest: Path
) -> None:
    cmd = [
        settings.ffmpeg_bin,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
    ]
    if start > 0:
        cmd += ["-ss", f"{start:.3f}"]
    cmd += ["-i", str(wav)]
    if length:
        cmd += ["-t", f"{length:.3f}"]
    cmd += [
        "-ac",
        "1",
        "-ar",
        "16000",
        "-c:a",
        "aac",
        "-b:a",
        "48k",
        str(dest),
    ]
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg 生成腾讯云音频段失败：{proc.stdout.strip()[-300:]}")


def _hotword_list(glossary: list[dict] | None) -> str:
    words: list[str] = []
    for entry in glossary or []:
        term = str(entry.get("term") or "").strip()
        if not term or len(term) > 10:
            continue
        words.append(f"{term}|5")
    return ",".join(words[:128])


def _submit_and_wait(settings: Settings, models, client, audio: bytes, hotwords: str, log: LogFn):
    request = models.CreateRecTaskRequest()
    request.EngineModelType = settings.tencent_engine
    request.ChannelNum = 1
    request.ResTextFormat = 2  # 词级时间戳 + 标点
    request.SourceType = 1  # base64 音频数据
    request.Data = base64.b64encode(audio).decode("ascii")
    request.DataLen = len(audio)
    request.ConvertNumMode = 1
    request.FilterModal = 1  # 过滤部分语气词
    if hotwords:
        request.HotwordList = hotwords

    response = client.CreateRecTask(request)
    task_id = response.Data.TaskId
    log(f"腾讯云任务已提交：TaskId={task_id}")

    started = time.time()
    while True:
        status_request = models.DescribeTaskStatusRequest()
        status_request.TaskId = task_id
        status = client.DescribeTaskStatus(status_request).Data
        state = status.StatusStr
        if state == "success":
            return status
        if state == "failed":
            raise RuntimeError(f"腾讯云识别失败：{status.ErrorMsg or '未知错误'}")
        if time.time() - started > POLL_MAX_WAIT:
            raise RuntimeError(f"腾讯云识别超时（TaskId={task_id}）")
        time.sleep(POLL_INTERVAL)


# --------------------------------------------------------------------------- #
# 对外入口
# --------------------------------------------------------------------------- #
_AUTH_ERROR_CODES = {"UnauthorizedOperation"}


def _is_auth_error(code: str) -> bool:
    code = code or ""
    return code.startswith("AuthFailure") or code in _AUTH_ERROR_CODES


def test_credentials(settings: Settings) -> dict:
    """用一个占位 TaskId 查询验证腾讯云密钥 / 地域。

    签名与权限校验通过时，``DescribeTaskStatus`` 对该 TaskId 返回业务错误；鉴权异常则直接报错。
    测试只做查询，识别任务保持零创建。
    """
    credential, asr_client, models, sdk_error = _sdk()
    if not settings.tencent_secret_id or not settings.tencent_secret_key:
        raise RuntimeError("未配置腾讯云 SecretId / SecretKey")
    cred = credential.Credential(settings.tencent_secret_id, settings.tencent_secret_key)
    client = asr_client.AsrClient(cred, settings.tencent_region)
    request = models.DescribeTaskStatusRequest()
    request.TaskId = 1
    try:
        client.DescribeTaskStatus(request)
    except sdk_error as exc:
        code = str(getattr(exc, "code", "") or "")
        if _is_auth_error(code):
            raise RuntimeError(f"密钥无效或没有语音识别权限：{exc}") from exc
        return {
            "ok": True,
            "message": f"密钥有效（{settings.tencent_region}，服务返回 {code or '业务错误'}）",
        }
    return {"ok": True, "message": f"密钥有效（{settings.tencent_region}）"}


def transcribe(
    settings: Settings,
    wav: Path,
    out_prefix: Path,
    log: LogFn = _noop,
    progress: Callable[[int], None] | None = None,
    glossary: list[dict] | None = None,
) -> Path:
    """把 wav 交给腾讯云识别，写出与 whisper 相同结构的 JSON，返回路径。"""
    credential, asr_client, models, sdk_error = _sdk()
    if not settings.tencent_secret_id or not settings.tencent_secret_key:
        raise RuntimeError("未配置腾讯云 SecretId / SecretKey，请在「设置 → 语音识别」里填写")

    out_prefix.parent.mkdir(parents=True, exist_ok=True)
    duration = probe_duration(settings, wav) or 0.0
    chunk_seconds = max(60, settings.tencent_chunk_seconds)
    total_chunks = max(1, math.ceil(duration / chunk_seconds)) if duration else 1

    cred = credential.Credential(settings.tencent_secret_id, settings.tencent_secret_key)
    client = asr_client.AsrClient(cred, settings.tencent_region)
    hotwords = _hotword_list(glossary)
    if hotwords:
        log(f"腾讯云热词：{len(hotwords.split(','))} 个")

    segments: list[dict] = []
    with tempfile.TemporaryDirectory(prefix="tencent-asr-") as tmp:
        tmp_dir = Path(tmp)
        for index in range(total_chunks):
            start = index * chunk_seconds
            length = min(chunk_seconds, duration - start) if duration else None
            if duration and length is not None and length <= 0:
                break
            chunk_path = tmp_dir / f"chunk_{index:04d}.m4a"
            _encode_chunk(settings, wav, start, length, chunk_path)
            audio = chunk_path.read_bytes()
            if len(audio) > MAX_AUDIO_BYTES:
                log(
                    f"⚠️ 第 {index + 1} 段 {len(audio) / 1e6:.1f}MB 超过 5MB，"
                    f"建议调小「腾讯云分段秒数」"
                )
            length_label = f"{length:.0f}s" if length else "整段"
            log(
                f"上传第 {index + 1}/{total_chunks} 段（{start:.0f}s 起，"
                f"{length_label}，{len(audio) / 1e6:.2f}MB）…"
            )
            try:
                status = _submit_and_wait(settings, models, client, audio, hotwords, log)
                chunk_segments = segments_from_status(status, offset_ms=int(round(start * 1000)))
            except sdk_error as exc:  # pragma: no cover - 需要真实凭据
                raise RuntimeError(f"腾讯云调用失败：{exc}") from exc
            segments.extend(chunk_segments)
            log(f"第 {index + 1} 段识别出 {len(chunk_segments)} 句")
            if progress:
                progress(int((index + 1) * 100 / total_chunks))

    segments.sort(key=lambda item: item["offsets"]["from"])
    data = {
        "backend": "tencent",
        "engine": settings.tencent_engine,
        "transcription": segments,
    }
    json_path = out_prefix.parent / (out_prefix.name + ".json")
    json_path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    log(f"转写完成：{json_path.name}（{len(segments)} 段）")
    return json_path
