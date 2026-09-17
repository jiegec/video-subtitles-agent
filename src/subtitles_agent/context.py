"""把视频背景、术语表、参考资料整理成给 LLM 的「参考上下文」。

- 纯文本 / Markdown / SRT / JSON 直接读取
- PDF 用 pypdf，docx 用 python-docx
- http(s) 链接直接抓取并粗略去标签
- 资料太长时，先让 LLM 压缩成「摘要 + 术语表」并缓存到侧车文件
"""

from __future__ import annotations

import hashlib
import html
import json
import re
from pathlib import Path

import httpx

from .config import Settings
from .video_config import EffectiveContext


def resolve_path(project_dir: Path, raw: str) -> Path:
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = project_dir / path
    return path


def _strip_html(text: str) -> str:
    text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def read_document(
    project_dir: Path, raw: str, log=lambda _: None
) -> tuple[str, str] | None:
    """返回 (显示名, 文本)，失败返回 None。"""
    if raw.startswith(("http://", "https://")):
        try:
            resp = httpx.get(raw, timeout=30, follow_redirects=True)
            resp.raise_for_status()
            return raw, _strip_html(resp.text)
        except Exception as exc:  # noqa: BLE001
            log(f"⚠️ 抓取资料失败 {raw}：{exc}")
            return None

    path = resolve_path(project_dir, raw)
    if not path.is_file():
        log(f"⚠️ 资料不存在：{raw}")
        return None
    suffix = path.suffix.lower()
    try:
        if suffix == ".pdf":
            from pypdf import PdfReader

            reader = PdfReader(str(path))
            text = "\n".join((page.extract_text() or "") for page in reader.pages)
        elif suffix in {".docx", ".doc"}:
            from docx import Document

            document = Document(str(path))
            text = "\n".join(p.text for p in document.paragraphs)
        else:
            text = path.read_text(encoding="utf-8", errors="ignore")
            if suffix in {".html", ".htm", ".xml"}:
                text = _strip_html(text)
        return path.name, text
    except Exception as exc:  # noqa: BLE001
        log(f"⚠️ 读取资料失败 {raw}：{exc}")
        return None


def load_documents(
    project_dir: Path,
    documents: list[str],
    log=lambda _: None,
    per_doc_max: int = 30000,
    total_max: int = 80000,
) -> list[tuple[str, str]]:
    loaded: list[tuple[str, str]] = []
    total = 0
    for raw in documents:
        result = read_document(project_dir, raw, log)
        if not result:
            continue
        name, text = result
        text = text.strip()
        if not text:
            continue
        text = text[:per_doc_max]
        if total + len(text) > total_max:
            text = text[: max(0, total_max - total)]
        if not text:
            break
        loaded.append((name, text))
        total += len(text)
    return loaded


def _digest_key(eff: EffectiveContext, docs: list[tuple[str, str]]) -> str:
    hasher = hashlib.sha1()
    hasher.update(eff.context.encode("utf-8"))
    hasher.update(json.dumps(eff.glossary, ensure_ascii=False).encode("utf-8"))
    for name, text in docs:
        hasher.update(name.encode("utf-8"))
        hasher.update(text.encode("utf-8"))
    return hasher.hexdigest()[:16]


DIGEST_PROMPT = """下面是视频的背景信息与参考资料。请阅读后输出一个 JSON：
{"summary": "用 300 字以内概括这份资料里和视频内容最相关、最可能被语音识别弄错的背景知识", "terms": [{"term": "正确写法", "aliases": ["常见误识别写法"], "note": "一句话说明"}]}
terms 请提取资料中出现的专业术语、人名、专有名词、缩写、公式名等，aliases 填写语音识别可能听错的同音词，最多 60 条。
只输出 JSON，不要解释。"""


def build_digest(
    client,
    eff: EffectiveContext,
    docs: list[tuple[str, str]],
    cache_path: Path,
    log=lambda _: None,
    telemetry: list | None = None,
) -> dict:
    key = _digest_key(eff, docs)
    if cache_path.is_file():
        try:
            cached = json.loads(cache_path.read_text(encoding="utf-8"))
            if cached.get("key") == key and cached.get("digest"):
                if telemetry is not None:
                    telemetry.append({"stage": "digest", "cached": True})
                return cached["digest"]
        except Exception:
            pass

    materials = []
    if eff.context:
        materials.append(f"背景：{eff.context}")
    if eff.glossary:
        terms = "，".join(t["term"] for t in eff.glossary)
        materials.append(f"已有术语表：{terms}")
    for name, text in docs:
        materials.append(f"===== 资料《{name}》 =====\n{text}")

    try:
        from .llm import _extract_json

        prompt = DIGEST_PROMPT + "\n\n" + "\n\n".join(materials)
        if hasattr(client, "chat_result"):
            result = client.chat_result(prompt)
            raw = result.content
            if telemetry is not None:
                telemetry.append(
                    {
                        "stage": "digest",
                        "cached": False,
                        "model": result.model,
                        "usage": result.usage,
                        "elapsed": round(result.elapsed, 2),
                        "finish_reason": result.finish_reason,
                        "attempts": result.attempts,
                    }
                )
        else:
            raw = client.chat(prompt)
        digest = _extract_json(raw)
        if not isinstance(digest, dict):
            raise ValueError("摘要格式不正确")
        digest.setdefault("summary", "")
        digest.setdefault("terms", [])
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(
            json.dumps({"key": key, "digest": digest}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        log("已从参考资料生成背景摘要与术语表（已缓存）")
        return digest
    except Exception as exc:  # noqa: BLE001
        log(f"⚠️ 参考资料摘要失败，直接截断注入：{exc}")
        return {"summary": "", "terms": []}


def build_reference(
    settings: Settings,
    eff: EffectiveContext,
    project_dir: Path,
    cache_path: Path,
    client=None,
    log=lambda _: None,
    telemetry: list | None = None,
) -> str:
    """生成注入到 LLM 提示里的参考上下文文本。"""
    docs = load_documents(project_dir, eff.documents, log)
    total = sum(len(t) for _, t in docs)

    glossary = list(eff.glossary)
    summary = ""
    doc_block = ""
    if docs and total > settings.context_max_chars and client is not None:
        digest = build_digest(client, eff, docs, cache_path, log, telemetry=telemetry)
        summary = digest.get("summary", "") or ""
        for term in digest.get("terms", []) or []:
            if not isinstance(term, dict) or not term.get("term"):
                continue
            glossary.append(
                {
                    "term": str(term["term"]),
                    "aliases": [str(a) for a in (term.get("aliases") or [])],
                    "note": str(term.get("note", "")),
                }
            )
    elif docs:
        budget = settings.context_max_chars
        chunks = []
        for name, text in docs:
            if budget <= 0:
                break
            piece = text[:budget]
            chunks.append(f"《{name}》摘录：\n{piece}")
            budget -= len(piece)
        doc_block = "\n\n".join(chunks)

    lines: list[str] = []
    if eff.title or eff.context:
        header = eff.title or "本视频"
        lines.append(f"# 内容说明\n{header}：{eff.context}".rstrip("："))
    if glossary:
        seen = set()
        items = []
        for term in glossary:
            name = term["term"]
            if name in seen:
                continue
            seen.add(name)
            aliases = "、".join(term.get("aliases") or [])
            note = term.get("note") or ""
            line = f"- {name}"
            if aliases:
                line += f"（易误听为：{aliases}）"
            if note:
                line += f"：{note}"
            items.append(line)
        lines.append("# 术语表（识别时优先采用这里的正确写法）\n" + "\n".join(items))
    if summary:
        lines.append(f"# 参考资料摘要\n{summary}")
    if doc_block:
        lines.append(f"# 参考资料\n{doc_block}")

    reference = "\n\n".join(lines).strip()
    if len(reference) > settings.context_max_chars * 2:
        reference = reference[: settings.context_max_chars * 2]
    return reference
