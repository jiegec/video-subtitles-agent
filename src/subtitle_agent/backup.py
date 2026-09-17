"""SRT 历史版本：写入前把旧版本备份到 `<视频名>.backups/<视频名>-<时间戳>.srt`。"""

from __future__ import annotations

import shutil
import time
from pathlib import Path

BACKUP_DIR_SUFFIX = ".backups"


def backups_dir_for(srt_path: Path) -> Path:
    return srt_path.parent / (srt_path.stem + BACKUP_DIR_SUFFIX)


def list_backups(srt_path: Path) -> list[dict]:
    directory = backups_dir_for(srt_path)
    if not directory.is_dir():
        return []
    items: list[dict] = []
    for path in directory.glob(srt_path.stem + "-*.srt"):
        try:
            stat = path.stat()
        except OSError:
            continue
        items.append({"name": path.name, "size": stat.st_size, "mtime": stat.st_mtime})
    items.sort(key=lambda item: item["mtime"], reverse=True)
    return items


def backup_srt(
    srt_path: Path,
    keep: int = 100,
    min_interval: float = 0.0,
) -> Path | None:
    """把当前 SRT 备份一份。内容与最新备份相同、或距上次备份太近时跳过。

    返回新建的备份路径，未备份则返回 None。
    """
    if not srt_path.is_file():
        return None
    try:
        content = srt_path.read_text(encoding="utf-8")
    except OSError:
        return None
    if not content.strip():
        return None

    directory = backups_dir_for(srt_path)
    existing = (
        sorted(directory.glob(srt_path.stem + "-*.srt"), key=lambda p: p.stat().st_mtime)
        if directory.is_dir()
        else []
    )
    if existing:
        newest = existing[-1]
        try:
            if newest.read_text(encoding="utf-8") == content:
                return None
        except OSError:
            pass
        if min_interval > 0 and (time.time() - newest.stat().st_mtime) < min_interval:
            return None

    directory.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    target = directory / f"{srt_path.stem}-{stamp}.srt"
    counter = 1
    while target.exists():
        target = directory / f"{srt_path.stem}-{stamp}-{counter}.srt"
        counter += 1
    # 注意：用 copyfile 而不是 copy2，否则备份会继承源文件的旧 mtime，
    # 导致按时间排序 / 限流判断都出错。
    shutil.copyfile(srt_path, target)

    if keep > 0:
        backups = sorted(directory.glob(srt_path.stem + "-*.srt"), key=lambda p: p.stat().st_mtime)
        for old in backups[:-keep]:
            old.unlink(missing_ok=True)
    return target
