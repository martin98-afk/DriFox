# -*- coding: utf-8 -*-
"""
共享 QWebEngineProfile 管理模块。

所有 WebEngine 视图（消息渲染、差异对比）使用同一个 Profile，
共享 Chromium renderer 进程池与 in-memory 缓存。

注意：Profile 与 Chromium renderer 进程是两个不同的概念。
- Profile：逻辑分组，决定缓存/Cookie/Storage 等用户数据的共享范围。
- Renderer 进程：由 Chromium 调度，每张卡片（CodeWebViewer）独立一个进程
  （见 main.py 的 QTWEBENGINE_CHROMIUM_FLAGS 设置）。

[PERF] 消息卡片通过本地 setHtml 渲染，无 HTTP 请求/cookie/localStorage 持久化需求。
改用内存缓存 + NoPersistentCookies，避免磁盘 I/O 和 Chromium 维护缓存索引的内存开销。
"""

import os
from typing import Optional

from PyQt5.QtCore import QObject
from PyQt5.QtWebEngineWidgets import QWebEngineProfile

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

    # 使用匿名 profile（OTR）；所有卡片共享此单例 profile，复用 Chromium
    # renderer 进程池。
    #
    # HTTP 缓存由内存改为磁盘：正文里的 http(s) 远程图片在长会话里会随滚动、
    # 重渲染、卡片懒加载反复下载，内存缓存进程退出即丢（重启后整屏图重下）。
    # 磁盘缓存让跨会话命中，配上限避免无限膨胀。正文是本地 setHtml 无 cookie
    # 需求，持久化策略仍为 NoPersistentCookies。
    profile = QWebEngineProfile(parent)
    profile.setHttpCacheType(QWebEngineProfile.DiskHttpCache)
    profile.setPersistentCookiesPolicy(QWebEngineProfile.NoPersistentCookies)
    _apply_cache_limits(profile)

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
        raise RuntimeError("共享 WebEngine Profile 尚未初始化。请先在 main.py 中调用 init_shared_web_profile()。")
    return _shared_profile


def _resolve_cache_dir() -> str:
    """HTTP 磁盘缓存目录（失败返回空串 → 交给 Chromium 默认位置）。

    刻意用函数内延迟导入：本模块在启动早期被调用，避免模块级牵出重型依赖
    或循环导入。目录与 app.utils.utils.get_app_data_dir() 保持同源。
    """
    try:
        from app.utils.utils import get_app_data_dir

        return os.path.join(get_app_data_dir(), "cache", "webengine")
    except Exception:
        return ""


# HTTP 磁盘缓存上限：200MB。远程图片是唯一实质使用者，超限由 Chromium LRU 淘汰。
_HTTP_CACHE_MAX_BYTES = 200 * 1024 * 1024


def _apply_cache_limits(profile: "QWebEngineProfile") -> None:
    """设置缓存目录与容量上限（Qt 版本不支持时静默跳过）。"""
    cache_dir = _resolve_cache_dir()
    if cache_dir:
        try:
            os.makedirs(cache_dir, exist_ok=True)
            profile.setPersistentStoragePath(cache_dir)
        except Exception:
            pass
    setter = getattr(profile, "setHttpCacheMaximumSize", None)
    if callable(setter):
        try:
            setter(_HTTP_CACHE_MAX_BYTES)
        except Exception:
            pass


def create_transient_web_profile(parent: Optional[QObject] = None) -> QWebEngineProfile:
    """创建一次性 WebEngine Profile。

    该 profile 不使用持久化缓存和 cookies，适合短生命周期的预览窗口
    （差异对比、图表放大查看等）。
    """
    profile = QWebEngineProfile(parent)
    profile.setHttpCacheType(QWebEngineProfile.MemoryHttpCache)
    profile.setPersistentCookiesPolicy(QWebEngineProfile.NoPersistentCookies)
    return profile
