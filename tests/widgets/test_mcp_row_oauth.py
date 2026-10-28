# -*- coding: utf-8 -*-
"""MCP 设置卡 OAuth 授权入口 + 插件图标回归测试。

覆盖两块新增 UI：
1. MCPServerRow：插件图标解析、授权按钮的构造与双态切换
   （未授权 → 「授权」可点发起授权；已授权 → 「解绑」可点撤销授权）+ 授权中忙态
2. MCPListSettingCard._on_authorize_server：参数校验、后台线程启动、
   _on_authorize_done 的结果分发（成功触发热连接 / 失败只提示）

关键约束：McpOAuthManager.authorize() 同步阻塞最长 300 秒，
必须走工作线程，不能在 UI 线程直接调用。测试锁定这一点。
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from PySide6.QtWidgets import QApplication


def _ensure_qapp():
    return QApplication.instance() or QApplication(sys.argv)


@pytest.fixture(scope="module", autouse=True)
def _qapp():
    return _ensure_qapp()


# ═══════════════════════════════════════════════════
# MCPServerRow：授权按钮
# ═══════════════════════════════════════════════════


def _make_row(data: dict):
    """构造最小可用的 MCPServerRow（绕开 Qt C++ 层）"""
    from app.widgets.cards.settings.mcp_setting_card import MCPServerRow

    row = MCPServerRow.__new__(MCPServerRow)
    row._name = data.get("name", "")
    row._server_data = data
    row._auth_btn = None
    row._authorized = False
    row._authorizing = False
    row._current_state = "disabled"
    return row


class TestAuthorizeButtonVisibility:
    def test_oauth_server_gets_button(self):
        row = _make_row({"name": "notion", "auth": {"type": "oauth"}})
        row._auth_btn = MagicMock()
        row.set_authorized(False)
        assert row._auth_btn.setText.call_args[0][0] == "授权"
        assert row._auth_btn.setEnabled.call_args[0][0] is True

    def test_non_oauth_server_has_no_button(self):
        """未声明 auth.type == oauth 的 server 不建授权按钮"""
        row = _make_row({"name": "plain", "type": "http"})
        assert row._auth_btn is None
        # set_authorized 对无按钮的行必须静默忽略，不能抛
        row.set_authorized(True)

    def test_bearer_auth_has_no_button(self):
        row = _make_row({"name": "gh", "auth": {"type": "bearer"}})
        row._auth_btn = None
        row.set_authorized(False)
        assert row._auth_btn is None


class TestSetAuthorized:
    def test_authorized_shows_unbind(self):
        """已授权 → 「解绑」可点（撤销授权），tooltip 说明清除凭证语义"""
        row = _make_row({"name": "n"})
        row._auth_btn = MagicMock()

        row.set_authorized(True)

        assert row._auth_btn.setText.call_args[0][0] == "解绑"
        assert row._auth_btn.setEnabled.call_args[0][0] is True
        assert "凭证" in row._auth_btn.setToolTip.call_args[0][0]

    def test_unauthorized_enables_button(self):
        row = _make_row({"name": "n"})
        row._auth_btn = MagicMock()

        row.set_authorized(False)

        assert row._auth_btn.setText.call_args[0][0] == "授权"
        assert row._auth_btn.setEnabled.call_args[0][0] is True
        assert "浏览器" in row._auth_btn.setToolTip.call_args[0][0]


class TestAuthAliasWarning:
    """插件填第三方 client_name 绕过白名单时，按钮提示必须明示风险。

    背景：Figma 的远程 MCP 只按 client_name 白名单放行动态注册（实测仅
    Claude Code / Codex / opencode / Claude Desktop 通过，DriFox 与 VS Code
    均 403）。figma 插件的 .mcp.json 因此填了 Claude Code。用户点按钮前
    必须知道这不是 DriFox 的真实身份。
    """

    def test_default_name_has_no_warning(self):
        row = _make_row({"name": "n", "auth": {"type": "oauth", "client_name": "DriFox"}})
        row._auth_btn = MagicMock()

        row.set_authorized(False)

        tip = row._auth_btn.setToolTip.call_args[0][0]
        assert "白名单" not in tip

    def test_missing_client_name_has_no_warning(self):
        row = _make_row({"name": "n", "auth": {"type": "oauth"}})
        row._auth_btn = MagicMock()

        row.set_authorized(False)

        assert "白名单" not in row._auth_btn.setToolTip.call_args[0][0]

    def test_alias_name_warns(self):
        row = _make_row({"name": "figma", "auth": {"type": "oauth", "client_name": "Claude Code"}})
        row._auth_btn = MagicMock()

        row.set_authorized(False)

        tip = row._auth_btn.setToolTip.call_args[0][0]
        assert "Claude Code" in tip
        assert "白名单" in tip
        # 保留原有基础提示，不是替换
        assert "浏览器" in tip


class TestAuthClickDispatch:
    """授权按钮双态分发：未授权点 → authorizeRequested，已授权点 → unbindRequested"""

    def test_click_unauthorized_emits_authorize(self):
        from app.widgets.cards.settings.mcp_setting_card import MCPServerRow

        row = _make_row({"name": "n"})
        row._authorized = False
        row.authorizeRequested = MagicMock()
        row.unbindRequested = MagicMock()

        MCPServerRow._on_auth_clicked(row)

        row.authorizeRequested.emit.assert_called_once_with("n")
        row.unbindRequested.emit.assert_not_called()

    def test_click_authorized_emits_unbind(self):
        from app.widgets.cards.settings.mcp_setting_card import MCPServerRow

        row = _make_row({"name": "n"})
        row._authorized = True
        row.authorizeRequested = MagicMock()
        row.unbindRequested = MagicMock()

        MCPServerRow._on_auth_clicked(row)

        row.unbindRequested.emit.assert_called_once_with("n")
        row.authorizeRequested.emit.assert_not_called()


class TestHasOauthToken:
    def test_missing_source_returns_false(self):
        row = _make_row({"name": "n", "auth": {"type": "oauth"}})
        assert row._has_oauth_token({"name": "n"}) is False

    def test_non_json_source_returns_false(self):
        row = _make_row({"name": "n"})
        assert row._has_oauth_token({"name": "n", "_source": "claude_desktop"}) is False

    def test_delegates_to_manager(self):
        """有插件来源时委托 McpOAuthManager.has_token 判断"""
        row = _make_row({"name": "notion"})
        data = {"name": "notion", "_source": r"D:\plugins\my-plugin\.mcp.json"}

        with patch("app.core.mcp_oauth.McpOAuthManager") as m:
            m.get_instance.return_value.has_token.return_value = True
            assert row._has_oauth_token(data) is True
            m.get_instance.return_value.has_token.assert_called_once_with("my-plugin", "notion")

    def test_manager_exception_degrades_to_false(self):
        row = _make_row({"name": "n"})
        data = {"name": "n", "_source": r"D:\plugins\p\.mcp.json"}

        mgr = MagicMock()
        mgr.get_instance.side_effect = RuntimeError("boom")
        with patch("app.core.mcp_oauth.McpOAuthManager", mgr):
            assert row._has_oauth_token(data) is False


class TestResolveMcpServerIcon:
    def test_no_source_returns_none(self):
        from app.widgets.cards.settings.mcp_setting_card import _resolve_mcp_server_icon

        assert _resolve_mcp_server_icon({"name": "n"}) is None

    def test_non_json_source_returns_none(self):
        from app.widgets.cards.settings.mcp_setting_card import _resolve_mcp_server_icon

        assert _resolve_mcp_server_icon({"_source": "claude_desktop"}) is None

    def test_plugin_icon_resolved(self):
        from app.widgets.cards.settings.mcp_setting_card import _resolve_mcp_server_icon

        sentinel = object()
        pm = MagicMock()
        pm.is_initialized.return_value = True
        pm.get_plugin.return_value.icon_config = {"dark": r"D:\plugins\p\icon.svg"}

        with (
            patch("app.plugins.managers.plugin_manager.PluginManager") as pm_cls,
            patch("app.widgets.cards.settings.mcp_setting_card.QIcon", return_value=sentinel) as qicon,
            patch("app.widgets.cards.settings.mcp_setting_card.isDarkTheme", return_value=True),
        ):
            pm_cls.get_instance.return_value = pm
            got = _resolve_mcp_server_icon({"_source": r"D:\plugins\p\.mcp.json"})

        assert got is sentinel
        qicon.assert_called_once_with(r"D:\plugins\p\icon.svg")
        pm.get_plugin.assert_called_once_with("p")

    def test_plugin_without_icon_returns_none(self):
        from app.widgets.cards.settings.mcp_setting_card import _resolve_mcp_server_icon

        pm = MagicMock()
        pm.is_initialized.return_value = True
        pm.get_plugin.return_value.icon_config = None

        with patch("app.plugins.managers.plugin_manager.PluginManager") as pm_cls:
            pm_cls.get_instance.return_value = pm
            assert _resolve_mcp_server_icon({"_source": r"D:\plugins\p\.mcp.json"}) is None

    def test_exception_degrades_to_none(self):
        from app.widgets.cards.settings.mcp_setting_card import _resolve_mcp_server_icon

        pm_cls = MagicMock()
        pm_cls.get_instance.side_effect = RuntimeError("boom")
        with patch("app.plugins.managers.plugin_manager.PluginManager", pm_cls):
            assert _resolve_mcp_server_icon({"_source": r"D:\plugins\p\.mcp.json"}) is None


# ═══════════════════════════════════════════════════
# MCPListSettingCard：授权流程编排
# ═══════════════════════════════════════════════════


def _make_card():
    """构造最小可用的 MCPListSettingCard（绕开 Qt C++ 层）"""
    from app.widgets.cards.settings.mcp_setting_card import MCPListSettingCard

    card = MCPListSettingCard.__new__(MCPListSettingCard)
    card._server_rows = {}
    card._get_servers = MagicMock(return_value=[])
    card._hot_connect = MagicMock()
    card.cfg = MagicMock()
    card.cfg.mcp_enabled.value = True
    # __new__ 出来的对象没有 C++ 层，window() 会抛 RuntimeError，
    # 而 InfoBar 需要 parent。mock 掉以隔离被测逻辑。
    card.window = MagicMock(return_value=MagicMock())
    return card


_OAUTH_SRC = r"D:\work\DriFox\plugins\demo\.mcp.json"


class TestAuthorizeServerGuard:
    def test_unknown_server_is_noop(self):
        card = _make_card()
        card._get_servers.return_value = []

        with patch("threading.Thread") as th:
            card._on_authorize_server("ghost")
            th.assert_not_called()

    def test_non_oauth_server_is_noop(self):
        card = _make_card()
        card._get_servers.return_value = [{"name": "plain", "type": "http"}]

        with patch("threading.Thread") as th:
            card._on_authorize_server("plain")
            th.assert_not_called()

    def test_server_outside_plugin_dir_is_rejected(self):
        """无插件来源 → 无法定位 token 存储位置，直接拒绝并提示"""
        card = _make_card()
        card._get_servers.return_value = [{"name": "n", "auth": {"type": "oauth"}, "url": "https://x/mcp"}]

        with (
            patch("threading.Thread") as th,
            patch("app.widgets.cards.settings.mcp_setting_card.InfoBar") as bar,
        ):
            card._on_authorize_server("n")
            th.assert_not_called()
            bar.warning.assert_called_once()
            assert "无法确定" in bar.warning.call_args[0][1]

    def test_oauth_server_starts_background_thread(self):
        """合法 OAuth server：起工作线程跑授权，绝不阻塞 UI"""
        card = _make_card()
        card._get_servers.return_value = [
            {"name": "notion", "auth": {"type": "oauth"}, "url": "https://mcp.notion.com/mcp", "_source": _OAUTH_SRC}
        ]

        with patch("threading.Thread") as th:
            card._on_authorize_server("notion")
            th.assert_called_once()
            # 必须是 daemon 线程（应用退出不被拖住）
            assert th.call_args[1]["daemon"] is True
            # 线程名带 server 名，便于排查
            assert "notion" in th.call_args[1]["name"]


class TestAuthorizeDone:
    def test_success_enables_button_and_connects(self):
        card = _make_card()
        card._get_servers.return_value = [{"name": "notion", "auth": {"type": "oauth"}, "url": "https://x/mcp"}]
        row = MagicMock()
        card._server_rows["notion"] = row

        with patch("app.widgets.cards.settings.mcp_setting_card.InfoBar") as bar:
            card._on_authorize_done("notion", True, "")

        row.set_authorized.assert_called_once_with(True)
        # 授权成功立即热连接，省去用户再拨开关
        card._hot_connect.assert_called_once()
        assert card._hot_connect.call_args[1]["force"] is True
        bar.success.assert_called_once()
        bar.error.assert_not_called()

    def test_failure_keeps_button_and_no_connect(self):
        card = _make_card()
        row = MagicMock()
        card._server_rows["notion"] = row

        with patch("app.widgets.cards.settings.mcp_setting_card.InfoBar") as bar:
            card._on_authorize_done("notion", False, "授权超时")

        row.set_authorized.assert_called_once_with(False)
        card._hot_connect.assert_not_called()
        bar.error.assert_called_once()
        assert "授权超时" in bar.error.call_args[0][1]

    def test_missing_row_does_not_crash(self):
        """行已被删除（用户删了 server）时结果回调不应抛"""
        card = _make_card()
        card._server_rows = {}

        with patch("app.widgets.cards.settings.mcp_setting_card.InfoBar"):
            card._on_authorize_done("ghost", True, "")


class TestWiringInSource:
    """源码级接线检查：防止重构漏掉连接"""

    def test_row_authorize_signal_connected(self):
        repo = Path(__file__).resolve().parent.parent.parent
        src = (repo / "app" / "widgets" / "cards" / "settings" / "mcp_setting_card.py").read_text(encoding="utf-8")
        assert "authorizeRequested.connect(self._on_authorize_server)" in src

    def test_authorize_done_signal_connected(self):
        repo = Path(__file__).resolve().parent.parent.parent
        src = (repo / "app" / "widgets" / "cards" / "settings" / "mcp_setting_card.py").read_text(encoding="utf-8")
        assert "_authorizeDone.connect(self._on_authorize_done)" in src

    def test_authorize_not_called_on_ui_thread_directly(self):
        """_on_authorize_server 内部必须起线程，不得直接调 authorize"""
        import ast

        repo = Path(__file__).resolve().parent.parent.parent
        src = (repo / "app" / "widgets" / "cards" / "settings" / "mcp_setting_card.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef) or node.name != "MCPListSettingCard":
                continue
            for method in node.body:
                if isinstance(method, ast.FunctionDef) and method.name == "_on_authorize_server":
                    body = ast.get_source_segment(src, method) or ""
                    assert "threading.Thread(" in body, "授权必须放工作线程，否则阻塞 UI 300 秒"
                    assert "authorize(" in body
                    # authorize 调用必须位于线程目标函数内，而非方法体直接调用
                    assert "def _work" in body


# ═══════════════════════════════════════════════════
# 中止授权（不必等 300s 超时）
# ═══════════════════════════════════════════════════


class TestCancelAuthorize:
    """授权等待期间按钮必须是「中止」且可点，点后发出取消请求"""

    def test_authorizing_shows_cancel_and_stays_clickable(self):
        row = _make_row({"name": "n"})
        row._auth_btn = MagicMock()

        row._set_authorizing(True)

        assert row._auth_btn.setText.call_args[0][0] == "中止"
        assert row._auth_btn.setEnabled.call_args[0][0] is True
        assert row._authorizing is True

    def test_cancel_click_emits_cancel_signal(self):
        from app.widgets.cards.settings.mcp_setting_card import MCPServerRow

        row = _make_row({"name": "n"})
        row._auth_btn = MagicMock()
        row._authorizing = True
        row.cancelAuthorizeRequested = MagicMock()
        row.authorizeRequested = MagicMock()

        MCPServerRow._on_auth_clicked(row)

        row.cancelAuthorizeRequested.emit.assert_called_once_with("n")
        row.authorizeRequested.emit.assert_not_called()

    def test_end_authorizing_restores_two_state(self):
        """取消返回后恢复双态：未授权 → 「授权」，且 _authorizing 复位"""
        row = _make_row({"name": "n"})
        row._auth_btn = MagicMock()
        row._set_authorizing(True)

        row._set_authorizing(False)

        assert row._authorizing is False
        assert row._auth_btn.setText.call_args[0][0] == "授权"

    def test_cancelled_result_shows_info_not_error(self):
        card = _make_card()
        card._server_rows["notion"] = MagicMock()

        with patch("app.widgets.cards.settings.mcp_setting_card.InfoBar") as bar:
            card._on_authorize_done("notion", False, "授权已中止", True)

        bar.info.assert_called_once()
        bar.error.assert_not_called()
        card._hot_connect.assert_not_called()


class TestCallbackCancelSemantics:
    """回调 server 的取消语义：唤醒等待方且不覆盖已到达的结果"""

    def test_cancel_wakes_waiter_and_marks_flag(self):
        from app.core.mcp_oauth.callback_server import CallbackServer

        server = CallbackServer()
        assert server.wait(0.05).code is None  # 先确认未就绪时确实等到超时

        server.cancel()
        result = server.wait(0.05)  # 取消后立即返回，不再等满 timeout
        assert result.cancelled is True
        assert result.code is None

    def test_cancel_does_not_override_received_code(self):
        from app.core.mcp_oauth.callback_server import CallbackServer

        server = CallbackServer()
        server.result.code = "the-code"
        server.cancel()

        assert server.result.cancelled is False
        assert server.result.code == "the-code"

    def test_factory_cancel_only_for_pending_session(self):
        from app.core.mcp_oauth.provider import McpOAuthProviderFactory

        factory = McpOAuthProviderFactory()
        # 无会话 → 无事可取消
        assert factory.cancel("plug", "srv") is False

