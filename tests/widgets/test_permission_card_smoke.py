# -*- coding: utf-8 -*-
"""审批卡真实控件冒烟：样式 / 渲染 / 倒计时 / 菜单

背景：审批卡的样式与渲染此前只用假控件（_FakeWidget）测宿主接线，
真实 PermissionApprovalWidget 的 `_apply_card_style` / `_render` /
`_build_remember_menu` 从未被执行过。一次改样式引用不存在的 Colors token
（REALTIME_WARNING）导致宿主 try 块抛 AttributeError → 兜底 deny，
表现为「卡片不弹、直接拒绝」，而全部测试仍绿。

本文件用真实控件跑这几条路径：任何异常都直接让用例红。
"""

import sys
from pathlib import Path

import pytest
from PyQt5.QtWidgets import QApplication

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


@pytest.fixture(scope="session")
def _app_keepalive():
    return QApplication.instance() or QApplication(sys.argv)


@pytest.fixture(autouse=True)
def _qapp(_app_keepalive):
    yield


def _make_card(risk: str, review_note: str = ""):
    from app.widgets.cards.floating.permission_approval_widget import PermissionApprovalWidget

    card = PermissionApprovalWidget()
    card.show_request(
        tool_name="bash",
        arguments={"command": "rm -rf ./dist"},
        source="delete",
        source_text="删除保护",
        risk=risk,
        workdir="D:/work/DriFox",
        impact={"paths": ["D:/x"], "missing": ["D:/y"], "domains": ["evil.com"], "writes": True},
        preview_payload={"tool_name": "bash", "tool_call_id": "c1", "arguments": {}},
        review_note=review_note,
    )
    return card


@pytest.mark.parametrize("risk", ["info", "warn", "danger"])
def test_card_renders_and_styles_all_risk_levels(risk):
    """三档风险的渲染 + 样式都必须不抛（样式崩溃 = 卡片不弹 + 宿主兜底 deny）"""
    card = _make_card(risk)
    card._apply_card_style()
    card.refresh_style()
    assert card._risk == risk
    assert "rm -rf ./dist" in card._cmd_label.text()


def test_countdown_lifecycle():
    """倒计时启停：徽标显隐 + 文本随阈值降级"""
    card = _make_card("warn")
    assert not card._countdown_label.isVisibleTo(card)

    card.start_countdown(120)
    assert card._countdown_label.isVisibleTo(card)
    assert "120" in card._countdown_label.text()
    assert card._countdown_label.property("level") == "normal"

    card._countdown_left = 30
    card._render_countdown()
    assert card._countdown_label.property("level") == "warn"

    card._countdown_left = 5
    card._render_countdown()
    assert card._countdown_label.property("level") == "danger"
    assert "自动拒绝" in card._countdown_label.text()

    # 减到 0 自动收起，不越界
    card._countdown_left = 1
    card._tick_countdown()
    assert not card._countdown_label.isVisibleTo(card)

    card.start_countdown(10)
    card.stop_countdown()
    assert not card._countdown_label.isVisibleTo(card)


def test_start_countdown_rejects_non_positive():
    """timeout_sec<=0 时不得显示徽标（宿主不超时配置）"""
    card = _make_card("info")
    card.start_countdown(0)
    card.start_countdown(-5)
    assert not card._countdown_label.isVisibleTo(card)


def test_review_note_row_visibility():
    """AI 审核失败原因：非空才显示，且带 ⚠ 前缀独立成行"""
    card = _make_card("warn", review_note="AI 审核失败：模型返回空响应，请人工决策")
    # isVisibleTo：父控件未 show 时子控件 isVisible 恒 False，此处只验显隐标志位
    assert card._review_note_label.isVisibleTo(card)
    assert "模型返回空响应" in card._review_note_label.text()
    assert card._review_note_label.text().startswith("⚠")

    clean = _make_card("warn")
    assert not clean._review_note_label.isVisibleTo(clean)


def test_scope_menu_builds_for_all_risks():
    """作用域菜单：info/warn 3 项，danger 4 项（含禁用的会话级说明行）"""
    info_menu = _make_card("info")._build_remember_menu()
    assert len(info_menu.actions()) == 3

    danger_menu = _make_card("danger")._build_remember_menu()
    assert len(danger_menu.actions()) == 4


def test_danger_blocks_session_scope():
    """danger 档会话级豁免必须被两层拦住：菜单项禁用 + _set_scope 兜底"""
    card = _make_card("danger")
    card._set_scope("session")
    assert card._remember_scope != "session"

    card._set_scope("round")
    assert card._remember_scope == "round"
    assert "当前轮次" in card._scope_btn.text()


def test_allow_button_has_scope_menu_hook():
    """作用域是独立按钮（保持既有交互）：点它切范围，左键「允许」直达放行"""
    card = _make_card("info")
    assert card._scope_btn.isVisibleTo(card)
    assert "当前工具" in card._scope_btn.text()
    assert card._scope_btn.receivers(card._scope_btn.clicked) >= 1
    # 「允许」左键仍是一次点击直达（不因作用域按钮存在而多一步）
    assert card._allow_btn.receivers(card._allow_btn.clicked) >= 1
