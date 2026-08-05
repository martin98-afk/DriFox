# -*- coding: utf-8 -*-
"""Tab 模式 Gitee 账户快捷栏测试"""

from unittest.mock import MagicMock, patch

import pytest
from PyQt5.QtCore import QObject, pyqtSignal

from app.widgets.cards.settings.gitee_card import GiteeAccountRow


class _FakeConfigItem(QObject):
    valueChanged = pyqtSignal(object)

    def __init__(self, value):
        super().__init__()
        self._value = value

    @property
    def value(self):
        return self._value

    @value.setter
    def value(self, value):
        if value == self._value:
            return
        self._value = value
        self.valueChanged.emit(value)


class _FakeSettings:
    def __init__(self, *, bound=False, owner="", repo="DriFox_uploads"):
        self.gitee_bound = _FakeConfigItem(bound)
        self.gitee_user_owner = _FakeConfigItem(owner)
        self.gitee_user_repo = _FakeConfigItem(repo)
        self.load = MagicMock()


@pytest.fixture
def row_factory(qtbot):
    created = []

    def _create(*, bound=False, owner="", repo="DriFox_uploads"):
        cfg = _FakeSettings(bound=bound, owner=owner, repo=repo)
        sync_service = MagicMock()
        sync_service._state = "idle"
        with (
            patch(
                "app.widgets.cards.settings.gitee_card.Settings.get_instance",
                return_value=cfg,
            ),
            patch(
                "app.core.config_sync.ConfigSyncService.get_instance",
                return_value=sync_service,
            ),
        ):
            row = GiteeAccountRow()
        qtbot.addWidget(row)
        created.append(row)
        return row, cfg, sync_service

    return _create


def test_unbound_state_shows_bind_action(row_factory):
    row, _, _ = row_factory()

    assert row._name_label._full_text == "Gitee 未绑定"
    assert row._repo_label._full_text == "绑定后可备份与分享"
    assert row._settings_btn.isEnabled() is True
    assert row._bound_owner == ""
    assert row._avatar.toolTip() == "未绑定"


def test_bound_state_shows_owner_and_repository(row_factory):
    row, _, _ = row_factory(bound=True, owner="martin98-afk")

    assert row._name_label._full_text == "martin98-afk"
    assert row._repo_label._full_text == "DriFox_uploads ↗"
    assert row._settings_btn.isEnabled() is True
    assert row._bound_owner == "martin98-afk"
    assert row._bound_repo == "DriFox_uploads"
    assert row._name_label.toolTip() == "martin98-afk"
    assert row._repo_label.toolTip() == "DriFox_uploads"


def test_config_changes_refresh_existing_row(row_factory):
    row, cfg, _ = row_factory()

    cfg.gitee_user_owner.value = "new-owner"
    cfg.gitee_user_repo.value = "DriFox_uploads"
    cfg.gitee_bound.value = True

    assert row._name_label._full_text == "new-owner"
    assert row._repo_label._full_text == "DriFox_uploads ↗"
    assert row._settings_btn.isEnabled() is True


def test_clicking_account_opens_bound_repository(row_factory):
    row, _, _ = row_factory(bound=True, owner="martin98-afk")

    with patch("app.widgets.cards.settings.gitee_card.webbrowser.open") as open_url:
        row._open_repository()

    open_url.assert_called_once_with("https://gitee.com/martin98-afk/DriFox_uploads")


def test_start_oauth_disables_button_and_starts_worker(row_factory):
    row, _, _ = row_factory()

    with patch("app.widgets.cards.settings.gitee_card.threading.Thread") as thread_cls:
        row._start_oauth(repo_private=True)

    assert row._binding is True
    assert row._settings_btn.isEnabled() is False
    thread_cls.assert_called_once_with(target=row._do_oauth, args=(True,), daemon=True)
    thread_cls.return_value.start.assert_called_once_with()


def test_oauth_failure_restores_bind_action(row_factory):
    row, _, _ = row_factory()
    row._binding = True
    row._refresh_ui()

    with patch("app.widgets.cards.settings.gitee_card.InfoBar.error") as show_error:
        row._on_oauth_result(False, "授权超时")

    assert row._binding is False
    assert row._settings_btn.isEnabled() is True
    show_error.assert_called_once()


def test_unbind_requires_confirmation(row_factory):
    row, _, _ = row_factory(bound=True, owner="martin98-afk")
    dialog = MagicMock()

    with patch(
        "app.widgets.cards.settings.gitee_card.ConfirmDialog",
        return_value=dialog,
    ):
        row._on_unbind()

    dialog.confirmed.connect.assert_called_once_with(row._do_unbind)
    dialog.exec_.assert_called_once_with()


def test_do_unbind_stops_sync_and_resets_state(row_factory):
    row, cfg, sync_service = row_factory(bound=True, owner="martin98-afk")
    backend = MagicMock()
    uploader = MagicMock()

    def clear_binding():
        cfg.gitee_bound.value = False
        cfg.gitee_user_owner.value = ""
        cfg.gitee_user_repo.value = ""
        return True, "已解绑 Gitee 账号"

    backend.unbind.side_effect = clear_binding
    sync_service.restore_local.return_value = True

    with (
        patch("app.gateway.auth.get_oauth_backend", return_value=backend),
        patch(
            "app.gateway.utils.gitee_uploader.GiteeUploader.get_instance",
            return_value=uploader,
        ),
        patch("app.widgets.cards.settings.gitee_card.InfoBar.success"),
    ):
        row._do_unbind()

    sync_service.disable.assert_called_once_with()
    backend.unbind.assert_called_once_with()
    uploader.reset_config.assert_called_once_with()
    sync_service.restore_local.assert_called_once_with()
    cfg.load.assert_called_once_with()
    assert row._settings_btn.isEnabled() is True


def test_bound_render_does_not_start_remote_sync(qtbot):
    cfg = _FakeSettings(bound=True, owner="martin98-afk")
    sync_service = MagicMock()
    sync_service._state = "disabled"
    backend = MagicMock()
    backend.get_bound_info.return_value = {
        "token": "test-token",
        "owner": "martin98-afk",
    }

    with (
        patch(
            "app.widgets.cards.settings.gitee_card.Settings.get_instance",
            return_value=cfg,
        ),
        patch(
            "app.core.config_sync.ConfigSyncService.get_instance",
            return_value=sync_service,
        ),
        patch("app.gateway.auth.get_oauth_backend", return_value=backend),
    ):
        row = GiteeAccountRow()

    qtbot.addWidget(row)
    backend.get_bound_info.assert_not_called()
    sync_service.enable.assert_not_called()


def test_oauth_success_enables_sync(row_factory):
    row, cfg, sync_service = row_factory()
    sync_service._state = "disabled"
    cfg.gitee_bound._value = True
    cfg.gitee_user_owner._value = "martin98-afk"
    cfg.gitee_user_repo._value = "DriFox_uploads"
    backend = MagicMock()
    backend.get_bound_info.return_value = {
        "token": "test-token",
        "owner": "martin98-afk",
    }
    uploader = MagicMock()

    with (
        patch("app.gateway.auth.get_oauth_backend", return_value=backend),
        patch(
            "app.gateway.utils.gitee_uploader.GiteeUploader.get_instance",
            return_value=uploader,
        ),
        patch("app.widgets.cards.settings.gitee_card.InfoBar.success"),
    ):
        row._on_oauth_result(True, "绑定成功")

    uploader.reset_config.assert_called_once_with()
    sync_service.enable.assert_called_once_with("test-token", "martin98-afk")


def test_unbind_failure_preserves_bound_state(row_factory):
    row, _, sync_service = row_factory(bound=True, owner="martin98-afk")
    backend = MagicMock()
    backend.unbind.return_value = False, "配置保存失败"

    with (
        patch("app.gateway.auth.get_oauth_backend", return_value=backend),
        patch("app.widgets.cards.settings.gitee_card.InfoBar.error") as show_error,
    ):
        row._do_unbind()

    sync_service.disable.assert_called_once_with()
    assert row._settings_btn.isEnabled() is True
    assert row._bound_owner == "martin98-afk"
    show_error.assert_called_once()


# ── T8B：GiteeCard._auto_enable_sync 失效链路 ──────────────────────────


class _FakeGiteeCardSettings:
    """GiteeCard 场景的 FakeSettings（含 token/expires 字段 + save）"""

    def __init__(self, *, bound=False, owner="", repo="DriFox_uploads"):
        self.gitee_bound = _FakeConfigItem(bound)
        self.gitee_user_owner = _FakeConfigItem(owner)
        self.gitee_user_repo = _FakeConfigItem(repo)
        self.gitee_user_token = _FakeConfigItem("token" if bound else "")
        self.gitee_user_refresh_token = _FakeConfigItem("refresh" if bound else "")
        self.gitee_token_expires_at = _FakeConfigItem(1234567890.0 if bound else 0.0)
        self.load = MagicMock()
        self.save = MagicMock()


@pytest.fixture
def gitee_card_factory(qtbot):
    created = []

    def _create(*, bound=False, owner="martin98-afk", repo="DriFox_uploads", bound_info=None):
        cfg = _FakeGiteeCardSettings(bound=bound, owner=owner, repo=repo)
        sync_service = MagicMock()
        sync_service._state = "disabled"
        uploader = MagicMock()
        backend = MagicMock()
        # 默认：get_bound_info 返回 None（token 无法刷新场景）；bound_info 参数可覆盖
        backend.get_bound_info.return_value = bound_info
        with (
            patch(
                "app.widgets.cards.settings.gitee_card.Settings.get_instance",
                return_value=cfg,
            ),
            patch(
                "app.core.config_sync.ConfigSyncService.get_instance",
                return_value=sync_service,
            ),
            patch(
                "app.gateway.utils.gitee_uploader.GiteeUploader.get_instance",
                return_value=uploader,
            ),
            patch("app.gateway.auth.get_oauth_backend", return_value=backend),
        ):
            from app.widgets.cards.settings.gitee_card import GiteeCard

            card = GiteeCard()
        qtbot.addWidget(card)
        created.append(card)
        return card, cfg, sync_service, backend

    return _create


def test_auto_enable_sync_bound_info_none_with_bound_true_triggers_invalid(gitee_card_factory):
    """断点①：曾绑定（bound=True）但 get_bound_info 返回 None → 清绑 + 红标 + 提醒"""
    card, cfg, sync_service, backend = gitee_card_factory(
        bound=True, bound_info={"token": "valid-token", "owner": "martin98-afk"}
    )
    # 构造时 _refresh_ui 已用有效 token enable 一次，重置计数以便精确断言
    sync_service.enable.reset_mock()
    cfg.save.reset_mock()
    # 模拟 token 之后被吊销：get_bound_info 返回 None
    backend.get_bound_info.return_value = None
    row = MagicMock()
    tm = MagicMock()
    tm._windows = [MagicMock()]
    tm._windows[0]._tab_panel._gitee_account_row = row
    ctrl = MagicMock()

    with (
        patch("app.gateway.auth.get_oauth_backend", return_value=backend),
        patch(
            "app.widgets.tab_manager_window.TabManagerWindow.get_instance",
            return_value=tm,
        ),
        patch(
            "app.widgets.cards.global_card_controller.get_global_card_controller",
            return_value=ctrl,
        ),
    ):
        card._auto_enable_sync()

    # sync 未启动（bound_info 无效）→ enable 不被调用
    sync_service.enable.assert_not_called()
    # 清绑
    assert cfg.gitee_bound.value is False
    assert cfg.gitee_user_token.value == ""
    cfg.save.assert_called_once()
    # 账户行红标置位
    row.set_invalid.assert_called_once_with(True)
    # 全局失效提醒触发
    ctrl.check_gitee_token_invalid_reminder.assert_called_once()


def test_auto_enable_sync_never_bound_skips_invalid(gitee_card_factory):
    """从未绑定（bound=False）+ get_bound_info None → 正常跳过不误报"""
    card, cfg, sync_service, backend = gitee_card_factory(bound=False)
    backend.get_bound_info.return_value = None
    ctrl = MagicMock()

    with (
        patch("app.gateway.auth.get_oauth_backend", return_value=backend),
        patch(
            "app.widgets.cards.global_card_controller.get_global_card_controller",
            return_value=ctrl,
        ),
    ):
        card._auto_enable_sync()

    sync_service.enable.assert_not_called()
    cfg.save.assert_not_called()
    ctrl.check_gitee_token_invalid_reminder.assert_not_called()


def test_auto_enable_sync_bound_info_valid_starts_sync(gitee_card_factory):
    """bound_info 有效 → 正常启动同步（不触发失效处理）"""
    card, cfg, sync_service, backend = gitee_card_factory(
        bound=True, bound_info={"token": "valid-token", "owner": "martin98-afk"}
    )
    sync_service.enable.reset_mock()
    cfg.save.reset_mock()
    ctrl = MagicMock()

    with (
        patch("app.gateway.auth.get_oauth_backend", return_value=backend),
        patch(
            "app.widgets.cards.global_card_controller.get_global_card_controller",
            return_value=ctrl,
        ),
    ):
        card._auto_enable_sync()

    sync_service.enable.assert_called_once_with("valid-token", "martin98-afk")
    cfg.save.assert_not_called()
    ctrl.check_gitee_token_invalid_reminder.assert_not_called()


def test_account_row_uploader_token_invalid_sets_red_badge(row_factory):
    """断点②：GiteeAccountRow 收到 tokenInvalid → 失效红标 + 提醒"""
    row, _, _ = row_factory(bound=True, owner="martin98-afk")
    ctrl = MagicMock()

    with patch(
        "app.widgets.cards.global_card_controller.get_global_card_controller",
        return_value=ctrl,
    ):
        row._on_uploader_token_invalid()

    assert row._invalid is True
    assert row._name_label._full_text == "绑定已失效"
    ctrl.check_gitee_token_invalid_reminder.assert_called_once()
