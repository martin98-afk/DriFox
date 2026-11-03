# -*- coding: utf-8 -*-
"""GiteeCard 同步状态圆点初始化测试。

背景（2026-11-03）：卡片可能在 ConfigSyncService.enable() 之后才构建
（启动恢复 / 设置弹窗重建），此刻收不到已发射过的 stateChanged，
圆点会永远不显示。构造时须按当前状态补一次。
"""

from unittest.mock import MagicMock, patch

import pytest
from PyQt5.QtCore import QObject, pyqtSignal

from app.widgets.cards.settings.gitee_card import GiteeCard


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
        self._value = value
        self.valueChanged.emit(value)


class _FakeSettings:
    def __init__(self, *, bound=False, owner="", repo="DriFox_uploads"):
        self.gitee_bound = _FakeConfigItem(bound)
        self.gitee_user_owner = _FakeConfigItem(owner)
        self.gitee_user_repo = _FakeConfigItem(repo)


def _build_card(qtbot, state, *, bound=True, owner="u-test"):
    cfg = _FakeSettings(bound=bound, owner=owner)
    svc = MagicMock()
    svc._state = state
    with (
        patch(
            "app.widgets.cards.settings.gitee_card.Settings.get_instance",
            return_value=cfg,
        ),
        patch(
            "app.core.sync.config_sync.ConfigSyncService.get_instance",
            return_value=svc,
        ),
        # 头像拉取是后台真线程：测试内断网
        patch("app.widgets.cards.settings.gitee_card._fetch_avatar_bytes", return_value=None),
    ):
        card = GiteeCard()
    qtbot.addWidget(card)
    return card


def test_dot_visible_when_sync_already_idle(qtbot):
    """启动恢复已 enable（idle）后再建卡：圆点必须显示"""
    card = _build_card(qtbot, "idle")
    assert card._sync_dot.isHidden() is False
    assert card._sync_dot.toolTip() == "同步正常"


def test_dot_visible_when_syncing(qtbot):
    card = _build_card(qtbot, "syncing")
    assert card._sync_dot.isHidden() is False
    assert card._sync_dot.toolTip() == "正在同步…"


def test_dot_hidden_when_disabled_and_unbound(qtbot):
    card = _build_card(qtbot, "disabled", bound=False, owner="")
    assert card._sync_dot.isHidden() is True
