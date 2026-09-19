"""腾讯云 ASR 后端单元测试：只测纯函数，不需要 SDK / 网络。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from subtitles_agent import tencent_asr


def test_tokens_from_sentence_uses_word_offsets():
    words = [
        SimpleNamespace(Word="你好", OffsetStartMs=0, OffsetEndMs=400),
        SimpleNamespace(Word="世界", OffsetStartMs=600, OffsetEndMs=1000),
    ]
    tokens = tencent_asr.tokens_from_sentence("你好，世界", words, 1000, 2000)
    assert len(tokens) == len("你好，世界")
    assert tokens[0]["offsets"]["from"] == 1000
    assert tokens[1]["offsets"]["to"] == 1400
    assert tokens[3]["offsets"]["from"] == 1600
    assert tokens[4]["offsets"]["to"] == 2000
    # 标点时间被线性补齐，且区间合法
    assert tokens[2]["offsets"]["from"] == 1400
    assert tokens[2]["offsets"]["to"] == 1600


def test_tokens_from_sentence_without_words_distributes_evenly():
    tokens = tencent_asr.tokens_from_sentence("你好世界", [], 0, 400)
    assert [t["text"] for t in tokens] == list("你好世界")
    assert tokens[0]["offsets"]["from"] == 0
    assert tokens[-1]["offsets"]["to"] == 400


def test_hotword_list_filters_and_limits():
    glossary = [
        {"term": "哈希表"},
        {"term": ""},
        {"term": "一二三四五六七八九十一"},  # 超过 10 字，丢弃
    ]
    hotwords = tencent_asr._hotword_list(glossary)
    assert hotwords == "哈希表|5"


def test_segments_from_status_uses_result_detail():
    status = SimpleNamespace(
        ResultDetail=[
            SimpleNamespace(
                FinalSentence="你好世界",
                StartMs=100,
                EndMs=1200,
                Words=[
                    SimpleNamespace(Word="你好", OffsetStartMs=0, OffsetEndMs=500),
                    SimpleNamespace(Word="世界", OffsetStartMs=600, OffsetEndMs=1100),
                ],
            )
        ],
        Result="ignored text timeline",
    )
    segments = tencent_asr.segments_from_status(status, offset_ms=1000)
    assert len(segments) == 1
    segment = segments[0]
    assert segment["text"] == "你好世界"
    assert segment["offsets"] == {"from": 1100, "to": 2200}
    assert segment["tokens"][0]["offsets"]["from"] == 1100
    assert segment["tokens"][-1]["offsets"]["to"] == 2200


def test_segments_from_status_empty_is_fine():
    status = SimpleNamespace(ResultDetail=[], Result="")
    assert tencent_asr.segments_from_status(status) == []


def test_segments_from_status_missing_detail_raises():
    status = SimpleNamespace(ResultDetail=[], Result="[0:00:00,000 --> 0:00:01,000] 你好")
    with pytest.raises(RuntimeError, match="ResultDetail"):
        tencent_asr.segments_from_status(status)


def test_is_auth_error():
    assert tencent_asr._is_auth_error("AuthFailure.SignatureFailure")
    assert tencent_asr._is_auth_error("AuthFailure.SecretIdNotFound")
    assert tencent_asr._is_auth_error("UnauthorizedOperation")
    assert not tencent_asr._is_auth_error("InvalidParameter")
    assert not tencent_asr._is_auth_error("")


def test_run_asr_dispatches_to_tencent(tmp_path, monkeypatch):
    from subtitles_agent import transcribe as transcribe_mod

    calls = {}

    def fake_tencent(settings, wav, out_prefix, **kwargs):
        calls["kwargs"] = kwargs
        return tmp_path / "out.json"

    monkeypatch.setattr(tencent_asr, "transcribe", fake_tencent)
    settings = SimpleNamespace(asr_backend="tencent")
    result = transcribe_mod.run_asr(
        settings, tmp_path / "a.wav", tmp_path / "out", glossary=[{"term": "哈希表"}]
    )
    assert result.name == "out.json"
    assert calls["kwargs"]["glossary"] == [{"term": "哈希表"}]
