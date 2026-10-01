# -*- coding: utf-8 -*-
"""消息身份：一条消息「谁发的」的显示信息。

设计要点
=========

1. **结构极简**：只有 ``name`` + ``avatar`` 两个字段。
   头像颜色由 name 稳定派生（不入库、不配置），避免身份结构膨胀。

2. **三级解析链**：
   ① 消息自带 ``_identity`` 快照（历史消息，直接返回）
   ② 插件 provider（name / avatar 两条独立通道，按 priority）
   ③ 内置默认（助手=Drifox 品牌图标；用户=系统用户名）

3. **会话级缓存 + 引用共享**：同一 (session_id, role, team_agent) 只解析一次，
   返回**同一个对象**；消息落库时各自写 dict，运行时共享引用，不逐条复制。

4. **无 Qt 依赖**：本模块只处理数据。头像 pixmap 的加载与缓存在
   ``app/widgets/modules/identity_header.py``（渲染层）。

插件的覆盖接口见 ``UIPluginRegistry.register_identity_name_provider`` /
``register_identity_avatar_provider``。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from threading import RLock
from typing import Any, Dict, Optional

# 默认身份
DEFAULT_ASSISTANT_NAME = "Drifox"
DEFAULT_USER_NAME = "用户"

# 内置头像引用前缀：``builtin:drifox`` → 渲染层画品牌图标
BUILTIN_AVATAR_PREFIX = "builtin:"
BUILTIN_AVATAR_DRIFOX = "builtin:drifox"


@dataclass(frozen=True)
class MessageIdentity:
    """消息发送者身份（不可变；跨消息共享实例）

    Attributes:
        name: 显示名（用户名 / 助手名 / 团队成员名）
        avatar: 头像引用。三种形态：
            - 本地图片绝对路径（``C:/.../avatar.png``）
            - 内置图标引用（``builtin:drifox``）
            - 空串 → 渲染层用 name 派生色块 + 首字母
    """

    name: str = ""
    avatar: str = ""

    def to_dict(self) -> Dict[str, str]:
        """序列化为消息体可落库的 dict（只含非空字段）"""
        data: Dict[str, str] = {}
        if self.name:
            data["name"] = self.name
        if self.avatar:
            data["avatar"] = self.avatar
        return data

    @classmethod
    def from_dict(cls, data: Any) -> Optional["MessageIdentity"]:
        """从消息体 ``_identity`` 还原；非法或无名返回 None"""
        if not isinstance(data, dict):
            return None
        name = str(data.get("name") or "").strip()
        if not name:
            return None
        return cls(name=name, avatar=str(data.get("avatar") or "").strip())


# ── 会话级缓存 ──────────────────────────────────────────────
# key = (session_id, role, team_agent)。同一会话内所有同角色消息共享同一实例。
# 上限保护：会话数超过阈值时整体清空（身份重新解析成本极低，无需 LRU 精度）。
_CACHE_LOCK = RLock()
_IDENTITY_CACHE: Dict[tuple, MessageIdentity] = {}
_CACHE_MAX_KEYS = 64


def clear_cache() -> None:
    """清空身份缓存（助手切换 / 插件热重载后调用）。"""
    with _CACHE_LOCK:
        _IDENTITY_CACHE.clear()


def _system_user_name() -> str:
    """系统登录名（用户侧默认身份）。

    ⚠ getpass 局部导入：本模块可能被按路径动态加载（插件/PyInstaller 场景），
    模块级导入在缺依赖时会炸掉整个模块。
    """
    try:
        import getpass

        name = (getpass.getuser() or "").strip()
        if name:
            return name
    except Exception:
        pass
    try:
        import os

        env_name = (os.environ.get("USERNAME") or os.environ.get("USER") or "").strip()
        if env_name:
            return env_name
    except Exception:
        pass
    return DEFAULT_USER_NAME


def _default_identity(role: str, team_agent: str = "") -> MessageIdentity:
    """内置默认身份（插件未提供时回落）"""
    if role == "assistant":
        # 团队成员窗口：角色名即身份
        if team_agent:
            return MessageIdentity(name=team_agent, avatar="")
        return MessageIdentity(name=DEFAULT_ASSISTANT_NAME, avatar=BUILTIN_AVATAR_DRIFOX)
    return MessageIdentity(name=_system_user_name(), avatar="")


def _query_providers(kind: str, ctx: Dict[str, Any]) -> str:
    """按 priority 依次询问插件 provider，返回首个非空字符串。

    kind: "name" | "avatar"。
    插件未加载 / 回调抛异常 → 视作未提供，继续下一个（绝不向上抛）。
    """
    try:
        from app.plugins.registries.ui_plugin_registry import UIPluginRegistry

        registry = UIPluginRegistry.get_instance()
        providers = (
            registry.get_identity_name_providers() if kind == "name" else registry.get_identity_avatar_providers()
        )
    except Exception:
        return ""

    for info in providers:
        try:
            value = info.resolve_func(ctx)
        except Exception:
            continue
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def resolve_identity(
    role: str,
    *,
    session_id: str = "",
    team_agent: str = "",
    window_id: str = "",
) -> MessageIdentity:
    """解析某角色在当前上下文下的身份（带会话级缓存）。

    Args:
        role: ``assistant`` / ``user``
        session_id: 当前会话 ID（插件据此取会话级助手 override）
        team_agent: 团队成员角色名（空 = 非团队窗口）
        window_id: 窗口标识（插件多窗口场景可用）

    Returns:
        MessageIdentity（同一上下文返回同一对象，可安全共享引用）
    """
    key = (session_id, role, team_agent)
    with _CACHE_LOCK:
        cached = _IDENTITY_CACHE.get(key)
    if cached is not None:
        return cached

    ctx: Dict[str, Any] = {
        "role": role,
        "session_id": session_id,
        "team_agent": team_agent,
        "window_id": window_id,
    }
    name = _query_providers("name", ctx)
    avatar = _query_providers("avatar", ctx)
    fallback = _default_identity(role, team_agent)
    identity = MessageIdentity(name=name or fallback.name, avatar=avatar or (fallback.avatar if not name else ""))

    with _CACHE_LOCK:
        if len(_IDENTITY_CACHE) >= _CACHE_MAX_KEYS:
            _IDENTITY_CACHE.clear()
        _IDENTITY_CACHE[key] = identity
    return identity


def identity_from_message(message: Any, role: str) -> Optional[MessageIdentity]:
    """从消息 dict 取自带身份快照；无快照返回 None。"""
    if not isinstance(message, dict):
        return None
    return MessageIdentity.from_dict(message.get("_identity"))


# 团队任务邮件的发送者前缀：`📨 **来自 [agent@window] 的任务邮件：**`
_TEAM_MAIL_SENDER_RE = re.compile(r"来自\s*\[([^\]@]+)(?:@[^\]]*)?\]")


def team_mail_sender(message: Any) -> str:
    """团队任务邮件的发送者名（非 TeamMail 返回空串）。

    邮件正文以 `📨 **来自 [agent@window] 的任务邮件：**` 开头，发送者名即
    `agent` 部分。用户希望在成员消息上看到成员名而非「用户」，故从内容解析。

    ⚠️ 与消息落库解耦：老数据（无 `_identity` 快照）打开历史会话时也能正确
    回显发送者，无需回填迁移。
    """
    if not isinstance(message, dict):
        return ""
    if str(message.get("_hook_event") or "") != "TeamMail":
        return ""
    content = message.get("content")
    if not isinstance(content, str):
        return ""
    match = _TEAM_MAIL_SENDER_RE.search(content[:400])
    return match.group(1).strip() if match else ""


def resolve_for_message(
    message: Any,
    role: str,
    *,
    session_id: str = "",
    team_agent: str = "",
    window_id: str = "",
) -> MessageIdentity:
    """渲染入口：消息自带快照优先，其次团队邮件发送者，最后走解析链。"""
    snapshot = identity_from_message(message, role)
    if snapshot is not None:
        return snapshot
    sender = team_mail_sender(message)
    if sender:
        return MessageIdentity(name=sender, avatar="")
    return resolve_identity(role, session_id=session_id, team_agent=team_agent, window_id=window_id)
