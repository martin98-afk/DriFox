"""共享 WebEngine Profile 管理模块

所有 WebEngine 视图（消息卡片、Diff Viewer 等）共用同一个 QWebEngineProfile，
实现统一的缓存、Cookie、LocalStorage 管理。

Usage:
    from app.core.webengine_profile import init_shared_web_profile, get_shared_web_profile

    # 应用启动时（在创建任何 QWebEngineView 之前）
    init_shared_web_profile(parent=app)

    # 创建 WebEngine Page 时
    page = QWebEnginePage(get_shared_web_profile(), self)
"""

from typing import Optional
from PyQt5.QtWebEngineWidgets import QWebEngineProfile
from loguru import logger

_shared_profile: Optional[QWebEngineProfile] = None


def init_shared_web_profile(parent=None) -> QWebEngineProfile:
    """初始化共享 WebEngine Profile。

    必须在创建任何 QWebEngineView 之前调用。
    所有 WebEngine 视图共用一个 Profile，共享缓存和 Cookie。

    Args:
        parent: 父对象（通常是 QApplication 实例）

    Returns:
        初始化后的 QWebEngineProfile 实例
    """
    global _shared_profile
    if _shared_profile is not None:
        logger.warning("共享 WebEngine Profile 已经初始化，跳过重复初始化")
        return _shared_profile

    _shared_profile = QWebEngineProfile("DriFoxSharedProfile", parent)
    # 启用磁盘缓存，避免每次启动都重新加载
    _shared_profile.setHttpCacheType(QWebEngineProfile.DiskHttpCache)
    # 启用持久化 Cookie，保持登录态
    _shared_profile.setPersistentCookiesPolicy(
        QWebEngineProfile.ForcePersistentCookies
    )

    logger.info("共享 WebEngine Profile 初始化完成")
    return _shared_profile


def get_shared_web_profile() -> Optional[QWebEngineProfile]:
    """获取已初始化的共享 WebEngine Profile。

    Returns:
        QWebEngineProfile 实例；若尚未初始化则返回 None
    """
    return _shared_profile
