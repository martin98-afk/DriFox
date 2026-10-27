# -*- coding: utf-8 -*-
"""ui.control —— 受控 UI 操控（双 surface，跨线程安全）。

**当前状态：已从 catalog 与双 surface 注册面摘除（能力不可用），实现留档。**
保留原因：fire-and-forget 投递机制与离线测试全部验证通过；真实 DriFox 环境
中动作提交后在主线程未执行且无失败日志（根因未定位，疑似与工具并行子
QThread 环境下的信号投递有关）。重新启用步骤：catalog 加回 ui.control spec、
ui_core/tool_core 各加一行注册、恢复本模块 docstring 描述。

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

    def call(self, fn: Callable[[], Any], timeout: float = _DISPATCH_TIMEOUT_S, wait: bool = True) -> Any:
        from PyQt5.QtCore import QThread
        from PyQt5.QtWidgets import QApplication

        app = QApplication.instance()
        if app is None:
            raise RuntimeError("无 GUI 环境（QApplication 未创建），ui.control 不可用")
        if QThread.currentThread() is app.thread():
            return fn()
        box: Dict[str, Any] = {"done": threading.Event()}
        self._host._exec.emit(fn, box)  # type: ignore[attr-defined]
        if not wait:
            return None  # fire-and-forget：结果由 _run_on_main 写入 box，无人等待
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


def _dispatch(fn: Callable[[], Any], wait: bool = True, timeout: float = _DISPATCH_TIMEOUT_S) -> Any:
    """主线程直调，工作线程投递（同一实现服务双 surface）。

    wait=False 为 fire-and-forget：投递即返回，动作结果只记日志。
    写操作（切页签/写输入框/通知）必须用它——它们会触发页面刷新等主线程
    重活，可能去拿工具循环正持有的会话/引擎锁；同步等待会与 worker 互相
    卡死（实测：worker Event.wait 等主线程，主线程 fn 抢锁等 worker，
    UI 冻结到超时，模型重试再冻结）。

    timeout 仅在 wait=True 且工作线程调用时生效（主线程直调不受限）；
    需要用户长时间交互的场景（如 process.start 首启确认框）传更大的值。
    """
    return _dispatcher().call(fn, wait=wait, timeout=timeout)


def preheat_main_thread_dispatcher() -> None:
    """在调用线程（应为主线程）预建 _Dispatcher 单例。

    _SignalHost(QObject) 的构造线程决定其初始线程亲和：若单例由插件后台
    线程首次触发 _dispatch 时才建，QObject 会先在工作线程构造再 moveToThread，
    该组合在 PyQt5/CPython 3.14 下破坏 Qt 内部同步状态——实测 process.start
    首启确认框模态循环内堆损坏崩溃（ntdll ACCESS_VIOLATION，VEH 栈钉死
    _ask，magpie 0.5.2/0.5.3 复现）。宿主启动期（capability provider 注册，
    主线程）调用本函数即可消除该窗口；对已建单例幂等。
    """
    _dispatcher()


#: 写操作的统一返回：动作已提交主线程，成败看日志（或用 snapshot 二次确认）
_SUBMITTED: Dict[str, Any] = {"submitted": True}


def _fire(action: str, fn: Callable[[], Any]) -> Dict[str, Any]:
    """投递写动作到主线程（不等待），完成/失败记日志。"""

    def _run_logged() -> None:
        try:
            fn()
            logger.info(f"[ui.control] {action} 已在主线程执行")
        except Exception as e:  # noqa: BLE001 - 异步动作失败只能记日志
            logger.warning(f"[ui.control] {action} 执行失败: {e}")

    _dispatch(_run_logged, wait=False)
    return dict(_SUBMITTED)


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
    """切工作台页签。走轻量路径：面板可见时直接 set_current_tab_by_id（与用户
    手点页签同一条路，只刷新目标页数据）。

    ★ 不走 open_workbench_tab 的完整路径（可见时它每次都 refresh_workbench →
    sync_plugin_pages）：后者的「残缺 context 自愈」在任一插件页 context 缺
    backend 且宿主可提供时会全量销毁重建所有插件页（worktree 跑 git 子进程、
    file-tree 扫盘、history 读库），主线程秒级阻塞 → 「Python 未响应」。
    仅在面板不可见（展开必经一次 reconcile，属正常成本）或页签不存在
    （可能需 reconcile 补挂新页）时退回完整路径一次。
    """
    tm = _resolve_tab_window()
    if tm is None:
        return False
    panel = getattr(tm, "workbench_panel", None)
    if panel is None:
        return False
    page_id = str(page_id)
    if panel.is_panel_visible() and panel.set_current_tab_by_id(page_id, user=True):
        return True
    opener = getattr(tm, "open_workbench_tab", None)
    if not callable(opener):
        return False
    return bool(opener(page_id))


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

    线程语义：
    - snapshot：同步等结果（只读宿主属性，无锁风险）
    - switch_tab / set_input / notify：fire-and-forget，立即返回
      ``{"submitted": True}``；动作在主线程异步完成，成败记主程序日志
      （写动作会触发页面刷新，可能拿 worker 正持有的锁，同步等待会死锁）
    """
    return {
        "snapshot": lambda: _dispatch(_action_snapshot),
        "switch_tab": lambda page_id: _fire("switch_tab", lambda: _action_switch_tab(page_id)),
        "run_command": lambda text: _fire("run_command", lambda: _action_run_command(text)),
        "notify": lambda title, message: _fire("notify", lambda: _action_notify(title, message)),
        "set_input": lambda text, append=False: _fire(
            "set_input", lambda: _action_set_input(text, append)
        ),
    }
