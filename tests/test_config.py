"""配置系统单元测试：TOML 读写、路径解析与就地更新。"""

from __future__ import annotations

from subtitles_agent import config


def test_toml_roundtrip(tmp_path):
    data = config._deep_merge(config.DEFAULT_CONFIG, {"llm": {"api_key": "sk-x"}})
    path = tmp_path / "config.toml"
    path.write_text(config.dumps_toml(data), encoding="utf-8")

    parsed = config._read_toml(path)
    assert parsed["llm"]["api_key"] == "sk-x"
    assert parsed["subtitles"]["max_chars"] == 18
    assert parsed["paths"]["project_dir"] == "project"


def test_settings_resolves_relative_paths(tmp_path):
    settings = config._settings_from_dict(config.DEFAULT_CONFIG, tmp_path)
    assert settings.project_dir == (tmp_path / "project").resolve()
    assert settings.whisper_model == (tmp_path / "ggml-medium.bin").resolve()
    assert settings.recover_model is None


def test_updated_with_merges_and_keeps_original(tmp_path):
    settings = config._settings_from_dict(config.DEFAULT_CONFIG, tmp_path)
    updated = settings.updated_with(
        {
            "paths": {"project_dir": "videos"},
            "llm": {"api_key": "sk-test"},
            "subtitles": {"max_chars": 22},
        }
    )
    assert updated.project_dir == (tmp_path / "videos").resolve()
    assert updated.llm_api_key == "sk-test"
    assert updated.default_max_chars == 22
    # 原对象不受影响
    assert settings.llm_api_key == ""
    assert settings.default_max_chars == 18


def test_copy_from_updates_in_place(tmp_path):
    settings = config._settings_from_dict(config.DEFAULT_CONFIG, tmp_path)
    updated = settings.updated_with({"llm": {"model": "another-model"}})
    settings.copy_from(updated)
    assert settings.llm_model == "another-model"


def test_save_then_load(tmp_path, monkeypatch):
    target = tmp_path / "config.toml"
    monkeypatch.setattr(config, "config_path", lambda: target)
    monkeypatch.setattr(config, "_pi_defaults", lambda: {})

    settings = config.load_settings()
    settings.copy_from(
        settings.updated_with(
            {
                "paths": {"project_dir": "videos"},
                "llm": {"api_key": "sk-secret", "model": "my-model"},
            }
        )
    )
    config.save_settings(settings)
    assert target.is_file()

    loaded = config.load_settings()
    assert loaded.llm_api_key == "sk-secret"
    assert loaded.llm_model == "my-model"
    assert loaded.project_dir == (tmp_path / "videos").resolve()
