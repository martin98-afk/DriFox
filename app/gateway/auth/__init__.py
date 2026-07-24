# -*- coding: utf-8 -*-
"""
OAuth 平台抽象层 — 注册表与工厂

对外入口：
    from app.gateway.auth import get_oauth_backend

    backend = get_oauth_backend("gitee")   # 显式指定平台
    backend = get_oauth_backend()          # 自动使用当前已绑定的平台

新增平台：
    1. 在本包下新建 <platform>.py，继承 OAuthBackend（见 base.py 文档字符串）
    2. 在下方 _BACKENDS 注册表中登记，或运行时调用 register_backend()
"""

from typing import Dict, Type

from app.gateway.auth.base import OAuthAppConfig, OAuthBackend, run_authorization_code_flow
from app.gateway.auth.gitee import GiteeOAuthBackend

__all__ = [
    "OAuthAppConfig",
    "OAuthBackend",
    "GiteeOAuthBackend",
    "get_oauth_backend",
    "register_backend",
    "available_platforms",
    "run_authorization_code_flow",
]

# ── 平台注册表 ────────────────────────────────────────────

_BACKENDS: Dict[str, Type[OAuthBackend]] = {
    GiteeOAuthBackend.name: GiteeOAuthBackend,
}


def register_backend(backend_cls: Type[OAuthBackend]) -> None:
    """注册一个平台后端（供插件或新平台模块调用）"""
    if not backend_cls.name:
        raise ValueError("OAuthBackend 子类必须定义非空的 name")
    _BACKENDS[backend_cls.name] = backend_cls


def available_platforms() -> list:
    """返回所有已注册的平台标识列表"""
    return sorted(_BACKENDS.keys())


# ── 工厂函数 ──────────────────────────────────────────────


def get_oauth_backend(name: str = "") -> OAuthBackend:
    """
    获取 OAuth 平台后端实例。

    Args:
        name: 平台标识（如 "gitee"）。为空时从配置 cloud_platform 读取
              当前已绑定的平台；若配置为空但存在旧版 gitee 绑定标记，
              则回退到 gitee（向后兼容）。

    Raises:
        ValueError: 平台名未注册，或未绑定任何平台
    """
    if not name:
        from app.utils.config import Settings

        cfg = Settings.get_instance()
        item = getattr(cfg, "cloud_platform", None)
        name = str(item.value).strip() if item is not None and item.value else ""

        # 向后兼容：旧版本配置只有 gitee_bound 标记
        if not name:
            gitee_bound = getattr(cfg, "gitee_bound", None)
            if gitee_bound is not None and gitee_bound.value:
                name = GiteeOAuthBackend.name

        if not name:
            raise ValueError("未绑定任何云平台账号")

    backend_cls = _BACKENDS.get(name)
    if backend_cls is None:
        raise ValueError(f"未知的 OAuth 平台: {name}（已注册: {', '.join(available_platforms())}）")
    return backend_cls()
