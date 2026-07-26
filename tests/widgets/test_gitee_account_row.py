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
    assert row._action_btn.text() == "绑定"
    assert row._bound_owner == ""
    assert row._avatar.toolTip() == "未绑定"


def test_bound_state_shows_owner_and_repository(row_factory):
    row, _, _ = row_factory(bound=True, owner="martin98-afk")

    assert row._name_label._full_text == "martin98-afk"
    assert row._repo_label._full_text == "DriFox_uploads ↗"
    assert row._action_btn.text() == "解绑"
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
    assert row._action_btn.text() == "解绑"
