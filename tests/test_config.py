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
            "llm": {"source": "manual", "api_key": "sk-test"},
            "subtitles": {"max_chars": 22},
        }
    )
    assert updated.project_dir == (tmp_path / "videos").resolve()
    assert updated.llm_api_key == "sk-test"
    assert updated.default_max_chars == 22
    # 原对象保持原值
    assert settings.llm_api_key == ""
    assert settings.default_max_chars == 18


def test_copy_from_updates_in_place(tmp_path):
    settings = config._settings_from_dict(config.DEFAULT_CONFIG, tmp_path)
    updated = settings.updated_with({"llm": {"source": "manual", "model": "another-model"}})
    settings.copy_from(updated)
    assert settings.llm_model == "another-model"


def test_pi_source_reads_pi_and_does_not_persist(tmp_path, monkeypatch):
    monkeypatch.setattr(
        config,
        "_pi_defaults",
        lambda: {
            "llm": {"base_url": "https://pi/v1", "api_key": "pi-key", "model": "pi-model"}
        },
    )
    settings = config._resolve_settings({}, tmp_path)
    assert settings.llm_source == "pi"
    assert settings.llm_base_url == "https://pi/v1"
    assert settings.llm_api_key == "pi-key"
    assert settings.llm_model == "pi-model"

    # pi 的凭据留在 ~/.pi，config.toml 对应字段为空
    saved_llm = settings.to_dict()["llm"]
    assert saved_llm["source"] == "pi"
    assert saved_llm["base_url"] == ""
    assert saved_llm["api_key"] == ""
    assert saved_llm["model"] == ""

    # 从 pi 切到手动：保存手动填写的值
    manual = settings.updated_with(
        {
            "llm": {
                "source": "manual",
                "base_url": "https://mine/v1",
                "api_key": "my-key",
                "model": "my-model",
            }
        }
    )
    assert manual.llm_source == "manual"
    assert manual.llm_api_key == "my-key"
    assert manual.to_dict()["llm"]["api_key"] == "my-key"

    # 切回 pi：配置取自 pi，手动值清空
    back = manual.updated_with({"llm": {"source": "pi"}})
    assert back.llm_source == "pi"
    assert back.llm_api_key == "pi-key"
    assert back.to_dict()["llm"]["api_key"] == ""


def test_pi_source_ignores_stale_file_values(tmp_path, monkeypatch):
    monkeypatch.setattr(
        config, "_pi_defaults", lambda: {"llm": {"api_key": "pi-key", "model": "pi-model"}}
    )
    settings = config._resolve_settings(
        {"llm": {"source": "pi", "api_key": "stale", "model": "stale"}}, tmp_path
    )
    assert settings.llm_api_key == "pi-key"
    assert settings.llm_model == "pi-model"


def test_legacy_file_without_source_is_manual(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "_pi_defaults", lambda: {})
    settings = config._resolve_settings(
        {"llm": {"api_key": "old-key", "model": "old-model"}}, tmp_path
    )
    assert settings.llm_source == "manual"
    assert settings.llm_api_key == "old-key"
    assert settings.llm_model == "old-model"


def test_legacy_copy_of_pi_values_falls_back_to_pi(tmp_path, monkeypatch):
    monkeypatch.setattr(
        config,
        "_pi_defaults",
        lambda: {
            "llm": {
                "base_url": "https://pi/v1",
                "api_key": "pi-key",
                "model": "pi-model",
            }
        },
    )
    # config.toml 保存 pi 配置副本并省略 source 字段
    settings = config._resolve_settings(
        {
            "llm": {
                "base_url": "https://pi/v1",
                "api_key": "pi-key",
                "model": "pi-model",
            }
        },
        tmp_path,
    )
    assert settings.llm_source == "pi"
    assert settings.llm_api_key == "pi-key"
    # 保存后文件里只保留 pi 来源标记
    assert settings.to_dict()["llm"]["api_key"] == ""


def test_save_then_load(tmp_path, monkeypatch):
    target = tmp_path / "config.toml"
    monkeypatch.setattr(config, "config_path", lambda: target)
    monkeypatch.setattr(config, "_pi_defaults", lambda: {})

    settings = config.load_settings()
    settings.copy_from(
        settings.updated_with(
            {
                "paths": {"project_dir": "videos"},
                "llm": {
                    "source": "manual",
                    "api_key": "sk-secret",
                    "model": "my-model",
                },
            }
        )
    )
    config.save_settings(settings)
    assert target.is_file()

    loaded = config.load_settings()
    assert loaded.llm_api_key == "sk-secret"
    assert loaded.llm_model == "my-model"
    assert loaded.project_dir == (tmp_path / "videos").resolve()
