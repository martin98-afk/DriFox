# -*- coding: utf-8 -*-
"""T2 回归用例：TabPanel 批量删除后的空组清理（devil-advocate D1 建议 U1）

覆盖 begin_batch_remove/end_batch_remove 两个场景：
1. 批量删除同一团队的所有 tab → end_batch_remove 后空组被清理
   （_maybe_remove_empty_group 生效）、布局结构正确、无残留 widget
2. 批量删除部分 tab（非全删）→ 组保留、_item_team 映射正确、逆序删除索引正确

断言基于 tab_panel.py 当前工作树实现（只读，不改实现）。
"""

from unittest.mock import patch

import pytest

from app.widgets.tab_panel import TabPanel


@pytest.fixture
def panel(qtbot):
    with patch("app.widgets.cards.settings.gitee_card.GiteeAccountRow._auto_enable_sync"):
        p = TabPanel()
    qtbot.addWidget(p)
    return p


def _layout_widgets(panel) -> list:
    """列出 _list_layout 中所有 widget（不含 stretch）"""
    return [
        panel._list_layout.itemAt(i).widget()
        for i in range(panel._list_layout.count())
        if panel._list_layout.itemAt(i).widget() is not None
    ]


class TestBatchRemoveEmptyGroup:
    def test_batch_remove_all_cleans_empty_group(self, panel):
        """批量删除同一团队全部 tab → 空组清理 + 布局正确 + 无残留 widget。"""
        # 组队：tab0/tab1/tab2 属于 team-A
        for i in range(3):
            panel.add_tab(f"tab-{i}")
        panel.set_tab_team(0, "team-A")
        panel.set_tab_team(1, "team-A")
        panel.set_tab_team(2, "team-A")
        grp = panel._team_groups.get("team-A")
        assert grp is not None
        assert grp in _layout_widgets(panel), "team 容器应在布局中"

        # 批量删除全部 3 个 tab（逆序删除避免索引漂移）
        panel.begin_batch_remove()
        with patch.object(panel, "_rebuild_team_layout", wraps=panel._rebuild_team_layout) as m:
            for idx in (2, 1, 0):
                panel.remove_tab(idx)
            # 批量期间不逐次重建
            assert m.call_count == 0
            panel.end_batch_remove()
            # 结束后统一重建一次
            assert m.call_count == 1

        # 空组应被清理
        assert "team-A" not in panel._team_groups, "空组应被 _maybe_remove_empty_group 清理"
        # 无残留 widget：布局中应只剩 stretch（无 team 容器、无 tab）
        known = [w for w in _layout_widgets(panel) if w is not None]
        assert len(known) == 0, f"布局不应残留 widget: {known}"
        # _items / _item_team 已清空
        assert panel.count == 0
        assert panel._item_team == {}

    def test_batch_remove_partial_keeps_group(self, panel):
        """批量删除部分 tab（非全删）→ 组保留 + 映射正确 + 逆序删除索引正确。"""
        for i in range(4):
            panel.add_tab(f"tab-{i}")
        # 组队：tab0/tab1 → team-A；tab2/tab3 → team-B
        panel.set_tab_team(0, "team-A")
        panel.set_tab_team(1, "team-A")
        panel.set_tab_team(2, "team-B")
        panel.set_tab_team(3, "team-B")

        # 批量删除 team-B 的两个 tab（逆序：3, 2）
        panel.begin_batch_remove()
        panel.remove_tab(3)
        panel.remove_tab(2)
        panel.end_batch_remove()

        # team-B 空组被清理；team-A 组保留
        assert "team-B" not in panel._team_groups, "team-B 空组应被清理"
        assert "team-A" in panel._team_groups, "team-A 有成员，组应保留"
        # 剩余 tab：tab0/tab1（team-A）
        assert panel.count == 2
        assert panel._item_team == {0: "team-A", 1: "team-A"}, f"映射错误: {panel._item_team}"
        # team-A 容器内仍有 2 个成员 tab
        grp = panel._team_groups["team-A"]
        inner = getattr(grp, "_team_inner_layout", None)
        if inner is None:
            inner = grp.layout()
        member_widgets = [inner.itemAt(i).widget() for i in range(inner.count())]
        member_widgets = [w for w in member_widgets if w is not None]
        assert len(member_widgets) == 2, f"team-A 应保留 2 个成员: {len(member_widgets)}"

    def test_batch_remove_partial_then_add_back(self, panel):
        """部分删除后重新 add + 组队 → 布局仍正确（组容器复用）。"""
        for i in range(3):
            panel.add_tab(f"tab-{i}")
        panel.set_tab_team(0, "team-A")
        panel.set_tab_team(1, "team-A")
        panel.set_tab_team(2, "team-A")

        # 删除中间一个（tab1，索引 1）
        panel.begin_batch_remove()
        panel.remove_tab(1)
        panel.end_batch_remove()

        assert panel.count == 2
        assert panel._item_team == {0: "team-A", 1: "team-A"}
        # 重新添加 tab 并归入 team-A
        new_idx = panel.add_tab("tab-new")
        assert new_idx == 2
        panel.set_tab_team(2, "team-A")
        assert panel._item_team == {0: "team-A", 1: "team-A", 2: "team-A"}
        grp = panel._team_groups.get("team-A")
        assert grp is not None, "team-A 组应存在"
        assert grp in _layout_widgets(panel)

    def test_batch_remove_single_group_mixed_with_independent(self, panel):
        """团队 tab 与独立 tab 混合：批量删除团队 tab 不影响独立 tab。"""
        for i in range(4):
            panel.add_tab(f"tab-{i}")
        # tab0/tab1 → team-A；tab2/tab3 独立
        panel.set_tab_team(0, "team-A")
        panel.set_tab_team(1, "team-A")

        panel.begin_batch_remove()
        panel.remove_tab(1)
        panel.remove_tab(0)
        panel.end_batch_remove()

        # team-A 空组清理；独立 tab 保留
        assert "team-A" not in panel._team_groups
        assert panel.count == 2
        # 独立 tab 是原来 tab2/tab3（现在索引 0/1），无团队归属
        assert panel._item_team == {0: "", 1: ""}, f"独立 tab 不应有团队归属: {panel._item_team}"

    def test_batch_remove_hides_group_before_delete(self, panel, monkeypatch):
        """tooltip 兜底：空组清理时 grp 先隐藏再 deleteLater

        团队关闭时 close_btn 等子控件若仍显示在屏幕上（deleteLater 是延迟
        销毁），hover 的 tooltip 会残留。_maybe_remove_empty_group 在
        deleteLater 前主动 hide() → 子控件收 HideToParent（27）→
        _HoverTooltipFilter 立即收起 tooltip。
        """
        for i in range(2):
            panel.add_tab(f"tab-{i}")
        panel.set_tab_team(0, "team-A")
        panel.set_tab_team(1, "team-A")
        grp = panel._team_groups.get("team-A")
        assert grp is not None

        # 记录 hide 调用（deleteLater 前应先 hide）
        hide_calls = []
        orig_hide = grp.hide

        def _tracked_hide():
            hide_calls.append(True)
            return orig_hide()

        monkeypatch.setattr(grp, "hide", _tracked_hide)

        panel.begin_batch_remove()
        panel.remove_tab(1)
        panel.remove_tab(0)
        panel.end_batch_remove()

        assert "team-A" not in panel._team_groups, "空组应被清理"
        assert hide_calls, "空组清理应先 hide 再 deleteLater（tooltip 兜底）"
