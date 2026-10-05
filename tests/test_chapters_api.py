"""章节列表接口测试（复用项目的 FastAPI app，但把 project_dir 指向临时目录）。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from subtitles_agent.web import app as appmod


def test_chapters_endpoints(tmp_path, monkeypatch):
    (tmp_path / "a.mp4").write_bytes(b"")
    (tmp_path / "a.srt").write_text(
        "1\n00:00:00,000 --> 00:00:02,000\n你好\n", encoding="utf-8"
    )
    monkeypatch.setattr(appmod.settings, "project_dir", tmp_path)
    appmod.library.clear_duration_cache()
    monkeypatch.setattr(
        appmod,
        "generate_chapters",
        lambda settings, cues: ([{"start": 0, "title": "开场"}], {"model": "test"}),
    )

    client = TestClient(appmod.app)

    # 尚未生成
    response = client.get("/api/chapters", params={"path": "a.mp4"})
    assert response.status_code == 200
    assert response.json()["exists"] is False
    assert response.json()["chapters"] == []

    # 生成并落盘
    response = client.post("/api/chapters", json={"path": "a.mp4"})
    assert response.status_code == 200
    data = response.json()
    assert data["chapters"] == [{"start": 0, "title": "开场"}]
    assert data["file_name"] == "a.chapters.txt"
    assert (tmp_path / "a.chapters.txt").is_file()

    # 再读取
    response = client.get("/api/chapters", params={"path": "a.mp4"})
    assert response.json()["exists"] is True
    assert response.json()["chapters"] == [{"start": 0, "title": "开场"}]

    # 下载
    response = client.get("/api/chapters/download", params={"path": "a.mp4"})
    assert response.status_code == 200
    assert response.text == "00:00:00 开场\n"


def test_chapters_requires_srt(tmp_path, monkeypatch):
    (tmp_path / "b.mp4").write_bytes(b"")
    monkeypatch.setattr(appmod.settings, "project_dir", tmp_path)
    client = TestClient(appmod.app)
    response = client.post("/api/chapters", json={"path": "b.mp4"})
    assert response.status_code == 404
