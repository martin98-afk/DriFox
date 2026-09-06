# -*- coding: utf-8 -*-
"""SQLite 存储引擎 — 系统插件实现（id="sqlite"）。

薄包装现有 SessionRepository（行为零变化）。Phase B 扩展：实现可选能力接口
（标题/计数/输入历史），委托 SessionStore 同名方法——能力方法经 isinstance
探测（SessionTitleCapability / SessionCountsCapability / InputHistoryCapability）。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


class SqliteStorageEngine:
    """SQLite 存储引擎 — 薄包装现有 SessionRepository（行为零变化）"""

    id = "sqlite"

    def __init__(self, db_dir: str = None):
        from app.core.store.session_store import SessionStore

        store = SessionStore(db_dir=db_dir) if db_dir is not None else SessionStore.get_instance()
        # 复用 SessionStore 内部已初始化的 SessionRepository，避免重复构造连接池；
        # SessionRepository 期望的是 DatabaseManager，而非 SessionStore 本身。
        self._repo = store.session_repo
        # 持有 store 引用：可选能力方法（标题/计数/输入历史）与消费方方法
        # 委托 SessionStore 同名方法，保持 get_instance() 全局单例语义
        # （db 路径/连接/线程模型不变，不产生第二个连接）。
        self._store = store

    @property
    def store(self):
        """底层 SessionStore（消费方底层访问：db 签名 / repo 构造等）"""
        return self._store

    @property
    def is_initialized(self) -> bool:
        """底层存储是否已初始化（消费方 hasattr 探测的降级判据）"""
        return self._store.is_initialized

    @property
    def _db_path(self):
        """底层 db 文件路径（history_manager 跨进程签名检查用，getattr 兼容）"""
        return getattr(self._store, "_db_path", None)

    def save(self, session: dict) -> bool:
        return self._repo.save(session)

    def get(self, session_id: str):
        return self._repo.get(session_id)

    def get_all(self, limit: int = 100, offset: int = 0):
        return self._repo.get_all(limit=limit, offset=offset)

    def get_by_project(self, project: str, limit: int = 100):
        return self._repo.get_by_project(project, limit=limit)

    def get_projects(self):
        return self._repo.get_projects()

    def delete(self, session_id: str) -> bool:
        return self._repo.delete(session_id)

    # ---------- 消费方方法（history_manager / memory_manager / session_handler 调用集，
    # 委托 SessionStore 同名方法，行为零变化） ----------

    def save_session(self, session: dict) -> bool:
        return self._store.save_session(session)

    def get_session(self, session_id: str):
        return self._store.get_session(session_id)

    def get_sessions(self, limit: int = 100, offset: int = 0):
        return self._store.get_sessions(limit=limit, offset=offset)

    def get_sessions_lightweight(self, limit: int = 100, offset: int = 0):
        return self._store.get_sessions_lightweight(limit=limit, offset=offset)

    def get_sessions_by_team_run_id(self, run_id: str):
        return self._store.get_sessions_by_team_run_id(run_id)

    def get_team_first_question_candidates(self, run_id: str):
        """★★ 团队首问候选（T4 落库列查询）—— 委托底层 SessionStore。

        此前漏加此委托导致 HistoryManager._lookup_team_first_question 探测
        getter=None，70 个 run 全部走全量 fallback，启动 +325MB。
        """
        return self._store.get_team_first_question_candidates(run_id)

    def delete_session(self, session_id: str) -> bool:
        return self._store.delete_session(session_id)

    def get_session_count(self) -> int:
        return self._store.get_session_count()

    def update_session_project(self, session_id: str, project: str) -> bool:
        return self._store.update_session_project(session_id, project)

    def archive_sessions_by_project(self, project: str) -> int:
        return self._store.archive_sessions_by_project(project)

    def clear_old_subagent_tasks(self, days: int = 7) -> int:
        return self._store.clear_old_subagent_tasks(days)

    def force_cleanup_project(self, project_name: str) -> bool:
        return self._store.force_cleanup_project(project_name)

    def record_file_operation(self, session_id: str, call_id: str, tool_name: str, file_path: str, backup_path: str) -> bool:
        return self._store.record_file_operation(session_id, call_id, tool_name, file_path, backup_path)

    def get_file_operations_by_call_id(self, session_id: str, call_id: str):
        return self._store.get_file_operations_by_call_id(session_id, call_id)

    def get_all_file_operations(self, session_id: str):
        return self._store.get_all_file_operations(session_id)

    def clear_session_file_operations(self, session_id: str):
        return self._store.clear_session_file_operations(session_id)

    def remove_file_operation(self, session_id: str, call_id: str) -> int:
        return self._store.remove_file_operation(session_id, call_id)

    # ---------- 可选能力：标题 / 计数 / 输入历史（委托 SessionStore 同名方法） ----------

    def update_session_title(self, session_id: str, title: str) -> bool:
        return self._store.update_session_title(session_id, title)

    def get_session_counts(self) -> Dict[str, int]:
        return self._store.get_session_counts()

    def get_input_history(self, limit: int = 50) -> List[Dict[str, Any]]:
        return self._store.get_input_history(limit=limit)

    def add_input_history(self, content: str, attachments: Optional[list] = None) -> bool:
        return self._store.add_input_history(content, attachments)


def register(registry):
    """系统插件注册入口 — 被 runtime_component_loader.scan_roots 调用。

    source 由 loader 的 _RegistryProxy 强制为 "plugin:system"，
    本函数不显式传入，保持与 tools/providers 插件约定一致。
    """
    registry.register(SqliteStorageEngine())
