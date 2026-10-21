# -*- coding: utf-8 -*-
"""回归测试：开关 MCP 服务器不应触发列表全量重建

背景（根因）：
- 用户反馈「开关 MCP 时感觉整个 MCP 列表都在重建」。
- 根因链：MCPServerRow 开关 → _do_debounced_switch / _do_debounced_global_switch
  内 emit serversChanged → GlobalCardController._on_mcp_servers_toggled
  无条件 mcpListCard._refresh() 全量重建（删除全部行 + 重建 MCPServerRow +
  processEvents + _adjustViewSize）。
- 实际上开关操作已在操作点做行级更新（row.set_enabled / set_status），
  列表内容未变，无需全量重建。增删改场景也在操作点自行 _refresh()。

修复：
1. mcp_setting_card.py：开关/连接结果路径不再 emit serversChanged；
   _do_remove 自行 _refresh 后不再重复 emit。
2. global_card_controller.py：断开 serversChanged→_on_mcp_servers_toggled 连接，
   _on_mcp_servers_toggled 不再做全量刷新。
3. main_widget.py：热重载 MCP 广播对全局唯一共享卡片只刷新一次
   （修 consume_hot_reload 布尔标记被多窗口重复消费的问题）。

本测试锁定上述行为，防止回归。
"""

import sys
import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from PyQt5.QtWidgets import QApplication


def _ensure_qapp():
    """确保 QApplication 已创建（返回现有实例，不重复创建）"""
    return QApplication.instance() or QApplication(sys.argv)


@pytest.fixture(scope="module", autouse=True)
def _qapp():
    return _ensure_qapp()


def _make_mcp_card():
    """构造最小可用的 MCPListSettingCard（绕过重量级 __init__）

    使用 __new__ + 手动补属性，只保留被测方法用到的 Python 侧状态，
    不触碰 Qt C++ 层（因此不会因未初始化 QObject 而崩溃）。
    """
    from app.widgets.cards.settings.mcp_setting_card import MCPListSettingCard

    card = MCPListSettingCard.__new__(MCPListSettingCard)
    # 信号用 mock 遮蔽（避免未初始化 QObject 上访问 pyqtSignal）
    card.serversChanged = MagicMock()
    card._server_rows = {}
    card._pending_server_switches = {}
    card._pending_global_switch = True
    card._global_switch_pending = False
    card._suppress_hot_reload_until = 0.0
    card._token_calc_running = False
    card.cfg = MagicMock()
    card.cfg.mcp_enabled.value = True

    # 被测方法依赖的外部方法全部 mock
    card._get_servers = MagicMock(return_value=[])
    card._get_pm = MagicMock(return_value=MagicMock())
    card._get_mcp_manager = MagicMock()
    card._hot_connect = MagicMock()
    card._hot_disconnect = MagicMock()
    card._hot_disconnect_all = MagicMock()
    card._refresh_status_dots = MagicMock()
    card._update_mcp_token_count = MagicMock()
    # _refresh 是我们要锁定「不被调用」的目标
    card._refresh = MagicMock()
    return card


class TestSingleServerSwitch:
    """单个服务器开关路径不触发全量刷新"""

    def test_debounced_switch_does_not_full_refresh(self):
        """开关单个服务器：不调用 _refresh，也不 emit serversChanged"""
        card = _make_mcp_card()
        card._pending_server_switches = {"srv1": False}
        card._get_servers.return_value = [{"name": "srv1", "enabled": True}]

        card._do_debounced_switch()

        card._refresh.assert_not_called()
        card.serversChanged.emit.assert_not_called()
        # 行级更新仍应执行（热连接/断开 + token 估算）
        card._hot_disconnect.assert_called_once_with("srv1")
        card._update_mcp_token_count.assert_called_once()
        # 自触发抑制标记已设置（带时间戳，3s 内有效），供热重载广播消费
        assert card._suppress_hot_reload_until > time.time()

    def test_debounced_switch_enable_does_not_full_refresh(self):
        """开启单个服务器：同样不触发全量刷新"""
        card = _make_mcp_card()
        card._pending_server_switches = {"srv1": True}
        card._get_servers.return_value = [{"name": "srv1", "enabled": False}]

        card._do_debounced_switch()

        card._refresh.assert_not_called()
        card.serversChanged.emit.assert_not_called()
        card._hot_connect.assert_called_once()


class TestGlobalSwitch:
    """全局开关路径不触发全量刷新"""

    def test_global_switch_off_does_not_full_refresh(self):
        card = _make_mcp_card()
        card._global_switch_pending = True
        card._pending_global_switch = False

        card._do_debounced_global_switch()

        card._refresh.assert_not_called()
        card.serversChanged.emit.assert_not_called()
        card.cfg.set.assert_called_once_with(card.cfg.mcp_enabled, False, save=True)
        card._hot_disconnect_all.assert_called_once()

    def test_global_switch_on_does_not_full_refresh(self):
        card = _make_mcp_card()
        card._global_switch_pending = True
        card._pending_global_switch = True
        card._get_servers.return_value = [
            {"name": "srv1", "enabled": True},
            {"name": "srv2", "enabled": False},
        ]

        card._do_debounced_global_switch()

        card._refresh.assert_not_called()
        card.serversChanged.emit.assert_not_called()
        card.cfg.set.assert_called_once_with(card.cfg.mcp_enabled, True, save=True)
        # 只热连接 enabled=True 的服务器
        assert card._hot_connect.call_count == 1


class TestHotConnectResult:
    """连接结果回调不触发全量刷新"""

    def test_connect_result_does_not_full_refresh(self):
        card = _make_mcp_card()

        card._on_hot_connect_result("srv1", True)

        card._refresh.assert_not_called()
        card.serversChanged.emit.assert_not_called()
        card._refresh_status_dots.assert_called_once()


class TestRemoveServer:
    """删除服务器：自行 _refresh 一次，不再重复 emit"""

    def test_remove_refreshes_once_without_emit(self):
        card = _make_mcp_card()

        card._do_remove("srv1")

        card._refresh.assert_called_once()
        card.serversChanged.emit.assert_not_called()
        card._get_pm().remove_mcp_server.assert_called_once_with("srv1")


class TestGlobalCardController:
    """GlobalCardController 侧：_on_mcp_servers_toggled 不再全量刷新"""

    def test_on_mcp_servers_toggled_does_not_refresh(self):
        from app.widgets.cards.global_card_controller import GlobalCardController

        cc = GlobalCardController.__new__(GlobalCardController)
        mcp_card = MagicMock()
        cc._settings_popup = MagicMock()
        cc._settings_popup.mcpListCard = mcp_card

        # 方法体已改为空操作：不抛异常且不触碰 _refresh
        cc._on_mcp_servers_toggled()
        mcp_card._refresh.assert_not_called()

    def test_no_servers_changed_connection_in_source(self):
        """源码中不再存在 serversChanged → _on_mcp_servers_toggled 的连接"""
        repo_root = Path(__file__).resolve().parent.parent.parent
        src = (repo_root / "app" / "widgets" / "cards" / "global_card_controller.py").read_text(
            encoding="utf-8"
        )
        assert "serversChanged.connect" not in src

    def test_mcp_card_switch_paths_have_no_emit(self):
        """开关路径源码中不再出现 serversChanged.emit

        允许保留的 emit 仅存在于增删改路径（此处全部移除）。
        """
        repo_root = Path(__file__).resolve().parent.parent.parent
        src = (repo_root / "app" / "widgets" / "cards" / "settings" / "mcp_setting_card.py").read_text(
            encoding="utf-8"
        )
        # 防抖开关 / 全局开关 / 连接结果 三个方法体内不允许再 emit
        import ast

        tree = ast.parse(src)
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef) or node.name != "MCPListSettingCard":
                continue
            for method in node.body:
                if not isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                if method.name in ("_do_debounced_switch", "_do_debounced_global_switch", "_on_hot_connect_result"):
                    method_src = ast.get_source_segment(src, method) or ""
                    assert "serversChanged.emit" not in method_src, (
                        f"{method.name} 不应 emit serversChanged（会触发全量重建）"
                    )
