# -*- coding: utf-8 -*-
"""历史会话 / 项目 / 团队会话面能力 provider（host = MainWidget）。

三条能力为「工作台历史页」类插件建模，把原先插件对宿主私有方法的散装依赖
收敛成语义动作：

- ``session.history`` / ``session.history.write``：持久化历史会话数据面。
  数据源是 ``HistoryManager`` 全局单例（不是窗口内存 ``session_manager``，
  两者语义不同：前者是跨会话落库记录，后者是当前窗口的活跃会话对象）。
- ``project.read`` / ``project.write``：项目面。写操作内部承担宿主的一整套
  联动（工作目录同步 + 团队广播 + Tab 图标 + 通知），插件从此不知道这些字段。
- ``team.session``：团队历史会话的恢复 / 归档 / 成员进入，副作用跨窗口
  （spawn_tab），故单独立能力而不是塞进 ``session.history.write``。

设计约束（与 ui_core 一致）：
- provider 是纯函数，不持有窗口引用；host 由 build_pool 传入
- 写能力不在 provider 里做二次确认弹窗，确认是 UI 层的事（插件自己弹）
"""

from __future__ import annotations

from typing import Any

from loguru import logger


def _history():
    """HistoryManager 单例（延迟 import，测试 monkeypatch 时自动跟随）。"""
    from app.utils.history_manager import HistoryManager

    return HistoryManager.get_instance()


def _notify(host: Any) -> None:
    """数据变更后通知宿主联动（历史页刷新 + 欢迎卡片 + 跨窗口广播）。

    这是写操作的固有副作用，不作为独立能力暴露：允许插件「改数据但不通知」
    只会制造跨窗口脏视图。
    """
    fn = getattr(host, "_notify_history_data_changed", None)
    if callable(fn):
        try:
            fn()
        except Exception as e:
            logger.warning(f"[Capability] history 数据变更通知失败: {e}")


# ──────────────────── 历史会话（读） ────────────────────


def _session_history(host: Any):
    """历史会话只读面。

    返回 dict of 闭包，字段取值走 HistoryManager 的公开方法（白名单即边界）。
    """

    def list_sessions(project: str = None, merge_team: bool = False) -> list:
        return list(_history().get_history_list(project, merge_team=merge_team) or [])

    def list_archived() -> list:
        return list(_history().get_archived_sessions() or [])

    def list_projects() -> list:
        return list(_history().get_project_list() or [])

    def find_index(session_id: str) -> Any:
        return _history().find_index_by_session_id(session_id)

    def get_session(session_id: str) -> Any:
        """按 session_id 取完整会话记录（含 messages）。"""
        return _history().get_session_by_session_id(session_id)

    def get_messages(session_id: str) -> Any:
        return _history().get_session_messages(session_id)

    def team_sessions(run_id: str) -> list:
        return list(_history().get_team_sessions_by_run_id(run_id) or [])

    return {
        "list": list_sessions,
        "archived": list_archived,
        "projects": list_projects,
        "find_index": find_index,
        "session": get_session,
        "messages": get_messages,
        "team_sessions": team_sessions,
    }


# ──────────────────── 历史会话（写） ────────────────────


def _session_history_write(host: Any):
    """历史会话写面（破坏性：置顶 / 移动 / 归档 / 恢复 / 删除 / 导入）。

    每个动作结束都触发一次宿主联动通知（``_notify``），插件不必自己再调。
    返回值统一 bool 或导入结果 dict / None，与 HistoryManager 契约一致。
    """

    def set_pinned(session_id: str, pinned: bool) -> bool:
        ok = bool(_history().set_session_pinned(session_id, bool(pinned)))
        if ok:
            _notify(host)
        return ok

    def move_to_project(session_id: str, project: str) -> bool:
        """按 session_id 移动会话到项目（内部做 index 解析，调用方不必自己查）。"""
        if not session_id or not project:
            return False
        hm = _history()
        idx = hm.find_index_by_session_id(session_id)
        if idx is None:
            return False
        ok = bool(hm.move_to_project(idx, project))
        if ok:
            _notify(host)
        return ok

    def archive(session_id: str) -> bool:
        """归档单个会话（当前会话归档时由宿主承担新会话切换）。"""
        if not session_id:
            return False
        fn = getattr(host, "_archive_history_session", None)
        if not callable(fn):
            return False
        hm = _history()
        idx = hm.find_index_by_session_id(session_id)
        if idx is None:
            return False
        try:
            fn(idx)
        except Exception as e:
            logger.warning(f"[Capability] 归档会话失败 session={session_id}: {e}")
            return False
        return True

    def restore(file_path: str) -> bool:
        """从归档文件恢复会话到历史（宿主负责删归档文件 + 提示 + 通知）。"""
        fn = getattr(host, "_on_archived_session_restored", None)
        if not callable(fn) or not file_path:
            return False
        try:
            fn(file_path)
        except Exception as e:
            logger.warning(f"[Capability] 恢复归档会话失败: {e}")
            return False
        return True

    def delete_archived(file_path: str) -> bool:
        """彻底删除归档会话（宿主内含确认对话框）。"""
        fn = getattr(host, "_on_archived_session_deleted", None)
        if not callable(fn) or not file_path:
            return False
        try:
            fn(file_path)
        except Exception as e:
            logger.warning(f"[Capability] 删除归档会话失败: {e}")
            return False
        return True

    def import_file(file_path: str) -> Any:
        """导入会话 JSON 文件（宿主负责提示 + 通知）。"""
        fn = getattr(host, "_on_session_imported", None)
        if not callable(fn) or not file_path:
            return None
        try:
            fn({"file_path": file_path})
        except Exception as e:
            logger.warning(f"[Capability] 导入会话失败: {e}")
            return None
        return True

    return {
        "set_pinned": set_pinned,
        "move_to_project": move_to_project,
        "archive": archive,
        "restore": restore,
        "delete_archived": delete_archived,
        "import": import_file,
    }


# ──────────────────── 会话切换（session.manage 补实现） ────────────────────


def _session_manage(host: Any):
    """切换 / 新建当前会话。

    - ``select(session_id)``：加载指定历史会话到当前窗口（含流式保护开新标签页）
    - ``new()``：新建空会话（切项目后的「真切换」语义由 ``project.write.switch``
      承担，这里只做纯新建）
    """

    def select(session_id: str) -> bool:
        if not session_id:
            return False
        record = _history().get_session_by_session_id(session_id)
        if record is None:
            return False
        # 流式保护：当前标签页正在输出时开新标签页加载该会话，不打断对话
        # （对齐 MainWidget._on_history_session_selected 的既有语义；插件历史页
        # 直接调 loader 会在 _load_session_from_record 里把当前对话停掉）。
        if getattr(host, "_is_streaming", False):
            try:
                from app.widgets.tab_manager_window import TabManagerWindow

                tm = TabManagerWindow.get_instance()
                if tm is not None and tm.spawn_tab(host, session_record=record) is not None:
                    return True
            except Exception as e:
                logger.warning(f"[Capability] 流式保护开新标签页失败: {e}")
        loader = getattr(host, "_load_session_from_record", None)
        if not callable(loader):
            return False
        try:
            loader(record)
        except Exception as e:
            logger.warning(f"[Capability] 加载会话失败 session={session_id}: {e}")
            return False
        return True

    def new() -> bool:
        fn = getattr(host, "_create_new_session", None)
        if not callable(fn):
            return False
        try:
            fn()
        except Exception as e:
            logger.warning(f"[Capability] 新建会话失败: {e}")
            return False
        return True

    return {"select": select, "new": new}


# ──────────────────── 项目（读 / 写） ────────────────────


def _project_read(host: Any):
    """项目只读面：当前项目 + 项目清单 + 元数据映射。"""

    def current() -> str:
        return str(getattr(host, "_current_project", "") or "")

    def list_projects() -> list:
        return list(_history().get_project_list() or [])

    def meta(projects=None) -> dict:
        names = list(projects) if projects else list_projects()
        builder = getattr(host, "_build_project_meta_map", None)
        if not callable(builder):
            return {}
        try:
            return dict(builder(names) or {})
        except Exception as e:
            logger.warning(f"[Capability] 项目元数据构建失败: {e}")
            return {}

    def root_dirs(projects=None) -> dict:
        names = list(projects) if projects else list_projects()
        builder = getattr(host, "_build_project_root_dir_map", None)
        if not callable(builder):
            return {}
        try:
            return dict(builder(names) or {})
        except Exception as e:
            logger.warning(f"[Capability] 项目根目录构建失败: {e}")
            return {}

    return {
        "current": current,
        "list": list_projects,
        "meta": meta,
        "root_dirs": root_dirs,
    }


def _project_write(host: Any):
    """项目写面。

    每个动作都是宿主的完整语义链（切项目含工作目录 + 团队广播 + Tab 图标；
    新建含根目录绑定），插件不需要知道其中任何一步。
    """

    def _call(name: str, *args) -> bool:
        fn = getattr(host, name, None)
        if not callable(fn):
            return False
        try:
            fn(*args)
        except Exception as e:
            logger.warning(f"[Capability] 项目操作 {name} 失败: {e}")
            return False
        return True

    def switch(project: str) -> bool:
        """切换当前项目（含新建会话，故标 high）。"""
        if not project:
            return False
        return _call("_on_project_selected", project)

    def create(project: str, root_dir: str = "") -> bool:
        """新建项目（root_dir 非空时绑定该目录为工作目录）。"""
        if not project:
            return False
        return _call("_on_new_project_created", project, root_dir)

    def submit(name: str) -> bool:
        """新建或切换（命中已有项目则切换，否则新建）——工具条输入框语义。"""
        if not name:
            return False
        return _call("_on_header_new_project", name)

    def archive(project: str) -> bool:
        if not project:
            return False
        return _call("_on_archive_project", project)

    def export_archive(project: str) -> bool:
        if not project:
            return False
        return _call("_on_export_project", project)

    def import_archive() -> bool:
        return _call("_on_import_project")

    def open_folder(project: str, root_dir: str = "") -> bool:
        if not project:
            return False
        return _call("_on_open_project_folder", project, root_dir)

    def drop_file(file_path: str) -> bool:
        if not file_path:
            return False
        return _call("_on_project_file_dropped", file_path)

    def drop_folder(folder_path: str) -> bool:
        if not folder_path:
            return False
        return _call("_on_project_folder_dropped", folder_path)

    def pick_folder() -> bool:
        return _call("_on_project_open_folder_btn")

    return {
        "switch": switch,
        "create": create,
        "submit": submit,
        "archive": archive,
        "export_archive": export_archive,
        "import_archive": import_archive,
        "open_folder": open_folder,
        "drop_file": drop_file,
        "drop_folder": drop_folder,
        "pick_folder": pick_folder,
    }


# ──────────────────── 团队历史会话 ────────────────────


def _team_session(host: Any):
    """团队历史会话动作（副作用跨窗口：spawn_tab / 批量归档）。"""

    def restore(run_id: str) -> bool:
        fn = getattr(host, "_on_team_restore_requested", None)
        if not callable(fn) or not run_id:
            return False
        try:
            fn(run_id)
        except Exception as e:
            logger.warning(f"[Capability] 团队会话恢复失败 run={run_id}: {e}")
            return False
        return True

    def archive(run_id: str) -> bool:
        fn = getattr(host, "_on_team_archive_requested", None)
        if not callable(fn) or not run_id:
            return False
        try:
            fn(run_id)
        except Exception as e:
            logger.warning(f"[Capability] 团队会话归档失败 run={run_id}: {e}")
            return False
        return True

    def enter_member(member_record: dict) -> bool:
        fn = getattr(host, "_on_team_member_selected", None)
        if not callable(fn) or not isinstance(member_record, dict):
            return False
        try:
            fn(member_record)
        except Exception as e:
            logger.warning(f"[Capability] 进入团队成员会话失败: {e}")
            return False
        return True

    return {"restore": restore, "archive": archive, "enter_member": enter_member}


# ──────────────────── 注册 ────────────────────


def register(registry) -> None:
    """把本模块的能力注册到 ui surface。"""
    registry.register("session.history", _session_history, surface="ui", owner="core:history")
    registry.register("session.history.write", _session_history_write, surface="ui", owner="core:history")
    registry.register("session.manage", _session_manage, surface="ui", owner="core:history")
    registry.register("project.read", _project_read, surface="ui", owner="core:history")
    registry.register("project.write", _project_write, surface="ui", owner="core:history")
    registry.register("team.session", _team_session, surface="ui", owner="core:history")
