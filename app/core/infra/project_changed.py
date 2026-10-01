# -*- coding: utf-8 -*-
"""项目 / 工作目录变更的派发助手（UI 插件可选协议 on_project_changed）

设计要点（详见 docs/superpowers/specs/2026-09-12-ui-plugin-project-changed-design.md）：

- 主程序在 ``MainWidget._sync_working_directory`` 收尾处经 ``UIEventBus`` 发布
  ``EV_PROJECT_CHANGED``（payload: project / workdir / window_id）；
- ``WorkbenchPanel``（插件页）与 ``UIPluginRegistry``（浮动卡）两处订阅，各自
  只对**可见**的组件派发，不可见的交给显示路径补刷；
- 「是否项目敏感」由组件是否实现 ``on_project_changed`` 决定（鸭子类型探测，
  不加注册元数据键），未实现的组件零开销跳过；
- 单组件异常隔离：一个插件抛错不影响其余组件与调用方主流程。
"""

from typing import Any

from loguru import logger


def dispatch_project_changed(
    widget: Any,
    *,
    project: str = "",
    workdir: str = "",
    window_id: str = "",
) -> bool:
    """向单个 UI 组件派发项目变更（组件未实现可选协议时跳过）

    Returns:
        True 表示组件实现了协议且调用成功；False 表示跳过或调用失败。
    """
    fn = getattr(widget, "on_project_changed", None)
    if not callable(fn):
        return False
    try:
        fn(project=project, workdir=workdir, window_id=window_id)
        return True
    except RuntimeError:
        return False  # C++ 对象已销毁（窗口/卡片析构竞态）
    except Exception as e:
        logger.warning(f"[ProjectChanged] {type(widget).__name__}.on_project_changed 异常: {e}")
        return False


def is_active_window(window_id: str) -> bool:
    """payload 的 window_id 是否为当前活跃窗口（后台窗口的变更不打扰前台）

    后台窗口自己切项目时，其卡片在变活跃时经 ``show_card()`` 重取 ctx 补刷，
    因此这里直接丢弃即可，避免同一次项目切换触发 N 份不可见刷新。

    无 Tab 管理器（单窗口场景）或无法判定时返回 True（放行）。
    """
    if not window_id:
        return True
    try:
        from app.widgets.tab_manager_window import TabManagerWindow

        tm = TabManagerWindow.get_instance()
        if tm is None:
            return True
        win = tm.get_current_window()
        if win is None:
            return True
        active_id = getattr(win, "_window_id", None)
        return active_id is None or active_id == window_id
    except Exception:
        return True
