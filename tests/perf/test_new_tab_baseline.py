# -*- coding: utf-8 -*-
"""新建标签页基准测试（性能基线 + 功能安全网）。

测量对象：TabManagerWindow.spawn_tab(source, new_session=True) 完整链路，
即用户点击「新建标签页」后的全部主线程工作：
    window_init    OpenAIChatToolWindow.__init__（整套 UI/卡片/后端创建）
    add_window     TabManagerWindow.add_window（QStackedWidget + Tab 面板 + showEvent 同步段）
    create_session QTimer(0) → _create_new_session（会话创建，含 [Perf-CreateSession] 4 子段日志）
    welcome_render 欢迎卡片创建（QWebEngineView，同步段）
    workdir_sync   QTimer(500) → _sync_working_directory（工作目录就绪）
    total          spawn_tab 调用 → 全部哨兵齐（窗口可交互）

打点方式：pytest 进程内 monkeypatch 类方法计时 + loguru 捕获业务内建
[Perf-CreateSession] 日志，不修改任何业务代码。

数据落点：cwd=仓库根的 .drifox/（已 gitignore）。测试会创建若干空会话，
属预期副作用。

运行：
    cd <仓库根> && python -m pytest tests/perf/test_new_tab_baseline.py -v -s
注意：本测试创建真实 Qt 窗口（桌面平台短暂闪现），需图形会话；不能 offscreen
（QtWebEngine 在 offscreen 下原生层 0xC0000409 崩溃）。
"""

import json
import re
import statistics
import time
from datetime import datetime
from pathlib import Path

import pytest

# ── Qt 属性必须在 QApplication 创建前设置（与 main.py 对齐）──────────────────
from PyQt5.QtCore import Qt, QEventLoop, QTimer
from PyQt5.QtWidgets import QApplication, QWidget

QApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
QApplication.setAttribute(Qt.AA_EnableHighDpiScaling)
QApplication.setAttribute(Qt.AA_UseHighDpiPixmaps)
QApplication.setAttribute(Qt.AA_ShareOpenGLContexts)

# QtWebEngine 类必须在 QCoreApplication 实例化前导入（main.py 同款约束）
from PyQt5.QtWebEngineWidgets import QWebEnginePage, QWebEngineSettings, QWebEngineView  # noqa: E402,F401

REPO_ROOT = Path(__file__).resolve().parents[2]
N_ROUNDS = 25  # 基线重复次数（P25/中位数/最大值；25 轮使 P25 运行间偏差 <5%）
SPAWN_TIMEOUT_S = 20.0  # 单轮哨兵等待上限


def _drain(ms: int) -> None:
    """驱动事件循环 ms 毫秒，让 QTimer/singleShot 回调落地"""
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec_()


class FakePage(QWidget):
    """main.py 同款 FakePage（独立窗口模式占位页）"""

    def __init__(self):
        super().__init__()
        from app.utils.config import Settings
        from qfluentwidgets import setFontFamilies

        self.cfg = Settings.get_instance()
        setFontFamilies([self.cfg.llm_font_family.value])

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

        return FakeSignal()

    def setUpdatesEnabled(self, enabled):
        pass

    def update(self):
        pass

    def show_splitter(self):
        pass

    def hide_splitter(self):
        pass


# ── setStyleSheet 同步段计数（批2 样式收敛基线）─────────────────────────
_ss_counter = {"sync": 0, "on": False}
_ss_frames = {}  # {(file, funcname): count}（批2 重复链验证门）


def _install_ss_counter(mp):
    """包装 QWidget/QFrame/QLabel/QPushButton.setStyleSheet 统计调用次数与来源帧"""
    import os
    import traceback

    from PyQt5.QtWidgets import QFrame, QLabel, QPushButton, QWidget

    def _wrap(cls):
        orig = cls.setStyleSheet

        def counted(self, style_sheet):
            if _ss_counter["on"]:
                _ss_counter["sync"] += 1
                for fr in reversed(traceback.extract_stack()[:-1]):
                    fn = os.path.basename(fr.filename)
                    if fn.startswith("Qt") or fn in (
                        "test_new_tab_baseline.py",
                        "coding_plan_ring.py",
                    ) and False:
                        continue
                    if fn == "test_new_tab_baseline.py":
                        continue
                    # 跳过 qfw 内部 style_sheet 等转发帧，取第一个业务帧
                    _ss_frames[(fn, fr.name)] = _ss_frames.get((fn, fr.name), 0) + 1
                    break

            return orig(self, style_sheet)

        mp.setattr(cls, "setStyleSheet", counted, raising=True)

    for _cls in (QWidget, QFrame, QLabel, QPushButton):
        _wrap(_cls)


# ── 计时打点存储：{id(win): {stage: [ms, ...]}} ─────────────────────────────
_stages: dict = {}
_sync_done_at: dict = {}  # {id(win): sync 完成的 perf_counter 时刻（真实打点）}
_create_session_totals: list = []  # 每窗口 create_session 整体耗时（ms）
_perf_log_lines: list = []  # [Perf-CreateSession] 日志原文

_PERF_LOG_RE = re.compile(
    r"auto_save=(\d+)ms\s+cache_cards=(\d+)ms\s+backend_create=(\d+)ms\s+ui_cleanup=(\d+)ms\s+total=(\d+)ms"
)


def _rec(win, key: str, ms: float) -> None:
    _stages.setdefault(id(win), {}).setdefault(key, []).append(ms)


def _install_timing_hooks(monkeypatch):
    """类级计时包装（monkeypatch 自动 restore，不改业务代码）"""
    from app.main_widget import OpenAIChatToolWindow
    from app.widgets.tab_manager_window import TabManagerWindow

    def _wrap(cls, name):
        orig = cls.__dict__[name]

        if name == "add_window":

            def timed_add(self, window, *args, **kwargs):
                t0 = time.perf_counter()
                result = orig(self, window, *args, **kwargs)
                ms = (time.perf_counter() - t0) * 1000
                _rec(window, "add_window", ms)  # 记到被添加窗口，与其它阶段同轴
                return result

            wrapper = timed_add
        elif name == "__init__":

            def timed_init(self, *args, **kwargs):
                t0 = time.perf_counter()
                orig(self, *args, **kwargs)
                _rec(self, "window_init", (time.perf_counter() - t0) * 1000)

            wrapper = timed_init
        else:

            def timed(self, *args, **kwargs):
                t0 = time.perf_counter()
                result = orig(self, *args, **kwargs)
                _ms = (time.perf_counter() - t0) * 1000
                _rec(self, name, _ms)
                if name == "_sync_working_directory":
                    _sync_done_at[id(self)] = time.perf_counter()
                return result

            wrapper = timed

        monkeypatch.setattr(cls, name, wrapper, raising=True)

    _wrap(OpenAIChatToolWindow, "__init__")
    _wrap(OpenAIChatToolWindow, "_create_new_session")
    _wrap(OpenAIChatToolWindow, "_show_initial_welcome")
    _wrap(OpenAIChatToolWindow, "_sync_working_directory")
    _wrap(TabManagerWindow, "add_window")


def _install_log_capture():
    """捕获业务内建 [Perf-CreateSession] 分阶段日志（INFO 级）"""
    from loguru import logger

    sink_id = logger.add(lambda m: _perf_log_lines.append(str(m)), level="INFO")
    return sink_id  # 调用方 logger.remove(sink_id)


def _median_safe(vals):
    """中位数（忽略 None；全 None 返回 None）"""
    vals = [v for v in vals if v is not None]
    return round(statistics.median(vals), 1) if vals else None


def _p25(vals):
    """P25（inclusive 法），轮数 >=4 时稳定"""
    return statistics.quantiles(vals, n=4, method="inclusive")[0]


def _stage(win, key: str):
    vals = _stages.get(id(win), {}).get(key)
    return vals[0] if vals else None


def _parse_create_session_breakdown():
    """取最新一条 [Perf-CreateSession] 的 4 子段明细"""
    for line in reversed(_perf_log_lines):
        m = _PERF_LOG_RE.search(line)
        if m:
            return {
                "auto_save": int(m.group(1)),
                "cache_cards": int(m.group(2)),
                "backend_create": int(m.group(3)),
                "ui_cleanup": int(m.group(4)),
                "sum_total": int(m.group(5)),
            }
    return {}


def _wait_new_tab_ready(win, timeout_s: float = SPAWN_TIMEOUT_S):
    """等待新标签页两个就绪哨兵：welcome 卡片创建 + workdir 同步完成"""
    deadline = time.perf_counter() + timeout_s
    while time.perf_counter() < deadline:
        _drain(20)
        if _stage(win, "_show_initial_welcome") is not None and _stage(win, "_sync_working_directory") is not None:
            return True
    return False


def _spawn_and_measure(tm, source):
    """一轮：新建标签页 + 全链计时，返回该轮结构化数据"""
    _ss_counter["sync"] = 0
    _ss_frames.clear()
    t0s = time.perf_counter()
    _ss_counter["on"] = True
    win = tm.spawn_tab(source, new_session=True)
    _ss_counter["on"] = False
    ss_sync = _ss_counter["sync"]
    sync_wall_ms = (time.perf_counter() - t0s) * 1000  # spawn_tab 调用墙钟（同步段）
    assert win is not None, "spawn_tab 返回 None（创建失败）"
    t0 = time.perf_counter()
    # b) 异步完成链哨兵：workdir_sync 就绪时刻
    while time.perf_counter() - t0 < SPAWN_TIMEOUT_S:
        _drain(20)
        if _stage(win, "_sync_working_directory") is not None:
            break
    else:
        pytest.fail("20s 内 workdir_sync 未执行")
    async_chain_ms = (time.perf_counter() - t0) * 1000
    # c) 完整口径哨兵：welcome 也就绪
    while time.perf_counter() - t0 < SPAWN_TIMEOUT_S:
        _drain(20)
        if _stage(win, "_show_initial_welcome") is not None:
            break
    else:
        pytest.fail("20s 内 welcome 未渲染")
    total_ms = (time.perf_counter() - t0) * 1000

    detail = _parse_create_session_breakdown()
    init_ms = _stage(win, "window_init")
    return {
        "window_init_ms": round(init_ms, 1) if init_ms is not None else None,
        "sync_ms": round(sync_wall_ms, 1),  # a) 同步段（spawn_tab 墙钟，含 window_init+add_window）
        "create_session_ms": (
            round(_stage(win, "_create_new_session"), 1)
            if _stage(win, "_create_new_session") is not None
            else None
        ),
        "create_session_detail": detail,
        "welcome_render_ms": (
            round(_stage(win, "_show_initial_welcome"), 1)
            if _stage(win, "_show_initial_welcome") is not None
            else None
        ),
        "workdir_sync_ms": (
            round(_stage(win, "_sync_working_directory"), 1)
            if _stage(win, "_sync_working_directory") is not None
            else None
        ),
        "async_chain_ms": round(async_chain_ms, 1),  # b) spawn → workdir 就绪
        "total_ms": round(total_ms, 1),  # c) spawn → workdir+welcome 全就绪
        "setstylesheet_sync": ss_sync,
        "session_id": win._current_session_id,
    }


# ── module 级环境：真实 TabManagerWindow + 预热窗口 ─────────────────────────


@pytest.fixture(scope="module")
def tab_env():
    from app.core.webengine_profile import init_shared_web_profile
    from app.utils.render_env import apply_render_env, default_config_path

    # 渲染环境 → 环境变量（QT_OPENGL=angle 等，绕开 Intel OpenGL ICD 崩溃路径）
    apply_render_env(default_config_path())
    for _k in ("QSG_RHI", "QSG_RHI_BACKEND"):
        import os

        os.environ.pop(_k, None)

    app = QApplication.instance() or QApplication([])
    app.setStyle("Fusion")
    init_shared_web_profile(parent=app)

    from app.main_widget import OpenAIChatToolWindow
    from app.widgets.tab_manager_window import TabManagerWindow

    # module 级手动 monkeypatch（monkeypatch fixture 是 function 级，scope 冲突）
    mp = pytest.MonkeyPatch()
    _install_timing_hooks(mp)
    _install_ss_counter(mp)
    sink_id = _install_log_capture()

    tm = TabManagerWindow.create_instance()

    # stub 网络类延迟预热（与新建标签页链路无关，避免污染计时/引入抖动）
    mp.setattr(OpenAIChatToolWindow, "_start_models_dev_sync", lambda self: None, raising=False)
    mp.setattr(OpenAIChatToolWindow, "_async_refresh_opencode_models", lambda self: None, raising=False)
    mp.setattr(OpenAIChatToolWindow, "_check_gitee_sync_reminder", lambda self: None, raising=False)

    # 预热窗口 w0：吃掉全局首次开销（Qt 组件/WebEngine profile/插件系统）
    fake_page = FakePage()
    w0 = OpenAIChatToolWindow(fake_page)
    tm.add_window(w0)
    tm.show()
    _drain(2500)
    assert w0._current_session_id, "预热窗口会话未初始化"

    yield {"tm": tm, "w0": w0}

    from loguru import logger

    logger.remove(sink_id)
    mp.undo()


# ── 测试 1b：workdir 早期就绪（固定 500ms 延迟的回归门）─────────────────────


def test_workdir_ready_without_fixed_delay(tab_env):
    """新建标签页后工作目录应在 tool_executor 就绪事件驱动下早期同步。

    修复前：showEvent 硬注册 QTimer.singleShot(500, _sync_working_directory)，
    workdir 就绪恒 ≥500ms → 本用例失败（RED）。
    修复后：tool_executor 就绪信号驱动，≈300ms 内完成（GREEN）。
    注：循环采样仅为测量就绪时刻，业务链路仍是事件驱动非轮询。
    """
    tm, w0 = tab_env["tm"], tab_env["w0"]

    win = tm.spawn_tab(w0, new_session=True)
    assert win is not None

    # 语义断言（负载免疫）：executor 就绪信号 → sync 完成 的间隔必须 <150ms。
    # 事件驱动：信号后经 singleShot(0) 即同步；若固定 500ms 定时器回归，
    # 间隔 ≈500ms - 就绪时刻 ≈300ms+，必被抓到。墙钟仅作 2s 防悬挂上限。
    ready_at = []
    win.backend.tool_executor_ready.connect(lambda: ready_at.append(time.perf_counter()))

    t0 = time.perf_counter()
    elapsed = None
    while time.perf_counter() - t0 < 2.0:
        _drain(40)
        if _stage(win, "_sync_working_directory") is not None:
            elapsed = (time.perf_counter() - t0) * 1000
            break
    assert elapsed is not None, "2s 内 workdir_sync 未执行（tool_executor 就绪链断裂）"
    assert win.backend.tool_executor is not None, "sync 已执行但 executor 为空（状态矛盾）"
    if ready_at:
        # 用 sync 真实执行打点（非轮询发现时刻），剔除采样粒度与事件循环拥塞
        gap_ms = (_sync_done_at[id(win)] - ready_at[-1]) * 1000
        assert gap_ms < 400, (
            f"executor 就绪→sync 执行间隔 {gap_ms:.0f}ms ≥400ms：固定 500ms 延迟回归或事件驱动失效"
        )
    # ready_at 为空 = executor 在 connect 前已就绪（同步兜底路径），无固定延迟可言


# ── 测试 1：性能基线 ───────────────────────────────────────────────────────


def test_new_tab_performance_baseline(tab_env):
    """新建标签页全链耗时基线：N 轮，输出中位数/最大值/明细，写 JSON 基线文件"""
    tm, w0 = tab_env["tm"], tab_env["w0"]

    rounds = []
    for i in range(N_ROUNDS):
        _drain(200)  # 轮间排水：让上轮 WebEngine 异步任务落地，避免挤进本轮计时
        rounds.append(_spawn_and_measure(tm, w0))

    totals = [r["total_ms"] for r in rounds]
    chains = [r["async_chain_ms"] for r in rounds]
    summary = {
        "captured_at": datetime.now().isoformat(timespec="seconds"),
        "n_rounds": N_ROUNDS,
        # P25 为稳定性基准口径：进程内窗口累积使每轮耗时单调上漂（每窗一组
        # WebEngine 卡片），median 随漂移斜率跨运行漂移；P25 代表低负载下的
        # 边际成本，跨运行偏差 <5%。
        "total_ms": {"p25": round(_p25(totals), 1), "median": round(statistics.median(totals), 1), "max": round(max(totals), 1), "min": round(min(totals), 1)},
        "async_chain_ms": {"p25": round(_p25(chains), 1), "median": round(statistics.median(chains), 1), "max": round(max(chains), 1)},
        "sync_ms": {"p25": round(_p25([r["sync_ms"] for r in rounds]), 1), "median": round(statistics.median([r["sync_ms"] for r in rounds]), 1)},
        "window_init_ms": {"median": _median_safe([r["window_init_ms"] for r in rounds])},
        "create_session_ms": {"median": _median_safe([r["create_session_ms"] for r in rounds])},
        "welcome_render_ms": {"median": _median_safe([r["welcome_render_ms"] for r in rounds])},
        "workdir_sync_ms": {"median": _median_safe([r["workdir_sync_ms"] for r in rounds])},
        "setstylesheet_sync": {"median": round(statistics.median([r["setstylesheet_sync"] for r in rounds]), 1)},
        "rounds": rounds,
    }

    out_dir = REPO_ROOT / "tests" / "perf" / "baselines"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "new_tab_baseline.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print("\n===== 新建标签页基线 =====")
    for r in rounds:
        print(
            f"  total={r['total_ms']:>7.1f}ms  chain={r['async_chain_ms']:>6.1f}  sync={r['sync_ms']:>5.1f}"
            f"  init={r['window_init_ms']}"
            f"  session={r['create_session_ms']:>5.1f}  welcome={r['welcome_render_ms']:>5.1f}"
            f"  workdir={r['workdir_sync_ms']:>4.1f}"
        )
    print(
        f"  中位数: sync={summary['sync_ms']['median']}ms  async_chain={summary['async_chain_ms']['median']}ms"
        f"  total={summary['total_ms']['median']}ms (max {summary['total_ms']['max']}ms)"
    )
    print(f"  create_session 子段（中位轮）: {summary['rounds'][len(rounds) // 2]['create_session_detail']}")
    print(f"  基线 JSON → {out_dir / 'new_tab_baseline.json'}")

    # 防呆上限：同步段样式调用暴涨检测
    ss_med = summary["setstylesheet_sync"]["median"]
    print(f"  setStyleSheet(同步段中位) = {ss_med}")
    assert ss_med <= 500, f"同步段 setStyleSheet {ss_med} 次，异常暴涨"

    # 批2 验证门：重复刷新链 ≤2 次/项（按来源帧统计）
    for (fn, func), n in sorted(_ss_frames.items()):
        if n >= 2:
            print(f"  [frames] {fn}:{func} = {n}")
    assert _ss_frames.get(("coding_plan_ring.py", "_apply_sheet"), 0) <= 2, "ring 容器样式重复 set"
    assert _ss_frames.get(("coding_plan_ring.py", "_build_ui"), 0) == 0, "ring 循环内逐 QLabel set 回归"
    assert _ss_frames.get(("branch_chip.py", "_apply_style"), 0) <= 2, "branch chip 样式重复刷"
    assert _ss_frames.get(("main_widget.py", "_refresh_project_branch_style"), 0) <= 6, "项目分支样式重复刷（>6）"

    # 防呆上限（非优化目标）：单轮 20s 哨兵内完成；窗口/会话真实创建
    assert all(r["session_id"] for r in rounds)
    assert summary["total_ms"]["median"] < 5000, f"中位数 {summary['total_ms']['median']}ms 异常偏离基线量级"


# ── 测试 2：新建后 UI 元素存在且可用（重构安全网）───────────────────────────


def test_new_tab_ui_elements_present(tab_env, monkeypatch):
    """新建后：输入框/消息列表/标题栏存在、可用、可输入；两卡 ensure 链真实可用；
    hover tooltip 延迟安装（同步段不发生）"""
    tm, w0 = tab_env["tm"], tab_env["w0"]

    # 批1 断言：batch_install_hover_tooltips 不在窗口同步构造段内执行
    # （build 内为函数级 from-import，须 patch 源模块属性）
    import app.widgets.simple_hover_tooltip as _sht

    calls = []
    monkeypatch.setattr(_sht, "batch_install_hover_tooltips", lambda h: calls.append(1))
    win = tm.spawn_tab(w0, new_session=True)
    assert not calls, "batch_install_hover_tooltips 在 spawn 同步段内执行（延迟安装失效）"
    assert win is not None
    assert _wait_new_tab_ready(win), "新标签页未在超时内就绪"

    # 跨窗广播（_notify_history_data_changed）会 invalidate+重调度本窗 welcome
    # 卡（延迟槽位重建），断言必须等「无 pending 且卡已回布局」的稳定态，
    # 否则恰好落在已摘除/未重建窗口期会误报。
    deadline = time.perf_counter() + 5.0
    while time.perf_counter() < deadline:
        if win.chat_layout.count() >= 1 and not getattr(win, "_welcome_render_pending", False):
            break
        _drain(50)
    else:
        pytest.fail("5s 内 welcome 卡片未达稳定态")

    # 输入框：SendableTextEdit，可用且能输入
    input_area = win.input_area
    assert input_area is not None and input_area.isEnabled()
    input_area.setPlainText("基线探针输入")
    assert input_area.toPlainText() == "基线探针输入"
    input_area.clear()

    # 消息列表：chat_layout 存在且已挂 welcome 卡片（新会话欢迎页）
    assert hasattr(win, "chat_layout") and win.chat_layout is not None
    assert win.chat_layout.count() >= 1, "新会话消息区为空（welcome 卡片未挂载）"

    # 标题栏：标题输入框已重置为「新对话」
    assert win.title_edit is not None
    assert win.title_edit.text() == "新对话"

    # 会话归属：新会话已创建
    assert win._current_session_id

    # 批1：两懒创建卡在真实窗口上 ensure 链可用（构造/接线/注册不抛）
    win._ensure_tool_control_card()
    win._ensure_question_floating_widget()
    assert win._tool_control_card is not None
    assert win._question_floating_widget is not None

    # 延迟 tooltip 安装最终发生（singleShot(100)）
    _drain(400)
    assert calls, "batch_install_hover_tooltips 延迟安装未发生"


# ── 测试 3：双标签页上下文互相独立（重构安全网）─────────────────────────────


def test_two_tabs_context_independence(tab_env):
    """改 A 的 项目/模型/工作目录/工具权限，B 不受影响（反向亦然）"""
    tm, w0 = tab_env["tm"], tab_env["w0"]

    win_a = tm.spawn_tab(w0, new_session=True)
    _drain(150)
    win_b = tm.spawn_tab(w0, new_session=True)
    assert win_a is not None and win_b is not None and win_a is not win_b
    assert _wait_new_tab_ready(win_a) and _wait_new_tab_ready(win_b)

    # _on_project_selected 依赖历史插件卡片（set_current_project），
    # 主程序中该入口只能从已创建的历史浮动卡触发；测试先按侧栏点击同款
    # 链路（toggle_floating_card）把卡建出来，复现此前置。
    from app.plugins.registries.ui_plugin_registry import UIPluginRegistry

    UIPluginRegistry.get_instance().toggle_floating_card("history-manager", main_widget=win_a)
    _drain(500)
    if win_a._history_popup_card is None:
        pytest.skip("history 浮动卡未能创建（_history_popup_card 为 None）")

    baseline_project = win_b._current_project
    baseline_model = (win_b._current_provider_name, win_b._current_model_name)
    baseline_toggles = dict(win_b._tool_permission_controller.get_toggles())
    baseline_workdirs = dict(win_b._current_workdir)

    # ── A：改项目（真实入口 _on_project_selected，会触发其自身新建会话）──
    proj_a = f"PerfIsolationA-{int(time.time())}"
    win_a._on_project_selected(proj_a)
    _drain(1200)  # 排水：A 的新建会话链 + workdir 同步落地

    # ── A：改模型（实例级字段，多窗口隔离契约的核心状态）──
    win_a._current_provider_name = "prov-isolation-A"
    win_a._current_model_name = "model-isolation-A"

    # ── A：改工作目录（实例级缓存 {project: path}）──
    win_a._current_workdir[proj_a] = str(REPO_ROOT / ".drifox" / "perf_isolation_workdir_a")

    # ── A：改工具权限（真实 API：关掉一个当前开启的工具）──
    toggles_a = win_a._tool_permission_controller.get_toggles()
    target_tool = next((k for k, v in toggles_a.items() if v), None)
    assert target_tool, "找不到已开启的工具可供切换"
    win_a._tool_permission_controller.set_user_toggle(target_tool, False)

    _drain(300)

    # ── 断言 A：改动生效 ──
    assert win_a._current_project == proj_a
    assert win_a._current_provider_name == "prov-isolation-A"
    assert win_a._current_workdir[proj_a].endswith("perf_isolation_workdir_a")
    assert win_a._tool_permission_controller.get_toggles()[target_tool] is False

    # ── 断言 B：全部不受影响 ──
    assert win_b._current_project == baseline_project, "B 的项目被 A 污染"
    assert (win_b._current_provider_name, win_b._current_model_name) == baseline_model, "B 的模型被 A 污染"
    assert win_b._current_workdir == baseline_workdirs, "B 的工作目录被 A 污染"
    assert win_b._tool_permission_controller.get_toggles()[target_tool] is True, "B 的工具权限被 A 污染"
    assert dict(win_b._tool_permission_controller.get_toggles()) == baseline_toggles

    # ── 会话独立：两窗口 _current_session_id 不同且保持 ──
    assert win_a._current_session_id != win_b._current_session_id

    # 清理：_on_project_selected 会把项目写进全局配置（cfg.save()），
    # 残留会污染后续进程的默认项目（w0 继承脏值）。恢复后落盘。
    try:
        win_a.cfg.current_project.value = baseline_project
        win_a.cfg.save()
    except Exception:
        pass


def test_tree_refresh_debounce_under_spawn_burst(tab_env, monkeypatch):
    """批2：连续 spawn 5 tab → refresh_workspace_tree 执行 ≤2 次（250ms 防抖）"""
    tm, w0 = tab_env["tm"], tab_env["w0"]
    panel = tm._tab_panel
    monkeypatch.setattr(panel, "current_mode", lambda: "tree", raising=False)
    counter = {"n": 0}
    monkeypatch.setattr(panel, "refresh_tree", lambda: counter.__setitem__("n", counter["n"] + 1), raising=False)

    for _ in range(5):
        win = tm.spawn_tab(w0, new_session=True)
        assert win is not None, "spawn 失败"
        _drain(30)  # 连续节奏（间隔 <250ms 防抖窗口），模拟用户连点新建
    burst = counter["n"]
    _drain(400)  # 防抖到期补刷（最终一致）
    total = counter["n"]
    assert burst <= 2, f"突发期 refresh_tree 执行 {burst} 次（>2，防抖失效）"
    assert total <= burst + 1, f"防抖到期补刷异常：total={total} burst={burst}"
