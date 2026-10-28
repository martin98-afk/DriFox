# -*- coding: utf-8 -*-
"""探针：关闭「刚对话完」的对话页卡顿定位（分段计时 + cProfile）

复现用户场景：一个刚完成对话的 tab（会话有消息、卡片已渲染、标题生成任务
可能还在后台跑），点 × 关闭，测量主线程各段耗时。

跑法：uv run python tests/debug/probe_close_tab_jank.py
"""

import os
import tempfile
import time

os.environ["DRIFOX_DATA_DIR"] = tempfile.mkdtemp(prefix="drifox_close_probe_")
print("=== PROBE START ===", flush=True)

from app.utils.render_env import apply_render_env, default_config_path  # noqa: E402

_RS = apply_render_env(default_config_path())

from PySide6.QtCore import QCoreApplication, QEventLoop, QTimer, Qt  # noqa: E402
from PySide6.QtWidgets import QApplication, QWidget  # noqa: E402

if _RS.get("use_open_gles", True):
    QCoreApplication.setAttribute(Qt.AA_UseOpenGLES)
if _RS.get("share_gl_contexts", True):
    QCoreApplication.setAttribute(Qt.AA_ShareOpenGLContexts)

app = QApplication.instance() or QApplication([])

from app.core.infra.webengine_profile import init_shared_web_profile  # noqa: E402

init_shared_web_profile(app)

from app.main_widget import OpenAIChatToolWindow  # noqa: E402
from app.utils.config import Settings  # noqa: E402
from app.widgets.tab_manager_window import TabManagerWindow  # noqa: E402

settings = Settings.get_instance()

OUT = []
_T0 = time.perf_counter()


def log(tag: str, extra: str = "") -> None:
    line = f"[{time.perf_counter() - _T0:8.3f}s] {tag} {extra}".rstrip()
    OUT.append(line)
    print(line, flush=True)


class _FakePage(QWidget):
    def __init__(self):
        super().__init__()
        self.cfg = settings

    def isActiveWindow(self):
        return True

    @property
    def workflow_name(self):
        return "ui_tests"

    @property
    def global_variables_changed(self):
        class _FakeSignal:
            def connect(self, *args, **kwargs):
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


def pump(ms=100):
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec_()


def _wrap_timed(owner, name: str, tag: str):
    """包裹方法：调用耗时 > 10ms 打点"""
    orig = getattr(owner, name)

    def inner(*a, **kw):
        t = time.perf_counter()
        r = orig(*a, **kw)
        dt = (time.perf_counter() - t) * 1000
        if dt > 10:
            log(f"  ⏱ {tag}", f"{dt:.1f}ms")
        return r

    setattr(owner, name, inner)


# ── 构造宿主 ──────────────────────────────────────────────
page = _FakePage()
tm = TabManagerWindow.create_instance()
tm.resize(1400, 900)
tm.show()
pump(400)

log("宿主就绪，开始构造对话窗口")

# ── 窗口 A：空（对照）────────────────────────────────────
wA = OpenAIChatToolWindow(page)
tm.add_window(wA)
pump(200)
log("wA(空会话) 构造+add 完成")

# ── 窗口 B：注入「刚对话完」状态 ─────────────────────────
wB = OpenAIChatToolWindow(page)
tm.add_window(wB)
pump(300)
log("wB 构造+add 完成")

# 注入消息：模拟一段真实对话（含 assistant），走真实渲染路径
try:
    sess = wB.session_manager.get_current_session()
    if sess is not None:
        user_text = "帮我写一个快速排序的 Python 实现，带详细注释"
        assistant_md = (
            "好的，下面是快速排序实现：\n\n"
            "```python\n"
            "def quicksort(arr):\n"
            "    if len(arr) <= 1:\n"
            "        return arr\n"
            "    pivot = arr[len(arr) // 2]\n"
            "    left = [x for x in arr if x < pivot]\n"
            "    mid = [x for x in arr if x == pivot]\n"
            "    right = [x for x in arr if x > pivot]\n"
            "    return quicksort(left) + mid + quicksort(right)\n"
            "```\n\n"
            "这个实现采用了分治策略，平均时间复杂度 O(n log n)。"
        )
        sess.messages.append({"role": "user", "content": user_text, "timestamp": "2026-10-13 17:00:00"})
        wB._append_user_message(content=user_text, timestamp="2026-10-13 17:00:00")
        sess.messages.append(
            {
                "role": "assistant",
                "content": assistant_md,
                "timestamp": "2026-10-13 17:00:05",
                "model_name": "test-model",
                "elapsed": 5.0,
            }
        )
        card = wB._append_assistant_message(timestamp="2026-10-13 17:00:05", model_name="test-model")
        if card is not None and hasattr(card, "update_content"):
            card.update_content(assistant_md)
        wB._session_dirty = True
        log("wB 注入消息 + 渲染卡片完成")
except Exception as e:
    import traceback

    log(f"注入消息失败: {e}")
    traceback.print_exc()

pump(500)

# ── 模拟标题生成任务还在跑（LLM HTTP 往返 3s）────────────
class _SlowTask:
    pass


from PySide6.QtCore import QRunnable, QThreadPool  # noqa: E402


class _SleepTask(QRunnable):
    def __init__(self, seconds):
        super().__init__()
        self.seconds = seconds
        self.setAutoDelete(True)

    def run(self):
        time.sleep(self.seconds)


if "--with-title-task" in os.sys.argv:
    if "--shared-pool" in os.sys.argv:
        # 实验组：模拟「修复后」形态——任务跑在应用级共享池上，
        # 窗口关闭时不再 waitForDone 等待它。
        from PySide6.QtCore import QThreadPool as _QTP  # noqa: E402

        _shared = _QTP()
        _shared.setMaxThreadCount(2)
        _shared.start(_SleepTask(5.0))

        class _DetachedPool:
            """窗口看到的池：start 转发到共享池；waitForDone 立即返回"""

            def waitForDone(self, *a, **kw):
                return True

            def start(self, task):
                _shared.start(task)

        wB._gen_thread_pool = _DetachedPool()
        log("已注入后台长任务到【共享池】+ 窗口池改为不等待（模拟修复后形态）")
    else:
        pool = getattr(wB, "_gen_thread_pool", None)
        if pool is not None:
            pool.start(_SleepTask(5.0))
            log("已注入后台长任务到窗口池（模拟标题生成 HTTP 往返 5s）")

# ── 计时包裹 ─────────────────────────────────────────────
for _name in (
    "_release_drop_targets",
    "_disconnect_command_shortcut_cleanup",
    "_sync_active_windows_to_team_manager",
    "_stop_team_watcher",
    "_launch_background_finalize",
    "_auto_save_current_session",
    "_schedule_gc_hook",
):
    if hasattr(OpenAIChatToolWindow, _name):
        _wrap_timed(OpenAIChatToolWindow, _name, f"wB.{_name}")

if hasattr(wB, "backend") and wB.backend is not None:
    _wrap_timed(wB.backend, "cleanup", "wB.backend.cleanup")
    _wrap_timed(wB.backend, "cancel_streaming", "wB.backend.cancel_streaming")

# 精确验证 waitForDone 耗时：包裹 QThreadPool.waitForDone
from PySide6.QtCore import QThreadPool  # noqa: E402

_orig_wfd = QThreadPool.waitForDone


def _timed_wfd(self, *a, **kw):
    t = time.perf_counter()
    r = _orig_wfd(self, *a, **kw)
    dt = (time.perf_counter() - t) * 1000
    if dt > 5:
        log("  ⏱ QThreadPool.waitForDone", f"{dt:.1f}ms args={a}")
    return r


QThreadPool.waitForDone = _timed_wfd

# 精确验证 _launch_background_finalize 的线程启动与 worker.finalize 耗时
_wrap_timed(tm, "_close_window_at", "tm._close_window_at")

# ── 执行关闭 ─────────────────────────────────────────────
log("=== 开始关闭 wB（刚对话完的页）===")
t = time.perf_counter()
tm._tab_panel.tabCloseRequested.emit(1)  # 走真实信号路径
dt = (time.perf_counter() - t) * 1000
log(f"=== 关闭调用返回，同步段耗时 {dt:.1f}ms ===")

# ── 事件循环里观测析构耗时（deleteLater 下一跳）──────────
for i in range(20):
    t0 = time.perf_counter()
    pump(50)
    dt = (time.perf_counter() - t0 - 0.05) * 1000
    if dt > 20:
        log(f"  事件循环第 {i} 跳卡顿", f"extra {dt:.1f}ms")

# ── 第二层验证：释放窗口引用 → 池析构是否冻结（真凶二）──────
if "--with-title-task" in os.sys.argv:
    import gc

    _wb_ref = wB  # 保留引用供第三层验证（del 后仍要测晚归回调）
    log("释放 wB 本地强引用 + gc.collect()（模拟真实场景窗口树回收）")
    t = time.perf_counter()
    del wB
    gc.collect()
    dt = (time.perf_counter() - t) * 1000
    log(f"  ⏱ gc.collect() 段", f"{dt:.1f}ms（含池析构等待）")
    for i in range(10):
        t0 = time.perf_counter()
        pump(50)
        dt = (time.perf_counter() - t0 - 0.05) * 1000
        if dt > 20:
            log(f"  gc 后第 {i} 跳卡顿", f"extra {dt:.1f}ms")
else:
    _wb_ref = wB

# ── 第三层验证：关窗后任务晚归，回调守卫必须静默丢弃（无异常）──
log("关窗后晚归回调安全性验证——直接调用已销毁窗口的槽")
try:
    # wB 已 close + _is_destroyed=True；此处直接调用主线程槽
    _wb_ref._on_topic_summary_generated({"topic_summary": "晚归标题"}, None)
    log("  ✓ _on_topic_summary_generated 静默返回（守卫生效）")
except Exception as e:
    log(f"  ✗ 守卫失效: {type(e).__name__}: {e}")

# 模拟共享池任务晚归：直接对已销毁窗口的 Signal emit
try:
    _wb_ref._topic_summary_ready.emit({"topic_summary": "信号晚归"}, None)
    log("  ✓ _topic_summary_ready.emit 直接发射（信号层不崩）")
except RuntimeError as e:
    log(f"  ⚠ emit 抛 RuntimeError（_thread_safe_callback 的 try/except 已覆盖）: {e}")
except Exception as e:
    log(f"  ✗ 意外异常: {type(e).__name__}: {e}")

log("探针结束")

with open("tests/debug/probe_close_tab_jank.out", "w", encoding="utf-8") as f:
    f.write("\n".join(OUT))
print("done", flush=True)
os._exit(0)
