"""单声道混音选择（左右声道处理）的单元测试：不调用 ffmpeg。"""

from __future__ import annotations

from types import SimpleNamespace

from subtitles_agent import transcribe


def _patch_volumes(monkeypatch, values):
    monkeypatch.setattr(transcribe, "probe_channels", lambda settings, media: 2)
    monkeypatch.setattr(
        transcribe, "_mean_volume", lambda settings, media, pan=None, duration=None: values.get(pan)
    )


def test_one_channel_silent_is_not_phase_inversion(monkeypatch):
    # 左 -25.6dB / 右 -91.0dB（右几乎静音）/ 混单 -31.6dB
    _patch_volumes(
        monkeypatch,
        {"c0=c0": -25.6, "c0=c1": -91.0, "c0=0.5*c0+0.5*c1": -31.6},
    )
    logs: list[str] = []
    result = transcribe.choose_mono_filter(SimpleNamespace(), "video.mp4", logs.append)
    assert result == "c0=c0"
    assert any("只有左声道有声音" in line for line in logs)
    assert all("反相" not in line for line in logs)


def test_phase_inversion_still_detected(monkeypatch):
    _patch_volumes(
        monkeypatch,
        {"c0=c0": -25.0, "c0=c1": -26.0, "c0=0.5*c0+0.5*c1": -60.0},
    )
    logs: list[str] = []
    result = transcribe.choose_mono_filter(SimpleNamespace(), "video.mp4", logs.append)
    assert result == "c0=c0"
    assert any("反相" in line for line in logs)


def test_normal_stereo_uses_default_mix(monkeypatch):
    _patch_volumes(
        monkeypatch,
        {"c0=c0": -20.0, "c0=c1": -21.0, "c0=0.5*c0+0.5*c1": -14.0},
    )
    assert transcribe.choose_mono_filter(SimpleNamespace(), "video.mp4") is None


def test_mono_skips_channel_analysis(monkeypatch):
    monkeypatch.setattr(transcribe, "probe_channels", lambda settings, media: 1)

    def _unexpected(*args, **kwargs):
        raise AssertionError("单声道不应再做声道音量分析")

    monkeypatch.setattr(transcribe, "_mean_volume", _unexpected)
    logs: list[str] = []
    assert transcribe.choose_mono_filter(SimpleNamespace(), "video.mp4", logs.append) is None
    assert logs == []


def test_multichannel_skips_channel_analysis(monkeypatch):
    monkeypatch.setattr(transcribe, "probe_channels", lambda settings, media: 6)

    def _unexpected(*args, **kwargs):
        raise AssertionError("多声道不应再做双声道分析")

    monkeypatch.setattr(transcribe, "_mean_volume", _unexpected)
    assert transcribe.choose_mono_filter(SimpleNamespace(), "video.mp4") is None


def test_unknown_channel_count_falls_back_to_analysis(monkeypatch):
    monkeypatch.setattr(transcribe, "probe_channels", lambda settings, media: None)
    monkeypatch.setattr(
        transcribe,
        "_mean_volume",
        lambda settings, media, pan=None, duration=None: {
            "c0=c0": -25.0,
            "c0=c1": -26.0,
            "c0=0.5*c0+0.5*c1": -60.0,
        }.get(pan),
    )
    logs: list[str] = []
    assert transcribe.choose_mono_filter(SimpleNamespace(), "video.mp4", logs.append) == "c0=c0"
    assert any("反相" in line for line in logs)
