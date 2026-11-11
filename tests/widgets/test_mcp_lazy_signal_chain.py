# -*- coding: utf-8 -*-
"""MCP 页懒态信号链回归（#35：懒态主卡 ensure_settings_popup 接线 crash）

#31 把 mcpListCard 改懒态（主卡构造期=None），global_card_controller 的
ensure_settings_popup 接线 `self._settings_popup.mcpListCard.showAddCard.connect(...)`
在懒态下 AttributeError(NoneType)——用户点设置按钮即炸。

修复（信号转发，非判空）：LLMSettingsCard 定义 showAddCard/showEditCard 转发
信号（永存在），_build_mcp_page 构建卡片时把卡片信号转发到主卡信号；controller
改连主卡信号——懒态不炸 + 进 MCP 页后信号链通（两个都要）。
"""

import os
import sys

import pytest

pytest.importorskip("PyQt5.QtWidgets")
pytest.importorskip("qfluentwidgets")

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtWidgets import QApplication  # noqa: E402

from app.widgets.cards.settings.llm_settings_card import LLMSettingsCard  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    app = QApplication.instance() or QApplication([])
    yield app


@pytest.fixture()
def card(qapp):
    c = LLMSettingsCard()
    yield c
    c.deleteLater()


@pytest.fixture()
def settings_card(qapp):
    c = LLMSettingsCard()
    yield c
    c.deleteLater()


def test_lazy_state_wiring_does_not_crash(qapp, settings_card):
    """回归复现原 crash 场景：懒态主卡（mcpListCard=None）+ controller 接线路径。

    原实现 controller 直接 `mcpListCard.showAddCard.connect(...)` → 懒态
    AttributeError(NoneType)。修复后 controller 连主卡转发信号（永存在），
    本测试按修复后形态接线：懒态下 connect 不炸。
    """
    assert settings_card.mcpListCard is None, "前置：主卡构造期 MCP 页保持懒（None）"
    received = {"add": 0, "edit": []}
    # controller 修复后形态：连主卡转发信号
    settings_card.showAddCard.connect(lambda: received.__setitem__("add", received["add"] + 1))
    settings_card.showEditCard.connect(lambda name, data: received["edit"].append((name, data)))
    assert received == {"add": 0, "edit": []}


def test_signal_chain_reaches_after_page_build(qapp, settings_card):
    """信号链：进 MCP 页构建卡片后，卡片信号经主卡转发可达消费槽（链通断言）"""
    received = {"add": 0, "edit": []}
    settings_card.showAddCard.connect(lambda: received.__setitem__("add", received["add"] + 1))
    settings_card.showEditCard.connect(lambda name, data: received["edit"].append((name, data)))

    assert settings_card.mcpListCard is None, "前置：进页前保持懒态"

    settings_card._ensure_page_ready("mcp")
    assert settings_card.mcpListCard is not None, "进页后卡片必须已构建"

    # 模拟用户点击「添加」按钮（卡片内 addButton.clicked → showAddCard.emit）
    settings_card.mcpListCard.showAddCard.emit()
    assert received["add"] == 1, "卡片信号必须经主卡转发到达消费槽（信号链通）"

    # 模拟「编辑」路径（带参转发：name + server_data）
    settings_card.mcpListCard.showEditCard.emit("srv-1", {"command": "node"})
    assert received["edit"] == [("srv-1", {"command": "node"})], "带参转发必须保真"


def test_hooks_lazy_state_wiring_does_not_crash(qapp, settings_card):
    """#39b 回归：懒态主卡（hookListCard=None）+ controller hooks 接线路径。

    原实现 controller 直连 `hookListCard.showAddHookCard.connect(...)` → 懒态
    AttributeError(NoneType)。修复后连主卡转发信号（永存在），懒态不炸。
    """
    assert settings_card.hookListCard is None, "前置：主卡构造期 hooks 页保持懒（None）"
    received = {"add": 0, "edit": [], "changed": 0, "toggled": []}
    settings_card.showAddHookCard.connect(lambda: received.__setitem__("add", received["add"] + 1))
    settings_card.showEditHookCard.connect(lambda i, d: received["edit"].append((i, d)))
    settings_card.hooksChanged.connect(lambda: received.__setitem__("changed", received["changed"] + 1))
    settings_card.hookToggled.connect(lambda i, e: received["toggled"].append((i, e)))
    assert received == {"add": 0, "edit": [], "changed": 0, "toggled": []}


def test_hooks_signal_chain_reaches_after_page_build(qapp, settings_card):
    """#39b 信号链：进 hooks 页构建卡片后，四条信号经主卡转发可达消费槽。"""
    received = {"add": 0, "edit": [], "changed": 0, "toggled": []}
    settings_card.showAddHookCard.connect(lambda: received.__setitem__("add", received["add"] + 1))
    settings_card.showEditHookCard.connect(lambda i, d: received["edit"].append((i, d)))
    settings_card.hooksChanged.connect(lambda: received.__setitem__("changed", received["changed"] + 1))
    settings_card.hookToggled.connect(lambda i, e: received["toggled"].append((i, e)))

    settings_card._ensure_page_ready("hooks")
    assert settings_card.hookListCard is not None, "进页后 hookListCard 必须已构建"

    settings_card.hookListCard.showAddHookCard.emit()
    assert received["add"] == 1, "添加 Hook 信号必须经主卡转发到达消费槽"
    settings_card.hookListCard.showEditHookCard.emit("h1", {"event": "BuildSystemPrompt"})
    assert received["edit"] == [("h1", {"event": "BuildSystemPrompt"})], "编辑 Hook 带参转发必须保真"
    settings_card.hookListCard.hooksChanged.emit()
    assert received["changed"] == 1, "hooksChanged 转发必须可达"
    settings_card.hookListCard.hookToggled.emit("h1", False)
    assert received["toggled"] == [("h1", False)], "hookToggled 带参转发必须保真"


def test_controller_has_no_lazy_card_direct_wiring():
    """#39b 系统性守卫：controller 接线段不得再直连任何懒化卡属性（mcp/hooks）。"""
    from pathlib import Path

    src = (
        Path(__file__).resolve().parent.parent.parent
        / "app"
        / "widgets"
        / "cards"
        / "global_card_controller.py"
    ).read_text(encoding="utf-8-sig")
    for banned in (
        ".mcpListCard.showAddCard.connect",
        ".mcpListCard.showEditCard.connect",
        ".hookListCard.showAddHookCard.connect",
        ".hookListCard.showEditHookCard.connect",
        ".hookListCard.hooksChanged.connect",
        ".hookListCard.hookToggled.connect",
    ):
        assert banned not in src, f"controller 接线段残留懒态卡直接接线: {banned}"
    assert "_settings_popup.showAddHookCard.connect" in src
    assert "_settings_popup.hooksChanged.connect" in src


def test_wiring_uses_forwarded_signals_not_lazy_card():
    """AST 守卫：controller 接线必须连主卡转发信号，不得直连懒态卡片属性"""
    from pathlib import Path

    src = (
        Path(__file__).resolve().parent.parent.parent
        / "app"
        / "widgets"
        / "cards"
        / "global_card_controller.py"
    ).read_text(encoding="utf-8-sig")
    assert "_settings_popup.showAddCard.connect" in src, "controller 必须连主卡转发信号"
    assert "_settings_popup.showEditCard.connect" in src, "controller 必须连主卡转发信号"
    assert "_settings_popup.mcpListCard.showAddCard.connect" not in src, (
        "controller 不得直连懒态卡片属性（懒态 NoneType crash 回归）"
    )
    assert "_settings_popup.mcpListCard.showEditCard.connect" not in src, (
        "controller 不得直连懒态卡片属性（懒态 NoneType crash 回归）"
    )


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
