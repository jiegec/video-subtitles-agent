"""章节列表生成相关纯函数测试，只使用本地数据。"""

from __future__ import annotations

from subtitles_agent.chapters import (
    build_timeline,
    format_time,
    parse_chapters,
    parse_chapters_text,
    render_chapters,
)
from subtitles_agent.subtitles import Cue


def test_format_time():
    assert format_time(0) == "00:00:00"
    assert format_time(3661) == "01:01:01"
    assert format_time(-5) == "00:00:00"


def test_parse_chapters_forces_zero_snaps_and_limits():
    text = """
    1. 00:03:10 基本概念
    00:00:07 开场介绍
    00:12:00 高阶内容
    00:20:00 第十一个
    """
    chapters = parse_chapters(text, [7, 190, 720, 1200, 1500], max_count=10, max_title_chars=16)
    assert chapters[0]["start"] == 0
    assert chapters[0]["title"] == "开场介绍"
    assert [c["start"] for c in chapters] == [0, 190, 720, 1200]


def test_parse_chapters_truncates_title():
    chapters = parse_chapters("00:00:00 这是一个非常非常非常长的标题超过十六个字", [0])
    assert len(chapters[0]["title"]) == 16


def test_parse_chapters_dedupes_snapped_times():
    chapters = parse_chapters("00:00:00 一\n00:00:01 二\n00:00:02 三", [0, 1, 2])
    assert [c["start"] for c in chapters] == [0, 1, 2]


def test_parse_chapters_max_count():
    lines = "\n".join(f"00:{i:02d}:00 章节{i}" for i in range(15))
    chapters = parse_chapters(lines, [i * 60 for i in range(15)], max_count=10)
    assert len(chapters) == 10


def test_render_and_reload(tmp_path):
    chapters = [{"start": 0, "title": "开场"}, {"start": 65, "title": "概念"}]
    text = render_chapters(chapters)
    assert text == "00:00:00 开场\n00:01:05 概念\n"
    assert parse_chapters_text(text) == chapters


def test_build_timeline_groups_by_window():
    cues = [Cue(0, 5, "你好"), Cue(6, 10, "世界"), Cue(30, 35, "新话题")]
    timeline = build_timeline(cues, budget=10000, window_sec=20)
    assert timeline.count("\n") == 1
    assert "[00:00:00] 你好世界" in timeline
    assert "[00:00:30] 新话题" in timeline


def test_parse_chapters_empty_returns_empty():
    assert parse_chapters("这里没有时间戳", [0]) == []
