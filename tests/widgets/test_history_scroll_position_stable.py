# -*- coding: utf-8 -*-
"""回归测试：点击列表下方记录后滚动位置保持（不跳回上方）。

== 问题描述 ==
「加载更多」展开到几十上百条后，滚动到列表下方点击某条记录，视口会跳回上方。
实测事件序列（滚动条 value/max）：

    ('range', 0,  660, 3767)   ← 内容瞬塌，value 被 Qt clamp
    ('value',  660,  660)      ← 不可逆，之后 range 恢复也不再弹回
    ('range', 0, 2070,  660)
    ('range', 0, 3855,  660)

触发链：点击条目 → sessionClicked → 窗口 _on_history_session_selected →
_load_session_from_record 末尾 _notify_history_data_changed →
_refresh_history_page_if_active → page.refresh → card.set_history → _update_display
→ _clear_content 把条目全部摘出布局 → 内容高度塌到 viewport 高 → QScrollArea 的
range 上限随之缩小 → QScrollBar 自动把 value clamp 到新上限。

列表越深（加载更多之后）落差越大；未展开分页时列表短、本来没滚多远，故无感。

== 修复（两层）==
1. **快路径**：`set_history` 比对「渲染形态」签名（`_history_signature`），一致时
   只就地换当前会话高亮（`_sync_current_highlight`），不重建布局 → range 全程不变，
   value 天然不动。点击记录时列表数据通常不变（只有 current_session 归属变化）。
2. **锚点兜底**：列表形态真变（旧会话被保存后浮到头部、新会话插入等）时仍走全量
   重建，此时 `_capture_scroll_anchor` 在清空前把内容最小高度钉在现值，重建期间
   range 不塌，value 不被 clamp；渲染收尾 `_release_scroll_anchor` 放开。
"""

import importlib
import time
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_CARD = _ROOT / "plugins" / "history-manager" / "ui" / "history_card.py"

_SESSION_COUNT = 100
_PAGE = 30


def _load_page_module():
    """按文件路径导入 history_page（插件目录带连字符，不能走普通 import）"""
    return importlib.import_module("plugins.history-manager.ui.history_page")


def _pump(qapp, ms: int = 400) -> None:
    """跑事件循环（分批渲染靠 QTimer，必须真实推进时间）"""
    t0 = time.time()
    while time.time() - t0 < ms / 1000:
        qapp.processEvents()
        time.sleep(0.004)


def _sessions(n: int = _SESSION_COUNT):
    """构造轻量会话列表（字段对齐 get_history_list 轻量条目）。

    last_time 取同一天：日期分组只有一组，渲染顺序 = 列表顺序，首屏
    （``_show_limit`` 条）边界可预测；否则多日期分组会让首屏落在哪个
    分组不确定，条目在不在缓存里都变成赌运气。
    """
    return [
        {
            "session_id": f"s{i:03d}",
            "title": f"会话 {i}",
            "last_time": "2026-09-19 10:00:00",
            "message_count": i,
            "project": "默认项目",
            "preview": "x" * 40,
        }
        for i in range(n)
    ]


def _make_page(qapp, current_index=None):
    """构造已渲染首页的 HistoryPage；几何不可用时返回 None（无显示环境）"""
    mod = _load_page_module()
    page = mod.HistoryPage()
    page.resize(320, 700)
    page.show()
    _pump(qapp, 200)
    card = page._card
    card.set_history(_sessions(), current_index)
    _pump(qapp, 400)
    sb = page._scroll_area.verticalScrollBar()
    if sb.maximum() <= 0:
        page.hide()
        page.setParent(None)
        page.deleteLater()
        return None
    return page


def _teardown(page) -> None:
    page.hide()
    page.setParent(None)
    page.deleteLater()


def _scroll_bottom(page, qapp):
    sb = page._scroll_area.verticalScrollBar()
    sb.setValue(sb.maximum())
    _pump(qapp, 120)
    return sb


# ═══════════════════════════════════════════════════════════════
# 1. 核心回归：点击下方记录不跳顶
# ═══════════════════════════════════════════════════════════════


def test_click_below_after_load_more_keeps_scroll(qapp):
    """加载 2 页后滚到底部点击下方记录：滚动值保持（本 bug 主场景）"""
    page = _make_page(qapp)
    if page is None:
        pytest.skip("无显示环境，几何不可用")
    try:
        card = page._card
        card._on_load_more()
        _pump(qapp, 350)
        card._on_load_more()
        _pump(qapp, 350)
        sb = _scroll_bottom(page, qapp)
        before = sb.value()
        assert before > 1000, "前置条件：列表应已有可观滚动空间"

        # 点击第 25 条 → 页面 refresh 传回同一份列表 + 新 current_index
        card.set_history(_sessions(), 25)
        _pump(qapp, 700)

        assert sb.value() == before, f"滚动位置被改动：{before} -> {sb.value()}"
    finally:
        _teardown(page)


def test_click_records_uses_fast_path(qapp, monkeypatch):
    """数据不变时走快路径：不触发 _clear_content（布局零重建）"""
    page = _make_page(qapp)
    if page is None:
        pytest.skip("无显示环境，几何不可用")
    try:
        card = page._card
        rebuilds: list = []
        original = type(card)._clear_content
        monkeypatch.setattr(
            type(card),
            "_clear_content",
            lambda self: (rebuilds.append(1), original(self))[1],
        )

        card.set_history(_sessions(), 42)
        _pump(qapp, 300)

        assert rebuilds == [], "列表形态未变时不应重建布局"
    finally:
        _teardown(page)


def test_highlight_moves_to_clicked_record(qapp):
    """快路径下高亮确实换到新当前会话（功能不能被性能优化吃掉）"""
    page = _make_page(qapp, current_index=0)
    if page is None:
        pytest.skip("无显示环境，几何不可用")
    try:
        card = page._card
        assert card._cached_cards["s000"]._is_current is True, "初始当前会话应在缓存中"

        card.set_history(_sessions(), 5)
        _pump(qapp, 300)

        assert card._cached_cards["s005"]._is_current is True
        assert card._cached_cards["s000"]._is_current is False
    finally:
        _teardown(page)


# ═══════════════════════════════════════════════════════════════
# 2. 快路径不得过度命中（列表形态真变必须重建）
# ═══════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "mutate, label",
    [
        (lambda d: d.pop(), "列表少一条"),
        (lambda d: d[3].update(pinned=True), "置顶状态翻转"),
        (lambda d: d[10].update(title="改名了"), "标题变化影响条目身份"),
    ],
)
def test_structural_change_still_rebuilds(qapp, monkeypatch, mutate, label):
    """影响渲染形态的字段变化 → 必须全量重建（否则列表停在旧内容）"""
    page = _make_page(qapp)
    if page is None:
        pytest.skip("无显示环境，几何不可用")
    try:
        card = page._card
        rebuilds: list = []
        original = type(card)._clear_content
        monkeypatch.setattr(
            type(card),
            "_clear_content",
            lambda self: (rebuilds.append(1), original(self))[1],
        )

        data = _sessions()
        mutate(data)
        card.set_history(data, 0)
        _pump(qapp, 400)

        assert rebuilds, f"{label} 未触发重建，列表会停留在旧内容"
    finally:
        _teardown(page)


def test_preview_and_count_change_still_fast_path(qapp, monkeypatch):
    """纯内容字段（preview/message_count）变化不该重建布局（改由缓存卡原地刷新）"""
    page = _make_page(qapp)
    if page is None:
        pytest.skip("无显示环境，几何不可用")
    try:
        card = page._card
        rebuilds: list = []
        original = type(card)._clear_content
        monkeypatch.setattr(
            type(card),
            "_clear_content",
            lambda self: (rebuilds.append(1), original(self))[1],
        )

        data = _sessions()
        data[30]["preview"] = "更新后的预览"
        data[30]["message_count"] = 999
        card.set_history(data, 30)
        _pump(qapp, 300)

        assert rebuilds == [], "preview/message_count 属内容字段，不应触发重建"
    finally:
        _teardown(page)


# ═══════════════════════════════════════════════════════════════
# 3. 锚点兜底：重建路径也不跳顶
# ═══════════════════════════════════════════════════════════════


def test_rebuild_path_keeps_scroll_position(qapp):
    """列表形态真变（走全量重建）时，视口位置仍保持（锚点钉高生效）"""
    page = _make_page(qapp)
    if page is None:
        pytest.skip("无显示环境，几何不可用")
    try:
        card = page._card
        card._on_load_more()
        _pump(qapp, 350)
        card._on_load_more()
        _pump(qapp, 350)
        sb = _scroll_bottom(page, qapp)
        before = sb.value()

        # 旧会话被保存 → last_time 更新并浮到头部（列表顺序真变 → 全量重建）
        data = _sessions()
        old = next(s for s in data if s["session_id"] == "s000")
        old["last_time"] = "2026-09-19 12:00:00"
        data.sort(key=lambda s: s["last_time"], reverse=True)
        target = data.index(next(s for s in data if s["session_id"] == "s087"))

        card.set_history(data, target)
        _pump(qapp, 800)

        assert sb.value() == before, f"重建后视口位置被改动：{before} -> {sb.value()}"
    finally:
        _teardown(page)


def test_anchor_released_after_render(qapp):
    """渲染收尾必须放开钉住的最小高度（否则列表后续无法自然滚动）"""
    page = _make_page(qapp)
    if page is None:
        pytest.skip("无显示环境，几何不可用")
    try:
        card = page._card
        card._on_load_more()
        _pump(qapp, 400)
        data = _sessions()
        data.pop()  # 触发全量重建
        card.set_history(data, 0)
        _pump(qapp, 800)

        assert card._scroll_anchor_widget is None, "锚点字段应在释放后清空"
        assert page._content_widget.minimumHeight() == 0, "内容最小高度应已放开"
    finally:
        _teardown(page)


# ═══════════════════════════════════════════════════════════════
# 4. 源码守卫（防快路径被误删/误改语义）
# ═══════════════════════════════════════════════════════════════


def test_source_guards():
    """关键实现点在源码中稳定存在（重命名/误删即失败）"""
    src = _CARD.read_text(encoding="utf-8")
    for token in (
        "def _history_signature",
        "def _sync_current_highlight",
        "def _capture_scroll_anchor",
        "def _release_scroll_anchor",
        "self._rendered_signature = None",
    ):
        assert token in src, f"缺失关键实现：{token}"
