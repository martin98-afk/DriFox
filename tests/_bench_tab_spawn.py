# -*- coding: utf-8 -*-
"""临时基准：新建对话标签页 / 加载历史会话 各段耗时（不入库）

跑法：.venv/Scripts/python.exe tests/_bench_tab_spawn.py [--profile]

构建链与 main.py 一致（同 tests/ui/conftest.py 的 ui_app fixture）：
Settings → FakePage → TabManagerWindow → OpenAIChatToolWindow → add_window

测量口径：
  1. 首个窗口冷构造（应用启动成本，不含在「新建 tab」里）
  2. spawn_tab(new_session=True) 同步段 = OpenAIChatToolWindow.__init__ + _build_new_window 剩余 + add_window
  3. pump 事件循环 → showEvent 注册的 deferred 任务（create_new_session）执行完
  4. spawn_tab(session_record=...) 同步段 + 加载历史会话（_load_session_from_record）
"""

import cProfile
import io
import os
import pstats
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ["DRIFOX_DATA_DIR"] = tempfile.mkdtemp(prefix="drifox_bench_tab_")

# 渲染前置链（与 main.py 同源）：必须在 QApplication/QWebEngine 初始化前
from app.utils.render_env import apply_render_env, default_config_path  # noqa: E402

_RENDER_SETTINGS = apply_render_env(default_config_path())

from PyQt5.QtCore import QCoreApplication, Qt  # noqa: E402
from PyQt5.QtWidgets import QApplication, QWidget  # noqa: E402
from PyQt5.QtWebEngineWidgets import QWebEngineView  # noqa: F401,E402

PROFILE = "--profile" in sys.argv
PROFILE_LOAD = "--profile-load" in sys.argv
RECORD: dict = {}


def _mem_mb():
    """当前进程 Private Bytes（MB）"""
    try:
        import ctypes
        from ctypes import wintypes

        class _PMC(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
                ("PrivateUsage", ctypes.c_size_t),
            ]

        counters = _PMC()
        counters.cb = ctypes.sizeof(_PMC)
        ctypes.windll.psapi.GetProcessMemoryInfo(
            ctypes.windll.kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb
        )
        return counters.PrivateUsage / (1024 * 1024)
    except Exception:  # noqa: BLE001
        return float("nan")


def pump(app, ms=200):
    """泵事件循环 ms 毫秒（processorEvents + 小睡，模拟真实事件循环）"""
    end = time.perf_counter() + ms / 1000.0
    while time.perf_counter() < end:
        app.processEvents()
        time.sleep(0.003)
    app.processEvents()


def make_app():
    if _RENDER_SETTINGS.get("use_open_gles", True):
        QCoreApplication.setAttribute(Qt.AA_UseOpenGLES)
    if _RENDER_SETTINGS.get("share_gl_contexts", True):
        QCoreApplication.setAttribute(Qt.AA_ShareOpenGLContexts)
    app = QApplication.instance() or QApplication([])
    from app.utils.config import Settings
    from app.core.infra.webengine_profile import init_shared_web_profile
    from qfluentwidgets import setFontFamilies, setTheme, Theme

    init_shared_web_profile(app)
    settings = Settings.get_instance()
    try:
        setTheme(Theme.LIGHT if settings.theme_mode.value == "light" else Theme.DARK)
    except Exception:  # noqa: BLE001
        setTheme(Theme.DARK)
    setFontFamilies([settings.llm_font_family.value])
    return app, settings


class _FakePage(QWidget):
    """MainWidget 的最小替身（tests/ui/conftest.py 同款）"""

    def __init__(self, settings):
        super().__init__()
        self.cfg = settings

        from qfluentwidgets import setFontFamilies

        setFontFamilies([self.cfg.llm_font_family.value])

    def isActiveWindow(self):
        return True

    @property
    def workflow_name(self):
        return "bench"

    @property
    def global_variables_changed(self):
        class _FakeSignal:
            def connect(self, *args, **kwargs):
                pass

            def disconnect(self, *args, **kwargs):
                pass

        return _FakeSignal()

    def setUpdatesEnabled(self, enabled):
        pass

    def update(self):
        pass

    def show_splitter(self):
        pass

    def hide_splitter(self):
        pass


def install_timers():
    """包裹关键实现做分段计时（不改变行为，环绕原实现调用）"""
    import app.main_widget as MW
    from app.widgets.tab_manager_window import TabManagerWindow

    orig_ctor = MW.OpenAIChatToolWindow.__init__

    def timed_ctor(self, *a, **kw):
        t0 = time.perf_counter()
        try:
            return orig_ctor(self, *a, **kw)
        finally:
            RECORD.setdefault("ctor", []).append((time.perf_counter() - t0) * 1000)

    MW.OpenAIChatToolWindow.__init__ = timed_ctor

    orig_build = TabManagerWindow._build_new_window

    def timed_build(self, *a, **kw):
        t0 = time.perf_counter()
        try:
            return orig_build(self, *a, **kw)
        finally:
            RECORD.setdefault("build_new_window", []).append((time.perf_counter() - t0) * 1000)

    TabManagerWindow._build_new_window = timed_build

    orig_add = TabManagerWindow.add_window

    def timed_add(self, *a, **kw):
        t0 = time.perf_counter()
        try:
            return orig_add(self, *a, **kw)
        finally:
            RECORD.setdefault("add_window", []).append((time.perf_counter() - t0) * 1000)

    TabManagerWindow.add_window = timed_add

    orig_new = MW.OpenAIChatToolWindow._create_new_session

    def timed_new(self, *a, **kw):
        t0 = time.perf_counter()
        try:
            return orig_new(self, *a, **kw)
        finally:
            RECORD.setdefault("create_new_session", []).append((time.perf_counter() - t0) * 1000)

    MW.OpenAIChatToolWindow._create_new_session = timed_new

    for _name in (
        "_notify_history_data_changed",
        "_clear_chat_area",
        "_load_agent_list",
        "_invalidate_welcome_card",
        "_sync_dialog_title",
        "_sync_working_directory",
        "_release_inactive_session_messages",
    ):
        _orig = getattr(MW.OpenAIChatToolWindow, _name, None)
        if _orig is None:
            continue

        def _make(orig, key):
            def _timed(self, *a, **kw):
                t0 = time.perf_counter()
                try:
                    return orig(self, *a, **kw)
                finally:
                    RECORD.setdefault(key, []).append((time.perf_counter() - t0) * 1000)

            return _timed

        setattr(MW.OpenAIChatToolWindow, _name, _make(_orig, _name.lstrip("_")))

    orig_load = MW.OpenAIChatToolWindow._load_session_from_record

    def timed_load(self, *a, **kw):
        t0 = time.perf_counter()
        try:
            return orig_load(self, *a, **kw)
        finally:
            RECORD.setdefault("load_session_from_record", []).append((time.perf_counter() - t0) * 1000)

    MW.OpenAIChatToolWindow._load_session_from_record = timed_load

    orig_disp = MW.OpenAIChatToolWindow._display_current_session

    def timed_disp(self, *a, **kw):
        t0 = time.perf_counter()
        try:
            return orig_disp(self, *a, **kw)
        finally:
            RECORD.setdefault("display_current_session", []).append((time.perf_counter() - t0) * 1000)

    MW.OpenAIChatToolWindow._display_current_session = timed_disp

    orig_welcome = MW.OpenAIChatToolWindow._schedule_initial_welcome
    def timed_welcome(self, *a, **kw):
        t0 = time.perf_counter()
        try:
            return orig_welcome(self, *a, **kw)
        finally:
            RECORD.setdefault("schedule_initial_welcome", []).append((time.perf_counter() - t0) * 1000)

    MW.OpenAIChatToolWindow._schedule_initial_welcome = timed_welcome

    orig_ready = MW.OpenAIChatToolWindow._maybe_build_deferred_content

    def timed_ready(self, *a, **kw):
        t0 = time.perf_counter()
        try:
            return orig_ready(self, *a, **kw)
        finally:
            RECORD.setdefault("maybe_build_deferred_content", []).append((time.perf_counter() - t0) * 1000)

    MW.OpenAIChatToolWindow._maybe_build_deferred_content = timed_ready

    orig_ui_plugins = MW.OpenAIChatToolWindow._init_ui_plugins_deferred

    def timed_ui_plugins(self, *a, **kw):
        t0 = time.perf_counter()
        try:
            return orig_ui_plugins(self, *a, **kw)
        finally:
            RECORD.setdefault("init_ui_plugins", []).append((time.perf_counter() - t0) * 1000)

    MW.OpenAIChatToolWindow._init_ui_plugins_deferred = timed_ui_plugins

    import app.widgets.ui_composition as uic

    orig_compose = uic.compose

    def timed_compose(*a, **kw):
        t0 = time.perf_counter()
        try:
            return orig_compose(*a, **kw)
        finally:
            ids = kw.get("module_ids") or (a[1] if len(a) > 1 else "?")
            RECORD.setdefault(f"compose[{'+'.join(ids) if isinstance(ids, list) else ids}]", []).append(
                (time.perf_counter() - t0) * 1000
            )

    uic.compose = timed_compose

    # 注意：build() 内是「函数体 import」，运行时取模块属性 → patch 源模块生效
    import app.widgets.simple_hover_tooltip as sht

    orig_batch = sht.batch_install_hover_tooltips

    def timed_batch(*a, **kw):
        t0 = time.perf_counter()
        try:
            return orig_batch(*a, **kw)
        finally:
            RECORD.setdefault("batch_hover_tooltips", []).append((time.perf_counter() - t0) * 1000)

    sht.batch_install_hover_tooltips = timed_batch


def report(tag):
    print(f"\n=== {tag} ===")
    for k, v in RECORD.items():
        vals = ", ".join(f"{x:.1f}" for x in v)
        print(f"  {k:32s} n={len(v)}  [{vals}] ms")


def main():
    app, settings = make_app()
    install_timers()

    from app.widgets.tab_manager_window import TabManagerWindow
    from app.main_widget import OpenAIChatToolWindow
    from tools.ui_driver import place_offscreen

    t0 = time.perf_counter()
    tm = TabManagerWindow.create_instance()
    print(f"TabManagerWindow.create_instance: {(time.perf_counter() - t0) * 1000:.0f}ms")

    page = _FakePage(settings)
    t0 = time.perf_counter()
    w0 = OpenAIChatToolWindow(page)
    print(f"首个窗口冷构造（含 setup_ui）: {(time.perf_counter() - t0) * 1000:.0f}ms")

    t0 = time.perf_counter()
    tm.add_window(w0)
    print(f"首个窗口 add_window: {(time.perf_counter() - t0) * 1000:.0f}ms")
    place_offscreen(tm)
    pump(app, 3000)  # 让首个窗口 deferred 队列跑完（create_new_session / load_model_configs 等）
    RECORD.clear()

    # ── 场景 1：新建对话标签页（热态，源窗口已存在）──
    for i in range(5):
        RECORD.clear()
        _mem0 = _mem_mb()
        t0 = time.perf_counter()
        new = tm.spawn_tab(w0, new_session=True)
        t_sync = (time.perf_counter() - t0) * 1000
        t1 = time.perf_counter()
        app.processEvents()
        t_pump1 = (time.perf_counter() - t1) * 1000
        # 首帧拆解：连续 processEvents，逐次计时（定位长尾在哪个事件批次）
        _frames = []
        for _ in range(12):
            _t = time.perf_counter()
            app.processEvents()
            _frames.append((time.perf_counter() - _t) * 1000)
        print("  首帧逐次:" + ", ".join(f"{v:.0f}" for v in _frames) + " ms")
        t2 = time.perf_counter()
        for _ in range(120):
            app.processEvents()
        t_pump = (time.perf_counter() - t2) * 1000
        print(f"\n[新建 tab #{i + 1}] spawn_tab 同步段={t_sync:.1f}ms 一次 processEvents={t_pump1:.1f}ms"
              f" pump600ms={t_pump:.1f}ms  内存增量={_mem_mb() - _mem0:.1f}MB")
        report(f"新建 tab #{i + 1} 分段")
        # 清理：关掉这个新窗口，回到单窗口状态
        try:
            idx = tm._window_to_index.get(id(new), -1)
            if idx >= 0:
                tm._close_window_at(idx)
        except Exception as e:  # noqa: BLE001
            print(f"  [warn] 关闭新窗口失败: {e}")
        pump(app, 300)

    # ── 场景 2：加载历史会话（新标签页打开一个有消息的历史会话）──
    sm = w0.session_manager
    session = sm.get_current_session()
    print(f"\n会话存储: {type(w0.session_store).__name__ if getattr(w0, 'session_store', None) else 'None'}")
    if session is None:
        print("  [warn] 无当前会话，跳过历史加载场景")
    else:
        # 造 100 条消息（50 轮）并落库
        for i in range(50):
            session.messages.append({"role": "user", "content": f"问题 {i}：请说明这个模块的作用。" * 2})
            session.messages.append(
                {"role": "assistant", "content": f"回答 {i}：" + "这是用于撑起批次与渲染的填充内容。" * 12}
            )
        sid = session.session_id
        t0 = time.perf_counter()
        try:
            data = session.to_dict()
            data["session_id"] = sid
            w0.session_store.save_session(data)
        except Exception as e:  # noqa: BLE001
            print(f"  [warn] 保存失败: {e}")
        print(f"落库 100 条消息: {(time.perf_counter() - t0) * 1000:.0f}ms")

        record = None
        try:
            record = w0.history_manager.get_session_by_session_id(sid)
            if record is None:
                recs = w0.session_store.get_sessions(limit=5) or []
                for r in recs:
                    if str(r.get("session_id", "")) == str(sid):
                        record = r
                        break
        except Exception as e:  # noqa: BLE001
            print(f"  [warn] 取 history record 失败: {e}")
        print(f"history record: {'有' if record else '无'} sid={sid}")

        for i in range(2):
            RECORD.clear()
            _pr = None
            if PROFILE_LOAD and i == 0:
                _pr = cProfile.Profile()
                _pr.enable()
            t0 = time.perf_counter()
            new = tm.spawn_tab(w0, session_record=record)
            t_sync = (time.perf_counter() - t0) * 1000
            t1 = time.perf_counter()
            pump(app, 3000)
            t_load = (time.perf_counter() - t1) * 1000
            print(f"\n[加载历史 tab #{i + 1}] spawn_tab 同步段={t_sync:.1f}ms 后续 pump={t_load:.1f}ms")
            report(f"加载历史 tab #{i + 1} 分段")
            if _pr is not None:
                _pr.disable()
                _sio = io.StringIO()
                pstats.Stats(_pr, stream=_sio).sort_stats("cumulative").print_stats(40)
                print("\n=== cProfile（加载历史会话全链）===")
                print(_sio.getvalue())
            try:
                idx = tm._window_to_index.get(id(new), -1)
                if idx >= 0:
                    tm._close_window_at(idx)
            except Exception as e:  # noqa: BLE001
                print(f"  [warn] 关闭失败: {e}")
            pump(app, 300)

    # ── 可选：cProfile 采一次新建 tab ──
    if PROFILE:
        RECORD.clear()
        pr = cProfile.Profile()
        pr.enable()
        new = tm.spawn_tab(w0, new_session=True)
        pr.disable()
        sio = io.StringIO()
        ps = pstats.Stats(pr, stream=sio).sort_stats("cumulative")
        ps.print_stats(45)
        print("\n=== cProfile（新建 tab 同步段 + 一次 processEvents）===")
        print(sio.getvalue())

    try:
        tm.cleanup()
    except Exception:  # noqa: BLE001
        pass


if __name__ == "__main__":
    main()
