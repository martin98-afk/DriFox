# -*- coding: utf-8 -*-
"""会话存储引擎契约 — 会话持久化后端可插拔（Phase A 只交付接口与 SQLite 默认实现，
消费方迁移在 Phase B）。方法签名与 SessionRepository 对齐。

Phase B 扩展：可选能力接口（消费方 isinstance 探测使用；引擎可不实现——
不支持时消费方安全降级）。"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Protocol, runtime_checkable


@runtime_checkable
class SessionStorageEngine(Protocol):
    """会话存储引擎接口"""

    id: str

    def save(self, session: Dict) -> bool: ...

    def get(self, session_id: str) -> Optional[Dict]: ...

    def get_all(self, limit: int = 100, offset: int = 0) -> List[Dict]: ...

    def get_by_project(self, project: str, limit: int = 100) -> List[Dict]: ...

    def get_projects(self) -> List[str]: ...

    def delete(self, session_id: str) -> bool: ...


@runtime_checkable
class SessionTitleCapability(Protocol):
    """可选能力：更新会话标题"""

    def update_session_title(self, session_id: str, title: str) -> bool: ...


@runtime_checkable
class SessionCountsCapability(Protocol):
    """可选能力：按项目统计会话数量"""

    def get_session_counts(self) -> Dict[str, int]: ...


@runtime_checkable
class InputHistoryCapability(Protocol):
    """可选能力：输入历史（最新在前）"""

    def get_input_history(self, limit: int = 50) -> Optional[List[Dict[str, Any]]]: ...

    def add_input_history(self, content: str, attachments: Optional[list] = None) -> bool: ...

@runtime_checkable
class SessionBulkGetCapability(Protocol):
    """可选能力：按 session_id 批量取回会话（I6 引用会话 N+1 消除）。

    与 SessionTitleCapability 同构的可选能力：引擎可不实现；消费方
    （session_reference）isinstance 探测，不支持时安全降级逐个 get()。
    本 Protocol 不进入 SessionStorageEngine 主契约，多存储插件零波及。

    约定签名：get_by_ids(session_ids) -> Dict[session_id, session_dict]，
    查不到 / 失败的 id 不出现在结果中。
    """

    def get_by_ids(self, session_ids: List[str]) -> Dict[str, Dict]: ...
