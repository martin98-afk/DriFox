# -*- coding: utf-8 -*-
"""泳道图滚轮缩放锚定回归：光标下的条目必须在缩放后留在光标下。

用户报（2026-09-18）：Duration 模式滚轮放大正常，**等宽 / Token 下条带乱跳**
（实测漂移 255~313px，多数条目被直接甩出视口）。

根因：x 坐标有两个不同的域，缩放逻辑只认其中一个。
- Duration：条带 x 由**真实时间**线性映射而来，与时间窗同源；
- 等宽 / Token：条带 x 是**槽位域**（窗内条目按序号铺满整轴，与自身时刻无关）。

``wheelEvent`` 早年按 ``anchor = t0 + frac * span`` 取锚点（线性时间插值），
在时长模式下恰好正确，切到槽位模式就完全跑偏 —— 时间上算出的锚点与光标
指着的那条毫无关系。修复后：时间窗由**条目窗口**表达，锚点取光标所压条目，
缩放前后该条目的槽内相对位置不变。

跑法：``pytest tests/plugins/test_agent_trace_timeline_zoom.py -v``
（加 ``-s`` 可看诊断表）
"""

from __future__ import annotations

import importlib
import importlib.util
import os
import sys
import time

import pytest

pytest.importorskip("PyQt5.QtWidgets")

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_UI = os.path.join(_ROOT, "plugins", "agent_trace", "ui")
for p in (_UI, _ROOT):
    if p not in sys.path:
        sys.path.insert(0, p)


def _load_pkg():
    name = "agent_trace_zoom_pkg"
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            name, os.path.join(_UI, "__init__.py"), submodule_search_locations=[_UI]
        )
        m = importlib.util.module_from_spec(spec)
        sys.modules[name] = m
        spec.loader.exec_module(m)
    return importlib.import_module(f"{name}.trace_models"), importlib.import_module(f"{name}.timeline_panel")


_models, _panel = _load_pkg()
TraceRecord = _models.TraceRecord
EntryKind = _models.EntryKind
_KINDS = [EntryKind.USER, EntryKind.ASSISTANT, EntryKind.TOOL, EntryKind.CONTEXT]

# 允许的漂移像素。锚点语义是「条目留在光标下」，不是「像素级纹丝不动」。
#
# 等宽模式：槽宽等分，锚点公式与渲染几何完全同源，精度只受槽序取整影响 → 收紧。
# Token 模式：槽宽 ∝ token 占比，而缩放改变了参与归一化的条目集合 → 同一条的
#   像素位置必然随分母变化（一条 19k 的回复夹在 19k/20k 邻居之间时，窗内集合
#   一变就移几十像素）。这是「按占比看宽度」这一语义的固有属性，不是缺陷；
#   实测单次 ≤40px / 轨道 1530px（2.6%），缩放方向与锚点条目均正确。
_TOL_PX = 30.0
_TOL_PX_TOKEN = 45.0


def _mk_records(n_sparse: int = 20, n_dense: int = 40) -> list:
    """时间刻意不均匀：稀疏段 30s/条、密集段 0.25s/条。

    「条目序号」与「时间位置」严重不同源 —— 槽位域均匀而时间域前疏后密，
    锚点若用错坐标系会立刻放大成几百像素的漂移。时间均匀时会掩盖问题。
    """
    base = time.time() - 700
    out = []
    t = base
    for i in range(n_sparse):
        out.append(_rec(f"sparse-{i}", _KINDS[i % 4], t, 0.4, i + 1, 500 + i * 10))
        t += 30.0
    for i in range(n_dense):
        out.append(_rec(f"dense-{i}", _KINDS[i % 4], t, 0.05, n_sparse + 1 + i, 20000 - i * 100))
        t += 0.25
    return out


def _rec(label: str, kind, start: float, dur: float, turn: int, tokens: int) -> TraceRecord:
    return TraceRecord(
        kind=kind,
        label=label,
        preview="",
        raw="x" * 40,
        source="messages[0]",
        start_ts=start,
        end_ts=start + dur,
        turn_no=turn,
        meta={"tokens": tokens},
    )


@pytest.fixture()
def panel(qapp):
    from PyQt5.QtGui import QPixmap

    p = _panel.TimelinePanel()
    p.resize(1600, p.height())
    p.show()
    qapp.processEvents()
    p._test_pixmap = QPixmap(1600, p.height())
    yield p
    p.close()


def _render(panel, qapp):
    panel.render(panel._test_pixmap)
    qapp.processEvents()


def _bar_of(panel, idx):
    for rect, i in panel._hit_areas:
        if i == idx:
            return rect
    return None


def _wheel_at(panel, x, qapp, zoom_in=True, monkeypatch=None):
    """在像素 x 处派发一次滚轮（真实走 panel.wheelEvent）。"""
    from PyQt5.QtCore import QPoint, QPointF, Qt
    from PyQt5.QtGui import QWheelEvent

    gpos = panel.mapToGlobal(QPoint(int(x), 10))

    class _Stub:
        @staticmethod
        def pos():
            return gpos

    real = _panel.QCursor
    _panel.QCursor = _Stub
    try:
        delta = QPoint(0, 120) if zoom_in else QPoint(0, -120)
        ev = QWheelEvent(
            QPointF(float(x), 10.0),
            QPointF(gpos),
            QPoint(0, 0),
            delta,
            Qt.NoButton,
            Qt.NoModifier,
            getattr(Qt, "NoScrollPhase", 0),
            False,
        )
        panel.wheelEvent(ev)
    finally:
        _panel.QCursor = real
    _render(panel, qapp)


def _anchor_case(panel, qapp, mode: str, label: str, zoom_in: bool = True) -> tuple:
    """把光标放在 label 那条的条带中心，缩放一次，返回 (漂移px, 是否被甩出)。"""
    panel._view = None
    panel._iwin = None
    panel.set_mode(mode)
    panel.set_records(_mk_records())
    _render(panel, qapp)

    recs = panel._records
    idx = next(i for i, r in enumerate(recs) if r.label == label)
    bar = _bar_of(panel, idx)
    if bar is None:
        return float("nan"), True
    x = int((bar.left() + bar.right()) / 2)

    _wheel_at(panel, x, qapp, zoom_in=zoom_in)

    after = _bar_of(panel, idx)
    if after is None:
        return float("nan"), True
    return float(after.left() - bar.left()), False


# ──────────────────── 用例 ────────────────────


@pytest.mark.parametrize("label", ["sparse-5", "sparse-19", "dense-10", "dense-39"])
def test_equal_mode_zoom_anchors_cursor_item(panel, qapp, label):
    """等宽模式：放大后光标下的条目仍在光标下（漂移 ≤ 容差）。"""
    drift, gone = _anchor_case(panel, qapp, _panel.MODE_EQUAL, label)
    assert not gone, f"{label} 在等宽模式放大后被甩出视口"
    assert abs(drift) <= _TOL_PX, f"{label} 等宽模式漂移 {drift:.1f}px（容差 {_TOL_PX}）"


@pytest.mark.parametrize("label", ["sparse-5", "sparse-19", "dense-10", "dense-39"])
def test_token_mode_zoom_anchors_cursor_item(panel, qapp, label):
    """Token 模式：同上（槽宽按 token 占比，容差放宽，理由见 _TOL_PX_TOKEN）。"""
    drift, gone = _anchor_case(panel, qapp, _panel.MODE_TOKEN, label)
    assert not gone, f"{label} 在 Token 模式放大后被甩出视口"
    assert abs(drift) <= _TOL_PX_TOKEN, f"{label} Token 模式漂移 {drift:.1f}px（容差 {_TOL_PX_TOKEN}）"


@pytest.mark.parametrize("label", ["sparse-5", "dense-10"])
def test_duration_mode_zoom_still_ok(panel, qapp, label):
    """Duration 模式行为不得回归（原本就对：锚点是真实时刻）。"""
    drift, gone = _anchor_case(panel, qapp, _panel.MODE_DURATION, label)
    assert not gone, f"{label} 在 Duration 模式放大后被甩出视口"
    assert abs(drift) <= 5.0, f"{label} Duration 模式漂移 {drift:.1f}px（原本应 ≈0）"


@pytest.mark.parametrize("mode", [_panel.MODE_EQUAL, _panel.MODE_TOKEN])
def test_zoom_out_also_anchors(panel, qapp, mode):
    """缩小（滚轮向下）同样要锚住光标下的条目 —— 同一条几何，两个方向都测。"""
    drift, gone = _anchor_case(panel, qapp, mode, "dense-10", zoom_in=False)
    assert not gone, "缩小时目标条目被甩出视口"
    assert abs(drift) <= _TOL_PX, f"缩小漂移 {drift:.1f}px"


@pytest.mark.parametrize("mode", [_panel.MODE_EQUAL, _panel.MODE_TOKEN])
def test_repeated_zoom_keeps_reference_item_on_screen(panel, qapp, mode):
    """连续滚 5 次（真实使用姿势）：基准条目必须**仍在光标附近可见**。

    不断言累计漂移的绝对值：槽位域下每放大一次参与铺满的条目就换一批，
    条带位置必然重排；要点是它别被甩出视野（旧实现第 4~5 次就消失）。
    Token 模式另有相对容差（条带自身宽度内）。
    """
    panel._view = None
    panel._iwin = None
    panel.set_mode(mode)
    panel.set_records(_mk_records())
    _render(panel, qapp)

    recs = panel._records
    idx = next(i for i, r in enumerate(recs) if r.label == "dense-10")
    bar0 = _bar_of(panel, idx)
    assert bar0 is not None
    x = int((bar0.left() + bar0.right()) / 2)

    for step in range(5):
        _wheel_at(panel, x, qapp, zoom_in=True)
        bar = _bar_of(panel, idx)
        assert bar is not None, f"第 {step + 1} 次放大后条目被甩出视口"
        # 每次缩放后条带都该还在光标所在的那半侧，不能被甩到轴的另一头
        assert abs(bar.left() - x) <= panel._track_w * 0.5, (
            f"第 {step + 1} 次放大后条带跑到 {bar.left()}px（光标 {x}px）—— 锚点失效"
        )


def test_zoom_resets_when_full_range_reached(panel, qapp):
    """一路缩小到覆盖全量 → 复位（不残留缩放状态）。"""
    panel._view = None
    panel._iwin = None
    panel.set_mode(_panel.MODE_EQUAL)
    panel.set_records(_mk_records())
    _render(panel, qapp)

    x = int(panel._track_x + panel._track_w * 0.5)
    for _ in range(8):
        _wheel_at(panel, x, qapp, zoom_in=False)
    assert panel._iwin is None, "缩到全量后条目窗口应复位"


def test_scrollbar_maps_to_item_window_in_slot_modes(panel, qapp):
    """槽位模式下滚动条的取值范围必须与条目窗口一致（否则拖了没反应或跳）。"""
    panel._view = None
    panel._iwin = None
    panel.set_mode(_panel.MODE_EQUAL)
    panel.set_records(_mk_records())
    _render(panel, qapp)

    x = int(panel._track_x + panel._track_w * 0.5)
    _wheel_at(panel, x, qapp, zoom_in=True)
    _render(panel, qapp)

    sb = panel._scrollbar
    assert sb.isVisible(), "放大后应出现平移滚动条"
    lo, hi = panel._iwin
    assert sb.value() == lo, f"滚动条值 {sb.value()} 与条目窗口起点 {lo} 不一致"
    assert sb.pageStep() == hi - lo + 1, f"滚动条页长 {sb.pageStep()} 与窗口条数 {hi - lo + 1} 不一致"


@pytest.mark.parametrize("mode", [_panel.MODE_EQUAL, _panel.MODE_TOKEN])
@pytest.mark.parametrize("width", [1, 2, 3])
def test_zoom_out_escapes_minimum_window(panel, qapp, mode, width):
    """窗口收到最小宽度（1~3 条）后，下滚必须仍能变宽。

    用户报（2026-09-19）：泳道图放大到最大后**无法缩小**。

    根因：槽位模式（等宽 / Token）的窗口宽度是**整数条数**，每次滚轮乘 k
    后取整。宽度 2 时两个方向都是固定点 ——
    ``round(2 * 0.8) = round(1.6) = 2``（放大不动）、
    ``round(2 * 1.25) = round(2.5) = 2``（缩小也不动，Python 的 banker's
    rounding 把 .5 归到偶数侧）。宽度 1 同样双向锁死。窗口不变 →
    ``_sync_scrollbar`` / ``update`` 无状态变化 → 滚轮彻底失效。

    Duration 模式不受影响：那条路径用浮点 ``new_span = max(0.02, span * k)``，
    缩小侧 0.02 → 0.025 → 0.03125 一路可退，所以只有槽位模式会锁死。
    """
    panel._view = None
    panel.set_mode(mode)
    panel.set_records(_mk_records())
    _render(panel, qapp)

    n_all = len(panel._records)
    i0 = n_all // 2
    panel._iwin = (i0, i0 + width - 1)
    panel._sync_scrollbar()
    _render(panel, qapp)

    x = int(panel._track_x + panel._track_w * 0.5)
    _wheel_at(panel, x, qapp, zoom_in=False)
    assert panel._iwin is not None, f"{mode} 下滚不应清空窗口（远未覆盖全量）"
    after = panel._iwin[1] - panel._iwin[0] + 1
    assert after > width, f"{mode} 宽度 {width} 下滚后仍是 {after} —— 缩放死锁"


@pytest.mark.parametrize("mode", [_panel.MODE_EQUAL, _panel.MODE_TOKEN])
def test_zoom_in_escapes_two_item_window(panel, qapp, mode):
    """宽度 2 时上滚必须收窄到 1（放大方向同样是固定点，只是危害小）。"""
    panel._view = None
    panel.set_mode(mode)
    panel.set_records(_mk_records())
    _render(panel, qapp)

    n_all = len(panel._records)
    i0 = n_all // 2
    panel._iwin = (i0, i0 + 1)
    panel._sync_scrollbar()
    _render(panel, qapp)

    x = int(panel._track_x + panel._track_w * 0.5)
    _wheel_at(panel, x, qapp, zoom_in=True)
    assert panel._iwin is not None, f"{mode} 放大不应清空窗口"
    after = panel._iwin[1] - panel._iwin[0] + 1
    assert after == 1, f"{mode} 宽度 2 上滚后是 {after}，应收到最小 1 条"


def _wheel_event_accepted(panel, x):
    """派发一次滚轮后返回事件的 accepted 状态（不经过 _wheel_at 的渲染）。"""
    from PyQt5.QtCore import QPoint, QPointF, Qt
    from PyQt5.QtGui import QWheelEvent

    gpos = panel.mapToGlobal(QPoint(int(x), 10))

    class _Stub:
        @staticmethod
        def pos():
            return gpos

    real = _panel.QCursor
    _panel.QCursor = _Stub
    try:
        ev = QWheelEvent(
            QPointF(float(x), 10.0),
            QPointF(gpos),
            QPoint(0, 0),
            QPoint(0, 120),
            Qt.NoButton,
            Qt.NoModifier,
            getattr(Qt, "NoScrollPhase", 0),
            False,
        )
        ev.setAccepted(False)  # Qt 派发时的初始态
        panel.wheelEvent(ev)
        return ev.isAccepted()
    finally:
        _panel.QCursor = real


@pytest.mark.parametrize("mode", [_panel.MODE_EQUAL, _panel.MODE_DURATION, _panel.MODE_TOKEN])
def test_wheel_event_is_consumed(panel, qapp, mode):
    """滚轮必须被消费（accept），否则会沿父链上浮，触发别的滚动区跟着滚。

    用户报（2026-09-19）：滚泳道图时**聊天消息列表跟着滚动**。

    根因：Qt5 里 ``QWidget::wheelEvent`` 默认实现是 ``event->ignore()``
    （qwidget.cpp，Qt 5.15），未接受的 Wheel 会沿 parent 链向上传播。本面板
    覆写 ``wheelEvent`` 后只做缩放、从不调 ``accept()``/``ignore()``，事件因此
    保持未接受状态并上浮到 ``TabManagerWindow._chat_wrapper``，命中
    ``tab_manager_window.py`` 的 ``QEvent.Wheel`` 分支 →
    ``_forward_wheel_to_scroll_area()`` → ``chat_scroll_area.wheelEvent(event)``。
    那就是「列表跟着滚」。

    该转发分支本身有其用途（限宽居中的留白区没有子控件接收滚轮，需要转发给
    对话区），所以修复点在**面板侧**：处理完就 accept，别让事件继续往上跑。
    """
    panel._view = None
    panel._iwin = None
    panel.set_mode(mode)
    panel.set_records(_mk_records())
    _render(panel, qapp)

    x = int(panel._track_x + panel._track_w * 0.5)
    assert _wheel_event_accepted(panel, x), (
        f"{mode} 模式：滚轮未被 accept → 事件会沿父链上浮到 _chat_wrapper，"
        "被转发给 chat_scroll_area，表现为聊天列表跟着滚"
    )


def test_wheel_on_empty_records_still_bubbles(panel, qapp):
    """无数据时**不**消费滚轮 —— 泳道图没内容可缩放，事件应正常上浮滚对话区。"""
    panel._view = None
    panel._iwin = None
    panel.set_records([])
    _render(panel, qapp)

    x = int(panel._track_x + panel._track_w * 0.5)
    assert not _wheel_event_accepted(panel, x), "空数据时不应吞掉滚轮事件"


def _print_diag(panel, qapp):
    """诊断表（``-s`` 可见）：各模式 × 各标的下的一次放大漂移。"""
    print(f"\n{'模式':>9} {'目标记录':>12} {'漂移px':>10}")
    print("-" * 34)
    for mode in (_panel.MODE_EQUAL, _panel.MODE_DURATION, _panel.MODE_TOKEN):
        for label in ("sparse-5", "sparse-19", "dense-10", "dense-39"):
            drift, gone = _anchor_case(panel, qapp, mode, label)
            txt = "甩出视口" if gone else f"{drift:.1f}"
            print(f"{mode:>9} {label:>12} {txt:>10}")


def test_zoom_diagnostics(panel, qapp):
    """汇总诊断（始终通过；断言在其余用例里）。"""
    _print_diag(panel, qapp)
