# -*- coding: utf-8 -*-
"""主题音乐的单曲开关存储 + 曲目路径解析

存储位置：`<app_data>/plugin_data/<插件名>/theme_music.json`

为什么按插件分组：主题由插件提供，用户「这个主题里我不听这首」的选择属于该
插件的数据，跟着插件走（插件卸载/迁移时一起带走），不占用主程序 app.config，
也不再新建独立的存储根目录。

文件结构（按主题 id 归组）::

    {"the-last-of-us": ["D:/.../01-theme-song.mp3"]}

用户主题（`<app_data>/themes/` 下自建，不属任何插件）统一落到
`plugin_data/user-themes/theme_music.json`。

写入用「临时文件 + os.replace」原子替换，与 plugin_config_store 同一套路。
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Iterable, Optional

from loguru import logger

# 不属任何插件的用户主题统一落到这个分组
_USER_SLUG = "user-themes"

_IO_LOCK = threading.RLock()


def plugin_slug_for_theme(theme_id: str) -> str:
    """推导主题所属插件的目录名（用作 plugin_data/<slug>/）

    主题路径约定：`<插件目录>/themes/<主题>/<主题>.yaml` 或单文件
    `<插件目录>/themes/<主题>.yaml`。从主题目录向上找到 `themes`，其父目录
    若带插件清单（`.drifox-plugin/` 或 `plugin.json`）即插件目录。
    用户主题目录 `<app_data>/themes/` 下没有插件清单，落 user-themes。
    """
    try:
        from app.utils.theme_manager import theme_manager

        theme = theme_manager.get_theme(theme_id) or {}
    except Exception:
        theme = {}

    raw = theme.get("_dir") or theme.get("_path") or ""
    if not raw:
        return _USER_SLUG
    node = Path(raw)
    if node.suffix:  # YAML 文件 → 取所在目录
        node = node.parent
    for anc in (node, *node.parents):
        if anc.name != "themes":
            continue
        candidate = anc.parent
        if (candidate / ".drifox-plugin").exists() or (candidate / "plugin.json").exists():
            return candidate.name
        break
    return _USER_SLUG


def _store_path(theme_id: str) -> Path:
    from app.utils.utils import get_app_data_dir

    slug = plugin_slug_for_theme(theme_id)
    return Path(get_app_data_dir()) / "plugin_data" / slug / "theme_music.json"


def _read(path: Path) -> dict:
    try:
        with _IO_LOCK:
            if path.exists():
                data = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return data
    except Exception as e:
        logger.warning(f"[ThemeMusicStore] 读取失败（按空处理）{path}: {e}")
    return {}


def _write(path: Path, data: dict) -> bool:
    try:
        with _IO_LOCK:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, path)
        return True
    except Exception as e:
        logger.warning(f"[ThemeMusicStore] 写入失败 {path}: {e}")
        return False


def load_disabled(theme_id: str) -> frozenset:
    """该主题下被用户关掉的曲目绝对路径集合"""
    data = _read(_store_path(theme_id))
    raw = data.get(theme_id) or []
    if isinstance(raw, str):
        raw = [raw]
    return frozenset(str(p) for p in raw if p)


def save_disabled(theme_id: str, paths: Iterable[str]) -> bool:
    """整体替换该主题的关闭清单（空集合 = 删除该键）"""
    path = _store_path(theme_id)
    with _IO_LOCK:
        data = _read(path)
        cleaned = [str(p) for p in paths if p]
        if cleaned:
            data[theme_id] = cleaned
        else:
            data.pop(theme_id, None)
        return _write(path, data)


def resolve_track_path(theme_id: str, raw: str) -> Optional[str]:
    """把 YAML 里的曲目路径解析成绝对路径；无效（qrc / 不存在）返回 None

    规则与播放器一致：绝对路径原样、相对路径基于主题目录、`:qrc` 不支持
    （QtMultimedia 后端读不到 Qt 资源系统）。
    """
    from app.utils.theme_manager import theme_manager

    text = str(raw or "").strip()
    if not text or text.startswith(":"):
        return None
    if os.path.isabs(text):
        return os.path.abspath(text) if os.path.exists(text) else None
    theme_dir = theme_manager.get_theme_dir(theme_id)
    if not theme_dir:
        return None
    # 主题目录可能本身是相对路径（插件扫描时按相对路径登记），
    # 必须 abspath 才能与播放器 _resolve_tracks 的绝对路径身份对齐，
    # 否则单曲开关的禁用集与播放列表求交集恒为空，开关等于没生效。
    path = os.path.abspath(os.path.normpath(str(theme_dir / text)))
    return path if os.path.exists(path) else None


def theme_tracks(theme_id: str) -> tuple:
    """当前主题的有效曲目：((绝对路径, 显示名), ...)，保序去重

    显示名取文件名；music 未配置 / enabled=False / 全部无效时返回空元组，
    设置卡据此隐藏整张卡。
    """
    from app.utils.theme_manager import theme_manager

    try:
        music = theme_manager.get_theme_backgrounds(theme_id).get("music")
    except Exception:
        music = None
    if not music or not music.get("enabled", True):
        return ()

    out = []
    seen = set()
    for raw in music.get("tracks") or []:
        path = resolve_track_path(theme_id, raw)
        if not path or path in seen:
            continue
        seen.add(path)
        out.append((path, os.path.basename(path)))
    return tuple(out)
