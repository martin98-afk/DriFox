# -*- coding: utf-8 -*-
"""隔离实例复现：完整启动链 + 自动关 tab

复刻 main() Tab 模式启动段（create_instance → ensure_started → preheat →
首窗 add_window），8 秒后自动 _close_window_at(0)，抓 no-tb 异常栈。

跑法：python tests/debug/repro_close_tab_full_boot.py
"""

import os
import sys
import tempfile
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
os.environ["DRIFOX_DATA_DIR"] = tempfile.mkdtemp(prefix="drifox_repro_boot_")

from app.utils.render_env import apply_render_env, default_config_path  # noqa: E402

apply_render_env(default_config_path())

from PySide6.QtCore import QCoreApplication, QEventLoop, QTimer, Qt  # noqa: E402
from PySide6.QtWidgets import QApplication, QWidget  # noqa: E402

if True:
    QCoreApplication.setAttribute(Qt.AA_UseOpenGLES)
    QCoreApplication.setAttribute(Qt.AA_ShareOpenGLContexts)

app = QApplication.instance() or QApplication([])

from app.core.infra.webengine_profile import init_shared_web_profile  # noqa: E402

init_shared_web_profile(app)

from app.utils.config import Settings  # noqa: E402

# ── 异常捕获：no-tb 时 dump 主线程当前栈（与 main.py 插桩同构）──
_caught = []


def _hook(etype, value, tb):
    stack = "".join(traceback.format_exception(etype, value, tb))
    if stack not in [c[0] for c in _caught]:
        tb2 = tb or getattr(value, "__traceback__", None)
        extra = ""
        if tb2 is None:
            import threading

            frame = sys._current_frames().get(threading.main_thread().ident)
            if frame is not None:
                extra = "\n[no-tb] 主线程当前栈:\n" + "".join(traceback.format_stack(frame))
        _caught.append((stack + extra, tb2 is None))
        print("\n=== UNHANDLED EXCEPTION ===", flush=True)
        print(stack, flush=True)
        if extra:
            print(extra, flush=True)
        print(f"[CRASH-NEIGHBORHOOD] 异常时刻销毁记录 {len(_destroy_log)} 条，尾部 15:", flush=True)
        for entry in _destroy_log[-15:]:
            print("  ", entry, flush=True)


sys.excepthook = _hook

_destroy_log = []  # 模块级：hook 与 main 共享

# ── connect 追踪：找出「slot 是 native Qt signal」的连接 ──
# pyqtSignal 的 bound 信号有 emit；Qt 内建（native）bound signal 没有 emit。
# native signal 被当 slot connect 后，C++ 事件循环直接调用它 → TypeError(tb=None)。
from PySide6.QtCore import pyqtBoundSignal  # noqa: E402

_conn_log = []
_orig_connect = pyqtBoundSignal.connect


def _traced_connect(self, slot, *a, **k):
    result = _orig_connect(self, slot, *a, **k)
    try:
        if isinstance(slot, pyqtBoundSignal):
            _conn_log.append((repr(self), repr(slot)))
            print(f"[CONNECT-TRACE] signal->signal: {repr(self)} -> {repr(slot)}", flush=True)
    except Exception:
        pass
    return result


pyqtBoundSignal.connect = _traced_connect


class FakePage(QWidget):
    def __init__(self):
        super().__init__()
        self.cfg = Settings.get_instance()

    def isActiveWindow(self):
        return True

    @property
    def workflow_name(self):
        return "standalone_llm_chatter"

    @property
    def global_variables_changed(self):
        class FakeSignal:
            def connect(self, *args, **kwargs):
                pass

            def disconnect(self, *args, **kwargs):
                pass

        return FakeSignal()

    def setUpdatesEnabled(self, enabled):
        pass

    def update(self):
        pass

    def show_splitter(self):
        pass

    def hide_splitter(self):
        pass


def pump(ms=200):
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


def main():
    settings = Settings.get_instance()
    tm_cls_mod = __import__("app.widgets.tab_manager_window", fromlist=["TabManagerWindow"])
    tm = tm_cls_mod.TabManagerWindow.create_instance()
    tm.show()
    tm.show_boot_placeholder()
    pump(300)

    from app.core.services.gateway_service import GatewayService
    from app.core.services.plugin_host_service import PluginHostService

    GatewayService.get_instance().ensure_started()
    PluginHostService.get_instance().ensure_started()

    from app.utils.preheat import preheat_process_level

    preheat_process_level()
    pump(300)

    from app.main_widget import OpenAIChatToolWindow

    chat_window = OpenAIChatToolWindow(FakePage())
    tm.add_window(chat_window)
    tm.remove_boot_placeholder()
    tm._mark_first_window_ready()
    pump(1500)
    print(f"=== 启动完成，窗口数: {tm.window_count}，8 秒后自动关 tab ===", flush=True)

    # 8 秒后自动关 tab（模拟用户点击关闭按钮）
    def _do_close():
        print("=== 关闭 tab ===", flush=True)
        try:
            tm._close_window_at(0)
            print("_close_window_at 同步段无异常", flush=True)
        except Exception:
            traceback.print_exc()

    QTimer.singleShot(8000, _do_close)

    # ── 析构探针：记录窗口树每个 QObject 的销毁顺序，CRASH 时看最后销毁的邻域 ──
    from PySide6.QtCore import QObject
    import shiboken6 as _sip_mod

    global _destroy_log

    def _make_probe(name, addr):
        def _probe(*_a):
            _destroy_log.append(f"{name}@{addr}")
        return _probe

    def _install_probes():
        n = 0
        for root in (tm, chat_window):
            objs = [root] + list(root.findChildren(QObject))
            for child in objs:
                try:
                    if not _sip_mod.isValid(child):
                        continue
                    mo = child.metaObject()
                    cls = mo.className() if mo else "?"
                    child.destroyed.connect(_make_probe(cls, hex(id(child))))
                    n += 1
                except Exception:
                    continue
        print(f"[PROBE] 已装析构探针 {n} 个", flush=True)

    QTimer.singleShot(7900, _install_probes)

    def _scan_native_signal_receivers():
        """扫描窗口树：信号上有 receivers 的连接（signal-to-signal 或槽）。"""
        from PySide6.QtCore import QObject, QMetaMethod

        roots = [tm, chat_window]
        seen = set()
        found = []
        qobj_roots = [r for r in roots if isinstance(r, QObject)]
        all_objs = []
        for root in qobj_roots:
            all_objs.append(root)
            all_objs.extend(root.findChildren(QObject))
        for obj in all_objs:
            oid = id(obj)
            if oid in seen:
                continue
            seen.add(oid)
            try:
                if not sip.isValid(obj):
                    continue
            except Exception:
                continue
            mo = obj.metaObject()
            if mo is None:
                continue
            cls_name = mo.className()
            for i in range(mo.methodCount()):
                m = mo.method(i)
                if m.methodType() != QMetaMethod.Signal:
                    continue
                sig_name = bytes(m.name()).decode()
                try:
                    bound = getattr(obj, sig_name)
                    n = bound.receivers()
                except Exception:
                    continue
                if n and n > 0:
                    found.append(f"{cls_name}#{hex(id(obj))} sig={sig_name} receivers={n}")
        print(f"[SCAN] 对象数={len(all_objs)} 信号带receivers={len(found)}", flush=True)
        for f in found[:80]:
            print("  ", f, flush=True)

    QTimer.singleShot(7900, _scan_native_signal_receivers)
    # 关 tab 后泵 12 秒覆盖 destroyed 链 + deferred 批次 + 心跳
    QTimer.singleShot(8500, lambda: None)
    for _ in range(12):
        pump(1000)

    print("\n=== 结果 ===", flush=True)
    if not _caught:
        print("未捕获到异常（复现失败）", flush=True)
    for stack, no_tb in _caught:
        print(f"[no_tb={no_tb}]", flush=True)
    print(f"[PROBE] 销毁记录 {len(_destroy_log)} 条，尾部 30:", flush=True)
    for entry in _destroy_log[-30:]:
        print("  ", entry, flush=True)
    tm.close()
    pump(500)
    os._exit(0)


if __name__ == "__main__":
    main()
