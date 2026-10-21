# -*- coding: utf-8 -*-
"""权限审批卡（PermissionApprovalWidget）：结构化决策 + 安全默认 + 渲染完整性

关键约束：
- 本测试**不构造 QWebEngineView**（本机测试环境实例化该控件会 AV 崩溃，环境级问题）
- 不依赖 pytest-qt：用 conftest 的 `qapp` fixture（进程级 QApplication）
"""

import sys
import time
from pathlib import Path

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QApplication

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def _ensure_app():
    return QApplication.instance() or QApplication(sys.argv)


_UNSET = object()


def _make_card(
    tool_name="bash",
    arguments=None,
    source="sandbox",
    source_text="⚠ 安全中心拦截",
    risk="info",
    workdir="D:/work/DriFox",
    impact=None,
    preview_payload=_UNSET,
):
    """构造并填充一张审批卡（不 show，避免窗口管理器依赖）

    preview_payload 用 sentinel 区分「未传参（默认给一个载荷）」与
    「显式传 None（无载荷，预览按钮应隐藏）」。
    """
    from app.widgets.cards.floating.permission_approval_widget import PermissionApprovalWidget

    card = PermissionApprovalWidget()
    card.show_request(
        tool_name=tool_name,
        arguments=arguments if arguments is not None else {"command": "rm -rf build"},
        source=source,
        source_text=source_text,
        risk=risk,
        workdir=workdir,
        impact=impact if impact is not None else {"paths": ["D:/work/DriFox/build"], "writes": True},
        preview_payload={"tool_call_id": "call_x"} if preview_payload is _UNSET else preview_payload,
    )
    return card


def _collect(card):
    """收集 answered 信号（返回列表，元素为 (decision, remember, reason)）"""
    got = []
    card.answered.connect(lambda d, r, s: got.append((d, r, s)))
    return got


# ── 一、结构化决策回传 ──


def test_decision_allow_emits_structured(qapp):
    """点允许（默认作用域=当前工具）→ answered("allow", "tool", "")"""
    card = _make_card()
    got = _collect(card)
    card._allow_btn.click()
    assert got == [("allow", "tool", "")]


def test_remember_round_emits_scope(qapp):
    """作用域选「当前轮次」后点允许 → ("allow", "round", "")"""
    card = _make_card()
    got = _collect(card)
    card._set_scope("round")
    card._allow_btn.click()
    assert got == [("allow", "round", "")]


def test_remember_session_emits_scope(qapp):
    """作用域选「当前会话」后点允许 → ("allow", "session", "")"""
    card = _make_card()
    got = _collect(card)
    card._set_scope("session")
    card._allow_btn.click()
    assert got == [("allow", "session", "")]


def test_scope_selector_defaults_and_labels(qapp):
    """作用域选择器：默认当前工具，选中什么按钮显示什么；选择不执行"""
    card = _make_card()
    got = _collect(card)
    assert card._remember_scope == "tool"
    assert "当前工具" in card._scope_btn.text()
    card._set_scope("session")
    assert "当前会话" in card._scope_btn.text()
    card._set_scope("round")
    assert "当前轮次" in card._scope_btn.text()
    # 仅选择不产生决策
    assert got == []
    # 非法作用域被拒
    card._set_scope("bogus")
    assert card._remember_scope == "round"


def test_deny_button_emits_deny(qapp):
    card = _make_card()
    got = _collect(card)
    card._deny_btn.click()
    assert got == [("deny", "", "")]


def test_deny_with_reason_carries_text(qapp, monkeypatch):
    """右键「拒绝并说明原因」→ reason 随决策回传"""
    card = _make_card()
    got = _collect(card)

    monkeypatch.setattr(
        "app.widgets.cards.floating.permission_approval_widget.QInputDialog.getText",
        staticmethod(lambda *a, **k: ("别删这个目录", True)),
    )
    card._on_deny_with_reason(None)
    assert got == [("deny", "", "别删这个目录")]


def test_deny_with_reason_cancelled_no_emit(qapp, monkeypatch):
    """理由输入框取消 → 不产生任何决策"""
    card = _make_card()
    got = _collect(card)
    monkeypatch.setattr(
        "app.widgets.cards.floating.permission_approval_widget.QInputDialog.getText",
        staticmethod(lambda *a, **k: ("", False)),
    )
    card._on_deny_with_reason(None)
    assert got == []


def test_no_duplicate_emit_after_first_decision(qapp):
    """首次决策后不再 emit（防按钮与 Esc 竞争重复回传）"""
    card = _make_card()
    got = _collect(card)
    card._allow_btn.click()
    card._deny_btn.click()
    card._decide("deny", "")
    assert got == [("allow", "tool", "")]


# ── 二、安全默认 ──


def test_esc_denies(qapp):
    """Esc → 拒绝（不是允许）"""
    card = _make_card()
    got = _collect(card)
    card._on_deny()
    assert got == [("deny", "", "")]


def test_close_emits_cancelled_only(qapp):
    """关闭路径 → 只发 cancelled（宿主据此走 deny），不发 answered

    不再同时 emit answered("deny")：那会让宿主第二次进入决策槽时 id 已空，
    打出误导性 WARNING（决策本身正确）。deny 语义由 cancelled 单通道承载。
    """
    from PyQt5.QtGui import QCloseEvent

    card = _make_card()
    got = _collect(card)
    cancelled = []
    card.cancelled.connect(lambda: cancelled.append(True))
    card.closeEvent(QCloseEvent())
    assert cancelled == [True], "关闭必须触发 cancelled"
    assert got == [], "关闭不得再发 answered（避免宿主重复收尾 + 误导 WARNING）"


def test_close_after_decision_is_noop(qapp):
    """已做决策后再关闭 → 不再发 cancelled（防重复）"""
    from PyQt5.QtGui import QCloseEvent

    card = _make_card()
    got = _collect(card)
    cancelled = []
    card.cancelled.connect(lambda: cancelled.append(True))
    card._allow_btn.click()
    card.closeEvent(QCloseEvent())
    assert got == [("allow", "tool", "")]
    assert cancelled == [], "已决策后关闭不应再发 cancelled"


def test_default_focus_is_deny_info_risk(qapp):
    """info 档位：显示后焦点在拒绝按钮（安全默认）"""
    card = _make_card(risk="info")
    card.show()
    qapp.processEvents()
    card._focus_deny()
    assert card._deny_btn.hasFocus(), "默认焦点必须是拒绝按钮"


def test_default_focus_is_deny_danger_risk(qapp):
    """danger 档位同样聚焦拒绝"""
    card = _make_card(risk="danger")
    card.show()
    qapp.processEvents()
    card._focus_deny()
    assert card._deny_btn.hasFocus()
    card.close()


def test_keyboard_enter_allows(qapp):
    """Enter → 允许（非 danger 档位）"""
    from PyQt5.QtGui import QKeyEvent

    card = _make_card(risk="info")
    got = _collect(card)
    card.keyPressEvent(QKeyEvent(QKeyEvent.KeyPress, Qt.Key_Return, Qt.NoModifier))
    assert got == [("allow", "tool", "")]


def test_space_on_focused_deny_denies(qapp):
    """焦点在拒绝按钮时按空格 → 拒绝（"焦点给拒绝"的实际价值）

    用 QTest 真实按键模拟（走完整事件派发链），验证 QPushButton 在
    StrongFocus 下 Space 触发 clicked。这是"默认安全"落地为可用操作的关键。
    """
    from PyQt5.QtTest import QTest

    card = _make_card(risk="info")
    got = _collect(card)
    card.show()
    qapp.processEvents()
    card._focus_deny()
    qapp.processEvents()
    assert card._deny_btn.hasFocus() is True

    QTest.keyClick(card._deny_btn, Qt.Key_Space)
    qapp.processEvents()
    assert got == [("deny", "", "")], "焦点在拒绝按钮时空格应触发拒绝"
    card.close()


def test_enter_still_allows_even_with_deny_focused(qapp):
    """裁定锁定：Enter 始终映射为允许（不因焦点在拒绝而改变）

    焦点给拒绝 + Enter = 允许是两个独立设计。本用例防止后人"顺手统一"
    把 Enter 改成走聚焦按钮，破坏经用户确认的交互惯例。
    """
    from PyQt5.QtTest import QTest

    card = _make_card(risk="info")
    got = _collect(card)
    card.show()
    qapp.processEvents()
    card._focus_deny()
    qapp.processEvents()

    QTest.keyClick(card._deny_btn, Qt.Key_Return)
    qapp.processEvents()
    assert got == [("allow", "tool", "")], "Enter 必须是允许（含焦点在拒绝按钮时）"
    card.close()


# ── 三、danger 档位防护 ──


def test_danger_disables_allow_500ms(qapp):
    """danger 档位：允许按钮初始禁用，约 500ms 后解锁（防连击误触）"""
    card = _make_card(risk="danger")
    assert card._allow_btn.isEnabled() is False, "危险操作初始必须禁用允许按钮"

    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline and not card._allow_btn.isEnabled():
        qapp.processEvents()
        time.sleep(0.05)
    assert card._allow_btn.isEnabled() is True, "延迟后应解锁"


def test_info_risk_allow_enabled_immediately(qapp):
    """非 danger 档位不延迟（避免无谓等待）"""
    card = _make_card(risk="info")
    assert card._allow_btn.isEnabled() is True


def test_danger_keyboard_enter_blocked_within_500ms(qapp):
    """danger 档位：500ms 内按 Enter 不得 emit allow（键盘路径须同受闸门约束）

    连按 Enter 是最典型误触形态——鼠标被 setEnabled(False) 挡住，若键盘
    直达 _decide 就等于给最快路径开后门。
    """
    from PyQt5.QtGui import QKeyEvent

    card = _make_card(risk="danger")
    got = []
    card.answered.connect(lambda d, r, s: got.append((d, r, s)))
    assert card._allow_btn.isEnabled() is False

    card.keyPressEvent(QKeyEvent(QKeyEvent.KeyPress, Qt.Key_Return, Qt.NoModifier))
    assert got == [], "防误触期内按 Enter 不得放行"

    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline and not card._allow_btn.isEnabled():
        qapp.processEvents()
        time.sleep(0.05)
    card.keyPressEvent(QKeyEvent(QKeyEvent.KeyPress, Qt.Key_Return, Qt.NoModifier))
    assert got == [("allow", "tool", "")], "解锁后 Enter 应正常放行"


def test_danger_direct_decide_blocked_within_500ms(qapp):
    """danger 档位：任何 allow 出口（含 Ctrl+Enter 与记住菜单）统一被闸门拦截"""
    card = _make_card(risk="danger")
    got = []
    card.answered.connect(lambda d, r, s: got.append((d, r, s)))
    card._decide("allow", "")
    card._decide("allow", "round")
    assert got == [], "防误触期内所有 allow 出口都应被拦"


def test_danger_deny_never_blocked(qapp):
    """闸门只拦 allow：拒绝路径任何时刻都可用（安全方向不能被挡）"""
    card = _make_card(risk="danger")
    got = []
    card.answered.connect(lambda d, r, s: got.append((d, r, s)))
    card._deny_btn.click()
    assert got == [("deny", "", "")], "拒绝必须立即生效"


def test_info_enter_not_blocked(qapp):
    """非 danger 档位无闸门（Enter 立即放行）"""
    from PyQt5.QtGui import QKeyEvent

    card = _make_card(risk="info")
    got = []
    card.answered.connect(lambda d, r, s: got.append((d, r, s)))
    card.keyPressEvent(QKeyEvent(QKeyEvent.KeyPress, Qt.Key_Return, Qt.NoModifier))
    assert got == [("allow", "tool", "")]


def test_danger_hint_shown_on_blocked_attempt(qapp):
    """被拦截时给可见提示（不静默失败）"""
    card = _make_card(risk="danger")
    default_hint = card._hint_label.text()
    card._decide("allow", "")
    assert card._hint_label.text() != default_hint
    assert "危险操作" in card._hint_label.text()


# ── 六、UI 布局与字体收敛（用户反馈批次）──


def test_footer_layout_buttons_right_aligned(qapp):
    """尾部布局：全部按钮靠右 = [预览][作用域] ｜[拒绝][允许]（主操作最右）

    用户要求按钮统一右对齐，辅助组与决策组之间留一点距离。
    用布局内实际索引断言（不依赖创建顺序）。注意：全是 layout 索引，
    不可与 widgets 列表索引混用（此前混用导致断言误判）。
    """
    from PyQt5.QtWidgets import QHBoxLayout

    card = _make_card()
    footer = card._deny_btn.parentWidget()
    lay = footer.layout()
    assert isinstance(lay, QHBoxLayout)

    idx = {}
    stretch_idx = None
    for i in range(lay.count()):
        it = lay.itemAt(i)
        w = it.widget()
        if w is not None:
            idx[w] = i
        elif it.spacerItem() is not None and stretch_idx is None:
            stretch_idx = i  # 起始 stretch：整体靠右

    assert stretch_idx == 0, "首位必须是 addStretch()，全部按钮靠右"
    assert idx[card._preview_btn] > stretch_idx, "预览应靠右"
    assert idx[card._scope_btn] > stretch_idx, "作用域按钮应靠右"
    assert idx[card._deny_btn] > idx[card._scope_btn], "拒绝在辅助按钮之后"
    assert idx[card._deny_btn] < idx[card._allow_btn], "允许必须最右（主操作）"
    # 辅助组与决策组之间有固定间隔（视觉分组）
    aux_end = max(idx[card._preview_btn], idx[card._scope_btn])
    assert idx[card._deny_btn] - aux_end >= 2, "辅助组与决策组之间应有间隔项（addSpacing）"


def test_no_page_indicator(qapp):
    """页码指示器已删除（审批卡恒定 1/1，零信息量且属"杂乱"来源）"""
    card = _make_card()
    assert not hasattr(card, "_page_label"), "_page_label 应已移除"


def test_digit_shortcuts_removed(qapp):
    """数字键直选已移除（避免与新布局视觉顺序错位）"""
    from PyQt5.QtGui import QKeyEvent

    card = _make_card(risk="info")
    got = []
    card.answered.connect(lambda d, r, s: got.append((d, r, s)))
    for key in (Qt.Key_1, Qt.Key_2, Qt.Key_3, Qt.Key_4):
        card.keyPressEvent(QKeyEvent(QKeyEvent.KeyPress, key, Qt.NoModifier))
    assert got == [], "数字键不应触发任何决策"


def test_remember_menu_is_round_menu(qapp):
    """记住菜单必须跟随 qfluentwidgets 主题，不得回归原生 QMenu（已由
    test_remember_menu_is_checkable_menu 覆盖类型与时序，此处保留主题继承断言）"""
    from qfluentwidgets import RoundMenu

    card = _make_card()
    menu = card._build_remember_menu()
    assert isinstance(menu, RoundMenu), f"菜单类型应为 RoundMenu 子类，实际 {type(menu).__name__}"
    assert type(menu) is not RoundMenu, "不得退回无勾选指示器的 RoundMenu"


def test_remember_menu_is_checkable_menu(qapp):
    """记住菜单必须是 CheckableMenu（跟随主题 + 有勾选指示器），不得回归原生 QMenu

    原生 QMenu 不跟随主题（深色下白底割裂）；RoundMenu 跟随主题但其
    delegate 无勾选指示器，setChecked 渲染零像素差异（选中态不可见）。
    """
    from qfluentwidgets import CheckableMenu

    card = _make_card()
    menu = card._build_remember_menu()
    assert isinstance(menu, CheckableMenu), f"菜单类型应为 CheckableMenu，实际 {type(menu).__name__}"
    # 勾选指示器由 CheckableMenu 的 delegate 提供（RoundMenu 不具备）
    assert type(menu.view.itemDelegate()).__name__ == "CheckIndicatorMenuItemDelegate"


def test_click_menu_item_updates_scope(qapp):
    """回归：真实点击菜单项必须更新作用域并回传所选值

    事故链路：CheckableMenu/RoundMenu 覆写了 exec_，非阻塞且恒返回 None，
    没有原生 QMenu.exec_() 的"返回被触发 action"语义。原实现用
    `chosen = menu.exec_(...)` 的返回值判断选中项，三个分支永远不命中，
    导致「当前工具/当前轮次/当前会话」全部点了没反应、作用域恒为 tool。
    本用例走真实鼠标点击（不经 _set_scope 直调），锁死该回归。

    注：菜单按 QMenu 基类查找（不绑 CheckableMenu），使本用例只检验
    "点击是否生效"这一行为契约，不因菜单类型实现变化而误判。
    """
    from PyQt5.QtTest import QTest
    from PyQt5.QtWidgets import QMenu

    card = _make_card(risk="warn")
    card.resize(600, 240)
    card.show()
    qapp.processEvents()

    card._scope_btn.click()
    qapp.processEvents()
    menus = [m for m in card.findChildren(QMenu) if m.isVisible()]
    assert menus, "点击作用域按钮应弹出菜单"
    menu = menus[0]

    # 点第二项「当前轮次」（索引 1，索引 3 是 separator）
    rect = menu.view.visualItemRect(menu.view.item(1))
    QTest.mouseClick(menu.view.viewport(), Qt.LeftButton, Qt.NoModifier, rect.center())
    qapp.processEvents()

    assert card._remember_scope == "round", "点击菜单项后作用域必须更新（原实现点了没反应）"
    assert "当前轮次" in card._scope_btn.text(), "按钮文案必须同步为所选作用域"

    # 关键闭环：所选作用域必须随「允许」回传
    got = _collect(card)
    card._allow_btn.click()
    assert got == [("allow", "round", "")], "所选作用域必须进入决策回传"
    card.close()


def test_click_disabled_session_item_is_noop(qapp):
    """danger 档位下点「当前会话」（禁用项）→ 作用域不变

    菜单项 disable 只是 UI 层拦截，本用例确认点击事件确实不会穿过
    (RoundMenu._onItemClicked 对 disabled action 提前 return)。
    """
    from PyQt5.QtTest import QTest
    from PyQt5.QtWidgets import QMenu

    card = _make_card(risk="danger")
    card.resize(600, 240)
    card.show()
    qapp.processEvents()

    card._scope_btn.click()
    qapp.processEvents()
    menu = [m for m in card.findChildren(QMenu) if m.isVisible()][0]
    assert menu._act_session.isEnabled() is False

    rect = menu.view.visualItemRect(menu.view.item(2))
    QTest.mouseClick(menu.view.viewport(), Qt.LeftButton, Qt.NoModifier, rect.center())
    qapp.processEvents()

    assert card._remember_scope == "tool", "danger 档位点会话级不得改变作用域"
    card.close()


def test_set_scope_rejects_session_when_danger(qapp):
    """danger 档位：绕过菜单直调也不得写入会话级作用域（兜底层）"""
    card = _make_card(risk="danger")
    card._set_scope("session")
    assert card._remember_scope == "tool", "danger 档位不得写入 session 作用域"
    # 轮次级仍可用
    card._set_scope("round")
    assert card._remember_scope == "round"


def test_scope_resets_on_new_request(qapp):
    """跨请求复位：上一张卡选过会话级，下一次请求必须回到最小范围

    卡片是复用单例（宿主 _ensure_permission_approval_widget 只创建一次），
    作用域若不复位，上一次选的「当前会话」会顺延成下一个（可能是完全
    不同的工具）的授权范围——静默扩大授权面。
    """
    card = _make_card(risk="warn")
    card._set_scope("session")
    assert card._remember_scope == "session"

    card.show_request(
        tool_name="write_file",
        arguments={"path": "D:/x/b.txt"},
        source="sandbox",
        source_text="⚠ 安全中心拦截",
        risk="warn",
        workdir="D:/work/DriFox",
        impact={"paths": ["D:/x/b.txt"], "writes": True},
        preview_payload=None,
    )
    assert card._remember_scope == "tool", "新请求必须复位为最小作用域"
    assert "当前工具" in card._scope_btn.text()


def test_scope_resets_on_clear(qapp):
    """clear 后作用域复位（防清理后残留的会话级选中被下一次请求继承）"""
    card = _make_card(risk="warn")
    card._set_scope("session")
    card.clear()
    assert card._remember_scope == "tool"


def test_remember_menu_items_present(qapp):
    """非 danger 档位：菜单只 3 项（无分隔线与说明行）

    说明性占位行（⚠ 删除类命令不支持会话级豁免）仅在它解释了「当前会话」
    为何禁用时才显示：非 danger 档会话项可用，该行就是永久灰色的死项
    （用户反馈"这个提示要去掉"）。
    """
    card = _make_card(risk="info")
    menu = card._build_remember_menu()
    labels = [a.text() for a in menu.actions() if not a.isSeparator()]
    assert labels == ["当前工具", "当前轮次", "当前会话"], "非 danger 档不得出现说明行"
    assert menu.view.count() == 3, "非 danger 档不得出现分隔线"
    assert menu._act_session.isEnabled() is True
    # 勾选态跟随当前作用域（默认 tool）
    assert menu._act_tool.isChecked() is True
    assert menu._act_round.isChecked() is False


def test_remember_menu_danger_shows_hint(qapp):
    """danger 档位：说明行随禁用的会话项一起出现（解释它为何不能选）"""
    from app.widgets.cards.floating.permission_approval_widget import _DANGER_SESSION_HINT

    card = _make_card(risk="danger")
    menu = card._build_remember_menu()
    non_sep = [a for a in menu.actions() if not a.isSeparator()]
    assert [a.text() for a in non_sep[:3]] == ["当前工具", "当前轮次", "当前会话"]
    assert menu._act_session.isEnabled() is False
    assert non_sep[-1].text() == _DANGER_SESSION_HINT
    assert non_sep[-1].isEnabled() is False, "说明行不可点击"


def test_remember_tool_emits_scope(qapp):
    """默认作用域（当前工具）下点允许 → ("allow", "tool", "")"""
    card = _make_card()
    got = _collect(card)
    card._allow_btn.click()
    assert got == [("allow", "tool", "")]


def test_font_scale_converged(qapp):
    """字号收敛：命令块（等宽）不得大于正文（原先 13 > 11 造成辅助信息压过正文）"""
    card = _make_card()
    qss = card._cmd_label.styleSheet()
    body_qss = card._impact_label.styleSheet()
    cmd_size = _extract_font_size(qss)
    body_size = _extract_font_size(body_qss)
    assert cmd_size is not None and body_size is not None
    assert cmd_size <= body_size, f"命令块字号 {cmd_size} 不应大于正文字号 {body_size}"


def test_hint_font_size_bumped(qapp):
    """提示行从 9 提到 10（原字号偏小）"""
    card = _make_card()
    size = _extract_font_size(card._hint_label.styleSheet())
    assert size is not None and size >= 10, f"提示行字号应 >= 10，实际 {size}"


def _extract_font_size(qss: str):
    import re

    m = re.search(r"font-size:\s*(\d+)px", qss)
    return int(m.group(1)) if m else None


def test_hint_text_no_digit_shortcut(qapp):
    """提示行文案不再列数字键（已移除该功能）"""
    from app.widgets.cards.floating.permission_approval_widget import _HINT_TEXT

    assert "直选" not in _HINT_TEXT
    assert "1/2/3/4" not in _HINT_TEXT
    assert "Enter" in _HINT_TEXT and "Esc" in _HINT_TEXT and "Ctrl+P" in _HINT_TEXT


def test_danger_hides_session_remember(qapp):
    """danger 档位：会话级记住项不可用（删除类不给会话级豁免）

    用菜单上显式留存的引用断言：RoundMenu 的 actions() 是否含 separator
    随库版本变化，不依赖索引更稳。
    """
    card = _make_card(risk="danger")
    menu = card._build_remember_menu()
    assert menu._act_round.isEnabled() is True, "本轮作用域应可用"
    assert menu._act_session.isEnabled() is False, "danger 档位会话级记住项必须禁用"
    # 第三项（删除类会话豁免）恒禁用
    non_sep = [a for a in menu.actions() if not a.isSeparator()]
    assert non_sep[-1].isEnabled() is False, "删除类会话豁免恒禁用"


def test_info_allows_session_remember(qapp):
    """info/warn 档位：会话级记住项可用"""
    card = _make_card(risk="info")
    assert card._build_remember_menu()._act_session.isEnabled() is True


def test_warn_allows_session_remember(qapp):
    """warn 档位同样允许会话级记住"""
    card = _make_card(risk="warn")
    assert card._build_remember_menu()._act_session.isEnabled() is True


# ── 四、渲染完整性 ──


def test_command_not_truncated(qapp):
    """长命令完整展示（不截断）"""
    long_cmd = "rm -rf " + " /very/long/path/segment" * 12
    card = _make_card(arguments={"command": long_cmd})
    assert card._cmd_label.text() == long_cmd, "命令必须完整展示，不得截断"


def test_arguments_rendered_as_lines_not_dict_literal(qapp):
    """无主参数时逐行 key: value（不显示 dict 字面量）"""
    card = _make_card(arguments={"query": "hello", "top_k": 5})
    text = card._cmd_label.text()
    assert "query: hello" in text
    assert "top_k: 5" in text
    assert "{" not in text and "'query'" not in text, "不得显示 dict 字面量"


def test_path_arg_preferred_as_main_parameter(qapp):
    """主参数优先级：command → path → file_path"""
    card = _make_card(arguments={"path": "D:/x/y.txt", "content": "z"})
    assert card._cmd_label.text() == "D:/x/y.txt"


def test_impact_rendered_with_paths(qapp):
    card = _make_card(impact={"paths": ["D:/a", "D:/b"], "missing": ["D:/c"], "writes": True})
    text = card._impact_label.text()
    assert "D:/a" in text and "D:/b" in text
    assert "1 项路径不存在" in text
    assert "写入文件" in text


def test_workdir_shown_in_meta(qapp):
    """工作目录从正文挪进详情折叠区（正文只留决策必需信息）"""
    card = _make_card(workdir="D:/work/DriFox")
    assert "D:/work/DriFox" in card._meta_label.text()


def test_body_labels_use_system_font(qapp):
    """正文/提示/按钮样式必须应用系统字体族（原先大部分 label 缺 font-family）"""
    from app.utils.utils import get_font_family_css

    card = _make_card()
    for widget_name in (
        "_source_desc",
        "_impact_label",
        "_meta_label",
        "_hint_label",
        "_meta_toggle",
        "_deny_btn",
        "_preview_btn",
        "_scope_btn",
        "_allow_btn",
    ):
        widget = getattr(card, widget_name)
        assert get_font_family_css() in widget.styleSheet(), f"{widget_name} 样式缺系统字体族"


def test_footer_buttons_right_aligned(qapp):
    """辅助按钮与决策按钮同行右对齐：辅助组与决策组之间有固定间隔"""
    card = _make_card()
    footer = card._preview_btn.parentWidget()
    lay = footer.layout()
    assert lay.indexOf(card._preview_btn) < lay.indexOf(card._deny_btn)
    # 预览按钮之前只能是 stretch，不得有左对齐的其他控件
    assert lay.count() > 0 and lay.itemAt(0).spacerItem() is not None


def test_styled_background_attribute_set(qapp):
    """WA_StyledBackground 必须设置，否则 QSS 全部静默失效（全项目已踩坑 13 处）"""
    card = _make_card()
    assert card.testAttribute(Qt.WA_StyledBackground) is True


def test_container_props_set(qapp):
    """容器协作两属性：高度跟随内容 + 跳过展开动画"""
    from app.widgets.cards.card_container import CardContainer

    card = _make_card()
    assert card.property(CardContainer.FOLLOW_CONTENT_PROP) is True
    assert card.property(CardContainer.NO_ANIMATION_PROP) is True


def test_preview_button_visibility_follows_payload(qapp):
    """有载荷 → 预览按钮显示；无载荷 → 隐藏

    注：必须 show() 后再断言 isVisible——未显示的父链下 Qt 的显隐判定
    不反映子控件真实可见性（isHidden 亦不可靠，实测为 False）。
    """
    card_with = _make_card(preview_payload={"tool_call_id": "c1"})
    card_with.show()
    qapp.processEvents()
    assert card_with._preview_btn.isVisible() is True
    card_with.close()

    card_without = _make_card(preview_payload=None)
    card_without.show()
    qapp.processEvents()
    assert card_without._preview_btn.isVisible() is False
    card_without.close()


def test_preview_emits_payload(qapp):
    payload = {"tool_call_id": "c9", "arguments": {"command": "ls"}}
    card = _make_card(preview_payload=payload)
    got = []
    card.previewRequested.connect(lambda p: got.append(p))
    card._on_preview()
    assert got == [payload]


# ── 五、宿主约定接口 ──


def test_clear_resets_and_hides(qapp):
    card = _make_card()
    card.show()
    qapp.processEvents()
    card.clear()
    assert card.isVisible() is False
    assert card._tool_name == ""
    assert card._arguments == {}
    assert card._preview_payload is None


def test_clear_blocks_further_emit(qapp):
    """clear 之后不得再产生决策（防已失效请求被误回传）"""
    card = _make_card()
    got = _collect(card)
    card.clear()
    card._decide("allow", "")
    assert got == []


def test_refresh_style_runs_without_error(qapp):
    card = _make_card()
    card.refresh_style()
    assert "PermissionApprovalWidget" in card.styleSheet()


def test_set_opacity_no_error(qapp):
    card = _make_card()
    card.set_opacity(0.5)


def test_tool_cn_name_from_registry_fallback(qapp):
    """registry 查不到中文名时回退英文名（不崩、不留空标题）"""
    card = _make_card(tool_name="__no_such_tool__")
    assert "__no_such_tool__" in card._title_label.text()


def test_height_changed_emitted_on_show_request(qapp):
    """show_request 触发 heightChanged（容器据此重算高度）"""
    from app.widgets.cards.floating.permission_approval_widget import PermissionApprovalWidget

    card = PermissionApprovalWidget()
    got = []
    card.heightChanged.connect(lambda: got.append(True))
    card.show_request(
        tool_name="bash",
        arguments={"command": "ls"},
        source="policy",
        source_text="🔒 工具权限策略",
        risk="info",
        workdir="D:/x",
        impact={},
    )
    qapp.processEvents()
    assert got, "show_request 后应发出 heightChanged"


def test_meta_toggle_expands(qapp):
    """元信息默认收起，点击后展开

    注：必须 show() 后再断言——未显示的父链下 Qt 显隐判定不反映子控件真实可见性。
    """
    card = _make_card()
    card.show()
    qapp.processEvents()
    assert card._meta_label.isVisible() is False
    card._toggle_meta()
    assert card._meta_label.isVisible() is True
    card._toggle_meta()
    assert card._meta_label.isVisible() is False
    card.close()
