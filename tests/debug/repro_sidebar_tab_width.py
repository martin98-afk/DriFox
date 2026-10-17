# -*- coding: utf-8 -*-
"""一次性探针：复现切页左栏宽度污染（切页保宽恢复失效）

复现序列：
1. 窗口 955x780（用户截图尺寸）
2. 两个会话 tab：B(index1) 左栏拉宽 414（工作台关）；A(index0) 工作台记忆开
3. 切 A → 等 700ms（挤压+恢复链跑完）
4. 切回 B → 0/100/300/800ms 密集采样，观察宽度何时被改
5. 打印每步 splitter sizes / collapsed / saved

跑法：uv run python tests/debug/repro_sidebar_tab_width.py
"""

import os
import tempfile

os.environ["DRIFOX_DATA_DIR"] = tempfile.mkdtemp(prefix="drifox_repro_")
print("=== SCRIPT START ===", flush=True)

from app.utils.render_env import apply_render_env, default_config_path  # noqa: E402

_RS = apply_render_env(default_config_path())

from PySide6.QtCore import QCoreApplication, QTimer, Qt  # noqa: E402
from PySide6.QtWidgets import QApplication, QWidget  # noqa: E402

if _RS.get("use_open_gles", True):
    QCoreApplication.setAttribute(Qt.AA_UseOpenGLES)
if _RS.get("share_gl_contexts", True):
    QCoreApplication.setAttribute(Qt.AA_ShareOpenGLContexts)

app = QApplication.instance() or QApplication([])

from app.core.infra.webengine_profile import init_shared_web_profile  # noqa: E402

init_shared_web_profile(app)

from app.main_widget import OpenAIChatToolWindow  # noqa: E402
from app.widgets.tab_manager_window import TabManagerWindow  # noqa: E402
from app.utils.config import Settings  # noqa: E402

settings = Settings.get_instance()


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


page = _FakePage()
tm = TabManagerWindow.create_instance()
w0 = OpenAIChatToolWindow(page)
w1 = OpenAIChatToolWindow(page)
tm.add_window(w0)
tm.add_window(w1)
tm.resize(955, 780)
tm.show()


OUT = []


def log(tag: str) -> None:
    sizes = tm._splitter.sizes()
    line = (
        f"[{tag}] left={sizes[0]} collapsed={tm._tab_panel._collapsed} "
        f"sqz={tm._tab_panel._collapsed_by_squeeze} "
        f"saved={getattr(tm, '_saved_panel_frame_width', None)}"
    )
    OUT.append(line)
    print(line, flush=True)




def step_b_widen() -> None:
    """B(index1)：强制展开态 + 拉宽左栏到 414（等效拖拽落定终态 + saved 补存）"""
    tm._tab_panel.set_active_index(1)
    tm._tab_panel._collapsed = False
    tm._tab_panel._collapsed_by_squeeze = False
    tm._tab_panel.sync_collapsed_ui()

    def _widen():
        panel = tm._tab_panel
        # 真实用户拖拽路径：置拖拽会话 → 拉宽 → 松手（_on_splitter_idle 内部清标志并落定）
        panel.set_dragging_splitter(True)
        sizes = tm._splitter.sizes()
        total = sum(sizes)
        wb = sizes[2] if len(sizes) >= 3 else 0
        tm._splitter.setSizes([414, max(0, total - 414 - wb), wb])
        tm._on_splitter_idle()
        log("B-widened")

        def _squeeze():
            # 模拟展开动画结束后解冻重排把左栏压穿折叠线（chat sizeHint 恢复）
            sizes = tm._splitter.sizes()
            total = sum(sizes)
            wb = sizes[2] if len(sizes) >= 3 else 0
            tm._splitter.setSizes([60, max(0, total - 60 - wb), wb])
            log("B-resqueeze(模拟重排挤压)")

        QTimer.singleShot(260, _squeeze)  # 展开动画 200ms 结束后
        QTimer.singleShot(900, step_switch_a)  # 给钉回复检（150/400ms）留时间

    QTimer.singleShot(300, _widen)


def step_switch_a() -> None:
    """切 A(index0)：工作台记忆开 + 窗口变窄触发挤压折叠（复现 saved 降级场景）"""
    w0._workbench_visible_memory = True
    w1._workbench_visible_memory = False  # B 页工作台关闭（用户在 B 没开工作台）
    OUT.append(">>> 切到 A（工作台记忆开）")
    tm._tab_panel.set_active_index(0)
    for d in (0, 100, 300, 700):
        QTimer.singleShot(d, lambda t=f"A+{d}ms": log(t))

    def _shrink():
        # 窗口变窄 → resize 完成 → _evaluate_squeeze_collapse 挤压折叠链
        OUT.append(">>> A 页窗口变窄 830px（触发挤压折叠）")
        tm.resize(830, 780)
        QTimer.singleShot(600, lambda: (log("A-after-shrink"), step_switch_b()))

    QTimer.singleShot(900, _shrink)


def step_switch_b() -> None:
    """切回 B(index1)：工作台记忆关"""
    OUT.append(">>> 切回 B（工作台关）")
    tm._tab_panel.set_active_index(1)
    for d in (0, 50, 100, 300, 800):
        QTimer.singleShot(d, lambda t=f"B+{d}ms": log(t))
    QTimer.singleShot(1000, finish)


def finish() -> None:
    sizes = tm._splitter.sizes()
    ok = abs(sizes[0] - 414) <= 2
    OUT.append(f"[结论] 切回 B 后左栏宽 = {sizes[0]}，期望 414 → {'✓ 通过' if ok else '✗ 仍被污染'}")
    with open("tests/debug/repro_sidebar_tab_width.out", "w", encoding="utf-8") as f:
        f.write("\n".join(OUT))
    app.quit()


# 宽度/saved 写点观测：包装所有可能动宽度的宿主方法
import time as _time

_T0 = _time.perf_counter()

for _fn_name in (
    "_on_sidebar_toggled",
    "_start_sidebar_anim",
    "_on_sidebar_anim_finished",
    "_apply_restored_panel_width",
    "_maybe_auto_expand_after_squeeze",
    "_maybe_restore_workbench_after_squeeze",
    "set_workbench_visible",
    "_deferred_resize_complete",
    "_force_relayout",
    "_on_splitter_idle",
    "_restore_tab_frame_after_tab_switch",
    "_evaluate_squeeze_collapse",
    "_on_left_card_area_toggled",
    "_sync_tab_frame_cap",
):
    _orig = getattr(TabManagerWindow, _fn_name)

    def _wrap(name, fn):
        def inner(self, *a, **kw):
            r = fn(self, *a, **kw)
            try:
                w = self._splitter.sizes()[0]
            except Exception:
                w = -1
            line = (
                f"  {_time.perf_counter() - _T0:7.3f}s +{name}{a} -> left={w} "
                f"saved={getattr(self, '_saved_panel_frame_width', None)}"
            )
            OUT.append(line)
            print(line, flush=True)
            return r

        return inner

    setattr(TabManagerWindow, _fn_name, _wrap(_fn_name, _orig))


QTimer.singleShot(600, step_b_widen)
app.exec()
with open("tests/debug/repro_sidebar_tab_width.out", "w", encoding="utf-8") as f:
    f.write("\n".join(OUT))
print("done", flush=True)
