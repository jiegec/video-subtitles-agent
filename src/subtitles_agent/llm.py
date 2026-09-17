"""LLM 客户端：调用任意 OpenAI 兼容的 /chat/completions 接口，并稳健地解析 JSON。"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from typing import Any

import httpx

from .config import Settings

SYSTEM_PROMPT = """你是一名专业的视频字幕编辑。你处理的可能是任意类型的视频：课堂讲解、教程、访谈、播客、vlog、会议、直播、纪录片等。你会收到一段自动语音识别(ASR)的原始文本，其中可能包含错别字、同音字错误、口语填充词、口头重复和结巴。

你还可能收到两类额外上下文：
- 用户提供的背景说明 / 术语表 / 参考资料（文本）；
- 同一时间段视频的关键截图（图片），画面中可能有演示文稿、板书、代码、界面文字、字幕、标牌等。
请充分利用这些上下文来判断正确的术语、人名、专有名词和数字，优先采用术语表中的写法。

请严格完成以下任务：
1. 纠正 ASR 的识别错误：结合上下文与截图内容，推断正确的同音字/词、专业术语、人名和数字。
2. 删除无意义的口语填充词与语气词（例如“嗯、呃、啊、哦、那个、这个、就是、然后、那么、呢、吧、哈”等），但仅当它们不承载语义时才删除；删除口头重复和结巴。
3. 只保留核心内容，让语句通顺；但必须忠实原意，不要总结、不要遗漏关键信息、不要添加原文没有的内容、不要改变人称与语气。
4. 不要翻译，保持原语言（通常为中文）。若原文混杂中英文，保留英文专有名词。
5. 如果某一项完全是无意义的语气词/噪声，请把它的 text 返回为空字符串 ""。
6. 不要调整项目顺序，不要合并或拆分项目。

输入中的某一项可能带一个 "alt" 字段：那是同一段音频的另一次识别结果，与 text 可能互补（有的词这次听到了、那次没听到），也可能有出入。请综合 text 与 alt，输出最完整、最准确的一版，不要重复内容；若 alt 没有帮助则忽略它。

输入和输出都必须是 JSON：
输入：{"items":[{"id":0,"text":"..."},{"id":1,"text":"...","alt":"..."}]}
输出：{"items":[{"id":0,"text":"..."},{"id":1,"text":"..."}]}
必须包含相同数量、相同 id 的项，顺序完全一致。只输出 JSON 本身，不要输出解释、注释或 markdown 代码块。"""


class LLMError(RuntimeError):
    pass


def _extract_json(content: str) -> Any:
    content = content.strip()
    if content.startswith("```"):
        content = re.sub(r"^```[a-zA-Z]*\s*", "", content)
        content = re.sub(r"\s*```$", "", content)
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        pass
    # 找到第一个平衡的 JSON 对象或数组
    for opener, closer in (("{", "}"), ("[", "]")):
        start = content.find(opener)
        if start == -1:
            continue
        depth = 0
        in_str = False
        escape = False
        for i in range(start, len(content)):
            ch = content[i]
            if in_str:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == opener:
                depth += 1
            elif ch == closer:
                depth -= 1
                if depth == 0:
                    candidate = content[start : i + 1]
                    try:
                        return json.loads(candidate)
                    except json.JSONDecodeError:
                        break
    raise LLMError("无法从模型输出中解析出 JSON")


def _image_part(path) -> dict:
    import base64
    from pathlib import Path as _Path

    data = base64.b64encode(_Path(path).read_bytes()).decode("ascii")
    return {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{data}"}}


@dataclass
class ChatResult:
    content: str
    usage: dict
    elapsed: float
    model: str
    finish_reason: str | None = None
    attempts: int = 1


def _usage_summary(raw: Any) -> dict:
    """把不同服务商的 usage 归一化，便于展示。"""
    if not isinstance(raw, dict):
        return {}
    prompt_details = raw.get("prompt_tokens_details") or {}
    completion_details = raw.get("completion_tokens_details") or {}
    return {
        "prompt_tokens": raw.get("prompt_tokens"),
        "completion_tokens": raw.get("completion_tokens"),
        "total_tokens": raw.get("total_tokens"),
        "reasoning_tokens": completion_details.get("reasoning_tokens"),
        "cached_tokens": raw.get("prompt_cache_hit_tokens")
        or prompt_details.get("cached_tokens"),
    }


class LLMClient:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._client = httpx.Client(
            base_url=settings.llm_base_url,
            timeout=httpx.Timeout(settings.llm_timeout, connect=30.0),
            headers={
                "Authorization": f"Bearer {settings.llm_api_key}",
                "Content-Type": "application/json",
            },
        )

    def close(self) -> None:
        self._client.close()

    def _payload(self, user_content, max_tokens: int | None, temperature: float) -> dict:
        payload = {
            "model": self.settings.llm_model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
            "temperature": temperature,
            "max_tokens": max_tokens or self.settings.llm_max_tokens,
        }
        thinking = (self.settings.llm_thinking or "auto").lower()
        effort = {"off": "none", "low": "low", "medium": "medium", "high": "high"}.get(thinking)
        if effort:
            payload["reasoning_effort"] = effort
        return payload

    def chat_result(
        self, user_content, max_tokens: int | None = None, temperature: float = 0.2
    ) -> ChatResult:
        payload = self._payload(user_content, max_tokens, temperature)
        last_error: Exception | None = None
        started = time.time()
        for attempt in range(1, 4):
            try:
                response = self._client.post("/chat/completions", json=payload)
                if response.status_code >= 400:
                    body = response.text[:300]
                    # 部分服务商不认 reasoning_effort，去除后重试一次
                    if payload.get("reasoning_effort") and response.status_code == 400:
                        payload = {k: v for k, v in payload.items() if k != "reasoning_effort"}
                    raise LLMError(f"HTTP {response.status_code}: {body}")
                data = response.json()
                choice = data["choices"][0]
                message = choice.get("message", {})
                content = message.get("content") or ""
                if not content.strip():
                    reasoning = message.get("reasoning_content") or ""
                    if "items" in reasoning:
                        content = reasoning
                    else:
                        raise LLMError(
                            f"模型未返回内容（finish_reason={choice.get('finish_reason')}，"
                            f"reasoning {len(reasoning)} 字）"
                        )
                if choice.get("finish_reason") == "length" and '"items"' not in content:
                    raise LLMError("模型输出被截断，未得到完整 JSON")
                return ChatResult(
                    content=content,
                    usage=_usage_summary(data.get("usage")),
                    elapsed=time.time() - started,
                    model=data.get("model") or self.settings.llm_model,
                    finish_reason=choice.get("finish_reason"),
                    attempts=attempt,
                )
            except Exception as exc:  # noqa: BLE001
                last_error = exc
                if attempt < 3:
                    time.sleep(1.5 * attempt)
        raise LLMError(f"LLM 调用失败：{last_error}")

    def chat(self, user_content, max_tokens: int | None = None, temperature: float = 0.2) -> str:
        return self.chat_result(user_content, max_tokens, temperature).content

    def clean_items(
        self,
        items: list[dict],
        reference: str = "",
        image_paths: list | None = None,
    ) -> tuple[dict[int, str], dict]:
        """items: [{"id": int, "text": str}] -> ({id: cleaned_text}, meta)。

        reference 为背景/术语表/资料；image_paths 为同一时间段的关键截图路径。
        meta 记录实际发送的请求文本与模型原始输出，供 trajectory 展示。
        """
        if not items:
            return {}, {
                "request_text": "",
                "response_text": "",
                "attached_images": 0,
                "usage": {},
                "elapsed": 0.0,
                "model": self.settings.llm_model,
                "finish_reason": None,
                "attempts": 0,
            }
        image_paths = image_paths or []
        sections: list[str] = []
        if reference:
            sections.append(f"# 参考资料与术语（用于纠错）\n{reference}")
        if image_paths:
            sections.append(
                f"# 画面截图\n下面附有 {len(image_paths)} 张该时间段的视频截图，请结合画面中的文字校正 ASR。"
            )
        sections.append("请处理以下 ASR 文本：\n" + json.dumps({"items": items}, ensure_ascii=False))
        request_text = "\n\n".join(sections)

        attached = 0
        if image_paths:
            content: list[dict] = [{"type": "text", "text": request_text}]
            for path in image_paths:
                try:
                    content.append(_image_part(path))
                    attached += 1
                except Exception:
                    continue
            user_content: object = content
        else:
            user_content = request_text

        result = self.chat_result(user_content)
        raw = result.content
        parsed = _extract_json(raw)

        results: dict[int, str] = {}
        if isinstance(parsed, dict):
            entries = parsed.get("items") or parsed.get("results") or []
        elif isinstance(parsed, list):
            entries = parsed
        else:
            entries = []
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            if "id" not in entry:
                continue
            try:
                item_id = int(entry["id"])
            except (TypeError, ValueError):
                continue
            value = entry.get("text")
            results[item_id] = "" if value is None else str(value).strip()
        meta = {
            "request_text": request_text,
            "response_text": raw,
            "attached_images": attached,
            "usage": result.usage,
            "elapsed": round(result.elapsed, 2),
            "model": result.model,
            "finish_reason": result.finish_reason,
            "attempts": result.attempts,
        }
        return results, meta
