"""纯函数单元测试：不依赖 ffmpeg / whisper / LLM，可在 CI 直接跑。"""

from __future__ import annotations

from types import SimpleNamespace

from subtitles_agent import backup
from subtitles_agent.subtitles import (
    Cue,
    cues_to_srt,
    diff_cues,
    format_timestamp,
    merge_cue_lists,
    parse_srt,
    reflow,
    split_spans,
)
from subtitles_agent.trajectory import char_diff, diff_stats
from subtitles_agent.transcribe import Segment, build_initial_prompt, strip_hallucinations
from subtitles_agent.video_config import VideoConfig, normalize_documents, normalize_glossary


# --------------------------------------------------------------------------- #
# SRT / 时间格式
# --------------------------------------------------------------------------- #
def test_format_timestamp():
    assert format_timestamp(0) == "00:00:00,000"
    assert format_timestamp(3661.5) == "01:01:01,500"
    assert format_timestamp(-1) == "00:00:00,000"


def test_srt_roundtrip():
    cues = [Cue(0.0, 1.5, "你好"), Cue(1.5, 3.0, "世界")]
    parsed = parse_srt(cues_to_srt(cues))
    assert [(c.start, c.end, c.text) for c in parsed] == [
        (0.0, 1.5, "你好"),
        (1.5, 3.0, "世界"),
    ]


# --------------------------------------------------------------------------- #
# 断句 / 对齐 / 重排
# --------------------------------------------------------------------------- #
def test_split_spans_contiguous_and_bounded():
    text = "一二三四五六七八九十，十一十二"
    spans = split_spans(text, 6)
    assert spans[0][0] == 0
    assert spans[-1][1] == len(text)
    for a, b in spans:
        assert b > a
        assert len(text[a:b]) <= 6 + 1
    # 连续覆盖
    for (_, prev_end), (next_start, _) in zip(spans, spans[1:]):
        assert prev_end == next_start


def test_split_spans_hard_cut_keeps_trailing_punct():
    # 10 个字 + 句号，限制 5，句号不应跑到下一句开头
    spans = split_spans("一二三四五六七八九十。下一句", 5)
    assert not any("一二三四五六七八九十。下一句"[a:b].startswith("。") for a, b in spans[1:])


def test_reflow_timing_within_bounds_and_monotonic():
    timeline = [(ch, i * 0.2, (i + 1) * 0.2) for i, ch in enumerate("你好世界")]
    cues = reflow("你好世界", timeline, 0.0, 0.8, max_chars=2, min_duration=0.05)
    assert [c.text for c in cues] == ["你好", "世界"]
    assert cues[0].start >= 0.0
    assert cues[-1].end <= 0.8 + 0.3
    for prev, cur in zip(cues, cues[1:]):
        assert cur.start >= prev.start
        assert cur.start >= prev.end - 0.001


# --------------------------------------------------------------------------- #
# 幻觉过滤 / whisper 提示词
# --------------------------------------------------------------------------- #
def test_strip_hallucinations_removes_repeated():
    segs = [Segment(i, i + 1, "我们可以看到很多不同的语音。", []) for i in range(8)]
    segs.append(Segment(8, 9, "谢谢大家", []))
    kept, removed = strip_hallucinations(segs, min_count=3, ratio=0.5)
    assert removed == 8
    assert [s.text for s in kept] == ["谢谢大家"]


def test_strip_hallucinations_keeps_normal():
    segs = [Segment(i, i + 1, f"第{i}句正常内容", []) for i in range(6)]
    kept, removed = strip_hallucinations(segs)
    assert removed == 0
    assert len(kept) == 6


def test_build_initial_prompt_does_not_cut_terms():
    settings = SimpleNamespace(whisper_prompt="以下是简体中文的语音内容。")
    glossary = [{"term": f"术语{i:02d}"} for i in range(40)]
    prompt = build_initial_prompt(settings, glossary, limit=40)
    assert len(prompt) <= 40
    assert prompt.endswith("。")


# --------------------------------------------------------------------------- #
# diff / 合并
# --------------------------------------------------------------------------- #
def test_char_diff_stats():
    ops = char_diff("你好世界", "你好，世界")
    assert diff_stats(ops)["inserted_chars"] == 1
    assert diff_stats(ops)["deleted_chars"] == 0


def test_diff_and_merge_cues():
    old = [Cue(0, 1, "你好"), Cue(1, 2, "世界")]
    new = [Cue(0, 1, "你好"), Cue(2, 3, "新内容")]
    stats = diff_cues(old, new)["stats"]
    assert stats == {
        "total_old": 2,
        "total_new": 2,
        "added": 1,
        "removed": 1,
        "changed": 0,
        "equal": 1,
    }
    merged = merge_cue_lists(new, old)
    assert len(merged) == 3
    assert [c.start for c in merged] == sorted(c.start for c in merged)


# --------------------------------------------------------------------------- #
# 备份
# --------------------------------------------------------------------------- #
def test_backup_dedupe_and_history(tmp_path):
    srt = tmp_path / "demo.srt"
    srt.write_text("1\n00:00:00,000 --> 00:00:01,000\n第一版\n", encoding="utf-8")
    assert backup.backup_srt(srt) is not None
    assert backup.backup_srt(srt) is None  # 内容相同，跳过
    srt.write_text("1\n00:00:00,000 --> 00:00:01,000\n第二版\n", encoding="utf-8")
    assert backup.backup_srt(srt) is not None
    assert len(backup.list_backups(srt)) == 2


# --------------------------------------------------------------------------- #
# video config
# --------------------------------------------------------------------------- #
def test_normalize_glossary_and_documents():
    assert normalize_glossary(["哈希表 = 哈西表"]) == [
        {"term": "哈希表", "aliases": [], "note": "哈西表"}
    ]
    assert normalize_glossary([{"term": "文件", "aliases": ["文见"]}]) == [
        {"term": "文件", "aliases": ["文见"], "note": ""}
    ]
    assert normalize_documents("a.pdf") == ["a.pdf"]


def test_video_config_roundtrip(tmp_path):
    path = tmp_path / "v.subtitles.json"
    VideoConfig(path).update(
        {
            "title": "标题",
            "context": "背景",
            "glossary": [{"term": "文件", "aliases": ["文见"]}],
            "documents": ["a.pdf"],
            "frame_interval": 30,
        }
    )
    config = VideoConfig(path)
    assert "use_frames" not in config.raw
    effective = config.effective()
    assert effective.title == "标题"
    assert effective.frame_interval == 30
    assert effective.documents == ["a.pdf"]
    assert effective.glossary[0]["term"] == "文件"
