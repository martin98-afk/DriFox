# -*- coding: utf-8 -*-
"""团队框 header hover 按钮占位回归测试

覆盖：
- hover 显示/隐藏三按钮（新建任务/快速新建成员/解散），badge.x / 团队名 x 恒定不横移
  （无占位处理时按钮 hidden→visible 会挤压左侧元素，实测 badge.x 220→148）
- hover 前后 header 高度恒定
- 空闲态无常驻空白（#11：btn_area 只定高不定宽，空闲零占位）
- badge 恒在团队名左侧（#12 用户定案布局）+ 运行/待答文案不裁断
- 折叠态按钮占位容器整体隐藏（C3 窄条无空白）、展开态恢复

风格对齐 tests/widgets/test_tab_panel_team_project_ui.py 的横向 geometry 断言。
"""

from unittest.mock import patch

import pytest
from PyQt5.QtWidgets import QApplication

from app.widgets.tab_panel import TabPanel


@pytest.fixture
def isolated_app_state(tmp_path, monkeypatch):
    """app_state 隔离到 tmp_path（不读写真实缓存文件）"""
    from app.utils import app_state

    monkeypatch.setattr(app_state, "_state_file", lambda: tmp_path / "app_state.json")
    monkeypatch.setattr(app_state, "_cache", None)


@pytest.fixture
def panel(qtbot):
    with patch("app.widgets.cards.settings.gitee_card.GiteeAccountRow._auto_enable_sync"):
        p = TabPanel()
    # 团队框只在列表模式创建，显式 pin（避免本机树模式残留导致 KeyError）
    p.set_mode("list", persist=False)
    qtbot.addWidget(p)
    return p


class TestTeamHoverButtonArea:
    """hover 三按钮占位：显隐不重排"""

    def _setup_team(self, panel, team_id="run_1"):
        idx = panel.add_tab("会话A")
        panel.set_tab_team(idx, team_id)
        grp = panel._team_groups[team_id]
        return idx, grp

    def test_hover_buttons_keep_badge_x_constant(self, panel, qtbot, isolated_app_state):
        """三按钮显隐前后 badge.x / 团队名 x 恒定（badge 在团队名左侧，不参与挤压）"""
        _, grp = self._setup_team(panel)
        panel.show()
        qtbot.waitExposed(panel)
        QApplication.processEvents()

        badge = grp._team_badge
        name = grp._team_name_label
        btn_area = grp._team_btn_area
        assert btn_area is not None and not btn_area.isHidden(), "按钮区展开态应始终可见"
        badge_x_before = badge.geometry().x()
        name_x_before = name.geometry().x()

        # hover 进入：三按钮 hidden→visible
        grp._team_new_task_btn.setVisible(True)
        grp._team_add_btn.setVisible(True)
        grp._team_close_btn.setVisible(True)
        QApplication.processEvents()
        assert badge.geometry().x() == badge_x_before, "hover 显示按钮后 badge 不得横移"
        assert name.geometry().x() == name_x_before, "hover 显示按钮后团队名不得横移"

        # hover 离开：三按钮恢复隐藏
        grp._team_new_task_btn.setVisible(False)
        grp._team_add_btn.setVisible(False)
        grp._team_close_btn.setVisible(False)
        QApplication.processEvents()
        assert badge.geometry().x() == badge_x_before, "hover 隐藏按钮后 badge 不得横移"
        assert name.geometry().x() == name_x_before, "hover 隐藏按钮后团队名不得横移"

    def test_header_height_constant_across_hover(self, panel, qtbot, isolated_app_state):
        """hover 显隐前后 header 高度恒定（容器高度吸收按钮高度）"""
        _, grp = self._setup_team(panel)
        panel.show()
        qtbot.waitExposed(panel)
        QApplication.processEvents()

        header = grp._team_header
        h_before = header.height()
        assert h_before > 0, "布局生效后 header 应有实际高度"

        grp._team_new_task_btn.setVisible(True)
        grp._team_add_btn.setVisible(True)
        grp._team_close_btn.setVisible(True)
        QApplication.processEvents()
        assert header.height() == h_before, "hover 显示按钮后 header 高度不得变化"

        grp._team_new_task_btn.setVisible(False)
        grp._team_add_btn.setVisible(False)
        grp._team_close_btn.setVisible(False)
        QApplication.processEvents()
        assert header.height() == h_before, "hover 隐藏按钮后 header 高度不得变化"

    def test_compact_mode_hides_btn_area(self, panel, qtbot, isolated_app_state):
        """折叠态占位容器整体隐藏（窄条无按钮区空白），展开态恢复"""
        _, grp = self._setup_team(panel)
        panel.show()
        qtbot.waitExposed(panel)

        panel._apply_team_compact(grp, True)
        assert grp._team_btn_area.isHidden(), "折叠态按钮占位容器应隐藏"

        panel._apply_team_compact(grp, False)
        assert not grp._team_btn_area.isHidden(), "展开态按钮占位容器应恢复"

    def test_idle_state_no_persistent_blank(self, panel, qtbot, isolated_app_state):
        """#11/#12 回归：非 hover 空闲态不得有常驻空白区

        #11：btn_area setFixedSize(68,20) 恒占位 → 右侧 68px 空白 + grp 最小宽
        被撑到 154；现只定高不定宽，空闲零占位。
        #12：badge 恒在团队名左侧。
        """
        _, grp = self._setup_team(panel)
        panel.show()
        qtbot.waitExposed(panel)
        QApplication.processEvents()

        badge = grp._team_badge
        name = grp._team_name_label
        btn_area = grp._team_btn_area

        # 空闲态按钮区零宽度（不占位，hover 才展开从标题借空间）
        assert btn_area.width() <= 1, f"空闲态按钮区应零占位，实际 {btn_area.width()}px（#11 常驻空白回归）"
        # grp 最小宽回落：窄侧边栏不再被撑出横向空白
        assert grp.minimumSizeHint().width() <= 120, (
            f"grp 最小宽应回落（回归时 154），实际 {grp.minimumSizeHint().width()}"
        )
        # #12 用户定案：数字在团队名左侧
        assert badge.geometry().x() < name.geometry().x(), (
            f"badge 应在团队名左侧：badge.x={badge.geometry().x()} name.x={name.geometry().x()}"
        )

    def test_badge_text_not_clipped(self, panel, qtbot, isolated_app_state):
        """#12/#13：『N 运行』/『N 待答』文案完整不裁断（旧 30px 定宽裁成『运』）

        断言内宽（width - 2×6 QSS padding）≥ 文本需求宽度——此前误用
        ``badge.width() >= needed``（未减 padding）宽松 12px 掩盖真实裁断。
        """
        _, grp = self._setup_team(panel)
        panel.show()
        qtbot.waitExposed(panel)

        badge = grp._team_badge
        tooltip = ""
        for running_count in (1, 12):
            grp._team_streaming_count = running_count
            grp._team_question_count = 0
            panel._update_team_badge(grp)
            QApplication.processEvents()
            text = badge.text()
            assert text == f"{running_count} 运行", f"运行态文案应为『N 运行』，实际 {text!r}"
            needed = badge.fontMetrics().horizontalAdvance(text)
            inner = badge.width() - 2 * 6  # QSS padding: 1px 6px
            assert inner >= needed, f"badge 内宽 {inner}px 不足以显示 {text!r}（需 {needed}px，外宽 {badge.width()}px）"
            tooltip = badge.toolTip()

        # 待答态同样完整
        grp._team_streaming_count = 0
        grp._team_question_count = 3
        panel._update_team_badge(grp)
        QApplication.processEvents()
        assert badge.text() == "3 待答", f"待答态文案应为『3 待答』，实际 {badge.text()!r}"
        inner = badge.width() - 2 * 6
        needed = badge.fontMetrics().horizontalAdvance("3 待答")
        assert inner >= needed, f"badge 内宽 {inner}px 不足以显示『3 待答』（需 {needed}px）"
        assert tooltip, "badge 应有 tooltip"

    def test_narrow_panel_hover_badge_not_clipped(self, panel, qtbot, isolated_app_state):
        """#13：窄栏 + hover + 长文案组合下 badge 不被压缩裁字

        实测压缩点：panelW≤180（header≤162）时布局压缩 badge——「12 运行」
        73→48 内宽 36 < 需 61，团队名被压光。minimumWidth 钉住后挤压转嫁给
        团队名（_ElidedLabel 有 elide 保护，显示省略号属预期），badge 文案完整。
        """
        _, grp = self._setup_team(panel)
        panel.show()
        qtbot.waitExposed(panel)

        badge = grp._team_badge
        name = grp._team_name_label

        # 长文案（12 运行），再收窄面板至压缩区
        grp._team_streaming_count = 12
        grp._team_question_count = 0
        panel._update_team_badge(grp)
        panel.resize(180, 500)
        QApplication.processEvents()

        # hover 稳态：按钮展开挤压弹性团队名
        grp._team_new_task_btn.setVisible(True)
        grp._team_add_btn.setVisible(True)
        grp._team_close_btn.setVisible(True)
        QApplication.processEvents()
        QApplication.processEvents()

        text = badge.text()
        needed = badge.fontMetrics().horizontalAdvance(text)
        inner = badge.width() - 2 * 6
        assert inner >= needed, (
            f"窄栏 hover 下 badge 被压缩裁字：内宽 {inner}px < 需 {needed}px"
            f"（文案 {text!r}，外宽 {badge.width()}px，header w={grp._team_header.width()}）"
        )
        # 挤压由团队名承担（elide 保护）：不出现负宽/异常
        assert name.width() >= 0, f"团队名宽度异常：{name.width()}"
