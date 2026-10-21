# -*- coding: utf-8 -*-
"""HookEditCard matcher 文本保护测试

回归测试（07-07）：
新增 Stop reason toggle 时引入的 bug：EditCard.__init__ 末尾调用
_sync_matcher_text_from_checks()，会把用户原本手动输入的 #team_member 等
非预定义 matcher 值静默清空成空字符串。

修复：
1. _sync_matcher_text_from_checks 在所有 toggle 都 unchecked 时跳过同步
2. #team_member 加入 SESSION_STATES/STOP_REASONS 作为 UI 选项，与其他互斥
"""
import os
import sys

# 必须在创建 QApplication 前设置 Qt 属性 + 导入 WebEngineWidgets
from PyQt5.QtCore import Qt
Qt.AA_ShareOpenGLContexts
try:
    from PyQt5.QtWebEngineWidgets import QWebEnginePage  # noqa: F401
except Exception:
    pass


def _ensure_qapp():
    from PyQt5.QtWidgets import QApplication
    return QApplication.instance() or QApplication(sys.argv)


def _make_edit_card(hook_data):
    """构造一个 HookEditCard 实例（无需保存按钮信号）"""
    from app.widgets.cards.settings.hook_setting_card import HookEditCard
    return HookEditCard(hook_data=hook_data)


# ───────────────────── 回归测试：#team_member 不被清空 ─────────────────────


def test_team_member_matcher_preserved_on_stop_event():
    """Stop 事件下加载 matcher='#team_member'，不应被静默清空"""
    _ensure_qapp()
    card = _make_edit_card({
        "id": "test_team_member",
        "type": "prompt",
        "prompt": "test prompt",
        "matcher": "#team_member",
        "_event": "Stop",
        "_source_type": "plugin",
        "_display_name": "test",
    })
    assert card.matcherEdit.text() == "#team_member", (
        f"加载 #team_member 后被清空，bug 复发！当前值: {card.matcherEdit.text()!r}"
    )


def test_team_member_matcher_preserved_on_session_start_event():
    """SessionStart 事件下加载 matcher='#team_member'，不应被静默清空"""
    _ensure_qapp()
    card = _make_edit_card({
        "id": "test_team_member_ss",
        "type": "prompt",
        "prompt": "test prompt",
        "matcher": "#team_member",
        "_event": "SessionStart",
        "_source_type": "plugin",
        "_display_name": "test",
    })
    assert card.matcherEdit.text() == "#team_member"


def test_empty_matcher_preserved():
    """空 matcher 不应被同步成其他值"""
    _ensure_qapp()
    card = _make_edit_card({
        "id": "test_empty",
        "type": "prompt",
        "prompt": "test prompt",
        "matcher": "",
        "_event": "Stop",
    })
    assert card.matcherEdit.text() == ""


# ───────────────────── 正常功能：toggle 勾选/取消仍然工作 ─────────────────────


def test_toggle_check_writes_matcher_text():
    """勾选 toggle 后，matcherEdit 应同步显示对应文本"""
    _ensure_qapp()
    card = _make_edit_card({
        "id": "test_toggle",
        "type": "prompt",
        "prompt": "test prompt",
        "matcher": "",
        "_event": "Stop",
    })
    # 找到 completed toggle 并勾选
    completed_cb = None
    for cb, opt in card._matcher_checkboxes:
        if opt == "completed":
            completed_cb = cb
            break
    assert completed_cb is not None, "Stop 事件下应生成 completed toggle"

    completed_cb.setChecked(True)
    assert "completed" in card.matcherEdit.text()

    # 取消勾选 → matcherEdit 应保留之前的文本（不被清空）
    completed_cb.setChecked(False)
    assert "completed" not in card.matcherEdit.text(), (
        "取消 toggle 后 matcherEdit 仍含 completed，说明保护逻辑未生效"
    )


def test_multiple_toggles_pipe_joined():
    """勾选多个 toggle 后，matcherEdit 显示 | 分隔文本"""
    _ensure_qapp()
    card = _make_edit_card({
        "id": "test_multi_toggle",
        "type": "prompt",
        "prompt": "test prompt",
        "matcher": "",
        "_event": "Stop",
    })
    for cb, opt in card._matcher_checkboxes:
        if opt in ("completed", "error"):
            cb.setChecked(True)

    text = card.matcherEdit.text()
    parts = text.split("|")
    assert "completed" in parts
    assert "error" in parts
    assert "cancelled" not in parts


# ───────────────────── 新建卡片场景仍工作 ─────────────────────


def test_new_card_with_stop_event_works():
    """新建卡片（无 hook_data）在 Stop 事件下应正常显示 4 个 toggle（含 #team_member）"""
    _ensure_qapp()
    card = _make_edit_card(None)  # 新建
    # 默认事件可能是其他，需要手动切到 Stop
    card.eventCombo.setCurrentText("Stop")
    options = [opt for _, opt in card._matcher_checkboxes]
    assert "#team_member" in options, f"Stop toggle 列表应含 #team_member，实际: {options}"
    assert "completed" in options
    assert "cancelled" in options
    assert "error" in options


# ───────────────────── 新增：#team_member toggle 与其他选项互斥 ─────────────────────


def test_team_member_toggle_appears_in_stop():
    """Stop 事件下应生成 #team_member toggle"""
    _ensure_qapp()
    card = _make_edit_card({
        "id": "test_tm_stop",
        "type": "prompt",
        "prompt": "test",
        "matcher": "",
        "_event": "Stop",
    })
    options = [opt for _, opt in card._matcher_checkboxes]
    assert "#team_member" in options
    assert options == ["completed", "cancelled", "error", "#team_member"]


def test_team_member_toggle_appears_in_sessionstart():
    """SessionStart 事件下应生成 #team_member toggle"""
    _ensure_qapp()
    card = _make_edit_card({
        "id": "test_tm_ss",
        "type": "prompt",
        "prompt": "test",
        "matcher": "",
        "_event": "SessionStart",
    })
    options = [opt for _, opt in card._matcher_checkboxes]
    assert "#team_member" in options
    assert options == ["startup", "resume", "clear", "compact", "#team_member"]


def test_team_member_toggle_does_not_appear_in_pretooluse():
    """PreToolUse 事件下不应有 #team_member toggle（只在 Stop/SessionStart 中）"""
    _ensure_qapp()
    card = _make_edit_card({
        "id": "test_tm_pto",
        "type": "command",
        "command": "echo",
        "matcher": "",
        "_event": "PreToolUse",
    })
    options = [opt for _, opt in card._matcher_checkboxes]
    assert "#team_member" not in options


def test_team_member_and_other_toggle_are_mutually_exclusive():
    """勾选 #team_member 时，其他选项应自动取消（互斥）"""
    _ensure_qapp()
    card = _make_edit_card({
        "id": "test_mutual_excl",
        "type": "prompt",
        "prompt": "test",
        "matcher": "",
        "_event": "Stop",
    })
    # 找到 completed 和 #team_member toggle
    completed_cb = None
    team_member_cb = None
    for cb, opt in card._matcher_checkboxes:
        if opt == "completed":
            completed_cb = cb
        elif opt == "#team_member":
            team_member_cb = cb
    assert completed_cb is not None and team_member_cb is not None

    # 1) 先勾选 completed
    completed_cb.setChecked(True)
    assert card.matcherEdit.text() == "completed"
    assert not team_member_cb.isChecked()

    # 2) 再勾选 #team_member → completed 应自动取消
    team_member_cb.setChecked(True)
    assert card.matcherEdit.text() == "#team_member", (
        f"勾选 #team_member 后 matcherEdit 应是 #team_member，实际: {card.matcherEdit.text()!r}"
    )
    assert not completed_cb.isChecked(), "completed 应被互斥取消"


def test_other_toggle_unchecks_team_member():
    """勾选非 #team_member 选项时，#team_member 应自动取消（互斥反向）"""
    _ensure_qapp()
    card = _make_edit_card({
        "id": "test_mutual_excl2",
        "type": "prompt",
        "prompt": "test",
        "matcher": "",
        "_event": "Stop",
    })
    completed_cb = None
    cancelled_cb = None
    team_member_cb = None
    for cb, opt in card._matcher_checkboxes:
        if opt == "completed":
            completed_cb = cb
        elif opt == "cancelled":
            cancelled_cb = cb
        elif opt == "#team_member":
            team_member_cb = cb

    # 先勾选 #team_member
    team_member_cb.setChecked(True)
    assert card.matcherEdit.text() == "#team_member"

    # 再勾选 cancelled → #team_member 应自动取消
    cancelled_cb.setChecked(True)
    assert "cancelled" in card.matcherEdit.text()
    assert "#team_member" not in card.matcherEdit.text(), (
        "勾选 cancelled 后 matcherEdit 不应含 #team_member"
    )
    assert not team_member_cb.isChecked(), "#team_member 应被互斥取消"


def test_loading_existing_team_member_matcher_shows_toggle_checked():
    """加载已有 matcher='#team_member' 的 hook，#team_member toggle 应被勾选"""
    _ensure_qapp()
    card = _make_edit_card({
        "id": "test_load_tm",
        "type": "prompt",
        "prompt": "test",
        "matcher": "#team_member",
        "_event": "Stop",
    })
    # matcherEdit 文本保持
    assert card.matcherEdit.text() == "#team_member"
    # #team_member toggle 应被勾选
    team_member_cb = next(cb for cb, opt in card._matcher_checkboxes if opt == "#team_member")
    assert team_member_cb.isChecked(), "加载 #team_member 时 toggle 应被勾选"


def test_loading_other_matcher_does_not_check_team_member_toggle():
    """加载 matcher='completed' 时，#team_member toggle 不应被勾选（互斥）"""
    _ensure_qapp()
    card = _make_edit_card({
        "id": "test_load_completed",
        "type": "prompt",
        "prompt": "test",
        "matcher": "completed",
        "_event": "Stop",
    })
    assert card.matcherEdit.text() == "completed"
    team_member_cb = next(cb for cb, opt in card._matcher_checkboxes if opt == "#team_member")
    assert not team_member_cb.isChecked()