# -*- coding: utf-8 -*-
"""
共享 QWebEngineProfile 管理模块。

所有 WebEngine 视图（消息渲染、差异对比）使用同一个 Profile，
共享 Cookie、缓存与 localStorage，确保跨视图状态一致。

注意：Profile 与 Chromium renderer 进程是两个不同的概念。
- Profile：逻辑分组，决定缓存/Cookie/Storage 等用户数据的共享范围。
- Renderer 进程：由 Chromium 调度，每张卡片（CodeWebViewer）独立一个进程
  （见 main.py 的 QTWEBENGINE_CHROMIUM_FLAGS 设置）。
因此这里保留共享 Profile，但每张卡片在浏览器内核层是隔离渲染的。
"""

from typing import Optional

from PyQt5.QtCore import QObject
from PyQt5.QtWebEngineWidgets import QWebEngineProfile

from app.utils.utils import get_app_data_dir

# 模块级单例
_shared_profile: Optional[QWebEngineProfile] = None


def init_shared_web_profile(parent: Optional[QObject] = None) -> QWebEngineProfile:
    """初始化共享 WebEngine Profile（应用启动时调用一次）。

    Args:
        parent: Profile 的 Qt 父对象，通常传入 QApplication 实例。

    Returns:
        已初始化的 QWebEngineProfile 实例。
    """
    global _shared_profile
    if _shared_profile is not None:
        return _shared_profile

    data_dir = get_app_data_dir()
    cache_dir = data_dir / "cache" / "webengine"
    storage_dir = data_dir / "cache" / "webengine_storage"
    cache_dir.mkdir(parents=True, exist_ok=True)
    storage_dir.mkdir(parents=True, exist_ok=True)

    profile = QWebEngineProfile("drifox", parent)
    profile.setHttpCacheType(QWebEngineProfile.DiskHttpCache)
    profile.setCachePath(str(cache_dir))
    profile.setPersistentStoragePath(str(storage_dir))
    profile.setPersistentCookiesPolicy(QWebEngineProfile.ForcePersistentCookies)

    _shared_profile = profile
    return profile


def get_shared_web_profile() -> QWebEngineProfile:
    """获取共享的 WebEngine Profile。

    Returns:
        QWebEngineProfile 实例。

    Raises:
        RuntimeError: 尚未调用 init_shared_web_profile()。
    """
    if _shared_profile is None:
        raise RuntimeError(
            "共享 WebEngine Profile 尚未初始化。"
            "请先在 main.py 中调用 init_shared_web_profile()。"
        )
    return _shared_profile
