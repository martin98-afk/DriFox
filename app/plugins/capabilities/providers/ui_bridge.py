# -*- coding: utf-8 -*-
"""ui.control —— 受控 UI 操控（双 surface，跨线程安全）。

背景：UI 操控能力（input.write / ui.notify / window.*）此前全部只开 ui
surface，原因是工具执行在工作线程，直接摸 QWidget 违反「UI 只在主线程动」
铁律。本模块把「操控」收敛成一组**白名单动作**，内部经 Qt 信号把调用投递
到主线程执行并同步等结果，工具面（tool surface 的智能体工具）因此也能安全
操控界面——类似 Playwright 之于浏览器：给的是动作，不是 widget 引用。

线程模型：
- 调用方在主线程（ui surface 的 host 就是 MainWidget）→ 直接执行
- 调用方在工作线程（tool surface 的 ToolExecutor）→ 经 _Dispatcher 单例
  信号投递（Auto→Queued），threading.Event 同步等待，超时 5s 抛 TimeoutError
  （防主线程卡死时把工具线程永久挂住）
- 无 QApplication（headless / 纯测试）→ RuntimeError，调用方按「暂无界面」降级

动作集（刻意小而稳，全部可枚举、可审计；不开任意 widget 注入口）：
- snapshot()                 界面语义快照（当前页签/会话/项目/工作目录/输入框）
- switch_tab(page_id)        切换工作台页签
- run_command(text)          触发已注册命令（FUNCTION 交 handler，替换类返回 replacement）
- notify(title, message)     InfoBar 通知
- set_input(text, append)    写输入框
"""

from __future__ import annotations

import threading
from typing import Any, Callable, Dict, Optional

from loguru import logger

_DISPATCH_TIMEOUT_S = 5.0

_DISPATCHER: Optional["_Dispatcher"] = None


class _Dispatcher:
    """工作线程 → 主线程的同步投递桥。

    Qt 对象必须在主线程创建后 moveToThread(app.thread())：工具面首次调用
    发生在工作线程，直接在调用线程构造会拿到错误线程亲和，信号变直连。
    """

    def __init__(self) -> None:
        from PyQt5.QtCore import QObject, pyqtSignal

        class _SignalHost(QObject):
            _exec = pyqtSignal(object, object)  # (fn, box)

        self._host = _SignalHost()
        self._host._exec.connect(self._run_on_main)  # type: ignore[attr-defined]

    def _run_on_main(self, fn: Callable[[], Any], box: Dict[str, Any]) -> None:
        try:
            box["result"] = fn()
        except Exception as e:  # noqa: BLE001 - 异常原样回传调用线程
            box["error"] = e
        finally:
            box["done"].set()

    def call(self, fn: Callable[[], Any], timeout: float = _DISPATCH_TIMEOUT_S) -> Any:
        from PyQt5.QtCore import QThread
        from PyQt5.QtWidgets import QApplication

        app = QApplication.instance()
        if app is None:
            raise RuntimeError("无 GUI 环境（QApplication 未创建），ui.control 不可用")
        if QThread.currentThread() is app.thread():
            return fn()
        box: Dict[str, Any] = {"done": threading.Event()}
        self._host._exec.emit(fn, box)  # type: ignore[attr-defined]
        if not box["done"].wait(timeout):
            raise TimeoutError(f"ui.control 主线程投递超时（{timeout}s），主线程可能被阻塞")
        if "error" in box:
            raise box["error"]
        return box.get("result")


def _dispatcher() -> _Dispatcher:
    global _DISPATCHER
    if _DISPATCHER is None:
        from PyQt5.QtWidgets import QApplication

        if QApplication.instance() is None:
            raise RuntimeError("无 GUI 环境（QApplication 未创建），ui.control 不可用")
        d = _Dispatcher()
        d._host.moveToThread(QApplication.instance().thread())  # type: ignore[union-attr]
        _DISPATCHER = d
    return _DISPATCHER


def _dispatch(fn: Callable[[], Any]) -> Any:
    """主线程直调，工作线程投递（同一实现服务双 surface）。"""
    return _dispatcher().call(fn)


# ── 窗口/组件解析（全部在主线程执行） ──


def _resolve_widget() -> Any:
    """承载模型配置的 MainWidget（host 进不来，自己解析活跃主窗口）。"""
    try:
        from app.plugins.registries.ui_plugin_registry import UIPluginRegistry

        reg = UIPluginRegistry.get_instance()
        widget = getattr(reg, "_main_widget", None) or next(
            iter(getattr(reg, "_window_main_widgets", {}).values()), None
        )
        return widget
    except Exception:
        return None


def _resolve_tab_window() -> Any:
    try:
        from app.widgets.tab_manager_window import TabManagerWindow

        return TabManagerWindow.get_instance()
    except Exception:
        return None


# ── 动作实现（每个都在主线程执行） ──


def _action_snapshot() -> Dict[str, Any]:
    """界面语义快照：插件拿语义字段，不拿 widget 引用。"""
    widget = _resolve_widget()
    tm = _resolve_tab_window()
    out: Dict[str, Any] = {
        "workbench_visible": None,
        "workbench_tab": None,
        "input_text": None,
        "session_id": None,
        "project": None,
        "workdir": None,
        "has_ui": widget is not None or tm is not None,
    }
    if tm is not None and hasattr(tm, "workbench_panel"):
        panel = getattr(tm, "workbench_panel", None)
        if panel is not None:
            out["workbench_visible"] = bool(panel.is_panel_visible())
            out["workbench_tab"] = panel.current_tab_id()
    if widget is not None:
        area = getattr(widget, "input_area", None)
        if area is not None:
            try:
                out["input_text"] = area.toPlainText()
            except RuntimeError:
                pass
        out["session_id"] = getattr(widget, "_current_session_id", None)
        project = getattr(widget, "_current_project", "") or ""
        out["project"] = project
        workdir = (getattr(widget, "_current_workdir", None) or {}).get(project) or ""
        out["workdir"] = workdir
    return out


def _action_switch_tab(page_id: str) -> bool:
    """切换工作台页签（不可见时自动展开工作台）。"""
    tm = _resolve_tab_window()
    if tm is None:
        return False
    opener = getattr(tm, "open_workbench_tab", None)
    if not callable(opener):
        return False
    return bool(opener(str(page_id)))


def _action_run_command(text: str) -> Optional[Dict[str, Any]]:
    """触发已注册命令。PROMPT/AGENT 类返回 replacement 摘要，不替插件发送。"""
    from app.core.commands.command_manager import CommandManager

    result = CommandManager.get_instance().execute(str(text))
    if result is None:
        return None
    return {
        "type": str(getattr(result, "type", "")),
        "command_name": str(getattr(result, "command_name", "") or ""),
        "replacement": str(getattr(result, "replacement", "") or ""),
        "remainder": str(getattr(result, "remainder", "") or ""),
    }


def _action_notify(title: str, message: str) -> None:
    from qfluentwidgets import InfoBar, InfoBarPosition

    parent = _resolve_tab_window() or _resolve_widget()
    if parent is None:
        raise RuntimeError("无可用窗口，无法弹通知")
    InfoBar.success(
        str(title),
        str(message),
        parent=parent,
        duration=5000,
        position=InfoBarPosition.BOTTOM,
    )


def _action_set_input(text: str, append: bool = False) -> None:
    widget = _resolve_widget()
    area = getattr(widget, "input_area", None) if widget is not None else None
    if area is None:
        raise RuntimeError("无可用输入框（窗口未就绪）")
    if append:
        area.insertPlainText(str(text))
    else:
        area.setPlainText(str(text))
    set_focus = getattr(area, "setFocus", None)
    if callable(set_focus):
        set_focus()


# ── provider ──


def _ui_control(host: Any):
    """ui.control 的 provider：返回 {动作名: 可调用} 白名单字典。

    host 参数按 provider 签名约定保留（ui surface 传 MainWidget，tool surface
    传 ToolExecutor），实现统一走解析 + 线程投递，不依赖 host 类型。
    """
    return {
        "snapshot": lambda: _dispatch(_action_snapshot),
        "switch_tab": lambda page_id: _dispatch(lambda: _action_switch_tab(page_id)),
        "run_command": lambda text: _dispatch(lambda: _action_run_command(text)),
        "notify": lambda title, message: _dispatch(lambda: _action_notify(title, message)),
        "set_input": lambda text, append=False: _dispatch(lambda: _action_set_input(text, append)),
    }
