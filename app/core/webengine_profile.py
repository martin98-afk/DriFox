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

    # 使用匿名 profile（OTR）+ 内存缓存，避免磁盘持久化开销
    # 所有卡片共享此单例 profile，复用 Chromium renderer 进程池
    profile = QWebEngineProfile(parent)
    profile.setHttpCacheType(QWebEngineProfile.MemoryHttpCache)
    profile.setPersistentCookiesPolicy(QWebEngineProfile.NoPersistentCookies)

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


def create_transient_web_profile(parent: Optional[QObject] = None) -> QWebEngineProfile:
    """创建一次性 WebEngine Profile。

    该 profile 不使用持久化缓存和 cookies，适合短生命周期的预览窗口。
    """
    profile = QWebEngineProfile(parent)
    profile.setHttpCacheType(QWebEngineProfile.MemoryHttpCache)
    profile.setPersistentCookiesPolicy(QWebEngineProfile.NoPersistentCookies)
    return profile
