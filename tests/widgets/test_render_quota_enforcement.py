# -*- coding: utf-8 -*-
"""回归测试：加载长会话时渲染配额不得被架空（2026-09-14 内存暴涨修复）。

三个缺陷（真机日志证据：``rendered=2`` 而 ``stats acquire=26 hit=0``，
单 renderer 进程 512MB，`_recycle_lru_batches` 一次都没跑）：

1. **计数漏记**：`_recycle_out_of_view_batches` 第一步的补渲染只累加日志用的
   `lazy_render_count`，从不写回 `_rendered_card_count` → 配额被永久低估 →
   淘汰链首行 `if _rendered_card_count <= quota: return` 恒成立。
2. **保留范围用了加载窗口**：`_visible_batch_start/_end` 加载完恒等于
   ``[len-12, len]``，小表（≤20 批）下保留区间覆盖整张表 → 回收范围恒为空，
   保护判定也让候选集恒为空。
3. **候选耗尽无降级**：即使前两项修好，全部批次被软保护（可视区 ±1）时
   依旧无候选可淘汰，计数无法回落。

测试策略：`__new__` 构造最小实例（与 test_main_widget_scroll_anchor.py 同法），
不实例化完整窗口、不创建 QWebEngineView。第 1 项用**源码断言**锁定归属关系，
第 2/3 项用真实几何/候选逻辑做行为断言。
"""

import sys
from unittest.mock import MagicMock

import pytest
from PySide6.QtCore import QRect

# 注意：不得在此插入 "app" 到 sys.path（见 test_main_widget_scroll_anchor.py 注释）

from app.main_widget import OpenAIChatToolWindow  # noqa: E402

_SRC_PATH = "app/main_widget.py"


def _src() -> str:
    with open(_SRC_PATH, encoding="utf-8") as f:
        return f.read()


def _make_window(**overrides):
    """构造 _recycle_* 系列可运行的最小实例。"""
    win = OpenAIChatToolWindow.__new__(OpenAIChatToolWindow)
    win._is_destroyed = False
    win._is_virtual_recycling = False
    win._recycle_lru_call_count = 0
    win._rendered_card_count = 0
    win._max_rendered_cards = 12
    win._visible_batch_start = 0
    win._visible_batch_end = 0
    win._batch_cards = []
    win._batch_placeholders = {}
    win._current_assistant_card = None
    win._programmatic_scroll = MagicMock()
    # 守卫依赖状态（2f22cf81 引入的 loading/jump 守卫在 __new__ 桩上不初始化会
    # 抛 RuntimeError: super-class __init__() was never called）
    win._is_loading_history_batches = False
    win._is_jump_in_flight = lambda: False
    win._last_visible_card_ids = set()
    win._pending_lazy_cards = []
    win._is_widget_alive = lambda w: True
    win._log_render_quota = MagicMock()
    win._maybe_strong_recycle = MagicMock()
    win._unload_batch = MagicMock(return_value=0)
    win._effective_max_rendered_cards = lambda: 12
    win._render_floor = lambda: 3
    win._last_quota_log_at = 0.0
    win.chat_layout = MagicMock()
    win.chat_layout.count.return_value = 0
    win.chat_layout.itemAt.return_value = None
    scroll_bar = MagicMock()
    scroll_bar.maximum.return_value = 1000
    scroll_bar.value.return_value = 0
    area = MagicMock()
    area.verticalScrollBar.return_value = scroll_bar
    area.viewport.return_value.height.return_value = 600
    win.chat_scroll_area = area
    for key, value in overrides.items():
        setattr(win, key, value)
    return win


def _card(lazy_rendered: bool = True, idx: int = 0):
    card = MagicMock()
    card._lazy_rendered = lazy_rendered
    card._message_index = idx
    card._is_welcome = False
    return card


@pytest.fixture(autouse=True)
def _reset_global_pages():
    """隔离模块级 _global_rendered_pages 计数器（所有权在 memory_governor）"""
    from app.core.infra import memory_governor

    old = memory_governor._global_rendered_pages
    memory_governor._global_rendered_pages = 0
    yield
    memory_governor._global_rendered_pages = old


# ─── 缺陷 1：补渲染必须计入配额 ───────────────────────────────


def test_recycle_path_render_is_counted_toward_quota(monkeypatch):
    """[MEM] 行为级：`_recycle_out_of_view_batches` 的补渲染必须真实写回计数。

    回归「配额驱动回收空转」前半段：补渲染（ensure_rendered）若不写回
    `_rendered_card_count`，计数恒低于实况 → 配额恒判「未超限」→ 淘汰链
    一次不跑（实验 10 实证）。构造 1 批 2 张未渲染卡，跑一次回收器，
    断言计数实测从 0 写回 2 且两卡的 ensure_rendered 真的被调用。
    （替代旧版源码字符串断言；isinstance 门控经 monkeypatch 模块内
    MessageCard 指向轻量桩类通过，生产代码零改动。）
    """
    import app.main_widget as mw

    class _FakeMessageCard:
        def __init__(self, idx):
            self._message_index = idx
            self._lazy_rendered = False  # 未渲染（漏记态）
            self.render_calls = 0

        def ensure_rendered(self):
            self.render_calls += 1
            self._lazy_rendered = True

    monkeypatch.setattr(mw, "MessageCard", _FakeMessageCard)
    cards = [_FakeMessageCard(idx=0), _FakeMessageCard(idx=1)]
    win = _make_window(
        _batch_cards=[cards],
        _rendered_card_count=0,  # 漏记态：实物未渲染、计数 0
        _effective_max_rendered_cards=lambda: 6,
        _is_loading_history_batches=False,
        _is_jump_in_flight=lambda: False,
        _incremental_visible_batch_count=8,
        _virtual_scroll_buffer=1,
        _resolve_viewport_range=lambda: (0, 0),
        _recycle_lru_batches=MagicMock(),
    )

    win._recycle_out_of_view_batches()

    for c in cards:
        assert c.render_calls == 1, "补渲染必须真的调用 ensure_rendered"
        assert c._lazy_rendered is True
    assert win._rendered_card_count == 2, "补渲染必须把计数从 0 写回实况 2"


def test_recount_helper_uses_batch_cards_and_lazy_flag():
    """`_recount_rendered_cards` 是漂移兜底：按实况重算并同步全局计数。"""
    text = _src()
    start = text.index("def _recount_rendered_cards(")
    body = text[start : text.index("def _recycle_lru_batches(", start)]
    assert 'getattr(card, "_lazy_rendered", False)' in body
    assert "self._sync_global_rendered_pages(actual)" in body

    win = _make_window()
    win._batch_cards = [[_card(True), _card(True)], None, [_card(False)]]
    win._rendered_card_count = 0
    win._recount_rendered_cards()
    assert win._rendered_card_count == 2, "只计已渲染卡片，未渲染卡不计"


# ─── 缺陷 2：保留范围必须基于真实视口 ─────────────────────────



def test_stale_zero_count_recovers_before_quota_check():
    """[MEM] 计数漂移钉子：实物 40 / 计数 0 → 配额判断前校准为 40 → 触发卸载。

    回归「配额驱动回收空转」：懒渲染消费路径漏更新计数 → `_rendered_card_count`
    恒 0 → `0 <= quota` 恒成立 → 淘汰链一次不跑（实验 10 的另一半）。修复在
    `_recycle_lru_batches` 入口对「明显不一致」（计数 0 或 > 批次总数）先按
    实况校准，再进配额比较。
    """
    cards = [[_card(True, idx=i)] for i in range(40)]
    unloaded = []
    win = _make_window(
        _batch_cards=cards,
        _rendered_card_count=0,
        _effective_max_rendered_cards=lambda: 6,
        _is_loading_history_batches=False,
        _is_jump_in_flight=lambda: False,
        _batch_fully_rendered=lambda cards_: all(
            getattr(c, "_lazy_rendered", False) for c in cards_
        ),
        _batch_is_protected=lambda idx, vp_range: False,
        _batch_distance=lambda idx, vp_range: 10 + idx,
        _resolve_viewport_range=lambda: (0, 1),
        _batch_is_streaming_or_welcome=lambda idx: False,
        _virtual_buffer_batches=lambda: 2,
    )

    def _fake_unload(idx):
        win._batch_cards[idx] = None
        unloaded.append(idx)
        win._rendered_card_count -= 1
        return 0

    win._unload_batch = _fake_unload

    win._recycle_lru_batches()

    assert win._rendered_card_count <= 6, "必须裁到配额内"
    assert len(unloaded) >= 34, "40 - quota(6) = 34 批应被卸载"

def _patch_layout_geometry(win, entries):
    """entries: [(index, top, height)] → 让 chat_layout 返回带几何的卡片。"""
    widgets = []
    for idx, top, height in entries:
        w = MagicMock()
        w.geometry.return_value = QRect(0, top, 800, height)
        w._message_index = idx
        widgets.append(w)
    win.chat_layout.count.return_value = len(widgets)
    win.chat_layout.itemAt.side_effect = lambda i: _item(widgets[i]) if i < len(widgets) else None


def _item(widget):
    it = MagicMock()
    it.widget.return_value = widget
    return it


def test_viewport_range_uses_real_geometry_not_loaded_window():
    """真实视口几何：视口内的批次入选，视口外的（即使已加载）不入选。

    这正是修复点 —— 加载窗口 ``[len-12, len]`` 会把视口外的批次也算进保留范围。
    """
    win = _make_window()
    win._visible_batch_start = 36
    win._visible_batch_end = 48  # 加载窗口 = 最后 12 批
    scroll_bar = win.chat_scroll_area.verticalScrollBar()
    scroll_bar.value.return_value = 1000  # 视口顶部
    # 视口 = [1000, 1600)（value=1000, height=600）
    _patch_layout_geometry(
        win,
        [
            (10, 0, 500),  # [0,499] 视口上方：不入选
            (11, 500, 600),  # [500,1099] 与视口相交：入选
            (12, 1100, 400),  # [1100,1499] 与视口相交：入选
            (13, 1700, 400),  # [1700,2099] 视口下方：不入选
        ],
    )
    rng = win._viewport_batch_range()
    assert rng == (11, 12), f"应只返回真实可见批次，实际 {rng}"


def test_viewport_range_returns_none_when_layout_degenerate():
    """布局未就位（全部零高）→ 返回 None，由调用方回退加载窗口。"""
    win = _make_window()
    _patch_layout_geometry(win, [(0, 0, 0), (1, 0, 0)])
    assert win._viewport_batch_range() is None


def test_viewport_range_falls_back_to_loaded_window():
    """无几何时 `_resolve_viewport_range` 回退加载窗口（兜底不为空）。"""
    win = _make_window()
    win._visible_batch_start = 5
    win._visible_batch_end = 17
    win._viewport_batch_range = lambda: None
    assert win._resolve_viewport_range() == (5, 16)


def test_recycle_keeps_only_viewport_neighborhood():
    """回收范围以真实视口为基准：远离视口的批次必须被卸载。

    修复前：`active_start` 恒为 0、`active_end ≥ len` → 回收范围为空，
    长会话里所有批次同时持有 viewer。
    """
    win = _make_window()
    win._batch_cards = [[_card()] for _ in range(20)]
    win._visible_batch_start = 8
    win._visible_batch_end = 20
    win._viewport_batch_range = lambda: (9, 10)
    win._incremental_visible_batch_count = 8
    win._virtual_scroll_buffer = 1
    win._current_assistant_card = None

    win._recycle_out_of_view_batches()

    unloaded = [i for i, c in enumerate(win._batch_cards) if c is None]
    # buffer = 8 → active = [1, 18]；回收 0 与 19
    assert 0 in unloaded, "远在视口上方的批次必须回收"
    assert 19 in unloaded, "远在视口下方的批次必须回收"
    assert 9 not in unloaded and 10 not in unloaded, "视口内批次不得回收"


# ─── 缺陷 3：候选耗尽时降级淘汰 ─────────────────────────────


def test_streaming_and_welcome_batches_never_eligible():
    """降级候选也排除流式卡与欢迎卡（绝对不淘汰判据）。"""
    win = _make_window()
    win._batch_cards = [[_card()], [_card()], [_card()]]
    win._current_assistant_card = win._batch_cards[0][0]
    win._batch_cards[1][0]._is_welcome = True

    assert win._batch_is_streaming_or_welcome(0) is True
    assert win._batch_is_streaming_or_welcome(1) is True
    assert win._batch_is_streaming_or_welcome(2) is False


def test_lru_recycle_degrades_when_all_batches_protected():
    """全部批次落在可视区 ±1 内时，仍要按距离降级淘汰到配额内。

    修复前：candidates 为空 → 循环不执行 → 计数永久停在超配额状态。

    T42 语义收紧后：降级候选只有 dist > _virtual_buffer_batches()（桩上回退 1）
    才会被真的卸载，dist ≤ buffer 的仍被「卸了立刻被建回」护栏挡住。故视口取
    (3,4)：dist(0)=2 > 1 是唯一可降级批次，dist(5)=0 在视口邻域守住。
    """
    win = _make_window()
    win._batch_cards = [[_card()] for _ in range(6)]
    win._rendered_card_count = 6
    win._effective_max_rendered_cards = lambda: 3
    win._viewport_batch_range = lambda: (3, 4)
    # 真实卡片高度：MagicMock 的 int(height())=1px 会让 _restore_window_batches
    # 的像素累加永远凑不满 need_px → 保护窗口被 limit 顶成全表 → 无候选（假红）
    for batch in win._batch_cards:
        batch[0].height.return_value = 3000
    unloaded = []

    def _fake_unload(idx):
        win._batch_cards[idx] = None
        unloaded.append(idx)
        win._rendered_card_count -= 1
        return 0

    win._unload_batch = _fake_unload
    win._recycle_lru_batches()

    # 视口 = (3,4) → 保护窗口 =1（3000px/批 一步凑满 need_px）→ dist≤1 全保护；
    # dist(0)=2 > buffer=1 → 主候选直接淘汰；降级候选（dist≤1）被护栏 break
    assert unloaded == [0], f"dist>buffer 的最远批应被淘汰，其余守住，实际 {unloaded}"
    assert win._rendered_card_count == 5, "主候选卸完计数回落但仍超配额，降级护栏截停"
    assert win._batch_cards[1] is not None and win._batch_cards[5] is not None


def test_lru_recycle_never_unloads_viewport_batches():
    """降级淘汰不得碰视口内批次（distance == 0 即刻停止）。"""
    win = _make_window()
    win._batch_cards = [[_card()] for _ in range(3)]
    win._rendered_card_count = 3
    win._effective_max_rendered_cards = lambda: 1
    win._viewport_batch_range = lambda: (1, 1)
    unloaded = []

    def _fake_unload(idx):
        win._batch_cards[idx] = None
        unloaded.append(idx)
        win._rendered_card_count -= 1
        return 0

    win._unload_batch = _fake_unload
    win._recycle_lru_batches()

    # 三批距离均为 0（含视口内）→ 降级循环首轮即 break，一个都不卸
    assert unloaded == [], "视口邻域内（distance==0）不得淘汰任何批次"
    assert win._rendered_card_count == 3, "宁可持续超配额，也不卸用户附近的内容"


def test_lru_recycle_noop_within_quota():
    """配额内不动任何批次（原有行为保留）。"""
    win = _make_window()
    win._batch_cards = [[_card()] for _ in range(3)]
    win._rendered_card_count = 3
    win._effective_max_rendered_cards = lambda: 12
    win._unload_batch = MagicMock()

    win._recycle_lru_batches()

    win._unload_batch.assert_not_called()

# ─── 守卫拒绝分支：加载中 / 跳转中必须零卸载（2f22cf81 守卫）────────

def test_loading_history_guard_blocks_unload():
    """守卫 A：历史加载中（`_is_loading_history_batches=True`）必须零卸载。

    加载中布局是中间态：回收器会拆掉视口外的目标批次 → 跳转落点失败
    （真机日志：跳转瞬间连着回收 8 批 → Card not found → 视口留在底部）。
    超配额也不得例外——配额压力由 45s 兜底节拍避让，不在本入口抢跑。
    """
    cards = [[_card(True, idx=i)] for i in range(10)]
    unloaded = []
    win = _make_window(
        _batch_cards=cards,
        _rendered_card_count=10,  # 超 quota 6：证明守卫优先于配额压力
        _effective_max_rendered_cards=lambda: 6,
        _is_loading_history_batches=True,  # 守卫命中
        _is_jump_in_flight=lambda: False,
        _resolve_viewport_range=lambda: (0, 1),
        _batch_is_protected=lambda idx, vp: False,
        _batch_distance=lambda idx, vp: 10 + idx,
        _batch_fully_rendered=lambda cards_: all(
            getattr(c, "_lazy_rendered", False) for c in cards_
        ),
        _batch_is_streaming_or_welcome=lambda idx: False,
        _virtual_buffer_batches=lambda: 2,
    )

    def _fake_unload(idx):
        unloaded.append(idx)
        win._rendered_card_count -= 1
        return 0

    win._unload_batch = _fake_unload

    win._recycle_lru_batches()

    assert unloaded == [], "加载中守卫必须零卸载"
    assert win._rendered_card_count == 10, "计数不得被守卫分支改动"

def test_jump_in_flight_guard_blocks_unload():
    """守卫 B：跳转收敛链未完成（`_is_jump_in_flight=True`）必须零卸载。"""
    cards = [[_card(True, idx=i)] for i in range(10)]
    unloaded = []
    win = _make_window(
        _batch_cards=cards,
        _rendered_card_count=10,  # 超 quota 6：证明守卫优先于配额压力
        _effective_max_rendered_cards=lambda: 6,
        _is_loading_history_batches=False,
        _is_jump_in_flight=lambda: True,  # 守卫命中
        _resolve_viewport_range=lambda: (0, 1),
        _batch_is_protected=lambda idx, vp: False,
        _batch_distance=lambda idx, vp: 10 + idx,
        _batch_fully_rendered=lambda cards_: all(
            getattr(c, "_lazy_rendered", False) for c in cards_
        ),
        _batch_is_streaming_or_welcome=lambda idx: False,
        _virtual_buffer_batches=lambda: 2,
    )

    def _fake_unload(idx):
        unloaded.append(idx)
        win._rendered_card_count -= 1
        return 0

    win._unload_batch = _fake_unload

    win._recycle_lru_batches()

    assert unloaded == [], "跳转中守卫必须零卸载"
    assert win._rendered_card_count == 10, "计数不得被守卫分支改动"

# ─── 池化成功场景：`_unload_batch` 必须按入口态递减（建议 B 测试锚）───

def test_unload_batch_counts_before_detach_pooled_success(monkeypatch):
    """池化成功（detach 置 `_lazy_rendered=False`）不得漏减：4 张 True 卡、
    detach 桩把其中 2 张翻 False，断言递减 **4**（入口态）而非 2（detach 后态）。

    防止后人把统计点挪回 detach 之后——挪回后本用例立即红（递减变 2）。
    pre/post 自检：卸载后强制 recount，pre == post 才算记账与实况一致。
    """
    import app.main_widget as mw

    class _PoolableCard:
        def __init__(self, idx):
            self._message_index = idx
            self._lazy_rendered = True
            self._renderer_pid = 0
            self._height = 300
            self.viewer = None

        def height(self):
            return self._height

    monkeypatch.setattr(mw, "MessageCard", _PoolableCard)
    cards = [_PoolableCard(idx=i) for i in range(4)]
    win = _make_window(
        _batch_cards=[cards],
        _rendered_card_count=4,
        _is_widget_alive=lambda w: True,
        _batch_fully_rendered=lambda cards_: all(
            getattr(c, "_lazy_rendered", False) for c in cards_
        ),
        _batch_slot_gap=lambda n: 0,
        _is_loading_history_batches=False,
        _is_jump_in_flight=lambda: False,
        _is_virtual_recycling=False,
    )
    # 池化语义：前 2 张池化成功（detach 置 False），后 2 张池化失败
    def _detach(card):
        if card is cards[0] or card is cards[1]:
            setattr(card, "_lazy_rendered", False)
        return True

    win.chat_layout = MagicMock()
    win.chat_layout.indexOf.return_value = 0
    win._try_detach_card_viewer = _detach
    win._register_unloaded_pid = MagicMock()
    win._install_batch_placeholder = MagicMock(return_value=True)
    win._last_visible_card_ids = set()
    win._maybe_strong_recycle = MagicMock()
    win._programmatic_scroll = MagicMock()
    win._unload_batch = lambda idx: OpenAIChatToolWindow._unload_batch(win, idx)

    result = win._unload_batch(0)

    assert result == 0, "占位成功时净高度为 0"
    assert win._batch_cards[0] is None
    # 入口态递减：4 张全部计减（含池化成功的 2 张）——统计点若在 detach 后则漏减为 2
    assert win._rendered_card_count == 0, (
        f"池化成功 2 张不得漏减：期望 4-4=0，实际 {win._rendered_card_count}"
    )
    # pre/post 自检：记账与实况一致
    pre = win._rendered_card_count
    win._recount_rendered_cards()
    post = win._rendered_card_count
    assert pre == post, f"记账与实况漂移：pre={pre} post={post}"


# ─── 方向测试：上方卸载递减 + 重建路径记账（双向 pre/post）───────────

def test_above_batches_decrement_on_recycle(monkeypatch):
    """[MEM] 上方（历史方向）批次卸载必须按入口态递减计数。

    修复前上方段只置 None 不递减：卸载后计数虚高不回落 → 配额永不满足 →
    下方批次被殃及。修复后上方卸载即递减：5→2（上方 3 批裁掉，quota 2
    之上的部分回收，视口附近 2 批不受影响）。
    pre/post 自检：recount 后计数与实况一致（上方 3 批置 None 后实况只剩
    下方 2 批，初始计数按实况 5 构造）。
    """
    import app.main_widget as mw

    class _FakeMessageCard:
        def __init__(self, idx):
            self._message_index = idx
            self._lazy_rendered = True
            self._is_welcome = False
            self._height = 100
            self.viewer = None

        def height(self):
            return self._height

    monkeypatch.setattr(mw, "MessageCard", _FakeMessageCard)
    cards_by_batch = [
        [_FakeMessageCard(0)],
        [_FakeMessageCard(1)],
        [_FakeMessageCard(2)],
        None, None, None, None, None,
        [_FakeMessageCard(8)],
        [_FakeMessageCard(9)],
    ]
    win = _make_window(
        _batch_cards=cards_by_batch,
        _rendered_card_count=5,  # 实况 = 上方 3 + 下方 2
        _effective_max_rendered_cards=lambda: 2,
        _is_loading_history_batches=False,
        _is_jump_in_flight=lambda: False,
        _incremental_visible_batch_count=1,
        _virtual_scroll_buffer=1,
        _resolve_viewport_range=lambda: (8, 9),
        _message_batch=[None] * 10,
        _current_assistant_card=None,
        _batch_is_protected=lambda idx, vp: False,
        _batch_distance=lambda idx, vp: 10 + idx,
        _batch_fully_rendered=lambda cards_: all(
            getattr(c, "_lazy_rendered", False) for c in cards_
        ),
        _batch_is_streaming_or_welcome=lambda idx: False,
        _virtual_buffer_batches=lambda: 2,
        _maybe_strong_recycle=MagicMock(),
    )
    win.chat_layout = MagicMock()
    win.chat_layout.indexOf.return_value = 0
    win._try_detach_card_viewer = lambda c: True

    win._recycle_out_of_view_batches()

    # 上方 3 批被卸载（置 None），下方视口批次保留
    assert win._batch_cards[0] is None and win._batch_cards[1] is None and win._batch_cards[2] is None
    assert win._batch_cards[8] is not None and win._batch_cards[9] is not None
    # 入口态递减：5 - 3（上方）= 2
    assert win._rendered_card_count == 2, (
        f"上方卸载必须递减计数：期望 2，实际 {win._rendered_card_count}"
    )
    # pre/post 自检：记账与实况一致
    pre = win._rendered_card_count
    win._recount_rendered_cards()
    post = win._rendered_card_count
    assert pre == post, f"记账与实况漂移：pre={pre} post={post}"
def test_restore_step_counts_new_cards(monkeypatch):
    """[MEM] 重建路径记账：原位重建的新卡必须累加计数。

    回归「重建渲染 6 张只记 3 张」（evidence-scout 实测）：缺口全部来自
    「第 1.5 步原位重建」——\`_render_message_to_card\` 新建卡片写回
    \`_batch_cards\`，但没有任何计数更新 → 计数低估 → 配额恒判「未超限」。
    修复在重建成功后按实况对账（\`_recount_rendered_cards\`）。
    计数流验证：初始 3（上方 3 批）→ 重建 +1 = 4 → 上方批次卸载 -3 = 1。
    pre/post 自检：卸载后 recount，计数与实况一致。
    """
    import app.main_widget as mw

    class _FakeLazyCard:
        def __init__(self, idx):
            self._message_index = idx
            self._lazy_rendered = True
            self._is_welcome = False
            self._height = 100
            self.viewer = None

        def height(self):
            return self._height

        def is_layout_height_settled(self):
            return True  # 已落定（缺此探针 _batch_fully_rendered 判 False 整批跳过）

    monkeypatch.setattr(mw, "MessageCard", _FakeLazyCard)
    cards_by_batch = [
        [_FakeLazyCard(0)],
        [_FakeLazyCard(1)],
        [_FakeLazyCard(2)],
        None,  # batch 3：占位批次（将被原位重建）
    ]
    win = _make_window(
        _batch_cards=cards_by_batch,
        _rendered_card_count=3,  # 上方 3 批已渲染
        _effective_max_rendered_cards=lambda: 99,
        _is_loading_history_batches=False,
        _is_jump_in_flight=lambda: False,
        _incremental_visible_batch_count=0,
        _virtual_scroll_buffer=0,
        _resolve_viewport_range=lambda: (3, 3),
        _message_batch=[None, None, None, [{"role": "assistant", "content": "重建批次的消息"}]],
        _current_assistant_card=None,
        _last_visible_card_ids=set(),
        _virtual_buffer_batches=lambda: 2,
        _maybe_strong_recycle=MagicMock(),
        _recycle_lru_batches=MagicMock(),
    )
    ph = MagicMock()
    ph.height.return_value = 200
    win._batch_placeholders = {3: ph}

    def _fake_render(batch, batch_offset, anchor_layout_index):
        # 模拟 \`_render_message_to_card\` 的产物：新卡片写回批次（已渲染态）
        win._batch_cards[batch_offset] = [_FakeLazyCard(idx=batch_offset)]

    win.chat_layout.indexOf.return_value = 0
    win._render_message_to_card = _fake_render

    decr_calls = []
    real_decr = OpenAIChatToolWindow._decr_rendered_count
    win._decr_rendered_count = lambda n: (decr_calls.append(n), real_decr(win, n))

    win._recycle_out_of_view_batches()

    # 终态：重建 +1（batch3）与上方卸载 -3（batch0-2）在同一次调用内完成 → 1
    assert win._batch_cards[3] is not None, "batch3 占位必须被原位重建"
    assert win._rendered_card_count == 1, (
        f"重建 +1 / 上方卸载 -3 的净效果：期望 1，实际 {win._rendered_card_count}"
    )
    # 「重建的新卡被记账」独立验证：再模拟一次原位重建（batch0 复活 +1 卡），
    # 对账式 recount 应把新卡计入（3→2 之外的新增 → 2）
    win._batch_cards[0] = [_FakeLazyCard(idx=0)]
    win._recount_rendered_cards()
    assert win._rendered_card_count == 2, (
        f"原位重建的新卡必须被对账计入：期望 2，实际 {win._rendered_card_count}"
    )
    # pre/post 自检：recount 幂等（连续两次第二次 diff=0）
    pre = win._rendered_card_count
    win._recount_rendered_cards()
    post = win._rendered_card_count
    assert pre == post, f"记账与实况漂移：pre={pre} post={post}"
