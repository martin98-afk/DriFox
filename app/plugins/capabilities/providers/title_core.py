# -*- coding: utf-8 -*-
"""会话标题提交能力 provider（host = MainWidget）。

插件只传 window_id 与标题字符串；窗口私有状态的守卫、落库与 UI 联动
留在 host 侧。对齐 history_core.py：纯函数 provider，不持有窗口引用。
"""
from __future__ import annotations

from typing import Any

from loguru import logger

_MAX_TITLE_LENGTH = 50


def _resolve_window_by_id(window_id: str) -> Any:
    """按 _window_id 找存活聊天窗口；与 ui_core 同源，找不到返回 None。"""
    from app.plugins.capabilities.providers.ui_core import _resolve_window_by_id as resolve

    return resolve(window_id)


def _session_title_commit(host: Any):
    """带全部守卫地提交标题。原 MainWidget._on_topic_summary_generated 的搬迁版。

    window_id 解析失败（窗口已关）静默返回 False，等价旧 _is_destroyed +
    RuntimeError 守卫。
    """

    def commit(window_id: str = "", title: str = "") -> bool:
        target = _resolve_window_by_id(window_id) if window_id else host
        if target is None:
            return False
        clean = str(title or "").strip()
        if not clean:
            return False

        backend = getattr(target, "backend", None)
        session = backend.get_current_session() if backend is not None else None
        if session is None or getattr(session, "user_edited_title", False):
            return False

        # 临时对话页：不落库，只刷 UI（其它调用方的兼容分支）
        if getattr(session, "is_temp", False):
            session.set_topic_summary(clean)
            target.title_edit.setText(clean)
            target._sync_dialog_title()
            return True

        if len(clean) > _MAX_TITLE_LENGTH:
            logger.warning(f"[TitleCommit] 标题过长({len(clean)}字)，跳过更新")
            return False

        session.set_topic_summary(clean)

        if target._current_session_id is None and session.messages:
            worktree_path = target._get_current_worktree_path()
            team_kwargs = {}
            if getattr(target, "_team_run_id", None):
                team_kwargs = {
                    "team_run_id": target._team_run_id,
                    "team_name": getattr(target, "_team_name", "") or "",
                    "agent_name": getattr(target, "_team_agent_name", "") or "",
                }
            resolved_project = target._resolve_session_project_fallback(
                session.session_id, target._current_project, session=session
            )
            target.history_manager.save_session(
                session.messages,
                title=clean,
                session_id=session.session_id,
                compaction_state=getattr(session, "compaction_state", {}),
                compaction_cache=getattr(session, "compaction_cache", {}),
                project=resolved_project,
                **target._api_usage_kwargs(session),
                worktree_path=worktree_path or "",
                **team_kwargs,
            )
            target._current_session_id = session.session_id

        if target._current_session_id is not None:
            idx = target.history_manager.find_index_by_session_id(target._current_session_id)
            if idx is not None:
                target.history_manager.update_topic_summary(idx, clean)

        target.title_edit.setText(clean)
        target._sync_dialog_title()
        return True

    return commit


def register(registry) -> None:
    registry.register(
        "session.title.commit", _session_title_commit, surface="ui", owner="core:title"
    )
